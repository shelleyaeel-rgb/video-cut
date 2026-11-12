import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from videocut.edit import plan_collection
from videocut.media import ffmpeg_args, probe, run
from videocut.pipeline import generate, render_clip, validate
from videocut.project_store import ProjectStore


def event(time, end=None, ident=None):
    return {'time': time, 'end': time if end is None else end,
            'confidence': .9, 'event_id': ident or str(time)}


class CollectionPlanningTests(unittest.TestCase):
    def test_presets_override_strength_but_old_strength_still_works(self):
        with patch('videocut.pipeline.local_file', return_value=Path('source.mp4')):
            for preset, strength in (('none', 0), ('light', .3), ('impact', .7)):
                cfg = validate({'video': 'source.mp4', 'effect_preset': preset, 'strength': .9})
                self.assertEqual(cfg['strength'], strength)
            self.assertEqual(validate({'video': 'source.mp4', 'strength': .9})['strength'], .9)
            with self.assertRaisesRegex(ValueError, '特效'):
                validate({'video': 'source.mp4', 'effect_preset': 'unknown'})

    def test_all_selected_events_survive_more_than_ninety_seconds(self):
        events = [event(time, time+.5) for time in range(10, 211, 10)]
        clips = plan_collection(events, 220)
        self.assertGreater(sum(clip['duration'] for clip in clips), 120)
        self.assertEqual([item['event_id'] for clip in clips for item in clip['events']],
                         [item['event_id'] for item in events])

    def test_continuous_fight_merges_without_ten_second_limit_or_replay(self):
        events = [event(time, time+.5) for time in range(4, 60, 2)]
        clips = plan_collection(events, 65)
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0]['start'], 0)
        self.assertEqual(clips[0]['end'], 60.5)
        self.assertEqual(len(clips[0]['events']), len(events))

    def test_first_and_last_partial_frame_markers_stay_inside_source(self):
        clips = plan_collection([event(.001), event(9.999)], 10.01, pre=0, post=0)
        self.assertEqual(len(clips), 2)
        self.assertEqual(clips[-1]['end'], 10.01)
        self.assertLess(clips[-1]['tail_padding'], 1/30)
        cursor = 0
        for clip in clips:
            self.assertEqual(clip['output_start'], cursor)
            self.assertGreater(clip['duration'], 0)
            for item in clip['events']:
                self.assertLessEqual(clip['start'], item['time'])
                self.assertLess(item['time'], clip['end'])
            cursor += clip['duration']

    def test_many_fractional_clips_share_an_exact_video_audio_frame_timeline(self):
        events = [event(index*10+.012345, index*10+.456789) for index in range(1, 101)]
        clips = plan_collection(events, 1020)
        self.assertEqual(len(clips), 100)
        cursor = 0
        for clip in clips:
            frames = round(clip['duration']*30)
            self.assertAlmostEqual(clip['duration']*30, frames, places=8)
            self.assertAlmostEqual(clip['output_start']*30, cursor, places=7)
            self.assertAlmostEqual(clip['start']*30, round(clip['start']*30), places=7)
            cursor += frames
        self.assertAlmostEqual(sum(clip['duration'] for clip in clips)*30, cursor, places=6)

    def test_deduplicate_identity_but_preserve_simultaneous_events(self):
        first = event(10, 11, 'first')
        clips = plan_collection([first, dict(first, end=12), event(10, 11, 'second')], 20)
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0]['end'], 14)
        self.assertEqual([item['event_id'] for item in clips[0]['events']], ['first', 'second'])
        self.assertEqual(first['end'], 11)

    def test_zero_outside_or_invalid_inputs(self):
        for events, duration in (([], 20), ([event(20)], 20), ([event(-1)], 20),
                                 ([event(1)], 0), ([event(float('nan'))], 20)):
            with self.subTest(events=events, duration=duration):
                self.assertEqual(plan_collection(events, duration), [])


@unittest.skipUnless(os.environ.get('VIDEOCUT_INTEGRATION') == '1', 'Set VIDEOCUT_INTEGRATION=1 for real FFmpeg tests')
class RecordingRenderIntegrationTests(unittest.TestCase):
    def test_collection_and_montage_use_original_and_never_scan_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / '原始录制.mkv'
            run(ffmpeg_args()+['-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=30:duration=14',
                               '-f', 'lavfi', '-i', 'sine=frequency=660:sample_rate=48000:duration=14',
                               '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'pcm_s16le', video])
            store = ProjectStore(root / 'projects.sqlite')
            session = store.create_session(source_path=video)
            for time in (3.3, 9.6):
                store.upsert_marker(session['id'], {'time': time, 'end': time+.4, 'source': 'manual'})
            store.finalize_asset(session['id'], metadata=probe(video))
            spec, source = store.build_generation_spec(session['id'], {'target': 5, 'effect_preset': 'impact'})
            with patch('videocut.pipeline.ROOT', root), patch('videocut.pipeline.detect_events', side_effect=AssertionError('must not rescan')), patch('videocut.pipeline.render_clip', wraps=render_clip) as render:
                collection = generate(spec, recording_source=source)
                self.assertGreater(collection['duration'], 10)
                self.assertEqual(len(collection['clips']), 1)
                self.assertEqual(collection['selected_event_count'], 2)
                self.assertEqual(collection['settings']['strength'], 0)
                self.assertEqual(collection['recording']['mode'], 'collection')
                run(ffmpeg_args()+['-i', collection['output'], '-f', 'null', '-'])
                self.assertTrue(probe(collection['output'])['audio_tracks'])
                spec, source = store.build_generation_spec(session['id'], {'target': 5, 'strength': .5,
                                                                          'music': str(video)}, mode='montage')
                montage = generate(spec, recording_source=source)
                self.assertLessEqual(montage['duration'], 5.15)
                self.assertEqual(montage['recording']['mode'], 'montage')
                for call in render.call_args_list:
                    self.assertEqual(call.args[0]['video'], video.resolve())
                self.assertEqual(montage['settings']['video'], str(video.resolve()))
                run(ffmpeg_args()+['-i', montage['output'], '-f', 'null', '-'])


if __name__ == '__main__':
    unittest.main()

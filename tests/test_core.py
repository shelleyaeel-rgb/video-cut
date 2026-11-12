import math
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from videocut.detect import scores_to_events
from videocut.edit import plan_clips
from videocut.media import Cancelled, Context, run
from videocut.pipeline import finite_number, validate


def event(t, confidence=.95):
    return {'time': t, 'end': t+.6, 'confidence': confidence, 'samples': 5}


class DetectionTests(unittest.TestCase):
    def test_continuous_indicator_is_one_event(self):
        events = scores_to_events([0]*8 + [.95]*24 + [0]*8, 8)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['time'], 1)

    def test_indicator_rearms_after_disappearance(self):
        events = scores_to_events([0]*8 + [.95]*4 + [0]*8 + [.91]*4, 8)
        self.assertEqual([e['time'] for e in events], [1, 2.5])

    def test_brief_dropout_does_not_duplicate(self):
        self.assertEqual(len(scores_to_events([.9]*8+[0]+[.9]*8, 8)), 1)

    def test_noise_is_not_an_event(self):
        self.assertEqual(scores_to_events([.2,.4,.1,.7]*10, 8), [])

    def test_minimum_visibility(self):
        self.assertEqual(scores_to_events([0,.9,0,0,0], 8, min_visible=.2), [])


class PlanningTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(plan_clips([], 100, 60), [])

    def test_budget_boundaries_and_event_retention(self):
        events = [event(t) for t in (0.2,5,6,12,13,14,30,40,50,90,99.8)]
        clips = plan_clips(events, 100, 19, [x/2 for x in range(40)])
        self.assertLessEqual(sum(c['duration'] for c in clips), 19)
        previous = 0
        for c in clips:
            self.assertGreaterEqual(c['start'], previous-1e-5)
            self.assertLessEqual(c['end'], 100)
            self.assertGreater(c['duration'], 0)
            self.assertTrue(c['events'])
            self.assertTrue(all(c['start'] <= e['time'] < c['end'] for e in c['events']))
            previous = c['end']

    def test_long_fight_does_not_duplicate_source(self):
        clips = plan_clips([event(t) for t in range(4,60,2)], 65, 60)
        self.assertGreater(len(clips), 2)
        for a,b in zip(clips,clips[1:]):
            self.assertLessEqual(a['end'], b['start']+.00001)

    def test_never_pad_sparse_footage(self):
        self.assertAlmostEqual(sum(c['duration'] for c in plan_clips([event(10)],100,90)), 7.6)

    def test_numeric_validation(self):
        for n in ('NaN', 'inf', '-1', 121):
            with self.assertRaises(ValueError): finite_number({'target':n},'target',90,5,120)

    def test_beat_alignment_keeps_duration_budget(self):
        clips=plan_clips([event(8),event(18)],30,8,[4.8,7.4])
        self.assertLessEqual(sum(c['duration'] for c in clips),8)
        self.assertLess(abs(clips[0]['output_start']+8-clips[0]['start']-4.8),.04)


class InputTests(unittest.TestCase):
    def test_music_can_be_omitted_null_or_blank_without_ai(self):
        for music in ({}, {'music':None}, {'music':''}, {'music':'  '}):
            with self.subTest(music=music), patch('videocut.pipeline.local_file',return_value=Path('video.mp4')) as local, patch('videocut.pipeline.get_profile',return_value={}), patch('videocut.audio.require_model',side_effect=AssertionError('BGM AI must not run')):
                cfg=validate({'video':'video.mp4','music_start':'invalid','music_volume':'invalid','music_voice_mode':'instrumental',**music})
                self.assertIsNone(cfg['music'])
                self.assertEqual(cfg['music_start'],0)
                self.assertEqual(cfg['music_voice_mode'],'keep')
                local.assert_called_once_with('video.mp4')

    def test_nonempty_invalid_music_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'文件不存在'):
            validate({'video':'video.mp4','music':'Z:/nonexistent-bgm.wav'})


class CancellationTests(unittest.TestCase):
    def test_running_process_can_be_cancelled(self):
        import sys
        ctx=Context()
        result=[]
        def worker():
            try: run([sys.executable,'-c','import time;time.sleep(30)'],ctx)
            except Exception as e: result.append(e)
        t=threading.Thread(target=worker)
        t.start()
        limit=time.monotonic()+3
        while not ctx.processes and time.monotonic()<limit: time.sleep(.01)
        ctx.cancel();t.join(4)
        self.assertFalse(t.is_alive())
        self.assertIsInstance(result[0],Cancelled)


if __name__=='__main__': unittest.main()

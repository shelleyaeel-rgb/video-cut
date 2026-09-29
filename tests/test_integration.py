import os
import unittest
from unittest.mock import patch

import numpy as np

from videocut.demo import make_demo
from videocut.detect import detect_events, get_profile
from videocut.edit import music_peaks
from videocut.media import ROOT, Cancelled, Context, ffmpeg_args, probe, run
from videocut.pipeline import generate
from videocut.profile_test import test_profile


@unittest.skipUnless(os.environ.get('VIDEOCUT_INTEGRATION')=='1','Set VIDEOCUT_INTEGRATION=1 for real FFmpeg tests')
class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec=make_demo()

    def test_detection_known_ground_truth_and_music_offset(self):
        events=detect_events(self.spec['video'],get_profile('demo'))
        self.assertEqual(len(events),4)
        for actual,expected in zip(events,self.spec['expected_events']):
            self.assertLessEqual(abs(actual['time']-expected),.15)
            self.assertGreater(actual['confidence'],.9)
        peaks=music_peaks(self.spec['music'],.15,5)
        self.assertGreater(len(peaks),7)
        self.assertLess(min(abs(p-.1) for p in peaks),.06)

    def test_profile_range_uses_absolute_times_and_leaves_no_artifacts(self):
        outputs = set((ROOT/'outputs').glob('*'))
        jobs = set((ROOT/'work'/'jobs').glob('*'))
        result = test_profile({**self.spec, 'start': 8.3, 'duration': 8})
        self.assertEqual(len(result['events']), 2)
        for actual, expected in zip(result['events'], [10, 15]):
            self.assertLessEqual(abs(actual['time']-expected), .15)
            self.assertGreaterEqual(actual['time'], 8.3)
            self.assertLess(actual['end'], 16.3)
        tail = test_profile({**self.spec, 'start': 22, 'duration': 30})
        self.assertEqual(tail['events'], [])
        self.assertAlmostEqual(tail['duration'], 2, places=1)
        self.assertEqual(outputs, set((ROOT/'outputs').glob('*')))
        self.assertEqual(jobs, set((ROOT/'work'/'jobs').glob('*')))

    def test_profile_scan_can_be_cancelled_while_decoding(self):
        ctx = Context()
        def cancel_after_progress(stage, progress, detail):
            if stage == '识别候选事件' and progress > 10:
                ctx.cancel()
        ctx.callback = cancel_after_progress
        with self.assertRaises(Cancelled):
            test_profile({**self.spec, 'start': 0, 'duration': 20}, ctx)
        self.assertFalse(ctx.processes)

    def test_full_render_chinese_paths_duration_audio_decode(self):
        result=generate({**self.spec,'target':16,'music_start':.15,'strength':.6})
        self.assertEqual(len(result['events']),4)
        self.assertLessEqual(result['duration'],16.15)
        self.assertGreater(result['duration'],10)
        self.assertEqual(result['output_meta']['video_codec'],'h264')
        run(ffmpeg_args()+['-i',result['output'],'-f','null','-'],timeout=120)
        raw=run(ffmpeg_args()+['-i',result['output'],'-vn','-ac','1','-ar','16000','-f','f32le','-'])
        audio=np.frombuffer(raw,'<f4')
        self.assertGreater(float(np.sqrt(np.mean(audio**2))),.015)
        (ROOT/'work'/'latest-test-result.txt').write_text(result['output'],encoding='utf-8')

    def test_no_audio_and_insufficient_music(self):
        silent=ROOT/'work'/'demo'/'无音轨 sample.mp4'
        run(ffmpeg_args()+['-ss','3','-i',self.spec['video'],'-t','5','-map','0:v:0','-an','-c:v','libx264','-preset','ultrafast',silent])
        result=generate({**self.spec,'video':str(silent),'target':20,'music_start':24,'strength':0})
        self.assertTrue(any('无音轨' in w for w in result['warnings']))
        self.assertTrue(any('音乐余量' in w for w in result['warnings']))
        self.assertLessEqual(result['duration'],6.15)
        self.assertTrue(probe(result['output'])['audio_tracks'])

    def test_no_bgm_preserves_source_audio_and_ignores_music_settings(self):
        with patch('videocut.audio.require_model',side_effect=AssertionError('must not need BGM AI')), patch('videocut.pipeline.music_peaks',side_effect=AssertionError('must not analyze BGM')):
            result=generate({**self.spec,'music':'','target':16,'music_start':86400,'music_voice_mode':'instrumental','strength':0,'game_volume':1})
        self.assertIsNone(result['settings']['music'])
        self.assertEqual(result['audio_processing']['music']['mode'],'none')
        self.assertEqual(result['music_peaks'],[])
        self.assertGreater(result['duration'],15)
        self.assertLessEqual(result['duration'],16.15)
        self.assertFalse(any('音乐' in w or 'BGM' in w for w in result['warnings']))
        run(ffmpeg_args()+['-i',result['output'],'-f','null','-'],timeout=120)
        raw=run(ffmpeg_args()+['-i',result['output'],'-vn','-ac','1','-ar','16000','-f','f32le','-'])
        signal=np.frombuffer(raw,'<f4')[16000:32000]
        self.assertGreater(float(np.sqrt(np.mean(signal**2))),.05)
        spectrum=abs(np.fft.rfft(signal))
        self.assertGreater(spectrum[180],100*max(spectrum[220],spectrum[330],1e-6))

    def test_no_bgm_and_no_source_audio_supports_effects_and_silence(self):
        silent=ROOT/'work'/'demo'/'no-bgm-silent.mp4'
        run(ffmpeg_args()+['-ss','3','-i',self.spec['video'],'-t','5','-map','0:v:0','-an','-c:v','libx264','-preset','ultrafast',silent])
        spec={k:v for k,v in self.spec.items() if k!='music'}
        for strength in (0,.5):
            with self.subTest(strength=strength):
                result=generate({**spec,'video':str(silent),'target':5,'strength':strength})
                self.assertTrue(probe(result['output'])['audio_tracks'])
                raw=run(ffmpeg_args()+['-i',result['output'],'-vn','-ac','1','-ar','16000','-f','f32le','-'])
                rms=float(np.sqrt(np.mean(np.frombuffer(raw,'<f4')**2)))
                if strength:
                    self.assertGreater(rms,.001)
                    self.assertTrue(any('仅使用效果音' in w for w in result['warnings']))
                else:
                    self.assertLess(rms,1e-6)
                    self.assertTrue(any('成片为静音' in w for w in result['warnings']))

    def test_no_events_is_explicit_failure(self):
        blank=ROOT/'work'/'demo'/'no-events.mp4'
        run(ffmpeg_args()+['-i',self.spec['video'],'-t','2','-map','0:v:0','-an','-c:v','copy',blank])
        with self.assertRaisesRegex(ValueError,'没有找到候选事件'):
            generate({**self.spec,'video':str(blank)})


if __name__=='__main__':unittest.main()

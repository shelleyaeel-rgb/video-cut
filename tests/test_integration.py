import os
import unittest

import numpy as np

from videocut.demo import make_demo
from videocut.detect import detect_events, get_profile
from videocut.edit import music_peaks
from videocut.media import ROOT, ffmpeg_args, probe, run
from videocut.pipeline import generate


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

    def test_no_events_is_explicit_failure(self):
        blank=ROOT/'work'/'demo'/'no-events.mp4'
        run(ffmpeg_args()+['-i',self.spec['video'],'-t','2','-map','0:v:0','-an','-c:v','copy',blank])
        with self.assertRaisesRegex(ValueError,'没有找到候选事件'):
            generate({**self.spec,'video':str(blank)})


if __name__=='__main__':unittest.main()

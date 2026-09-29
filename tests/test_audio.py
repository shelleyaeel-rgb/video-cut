import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

import numpy as np

from videocut import audio
from videocut.audio_model import SAMPLE_RATE, status
from videocut.media import ROOT, Cancelled, Context, ffmpeg_args, probe, run
from videocut.separator import Spectrogram, separate_array


class AudioUnitTests(unittest.TestCase):
    def test_defaults_preserve_both_sources(self):
        with patch('videocut.audio.require_model',side_effect=AssertionError('must not need AI')):
            self.assertEqual(audio.validate_modes({}),{'voice_mode':'keep','music_voice_mode':'keep','game_audio_track':None})

    def test_game_track_requires_explicit_valid_selection(self):
        for value in (None,'',-1,1.5,True,64,'nan'):
            with self.subTest(value=value),self.assertRaises(ValueError):
                audio.validate_modes({'voice_mode':'game_track','game_audio_track':value})
        cfg={**audio.validate_modes({'voice_mode':'game_track','game_audio_track':'1'}),'audio_track':0}
        self.assertEqual(audio.validate_tracks(cfg,{'audio_tracks':[{},{}]}),1)
        with self.assertRaisesRegex(ValueError,'没有多条音轨'):
            audio.validate_tracks(cfg,{'audio_tracks':[{}]})
        with self.assertRaisesRegex(ValueError,'不存在'):
            audio.validate_tracks({**cfg,'game_audio_track':4},{'audio_tracks':[{},{}]})

    def test_unavailable_ai_and_unknown_modes_are_explicit_errors(self):
        with patch('videocut.audio.require_model',side_effect=ValueError('not installed')):
            for spec in ({'voice_mode':'reduce'},{'music_voice_mode':'instrumental'}):
                with self.assertRaisesRegex(ValueError,'not installed'):audio.validate_modes(spec)
        with self.assertRaisesRegex(ValueError,'无效'):audio.validate_modes({'voice_mode':'magic'})

    def test_no_bgm_skips_music_ai_but_still_requires_video_ai(self):
        with patch('videocut.audio.require_model',side_effect=ValueError('not installed')):
            self.assertEqual(audio.validate_modes({'music_voice_mode':'instrumental'},has_music=False)['music_voice_mode'],'keep')
            with self.assertRaisesRegex(ValueError,'not installed'):
                audio.validate_modes({'voice_mode':'reduce'},has_music=False)

    def test_stft_roundtrip_has_no_delay_or_channel_swap(self):
        transform=Spectrogram(n_fft=512,hop=128,bins=257)
        signal=np.random.default_rng(41).normal(size=(2,4096)).astype(np.float32)
        reconstructed=transform.decode(transform.encode(signal))
        np.testing.assert_allclose(reconstructed,signal,atol=1e-6)

    def test_subtracts_predicted_vocals_and_preserves_unpredicted_signal(self):
        t=np.arange(300000)/SAMPLE_RATE
        signal=np.stack([.1*np.sin(2*np.pi*400*t),.15*np.sin(2*np.pi*1200*t)]).astype(np.float32)
        untouched=separate_array(signal,lambda spec:np.zeros_like(spec))
        np.testing.assert_array_equal(untouched,signal)
        remainder=separate_array(signal,lambda spec:spec)
        self.assertLess(float(np.sqrt(np.mean(remainder[:,8000:-8000]**2))),.003)


@unittest.skipUnless(os.environ.get('VIDEOCUT_INTEGRATION')=='1','Requires FFmpeg')
class AudioIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='audio-test-',dir=ROOT/'work')
        self.folder=Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def read_pcm(self,path):
        with wave.open(str(path),'rb') as source:
            return np.frombuffer(source.readframes(source.getnframes()),'<i2').reshape(-1,2).astype(np.float32)/32768

    def test_only_selected_intervals_and_tracks_enter_output(self):
        source=self.folder/'two-tracks.mkv'
        music=self.folder/'music.wav'
        run(ffmpeg_args()+['-f','lavfi','-i','sine=frequency=220:duration=4','-f','lavfi','-i','sine=frequency=880:duration=4','-map','0:a','-map','1:a','-c:a','pcm_s16le',source])
        run(ffmpeg_args()+['-f','lavfi','-i','sine=frequency=1500:duration=4',music])
        cfg={'video':source,'music':music,'voice_mode':'game_track','game_audio_track':1,'audio_track':0,'music_voice_mode':'keep','music_start':1.5}
        clips=[{'start':.5,'duration':.5},{'start':2.5,'duration':.5}]
        with patch('videocut.audio.reduce_voice',side_effect=AssertionError('AI should not run')):
            game,bgm,report,warnings=audio.prepare_audio(cfg,probe(source),clips,1,self.folder,Context())
        data=self.read_pcm(game)
        self.assertEqual(len(data),SAMPLE_RATE)
        spectrum=abs(np.fft.rfft(data[:,0]))
        self.assertGreater(spectrum[880],spectrum[220]*100)
        self.assertEqual(len(self.read_pcm(bgm)),SAMPLE_RATE)
        self.assertEqual(report['game']['audio_track'],1)

    def test_video_and_bgm_processing_are_independent(self):
        music=self.folder/'music.wav'
        run(ffmpeg_args()+['-f','lavfi','-i','sine=frequency=440:duration=2',music])
        cfg={'video':music,'music':music,'voice_mode':'keep','audio_track':0,'music_voice_mode':'instrumental','music_start':.5}
        def separate(source,destination,*args):return source,{'mode':'ai','cache_hit':False}
        with patch('videocut.audio.reduce_voice',side_effect=separate) as mocked:
            _,_,report,_=audio.prepare_audio(cfg,probe(music),[{'start':0,'duration':1}],1,self.folder,Context())
        self.assertEqual(mocked.call_count,1)
        self.assertEqual(mocked.call_args.args[0].name,'music-original.wav')
        self.assertEqual(report['game']['mode'],'keep')
        self.assertEqual(report['music']['mode'],'ai')

    def test_music_peaks_and_extraction_use_the_same_first_track(self):
        from videocut.demo import make_demo
        from videocut.edit import music_peaks
        source=self.folder/'music-two-tracks.mkv'
        demo=make_demo()
        run(ffmpeg_args()+['-f','lavfi','-i','anullsrc=r=44100:cl=mono:d=3','-i',demo['music'],'-filter_complex','[1:a]aformat=channel_layouts=stereo[m]','-map','0:a','-map','[m]','-t','3','-c:a','pcm_s16le',source])
        self.assertEqual(music_peaks(source,0,2),[])
        self.assertFalse(any(audio.pcm_segment(source,0,2,0,Context())))

    @unittest.skipUnless(os.environ.get('VIDEOCUT_AI_INTEGRATION')=='1','Requires installed AI model')
    def test_real_ai_output_cache_and_corrupt_cache_rebuild(self):
        self.assertTrue(status()['ready'])
        source=self.folder/'input.wav'
        run(ffmpeg_args()+['-f','lavfi','-i','sine=frequency=440:duration=1','-ar',str(SAMPLE_RATE),'-ac','2','-c:a','pcm_s16le',source])
        with patch('videocut.audio.CACHE',self.folder/'cache'):
            result,report=audio.reduce_voice(source,self.folder/'clean.wav',Context(),'AI test',0,100)
            self.assertFalse(report['cache_hit'])
            data=self.read_pcm(result)
            self.assertEqual(len(data),SAMPLE_RATE)
            self.assertTrue(np.isfinite(data).all())
            with patch('videocut.audio.LoggedProcess',side_effect=AssertionError('cache should avoid inference')):
                _,cached=audio.reduce_voice(source,self.folder/'other.wav',Context(),'AI test',0,100)
            self.assertTrue(cached['cache_hit'])
            result.write_bytes(b'bad cache')
            with patch('videocut.audio.LoggedProcess',side_effect=RuntimeError('attempted rebuild')):
                with self.assertRaisesRegex(RuntimeError,'attempted rebuild'):
                    audio.reduce_voice(source,self.folder/'other.wav',Context(),'AI test',0,100)

    @unittest.skipUnless(os.environ.get('VIDEOCUT_AI_INTEGRATION')=='1','Requires installed AI model')
    def test_ai_cancellation_terminates_worker_without_cache(self):
        source=self.folder/'cancel.wav'
        run(ffmpeg_args()+['-f','lavfi','-i','sine=frequency=440:duration=30','-ar',str(SAMPLE_RATE),'-ac','2','-c:a','pcm_s16le',source])
        ctx=Context();errors=[]
        def work():
            try:audio.reduce_voice(source,self.folder/'clean.wav',ctx,'AI test',0,100)
            except Exception as exc:errors.append(exc)
        with patch('videocut.audio.CACHE',self.folder/'cache'):
            thread=threading.Thread(target=work);thread.start()
            deadline=time.monotonic()+5
            while not ctx.processes and thread.is_alive() and time.monotonic()<deadline:time.sleep(.01)
            ctx.cancel();thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0],Cancelled)
        self.assertFalse(list((self.folder/'cache').glob('*.wav')))


if __name__=='__main__':unittest.main()

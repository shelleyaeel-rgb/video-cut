import unittest
from pathlib import Path
from unittest.mock import patch

from videocut.media import Cancelled, Context
from videocut.profile_test import test_profile, validate_profile_test


class ProfileTestTests(unittest.TestCase):
    def setUp(self):
        self.profile = {'id': 'test', 'name': '测试模板'}
        self.meta = {'duration': 24, 'width': 960, 'color_transfer': None}
        self.local = patch('videocut.profile_test.local_file', return_value=Path('test.mp4'))
        self.profile_patch = patch('videocut.profile_test.get_profile', return_value=self.profile)
        signature = patch('videocut.profile_test.review_signature', return_value='fixture-signature')
        signature.start(); self.addCleanup(signature.stop)
        self.local.start(); self.profile_patch.start()
        self.addCleanup(self.local.stop); self.addCleanup(self.profile_patch.stop)

    def test_rejects_unbounded_and_nonfinite_inputs(self):
        for field, values in {'duration': [0, 4, 61, 'NaN', 'inf', None], 'start': [-1, 'NaN', 'inf'], 'threshold': [.49, 1, 'NaN']}.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    validate_profile_test({field: value})

    def test_caps_scan_at_eof_and_accepts_zero_events(self):
        with patch('videocut.profile_test.probe', return_value=self.meta), patch('videocut.profile_test.detect_events', return_value=[]) as detect:
            ctx = Context()
            result = test_profile({'start': 22, 'duration': 30, 'music': 'missing.wav', 'voice_mode': 'reduce'}, ctx)
        self.assertEqual(result['duration'], 2)
        self.assertEqual(result['requested_duration'], 30)
        self.assertEqual(result['events'], [])
        detect.assert_called_once_with(Path('test.mp4'), self.profile, ctx, .82, start=22, duration=2, progress=(5, 98))
        self.assertEqual(ctx.stage, '配置测试完成')

    def test_rejects_start_at_or_after_eof_without_scanning(self):
        with patch('videocut.profile_test.probe', return_value=self.meta), patch('videocut.profile_test.detect_events') as detect:
            for start in (24, 25):
                with self.assertRaisesRegex(ValueError, '末尾'):
                    test_profile({'start': start})
            detect.assert_not_called()

    def test_cancelled_test_never_starts_scan(self):
        ctx = Context(); ctx.cancel()
        with patch('videocut.profile_test.detect_events') as detect, self.assertRaises(Cancelled):
            test_profile({}, ctx)
        detect.assert_not_called()


if __name__ == '__main__':
    unittest.main()

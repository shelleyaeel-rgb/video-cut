"""Exercise automatic selection and trusted whole-recording scan reuse."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from videocut.detect import AUTO_PROFILE_ID, detect_events, get_profile, list_profiles, save_profile, update_template
from videocut.media import Context
from videocut.pipeline import detection_candidates
from videocut.review import review_signature


class DetectionFlowTests(unittest.TestCase):
    def test_automatic_profile_exists_without_calibration(self):
        with tempfile.TemporaryDirectory() as temporary, patch('videocut.detect.PROFILES', Path(temporary)):
            profile = get_profile(AUTO_PROFILE_ID)
            self.assertEqual(list_profiles()[0]['id'], AUTO_PROFILE_ID)
            self.assertEqual(profile['templates'], [])
            with self.assertRaisesRegex(ValueError, '内置'):
                update_template(AUTO_PROFILE_ID, 'made-up', False, None)
            with patch('videocut.detect.prepare_template', return_value=(None, None, [960, 540], [])):
                with self.assertRaisesRegex(ValueError, '内置'):
                    save_profile('video', 0, [], 'test', profile_id=AUTO_PROFILE_ID)

    def test_automatic_dispatch_does_not_require_template_files(self):
        profile = get_profile(AUTO_PROFILE_ID)
        with patch('videocut.autodetect.detect_auto_events', return_value=[]) as scan:
            self.assertEqual(detect_events('fixture.mp4', profile, start=12, duration=5), [])
        scan.assert_called_once_with('fixture.mp4', None, start=12, duration=5, progress=(5, 47))

    def test_only_matching_full_scan_reused_and_events_copied(self):
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / 'video.mp4'
            video.write_bytes(b'fixture')
            cfg = {'video': video, 'profile': get_profile(AUTO_PROFILE_ID), 'threshold': .82}
            cache = {'full_scan': True, 'events': [{'time': 3, 'included': True}],
                     'review_signature': review_signature(video, cfg['profile'], .82)}
            with patch('videocut.pipeline.detect_events', return_value=[{'time': 7}]) as scan:
                result = detection_candidates(cfg, Context(), cache)
                result[0]['included'] = False
                self.assertTrue(cache['events'][0]['included'])
                scan.assert_not_called()
                for bad in ({**cache, 'full_scan': False}, {**cache, 'review_signature': 'stale'}):
                    self.assertEqual(detection_candidates(cfg, Context(), bad), [{'time': 7}])
                video.write_bytes(b'changed source')
                self.assertEqual(detection_candidates(cfg, Context(), cache), [{'time': 7}])
                self.assertEqual(scan.call_count, 3)


if __name__ == '__main__':
    unittest.main()

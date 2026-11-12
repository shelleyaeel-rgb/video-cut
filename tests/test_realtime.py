import queue
import time
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from videocut.autodetect import Cue, candidates_from_ocr
from videocut.realtime import (Detector, MarkerTracker, RealtimeCore,
                               prepare_sample, roi_for_size)


def cue(name='甲', confidence=.95, kind='kill'):
    return Cue('击败'+name, kind, confidence, (850, 765, 1060, 793), .9, name)


def sample():
    rect = roi_for_size(1920, 1080)
    return np.zeros((rect[3]-rect[1], rect[2]-rect[0], 3), np.uint8), {
        'source_width': 1920, 'source_height': 1080, 'roi': list(rect)}


class SampleTests(unittest.TestCase):
    def test_full_and_native_crop_are_identical_owned_samples(self):
        frame = np.zeros((1080, 1920, 3), np.uint8)
        crop, meta = prepare_sample(frame)
        crop[0, 0] = 255
        self.assertFalse(frame.any())
        cropped, copied_meta = prepare_sample(crop, meta)
        self.assertTrue(np.array_equal(crop, cropped))
        self.assertEqual(copied_meta['roi'], list(roi_for_size(1920, 1080)))

    def test_rejects_unsupported_layout_and_scaled_native_crop(self):
        with self.assertRaises(ValueError):
            roi_for_size(3440, 1440)
        crop, meta = sample()
        with self.assertRaises(ValueError):
            prepare_sample(crop[::2], meta)

    def test_roi_ocr_uses_source_height_for_text_size(self):
        image = np.zeros((206, 596, 3), np.uint8)
        cv2.rectangle(image, (255, 63), (290, 78), (250, 250, 250), -1)
        cv2.rectangle(image, (255, 63), (262, 70), (0, 0, 0), -1)
        rows = [[[[220, 110], [330, 110], [330, 140], [220, 140]], '击败敌人', .96]]
        self.assertEqual(candidates_from_ocr(image, rows), [])
        cues = candidates_from_ocr(image, rows, source_size=(1920, 1080))
        self.assertEqual(len(cues), 1)


class MarkerTests(unittest.TestCase):
    def test_updates_keep_uuid_and_pts_even_after_slow_ocr(self):
        updates = []
        tracker = MarkerTracker(lambda kind, item: updates.append(item))
        tracker.observe(12.05, [cue()])
        tracker.observe(12.25, [cue()])
        tracker.finish()
        self.assertEqual(len({item['id'] for item in updates}), 1)
        self.assertEqual(updates[-1]['time'], 12.05)
        self.assertEqual(updates[-1]['end'], 12.25)
        self.assertTrue(updates[-1]['final'])

    def test_unanalysed_or_ocr_missing_frame_does_not_mean_disappearance(self):
        tracker = MarkerTracker(lambda *args: None)
        tracker.observe(1, [cue()])
        tracker.observe(2, [])
        tracker.observe(2.2, [cue()])
        self.assertEqual(len(tracker.markers), 1)
        tracker.blank(2.3)
        tracker.blank(2.8)
        tracker.observe(3, [cue()])
        self.assertEqual(len(tracker.markers), 2)

    def test_refresh_requires_consistent_target_but_retains_strong_short_refresh(self):
        tracker = MarkerTracker(lambda *args: None)
        tracker.observe(1, [cue('甲')])
        tracker.observe(1.2, [cue('乙')])
        tracker.observe(1.4, [cue('甲')])
        self.assertEqual(len(tracker.markers), 1)
        tracker.observe(1.6, [cue('乙')])
        tracker.finish()
        self.assertEqual(len(tracker.markers), 2)

    def test_weak_single_read_remains_reviewable(self):
        tracker = MarkerTracker(lambda *args: None)
        tracker.observe(1, [cue(confidence=.6)])
        tracker.finish()
        marker = next(iter(tracker.markers.values()))
        self.assertEqual(marker['state'], 'needs_review')
        self.assertTrue(marker['included'])

    def test_fading_low_confidence_name_does_not_split_confirmed_prompt(self):
        tracker = MarkerTracker(lambda *args: None)
        first = cue('模糊姓名', .76)
        first.ocr_confidence = .78
        tracker.observe(17.6, [first])
        tracker.observe(17.8, [cue('真实姓名')])
        tracker.observe(18., [cue('真实姓名')])
        tracker.finish()
        self.assertEqual(len(tracker.markers), 1)
        self.assertEqual(next(iter(tracker.markers.values()))['time'], 17.6)


class CoreTests(unittest.TestCase):
    def test_gap_closes_uncertain_track_without_fabricating_blank(self):
        crop, meta = sample()
        core = RealtimeCore(recognize=lambda *args: [cue()])
        with patch('videocut.realtime.gate_score', return_value=1):
            core.process(crop, 0., meta)
            core.process(crop, .2, meta)
            core.process(crop, 2., meta)
        result = core.finish()
        self.assertEqual(len(result['markers']), 2)
        self.assertEqual(result['markers'][0]['state'], 'needs_review')
        self.assertEqual(result['gaps'][0]['reason'], 'sample_gap')

    def test_ring_and_ocr_rate_remain_bounded(self):
        crop, meta = sample()
        core = RealtimeCore(recognize=lambda *args: [cue()])
        with patch('videocut.realtime.gate_score', return_value=1):
            for index in range(600):
                core.process(crop, index/15, meta)
        self.assertLessEqual(len(core.ring), 45)
        self.assertLessEqual(core.stats['ocr_calls'], 201)


class ServiceTests(unittest.TestCase):
    def test_full_input_queue_never_calls_callbacks_or_waits(self):
        crop, meta = sample()
        callbacks = []
        service = Detector(on_gap=callbacks.append)
        service.state = 'running'
        service.samples = queue.Queue(maxsize=1)
        service.process = type('Alive', (), {'is_alive': lambda self: True})()
        self.assertTrue(service.push(crop, 0., meta))
        started = time.perf_counter()
        self.assertFalse(service.push(crop, .1, meta))
        self.assertLess(time.perf_counter()-started, .1)
        self.assertEqual(callbacks, [])
        self.assertEqual(service.snapshot()['gaps'][0]['reason'], 'queue_full')

    def test_local_gap_callback_does_not_requeue_itself(self):
        calls = []
        service = Detector(on_gap=calls.append)
        service._record_gap(0, 1, 'test')
        kind, value = service._notifications.get_nowait()
        service._consume(kind, value)
        self.assertEqual(len(calls), 1)
        self.assertTrue(service._notifications.empty())
        self.assertEqual(len(service.gaps), 1)

    def test_real_spawn_worker_drains_without_ocr_on_blank_frames(self):
        crop, meta = sample()
        service = Detector().start()
        try:
            for index in range(12):
                self.assertTrue(service.push(crop, index/15, meta))
            result = service.stop(timeout=10)
            self.assertEqual(result['state'], 'stopped')
            self.assertEqual(result['stats']['frames'], 12)
            self.assertEqual(result['stats']['ocr_calls'], 0)
            self.assertEqual(result['gaps'], [])
        finally:
            if service.process.is_alive():
                service.process.terminate()
                service.process.join(2)

    def test_drain_timeout_is_reported_as_unanalysed_coverage(self):
        crop, meta = sample()
        service = Detector().start()
        try:
            service.push(crop, 5., meta)
            result = service.stop(timeout=0)
            self.assertEqual(result['state'], 'needs_rescan')
            self.assertEqual(result['gaps'][-1]['reason'], 'drain_timeout')
            self.assertEqual(result['gaps'][-1]['end'], 5.)
        finally:
            if service.process.is_alive():
                service.process.terminate()
                service.process.join(2)


if __name__ == '__main__':
    unittest.main()

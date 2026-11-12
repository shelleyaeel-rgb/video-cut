import io
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from videocut.autodetect import (Cue, EventTracker, candidates_from_ocr,
                                detect_auto_events, status)
from videocut.media import Cancelled, Context


def row(text='击倒 敌方甲', box=(280, 330, 380, 350), score=.96):
    x0, y0, x1, y1 = box
    return [[[x0, y0], [x1, y0], [x1, y1], [x0, y1]], text, score]


def prompt_frame(box=(280, 330, 380, 350)):
    frame = np.zeros((600, 800, 3), np.uint8)
    x0, y0, x1, y1 = box
    x = (x0+x1)//2
    # A small white pictogram independent of its absolute screen location.
    cv2.rectangle(frame, (x-13, y0-36), (x+13, y0-26), (245, 245, 245), -1)
    cv2.rectangle(frame, (x+8, y0-40), (x+16, y0-31), (245, 245, 245), -1)
    cv2.rectangle(frame, (x-13, y0-26), (x-8, y0-19), (245, 245, 245), -1)
    return frame


def cue(target='敌方甲', kind='knockdown', confidence=.95, box=(280, 330, 380, 350)):
    verb = '击倒' if kind == 'knockdown' else '击败'
    return Cue(verb+target, kind, confidence, box, .9, target)


class PromptRecognitionTests(unittest.TestCase):
    def test_discovers_prompt_without_predefined_screen_region(self):
        for box in [(50, 90, 150, 110), (550, 330, 650, 350), (280, 450, 380, 470)]:
            with self.subTest(box=box):
                result = candidates_from_ocr(prompt_frame(box), [row(box=box)])
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0].event_type, 'knockdown')
                self.assertEqual(result[0].box, box)

    def test_rejects_keyword_mentions_feed_sentences_menus_and_missing_icon(self):
        frame = prompt_frame()
        for text in ['玩家甲 击倒 玩家乙', '累计击败敌人10次', '击倒次数', '击败任务奖励', '队友击倒敌人',
                     '击败3名敌方特战干员', '击败9名故于特成干员']:
            with self.subTest(text=text):
                self.assertEqual(candidates_from_ocr(frame, [row(text)]), [])
        self.assertEqual(candidates_from_ocr(np.zeros_like(frame), [row()]), [])
        self.assertEqual(candidates_from_ocr(frame, [row(score=.3)]), [])

    def test_gold_headshot_defeat_prompt_is_recognized(self):
        frame = prompt_frame()
        frame[np.any(frame > 0, axis=2)] = (40, 175, 240)
        found = candidates_from_ocr(frame, [row('击中头部击败敌方甲')])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].event_type, 'headshot')

    def test_joins_colored_target_text_and_keeps_original_evidence(self):
        first = row('击倒', (280, 330, 320, 350))
        second = row('敌方乙', (328, 330, 390, 350))
        result = candidates_from_ocr(prompt_frame(), [first, second])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].target, '敌方乙')
        self.assertEqual(result[0].text, '击倒 敌方乙')

    def test_local_crop_coordinates_are_restored_for_global_icon_validation(self):
        result = candidates_from_ocr(prompt_frame(), [row(box=(80, 130, 180, 150))], offset=(200, 200))
        self.assertEqual(result[0].box, (280, 330, 380, 350))

    def test_bad_ocr_rows_do_not_break_scan(self):
        self.assertEqual(candidates_from_ocr(prompt_frame(), [None, [], ['bad', '击倒', .9], row(score=float('nan'))]), [])


class TemporalRecognitionTests(unittest.TestCase):
    def test_persistent_notification_is_one_event_with_bounded_evidence(self):
        tracker = EventTracker()
        for i in range(24):
            tracker.observe(i/4, [cue()])
        events = tracker.finish()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['samples'], 24)
        self.assertEqual(events[0]['end'], 5.75)
        self.assertLessEqual(len(events[0]['evidence']), 4)
        self.assertEqual(events[0]['classification'], 'unknown')
        self.assertTrue(events[0]['included'])

    def test_content_refresh_without_blank_frame_starts_next_event(self):
        tracker = EventTracker()
        for t, target in [(0, 'AAAA'), (.25, 'AAAA'), (.5, 'BBBB'), (.75, 'BBBB')]:
            tracker.observe(t, [cue(target)])
        events = tracker.finish()
        self.assertEqual(len(events), 2)
        self.assertEqual([event['time'] for event in events], [0, .5])
        self.assertEqual([event['end'] for event in events], [.25, .75])

    def test_similar_but_stably_different_names_are_separate_events(self):
        tracker = EventTracker()
        for t, target in [(0, '玩家甲'), (.25, '玩家甲'), (.5, '玩家乙'), (.75, '玩家乙')]:
            tracker.observe(t, [cue(target)])
        self.assertEqual(len(tracker.finish()), 2)

    def test_strong_short_refresh_is_not_lost_when_old_track_expires(self):
        tracker = EventTracker()
        for t, cues in [(0, [cue('AAAA')]), (.25, [cue('AAAA')]), (.5, [cue('BBBB')]),
                        (.75, []), (1, []), (1.25, [])]:
            tracker.observe(t, cues)
        self.assertEqual([event['time'] for event in tracker.finish()], [0, .5])

    def test_knockdown_and_defeat_are_separate_events(self):
        tracker = EventTracker()
        for t, kind in [(0, 'knockdown'), (.25, 'knockdown'), (.5, 'kill'), (.75, 'kill')]:
            tracker.observe(t, [cue(kind=kind)])
        self.assertEqual([event['event_types'] for event in tracker.finish()], [['knockdown'], ['kill']])

    def test_single_bad_read_does_not_split_event(self):
        tracker = EventTracker()
        for t, target in [(0, 'AAAA'), (.25, 'AAAA'), (.5, 'BBBB'), (.75, 'AAAA'), (1, 'AAAA')]:
            tracker.observe(t, [cue(target)])
        self.assertEqual(len(tracker.finish()), 1)

    def test_prompt_reappearing_after_disappearance_is_new_event(self):
        tracker = EventTracker()
        for t, cues in [(0, [cue()]), (.25, [cue()]), (.5, []), (.75, []), (1, []), (1.25, [cue()]), (1.5, [cue()])]:
            tracker.observe(t, cues)
        self.assertEqual(len(tracker.finish()), 2)

    def test_long_static_menu_like_content_is_rejected(self):
        tracker = EventTracker()
        for i in range(60):
            tracker.observe(i/4, [cue()])
        self.assertEqual(tracker.finish(), [])
        self.assertEqual(tracker.rejected_static, 1)

    def test_short_strong_prompt_retained_weak_single_read_rejected(self):
        for confidence, expected in [(.96, 1), (.7, 0)]:
            tracker = EventTracker()
            tracker.observe(2, [cue(confidence=confidence)])
            self.assertEqual(len(tracker.finish()), expected)


class ScanContractTests(unittest.TestCase):
    def test_local_dependency_status_does_not_initialize_engine(self):
        with patch('videocut.autodetect.importlib.util.find_spec', return_value=None), \
                patch('videocut.autodetect._engine') as engine:
            result = status()
        self.assertFalse(result['available'])
        self.assertIn('本地中文识别', result['reason'])
        engine.assert_not_called()

    def test_cancelled_task_stops_before_model_loading(self):
        ctx = Context()
        ctx.cancel()
        with patch('videocut.autodetect.probe', return_value={'duration': 20, 'width': 800, 'height': 600}), \
                patch('videocut.autodetect._engine') as engine:
            with self.assertRaises(Cancelled):
                detect_auto_events('unused.mp4', ctx)
        engine.assert_not_called()

    def test_streaming_scan_preserves_source_time_and_continues_global_discovery(self):
        frame = prompt_frame()
        ocr_sizes, commands, records = [], [], []

        def engine(image, **kwargs):
            ocr_sizes.append(image.shape[:2])
            # Global OCR sees a prompt. Local misses emulate a faded animation;
            # the subsequent global pass must still run and reconfirm the cue.
            return ([row()] if image.shape == frame.shape else None), None

        class FakeProcess:
            stdout = io.BytesIO(frame.tobytes()*4)
            @staticmethod
            def wait(timeout):
                return 0

        class FakeLoggedProcess:
            def __init__(self, args, ctx):
                commands.append(args)
                self.process = FakeProcess()
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return None
            def check_result(self):
                return None

        ctx = Context()
        ctx.record = lambda *args, **kwargs: records.append((args, kwargs))
        with patch('videocut.autodetect.probe', return_value={'duration': 20, 'width': 800, 'height': 600}), \
                patch('videocut.autodetect._engine', return_value=engine), \
                patch('videocut.autodetect.ffmpeg_args', return_value=['ffmpeg']), \
                patch('videocut.autodetect.LoggedProcess', FakeLoggedProcess):
            events = detect_auto_events('sample.mp4', ctx, start=5, duration=1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['time'], 5)
        self.assertEqual(events[0]['end'], 5.5)
        self.assertEqual(ocr_sizes.count((600, 800)), 2)
        self.assertTrue(any(size != (600, 800) for size in ocr_sizes))
        self.assertIn('-ss', commands[0])
        self.assertNotIn('crop=', ' '.join(map(str, commands[0])))
        self.assertTrue(any(fields.get('global_ocr_count') == 2 for _, fields in records))


if __name__ == '__main__':
    unittest.main()

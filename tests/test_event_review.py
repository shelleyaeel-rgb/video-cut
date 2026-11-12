from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from videocut.detect import AUTO_PROFILE_ID, detect_events, get_profile, select_events, tag_event_ids
from videocut.review import review_exclusions, review_signature


def event(time=10., end=11., name='first'):
    return {'time': time, 'end': end, 'confidence': .95, 'event_types': ['kill'],
            'matches': [{'id': name, 'name': name}], 'classification': 'unknown'}


class EventIdentityTests(unittest.TestCase):
    def test_ids_are_stable_unique_and_ignore_mutable_review_fields(self):
        events = tag_event_ids([event(), event(name='second'), event()])
        ids = [item['event_id'] for item in events]
        self.assertEqual(len(set(ids)), 3)
        for ident in ids:
            self.assertRegex(ident, r'^[0-9a-f]{64}$')
        copied = deepcopy(events)
        copied[0].update(included=False, exclusion_reason='manual', confidence=.7)
        self.assertEqual([item['event_id'] for item in tag_event_ids(copied)], ids)

    def test_auto_dispatch_tags_events(self):
        with patch('videocut.autodetect.detect_auto_events', return_value=[event()]):
            events = detect_events('fixture.mp4', get_profile(AUTO_PROFILE_ID))
        self.assertRegex(events[0]['event_id'], r'^[0-9a-f]{64}$')

    def test_id_excludes_only_selected_same_time_event(self):
        events = tag_event_ids([event(), event(name='second')])
        kept = select_events(events, [{'event_id': events[0]['event_id']}])
        self.assertEqual(kept, [events[1]])
        self.assertFalse(events[0]['included'])
        self.assertTrue(events[1]['included'])

    def test_id_excludes_only_selected_overlapping_event(self):
        events = tag_event_ids([event(), event(10.5, 11.5, 'second')])
        kept = select_events(events, [{'event_id': events[0]['event_id']}])
        self.assertEqual(kept, [events[1]])

    def test_unknown_missing_and_ambiguous_ids_fail_before_mutating_selection(self):
        events = tag_event_ids([event(), event(name='second')])
        for exclusion in ({'event_id': 'a' * 64}, {}, {'event_id': None}):
            with self.subTest(exclusion=exclusion), self.assertRaises(ValueError):
                select_events(events, [exclusion])
        self.assertTrue(all('included' not in item for item in events))
        events[1]['event_id'] = events[0]['event_id']
        with self.assertRaises(ValueError):
            select_events(events, [{'event_id': events[0]['event_id']}])

    def test_legacy_span_chooses_one_best_event(self):
        events = [event(), event(10.5, 11.5, 'second')]
        self.assertEqual(select_events(events, [[10, 11]]), [events[1]])
        events = [event(1, 1.875), event(4, 4.875, 'second')]
        self.assertEqual(select_events(events, [[1, 1.5]]), [events[1]])

    def test_ambiguous_or_unmatched_legacy_span_fails(self):
        cases = [([event(), event(name='second')], [10, 11]),
                 ([event(), event(10.5, 11.5, 'second')], [10.5, 11]),
                 ([event()], [30, 31])]
        for events, span in cases:
            with self.subTest(span=span), self.assertRaises(ValueError):
                select_events(events, [span])
            self.assertTrue(all('included' not in item for item in events))


class ReviewIdentityValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.video = Path(self.directory.name) / 'video.mp4'
        self.video.write_bytes(b'fixture')
        self.profile = {'id': 'test', 'revision': 'one'}
        self.signature = review_signature(self.video, self.profile, .82)

    def parse(self, **review):
        return review_exclusions({'review': {'signature': self.signature, **review}},
                                 self.video, self.profile, .82)

    def test_valid_ids_and_legacy_spans_preserve_list_contract(self):
        ident = 'a' * 64
        self.assertEqual(self.parse(excluded_ids=[ident, ident], excluded=[[1, 2]]),
                         [{'event_id': ident}, [1, 2]])
        self.assertEqual(self.parse(), [])
        self.assertEqual(self.parse(excluded=[[1, 2]]), [[1, 2]])

    def test_invalid_id_format_and_count_rejected(self):
        for ids in (None, 'a' * 64, [1], ['a' * 63], ['g' * 64], ['A' * 64], ['a' * 64] * 501):
            with self.subTest(ids=str(ids)[:30]), self.assertRaises(ValueError):
                self.parse(excluded_ids=ids)
        with self.assertRaises(ValueError):
            self.parse(excluded_ids=['a' * 64] * 500, excluded=[[1, 2]])


if __name__ == '__main__':
    unittest.main()

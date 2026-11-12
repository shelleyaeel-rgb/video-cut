import math
import tempfile
import unittest
from pathlib import Path

from videocut.edit import plan_clips, unique_effect_events, write_impacts


def event(time, end=None, confidence=.95):
    return {'time': time, 'end': time + .6 if end is None else end, 'confidence': confidence}


class ClipBoundaryTests(unittest.TestCase):
    def assert_valid(self, clips, source_duration, budget):
        self.assertLessEqual(sum(c['duration'] for c in clips), budget + 1e-6)
        previous_end = 0
        previous_output_end = 0
        for clip in clips:
            self.assertGreaterEqual(clip['start'] + 1e-6, previous_end)
            self.assertLessEqual(clip['end'], source_duration + 1e-6)
            self.assertLessEqual(clip['duration'], 10)
            self.assertAlmostEqual(clip['end'] - clip['start'], clip['duration'], places=5)
            self.assertAlmostEqual(clip['output_start'], previous_output_end, places=5)
            self.assertTrue(clip['events'])
            for item in clip['events']:
                self.assertLessEqual(clip['start'] - 1e-6, item['time'])
                self.assertLess(item['time'], clip['end'] + 1e-6)
            previous_end = clip['end']
            previous_output_end += clip['duration']

    def test_prompt_end_keeps_following_action(self):
        clips = plan_clips([event(10, 12)], 100, 60)
        self.assertEqual((clips[0]['start'], clips[0]['end']), (5, 14))

    def test_earlier_long_prompt_tail_is_not_shortened_by_next_event(self):
        clips = plan_clips([event(10, 13), event(11, 11.6)], 100, 60)
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0]['end'], 15)
        self.assertEqual([e['time'] for e in clips[0]['events']], [10, 11])

    def test_persistent_indicator_has_bounded_context(self):
        clips = plan_clips([event(10, 90)], 100, 90)
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0]['duration'], 10)
        self.assertEqual(len(clips[0]['events']), 1)
        self.assert_valid(clips, 100, 90)

    def test_long_fight_keeps_every_event_and_last_action_without_replay(self):
        times = list(range(4, 60, 2))
        clips = plan_clips([event(t) for t in times], 65, 100)
        self.assertGreater(len(clips), 2)
        self.assertEqual([e['time'] for c in clips for e in c['events']], times)
        self.assertAlmostEqual(clips[-1]['end'], 60.6)
        self.assert_valid(clips, 65, 100)

    def test_budget_shorter_than_event_window_still_has_a_highlight(self):
        for target in (2, 5, 7.1):
            with self.subTest(target=target):
                clips = plan_clips([event(10.01, 13.01)], 100, target)
                self.assertEqual(len(clips), 1)
                self.assertEqual(clips[0]['events'][0]['time'], 10.01)
                self.assert_valid(clips, 100, target)

    def test_duplicate_event_identity_is_only_counted_once(self):
        first = dict(event(10, 10.5, .8), event_id='first')
        repeated = dict(first)
        clips = plan_clips([first, repeated, event(20)], 100, 60)
        items = [e for c in clips for e in c['events']]
        self.assertEqual([e['time'] for e in items], [10, 20])
        clips = plan_clips([event(10), event(10)], 100, 60)
        self.assertEqual(len(clips[0]['events']), 1)

    def test_different_same_frame_events_are_retained_in_one_clip(self):
        first = dict(event(10), event_id='first', evidence=[{'box': [10, 10, 20, 20]}])
        second = dict(event(10.01, 13.01), event_id='second', evidence=[{'box': [30, 10, 40, 20]}])
        clips = plan_clips([first, second], 100, 60)
        self.assertEqual(len(clips), 1)
        self.assertEqual([e['event_id'] for e in clips[0]['events']], ['first', 'second'])
        self.assertEqual(len(unique_effect_events(clips[0]['events'])), 1)
        self.assert_valid(clips, 100, 60)

    def test_distinct_same_frame_regions_without_ids_are_not_lost(self):
        first = dict(event(10), evidence=[{'box': [10, 10, 20, 20]}])
        second = dict(event(10), evidence=[{'box': [30, 10, 40, 20]}])
        clips = plan_clips([first, second, dict(first)], 100, 60)
        self.assertEqual(len(clips[0]['events']), 2)
        self.assertEqual([e['evidence'] for e in clips[0]['events']], [first['evidence'], second['evidence']])

    def test_long_fight_never_splits_between_simultaneous_events(self):
        events = [dict(event(t), event_id=str(t)) for t in range(4, 24, 2)]
        events.append(dict(event(8.01, 11.01), event_id='simultaneous'))
        clips = plan_clips(events, 30, 60)
        self.assertEqual(sum(len(c['events']) for c in clips), len(events))
        shot = next(c for c in clips if any(e['event_id'] == 'simultaneous' for e in c['events']))
        self.assertIn('8', [e['event_id'] for e in shot['events']])
        self.assert_valid(clips, 30, 60)

    def test_simultaneous_events_do_not_double_the_impact_audio(self):
        first, second = dict(event(1), event_id='first'), dict(event(1.01), event_id='second')
        clip = {'start': 0, 'output_start': 0, 'events': [first]}
        with tempfile.TemporaryDirectory() as temporary:
            single, multiple = Path(temporary) / 'single.wav', Path(temporary) / 'multiple.wav'
            write_impacts(single, [clip], 3, 1)
            write_impacts(multiple, [{**clip, 'events': [first, second]}], 3, 1)
            self.assertEqual(single.read_bytes(), multiple.read_bytes())
            # Even input clips whose distinct source frames map to one output
            # frame may produce only one audible impact at that instant.
            write_impacts(multiple, [clip, {'start': 10, 'output_start': 0, 'events': [event(11)]}], 3, 1)
            self.assertEqual(single.read_bytes(), multiple.read_bytes())

    def test_invalid_ends_fall_back_to_event_time(self):
        for end in (float('nan'), float('inf'), 'invalid', None, -3):
            with self.subTest(end=end):
                item = event(10)
                item['end'] = end
                clips = plan_clips([item], 100, 60)
                self.assertEqual(clips[0]['end'], 12)
                self.assertTrue(math.isfinite(clips[0]['events'][0]['end']))
                self.assert_valid(clips, 100, 60)

    def test_invalid_times_are_ignored_and_source_edges_are_respected(self):
        items = [event(t) for t in (-1, float('nan'), float('inf'), 0, 99.99, 100, 200)]
        clips = plan_clips(items, 100, 60)
        self.assertEqual([e['time'] for c in clips for e in c['events']], [0, 99.99])
        self.assert_valid(clips, 100, 60)

    def test_music_alignment_shifts_whole_clip_without_spending_more_budget(self):
        clips = plan_clips([event(10)], 100, 7.6, [5.3, float('nan'), 'bad'])
        self.assertEqual(len(clips), 1)
        self.assertAlmostEqual(clips[0]['duration'], 7.6)
        self.assertAlmostEqual(10 - clips[0]['start'], 5.3)
        self.assert_valid(clips, 100, 7.6)

    def test_alignment_cannot_overlap_adjacent_shots_or_lose_events(self):
        times = list(range(4, 60, 2))
        clips = plan_clips([event(t) for t in times], 65, 100, [i + .3 for i in range(65)])
        self.assertEqual([e['time'] for c in clips for e in c['events']], times)
        self.assert_valid(clips, 65, 100)

    def test_invalid_budgets_and_source_durations_are_empty(self):
        for value in (float('nan'), float('inf'), -1, 0, 'invalid'):
            self.assertEqual(plan_clips([event(10)], 100, value), [])
            self.assertEqual(plan_clips([event(10)], value, 60), [])


if __name__ == '__main__':
    unittest.main()

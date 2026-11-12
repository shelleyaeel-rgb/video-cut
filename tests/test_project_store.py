import math
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from videocut.media import Context
from videocut.pipeline import detection_candidates
from videocut.project_store import ProjectStore


class ProjectStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root / 'projects.sqlite')
        self.video = self.root / '原始录像.mkv'
        self.video.write_bytes(b'recording fixture')
        self.session = self.store.create_session({'game': 'delta'}, self.video)
        self.ident = self.session['id']

    def seal(self):
        return self.store.finalize_asset(self.ident, metadata={'duration': 60., 'width': 1920, 'height': 1080, 'audio_tracks': []})

    def test_restart_retains_stable_marker_identity_selection_and_exact_timestamp(self):
        marker = self.store.upsert_marker(self.ident, {'time_us': 12_345_678, 'end_us': 12_700_000,
                                                     'source': 'automatic', 'confidence': .93})
        uuid.UUID(marker['event_id'])
        marker = self.store.upsert_marker(self.ident, {'time': 12.345678, 'end': 13.1, 'confidence': .96}, marker['id'])
        self.store.update_selection(self.ident, [marker['id']], pre=7, post=3, music='song.mp3')
        reopened = ProjectStore(self.store.path).get_session(self.ident)
        self.assertEqual(len(reopened['markers']), 1)
        self.assertEqual(reopened['markers'][0]['time_us'], 12_345_678)
        self.assertEqual(reopened['markers'][0]['end_us'], 13_100_000)
        self.assertEqual(reopened['selection'], [marker['id']])
        self.assertEqual(reopened['revision'], 3)
        self.assertEqual(reopened['settings'], {'pre': 7., 'post': 3., 'music': 'song.mp3'})

    def test_empty_selection_does_not_reset_or_select_later_markers(self):
        self.store.upsert_marker(self.ident, {'time': 1})
        self.store.update_selection(self.ident, [])
        self.store.upsert_marker(self.ident, {'time': 2})
        self.assertEqual(self.store.get_session(self.ident)['selection'], [])

    def test_new_manual_marker_is_selected_but_update_does_not_undo_its_exclusion(self):
        self.store.update_selection(self.ident, [])
        manual = self.store.upsert_marker(self.ident, {'time': 1, 'source': 'manual'})
        self.assertEqual(self.store.get_session(self.ident)['selection'], [manual['id']])
        self.store.update_selection(self.ident, [])
        self.store.upsert_marker(self.ident, {'end': 1.5}, manual['id'])
        self.assertEqual(self.store.get_session(self.ident)['selection'], [])

    def test_constructor_does_not_recover_active_session_and_recovery_keeps_media(self):
        self.store.update_session(self.ident, 'recording', detection_state='degraded')
        second = ProjectStore(self.store.path)
        self.assertEqual(second.get_session(self.ident)['state'], 'recording')
        recovered = second.recover_pending()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]['state'], 'interrupted')
        self.assertEqual(recovered[0]['interrupted_from'], 'recording')
        self.assertEqual(self.video.read_bytes(), b'recording fixture')
        self.seal()
        self.assertEqual(second.recover_pending(), [])
        self.assertEqual(second.get_session(self.ident)['state'], 'ready')

    def test_sealing_required_and_source_signature_changes_invalidate_snapshot(self):
        with self.assertRaisesRegex(ValueError, '封存'):
            self.store.build_generation_spec(self.ident)

        with self.assertRaisesRegex(ValueError, '封存'):
            self.store.update_session(self.ident, 'ready')
        with self.assertRaisesRegex(ValueError, '检查结果'):
            self.store.finalize_asset(self.ident, metadata={'duration': math.nan, 'width': 1920})
        marker = self.store.upsert_marker(self.ident, {'time': 10, 'end': 10.5})
        sealed = self.seal()
        self.assertEqual(sealed['source']['path'], str(self.video.resolve()))
        spec, source = self.store.build_generation_spec(self.ident, {'video': 'evil.mp4', 'events': [{'time': 4}]})
        self.assertEqual(spec['video'], str(self.video.resolve()))
        self.assertNotIn('events', spec)
        self.assertEqual(source.selected_ids, (marker['id'],))
        self.video.write_bytes(b'changed source')
        with self.assertRaisesRegex(ValueError, '已改变'):
            source.validate()
        with self.assertRaisesRegex(ValueError, '已改变'):
            self.store.build_generation_spec(self.ident)

    def test_recovered_short_tail_cannot_silently_drop_selected_marker(self):
        marker = self.store.upsert_marker(self.ident, {'time': 61, 'source': 'manual'})
        self.seal()
        with self.assertRaisesRegex(ValueError, '超出已保存录像范围'):
            self.store.build_generation_spec(self.ident)
        self.store.update_selection(self.ident, [])
        spec, source = self.store.build_generation_spec(self.ident)
        self.assertEqual(source.selected_ids, ())
        self.assertEqual(source.candidates()[0]['event_id'], marker['id'])

    def test_export_snapshot_is_independent_of_later_edits_and_marker_updates(self):
        first = self.store.upsert_marker(self.ident, {'time': 10, 'end': 11})
        second = self.store.upsert_marker(self.ident, {'time': 20, 'end': 21})
        self.seal()
        spec, source = self.store.build_generation_spec(self.ident, mode='montage')
        self.store.update_selection(self.ident, [first['id']])
        self.store.upsert_marker(self.ident, {'time': 20, 'end': 22}, second['id'])
        self.assertEqual(source.selected_ids, (first['id'], second['id']))
        self.assertEqual(source.candidates()[1]['end'], 21)
        with patch('videocut.pipeline.detect_events', side_effect=AssertionError('recording must never rescan')):
            copied = detection_candidates({'video': self.video}, Context(), recording_source=source)
            copied[0]['end'] = 999
            self.assertEqual(source.candidates()[0]['end'], 11)
        with self.assertRaisesRegex(ValueError, '服务端'):
            detection_candidates({'video': self.video}, Context(), recording_source={'events': []})

    def test_selection_rejects_foreign_markers_and_stale_revision(self):
        marker = self.store.upsert_marker(self.ident, {'time': 1})
        with self.assertRaisesRegex(ValueError, '不属于'):
            self.store.update_selection(self.ident, ['foreign'])
        with self.assertRaisesRegex(ValueError, '已更新'):
            self.store.update_selection(self.ident, [marker['id']], expected_revision=0)
        self.assertEqual(self.store.get_session(self.ident)['selection'], [marker['id']])

    def test_separate_store_connections_cannot_both_commit_the_same_edit_revision(self):
        marker = self.store.upsert_marker(self.ident, {'time': 1})
        second = ProjectStore(self.store.path)
        barrier = threading.Barrier(2)
        results = []
        def save(store, selected):
            barrier.wait()
            try:
                store.update_selection(self.ident, selected, expected_revision=1)
                results.append('saved')
            except ValueError:
                results.append('stale')
        workers = [threading.Thread(target=save, args=(store, selected))
                   for store, selected in ((self.store, []), (second, [marker['id']]))]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertCountEqual(results, ['saved', 'stale'])
        self.assertEqual(self.store.get_session(self.ident)['revision'], 2)

    def test_invalid_timestamps_and_failed_mutation_leave_previous_state(self):
        for marker in ({'time': -1}, {'time': math.nan}, {'time': 1, 'end': .5},
                       {'time_us': True}, {'time_us': 1.2}):
            with self.subTest(marker=marker), self.assertRaises(ValueError):
                self.store.upsert_marker(self.ident, marker)
        with self.assertRaises(ValueError):
            self.store.update_session(self.ident, 'recording', unsupported=math.nan)
        self.assertEqual(self.store.get_session(self.ident)['state'], 'created')
        self.assertEqual(self.store.get_session(self.ident)['markers'], [])

    def test_concurrent_marker_writes_are_committed_and_gaps_persist(self):
        errors = []
        def add(index):
            try:
                self.store.upsert_marker(self.ident, {'time': index+.123456})
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=add, args=(index,)) for index in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.store.add_gap(self.ident, 1.123456, 2.5, 'OCR worker overloaded')
        current = ProjectStore(self.store.path).get_session(self.ident)
        self.assertEqual(len(current['markers']), 16)
        self.assertEqual(current['revision'], 16)
        self.assertEqual(current['coverage'][0]['start_us'], 1_123_456)
        self.assertEqual(current['coverage'][0]['kind'], 'detection')

    def test_candidate_and_rejected_are_not_selected_by_default(self):
        candidate = self.store.upsert_marker(self.ident, {'time': 1, 'state': 'candidate'})
        rejected = self.store.upsert_marker(self.ident, {'time': 2, 'state': 'rejected'})
        review = self.store.upsert_marker(self.ident, {'time': 3, 'state': 'needs_review'})
        self.assertEqual(self.store.get_session(self.ident)['selection'], [review['id']])
        self.store.update_selection(self.ident, [candidate['id'], review['id']], pre=8, post=4)
        self.store.upsert_marker(self.ident, {'state': 'confirmed'}, candidate['id'])
        self.store.upsert_marker(self.ident, {'state': 'rejected'}, review['id'])
        current = self.store.update_selection(self.ident, [candidate['id']])
        self.assertEqual(current['settings']['pre'], 8)
        self.assertEqual(current['settings']['post'], 4)
        self.assertEqual(current['selection'], [candidate['id']])

    def test_rescan_preserves_manual_and_matched_review_but_new_markers_need_selection(self):
        manual = self.store.upsert_marker(self.ident, {'time': 5, 'source': 'manual'})
        kept = self.store.upsert_marker(self.ident, {'time': 10, 'end': 11})
        excluded = self.store.upsert_marker(self.ident, {'time': 20, 'end': 21})
        self.store.update_selection(self.ident, [manual['id'], kept['id']])
        self.seal()
        result = self.store.replace_automatic_markers(self.ident, [
            {'time': 10.125, 'end': 11.5}, {'time': 20.25, 'end': 21.5}, {'time': 30, 'end': 31}])
        self.assertEqual(len(result['markers']), 4)
        self.assertEqual(result['markers'][1]['event_id'], kept['id'])
        self.assertEqual(result['markers'][2]['event_id'], excluded['id'])
        self.assertEqual(result['selection'], [manual['id'], kept['id']])
        self.assertEqual(result['rescan_summary']['new_unselected'], 1)
        self.assertTrue(result['rescan_summary']['selection_required'])

    def test_ambiguous_rescan_does_not_transfer_a_review_to_another_event(self):
        first = self.store.upsert_marker(self.ident, {'time': 10, 'end': 11})
        second = self.store.upsert_marker(self.ident, {'time': 10, 'end': 11})
        self.store.update_selection(self.ident, [first['id']])
        self.seal()
        result = self.store.replace_automatic_markers(self.ident, [{'time': 10, 'end': 11}])
        self.assertEqual(result['selection'], [])
        self.assertNotIn(result['markers'][0]['event_id'], (first['id'], second['id']))
        self.assertEqual(result['rescan_summary']['new_unselected'], 1)

    def test_invalid_rescan_rolls_back_without_deleting_markers(self):
        marker = self.store.upsert_marker(self.ident, {'time': 10})
        self.seal()
        with self.assertRaises(ValueError):
            self.store.replace_automatic_markers(self.ident, [{'time': 20}, {'time': -1}])
        self.assertEqual(self.store.get_session(self.ident)['markers'][0]['id'], marker['id'])

    def test_rescan_does_not_promote_rejected_or_uncertain_markers(self):
        self.seal()
        result = self.store.replace_automatic_markers(self.ident, [
            {'time': 3, 'state': 'rejected'}, {'time': 4, 'state': 'needs_review'},
            {'time': 5, 'state': 'candidate'}])
        self.assertEqual([item['state'] for item in result['markers']], ['rejected', 'needs_review', 'candidate'])
        self.assertEqual(result['selection'], [result['markers'][1]['event_id']])


if __name__ == '__main__':
    unittest.main()

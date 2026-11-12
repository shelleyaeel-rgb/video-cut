"""Lifecycle tests use a fake capture engine, never the user's screen/audio."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from videocut.project_store import ProjectStore
from videocut.recording import RecordingManager


METADATA = {'duration': 12., 'width': 1920, 'height': 1080,
            'audio_tracks': [{'index': 0, 'title': 'System audio'}]}
OPTIONS = {'source': {'kind': 'window', 'id': 'fixture-window'}, 'audio': True, 'fps': 60}


class FakeHotkey:
    def __init__(self, callback):
        self.callback, self.available = callback, False
    def start(self):
        self.available = True
        return True
    def close(self):
        self.available = False


class FakeBackend:
    def __init__(self, *, cleanup_gate=None, stop_failure=False):
        self.state = 'idle'
        self.started = threading.Event()
        self.cleanup_entered = threading.Event()
        self.cleanup_gate = cleanup_gate
        self.stop_failure = stop_failure
        self.stop_calls = []
        self.pts = 3.125
        self.path = None

    def start(self, options, output_path, *, on_sample, on_status):
        self.path = Path(output_path)
        self.path.write_bytes(b'capture-cluster-fixture')
        self.on_sample, self.on_status = on_sample, on_status
        self.state = 'recording'
        self.started.set()
        return {'started': True}

    def current_time(self):
        return self.pts

    def snapshot(self):
        return {'state': self.state}

    def stop(self, timeout=20):
        self.stop_calls.append(timeout)
        if len(self.stop_calls) > 1:
            self.cleanup_entered.set()
            if self.cleanup_gate is not None:
                if not self.cleanup_gate.wait(3):
                    raise RuntimeError('test cleanup gate timed out')
        self.state = 'stopped'
        if self.stop_failure:
            return {'stopped': self.stop_failure == 'graceful', 'graceful': False, 'error': 'fixture flush failed'}
        return {'stopped': True, 'duration_seconds': 12.}


class FakeDetector:
    def __init__(self, *, on_marker, on_gap, on_status):
        self.on_marker, self.on_gap, self.on_status = on_marker, on_gap, on_status
        self.stopped = False
    def start(self):
        self.on_status({'state': 'running'})
    def push(self, frame, pts, metadata):
        self.on_marker({'id': 'fixed-detector-event', 'time': pts, 'end': pts+.2,
                        'state': 'confirmed', 'source': 'automatic', 'confidence': .9})
    def stop(self, timeout=10):
        self.stopped = True
        return {'state': 'complete', 'markers': [], 'gaps': []}


class FailingDetector(FakeDetector):
    def start(self):
        raise RuntimeError('fixture OCR initialization failure')
    def stop(self, timeout=10):
        return {'state': 'failed', 'markers': [], 'gaps': []}


class StopFailingDetector(FakeDetector):
    def stop(self, timeout=10):
        raise RuntimeError('fixture OCR stop failure')


class RecordingLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ProjectStore(self.root / 'data' / 'projects.sqlite')
        for target, value in (('videocut.recording.probe', METADATA), ('videocut.recording.MarkerHotkey', FakeHotkey)):
            p = patch(target, return_value=value) if isinstance(value, dict) else patch(target, value)
            p.start()
            self.addCleanup(p.stop)

    def manager(self, backend=None, detector_factory=FakeDetector):
        backend = backend or FakeBackend()
        manager = RecordingManager(self.root, self.store, backend_factory=lambda: backend,
                                   detector_factory=detector_factory)
        self.addCleanup(manager.close)
        return manager, backend

    def wait_state(self, manager, expected, timeout=3):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if manager.state == expected:
                return
            if manager.state in ('failed', 'interrupted') and expected not in ('failed', 'interrupted'):
                self.fail(f'Unexpected recording failure: {manager.status()}')
            time.sleep(.005)
        self.fail(f'Expected {expected}, got {manager.status()}')

    def test_graceful_stop_persists_capture_pts_and_sealed_media(self):
        manager, backend = self.manager()
        started = manager.start(OPTIONS)
        self.wait_state(manager, 'recording')
        marker = manager.manual_marker()
        backend.on_sample(None, 4.25, {})
        manager.stop()
        self.assertTrue(manager.finished.wait(3))
        self.assertEqual(manager.state, 'ready')
        self.assertFalse(manager.busy)
        self.assertTrue(backend.stop_calls)
        self.assertEqual(backend.stop_calls[0], 20)
        session = self.store.get_session(started['session_id'])
        self.assertEqual(session['state'], 'ready')
        self.assertEqual(marker['time_us'], 3_125_000)
        self.assertEqual([item['time'] for item in session['markers']], [3.125, 4.25])
        self.assertEqual(session['source']['metadata']['audio_tracks'], METADATA['audio_tracks'])

    def test_detector_start_failure_keeps_capture_and_manual_markers_working(self):
        manager, backend = self.manager(detector_factory=FailingDetector)
        started = manager.start(OPTIONS)
        self.wait_state(manager, 'recording')
        manager.manual_marker()
        self.assertEqual(backend.state, 'recording')
        self.assertTrue(any('自动标记启动失败' in item for item in manager.warnings))
        manager.stop()
        self.assertTrue(manager.finished.wait(3))
        session = self.store.get_session(started['session_id'])
        self.assertEqual(session['state'], 'ready')
        self.assertEqual(len(session['markers']), 1)
        self.assertTrue(session['coverage'])

    def test_audio_warning_keeps_recording_and_is_saved_with_the_project(self):
        manager, backend = self.manager()
        started = manager.start(OPTIONS)
        self.wait_state(manager, 'recording')
        backend.on_status({'type': 'warning', 'code': 'audio_unavailable', 'message': 'Device unavailable'})
        self.assertEqual(manager.state, 'recording')
        self.assertFalse(manager.stop_requested.is_set())
        self.assertTrue(any('声音设备不可用' in item for item in manager.status()['warnings']))
        manager.manual_marker()
        manager.stop()
        self.assertTrue(manager.finished.wait(3))
        session = self.store.get_session(started['session_id'])
        self.assertEqual(session['state'], 'ready')
        self.assertTrue(any('声音设备不可用' in item for item in session['warnings']))
        self.assertEqual(len(session['markers']), 1)

    def test_detector_cleanup_failure_does_not_discard_a_finalized_recording(self):
        manager, backend = self.manager(detector_factory=StopFailingDetector)
        started = manager.start(OPTIONS)
        self.wait_state(manager, 'recording')
        manager.manual_marker()
        manager.stop()
        self.assertTrue(manager.finished.wait(3))
        session = self.store.get_session(started['session_id'])
        self.assertEqual(session['state'], 'ready')
        self.assertIsNotNone(session['source'])
        self.assertTrue(session['coverage'])

    def test_unconfirmed_flush_is_interrupted_never_ready(self):
        manager, backend = self.manager(FakeBackend(stop_failure=True))
        started = manager.start({**OPTIONS, 'detect': False})
        self.wait_state(manager, 'recording')
        manager.stop()
        self.assertTrue(manager.finished.wait(3))
        session = self.store.get_session(started['session_id'])
        self.assertEqual(session['state'], 'interrupted')
        self.assertIsNone(session['source'])
        self.assertTrue(Path(session['source_path']).is_file())

    def test_disconnected_native_process_is_interrupted_even_when_stopped(self):
        manager, backend = self.manager(FakeBackend(stop_failure='graceful'))
        started = manager.start({**OPTIONS, 'detect': False})
        self.wait_state(manager, 'recording')
        manager.stop()
        self.assertTrue(manager.finished.wait(3))
        session = self.store.get_session(started['session_id'])
        self.assertEqual(session['state'], 'interrupted')
        self.assertIsNone(session['source'])

    def test_cleanup_blocks_new_session_and_cannot_stop_next_capture(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        manager, backend = self.manager(FakeBackend(cleanup_gate=gate))
        manager.start({**OPTIONS, 'detect': False})
        self.wait_state(manager, 'recording')
        manager.stop()
        self.assertTrue(backend.cleanup_entered.wait(3))
        self.assertTrue(manager.busy)
        with self.assertRaisesRegex(ValueError, '录制'):
            manager.start(OPTIONS)
        gate.set()
        self.assertTrue(manager.finished.wait(3))
        second = FakeBackend()
        manager.backend_factory = lambda: second
        manager.start({**OPTIONS, 'detect': False})
        self.wait_state(manager, 'recording')
        self.assertEqual(second.stop_calls, [])
        manager.stop()
        self.assertTrue(manager.finished.wait(3))
        self.assertEqual(second.state, 'stopped')

    def test_second_manager_cannot_interrupt_or_start_over_existing_recording(self):
        manager, backend = self.manager()
        started = manager.start(OPTIONS)
        self.wait_state(manager, 'recording')
        other, _ = self.manager()
        self.assertEqual(self.store.get_session(started['session_id'])['state'], 'recording')
        with self.assertRaisesRegex(ValueError, '另一个'):
            other.start(OPTIONS)
        self.assertEqual(backend.state, 'recording')
        manager.stop()
        self.assertTrue(manager.finished.wait(3))

    def test_late_callbacks_cannot_change_a_new_session(self):
        manager, backend = self.manager()
        manager.start(OPTIONS)
        self.wait_state(manager, 'recording')
        old_status, old_sample = backend.on_status, backend.on_sample
        old_marker = manager.detector.on_marker
        manager.stop()
        self.assertTrue(manager.finished.wait(3))
        second = FakeBackend()
        manager.backend_factory = lambda: second
        started = manager.start(OPTIONS)
        self.wait_state(manager, 'recording')
        old_status({'type': 'error', 'message': 'late old capture error'})
        old_sample(None, 8., {})
        old_marker({'id': 'late-event', 'time': 8., 'end': 8.5, 'state': 'confirmed'})
        self.assertEqual(manager.state, 'recording')
        self.assertFalse(manager.stop_requested.is_set())
        self.assertIsNone(manager.error)
        self.assertEqual(self.store.get_session(started['session_id'])['markers'], [])
        second.on_sample(None, 4., {})
        self.assertEqual(len(self.store.get_session(started['session_id'])['markers']), 1)
        manager.stop()
        self.assertTrue(manager.finished.wait(3))

    def test_startup_reconciles_stale_session_without_deleting_media(self):
        source = self.root / 'abandoned.mkv'
        source.write_bytes(b'partial recording')
        session = self.store.create_session(source_path=source)
        self.store.update_session(session['id'], 'recording')
        manager, _ = self.manager()
        recovered = self.store.get_session(session['id'])
        self.assertEqual(recovered['state'], 'interrupted')
        self.assertEqual(source.read_bytes(), b'partial recording')

    def test_close_requests_graceful_stop_and_forbids_future_recording(self):
        manager, backend = self.manager()
        manager.start(OPTIONS)
        self.wait_state(manager, 'recording')
        manager.close()
        self.assertTrue(manager.finished.is_set())
        self.assertFalse(manager.worker.is_alive())
        self.assertTrue(backend.stop_calls)
        with self.assertRaisesRegex(ValueError, '关闭'):
            manager.start(OPTIONS)


if __name__ == '__main__':
    unittest.main()

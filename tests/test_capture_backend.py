import io
import json
from pathlib import Path
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from videocut.capture_backend import CaptureBackend, CaptureError, MAX_PAYLOAD, _read_message, normalize_options


def message(value, payload=b""):
    header = json.dumps(value).encode()
    return struct.pack("<I", len(header)) + header + struct.pack("<I", len(payload)) + payload


class ShortReads(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(size, 3))


class CaptureBoundaryTests(unittest.TestCase):
    def test_requires_explicit_target_never_falls_back_to_desktop(self):
        for invalid in ({}, {"source": {"kind": "monitor", "id": ""}},
                        {"source": {"kind": "unknown", "id": "anything"}}):
            with self.assertRaises(ValueError):
                normalize_options(invalid)

    def test_validates_encoder_and_dimension_limits(self):
        source = {"kind": "window", "id": "generated:Tk:python.exe"}
        for extra in ({"width": 1281}, {"fps": 999}, {"encoder": "external-command"},
                      {"height": float("nan")}, {"audio": "false"}):
            with self.assertRaises((ValueError, OverflowError)):
                normalize_options({"source": source, **extra})

    def test_will_not_overwrite_a_recording(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "existing.mkv"
            output.write_bytes(b"existing valuable video")
            with patch("videocut.capture_backend._new_process") as process:
                with self.assertRaises(FileExistsError):
                    CaptureBackend().start({"source": {"kind": "window", "id": "generated"}}, output)
                process.assert_not_called()
            self.assertEqual(output.read_bytes(), b"existing valuable video")

    def test_binary_messages_survive_partial_pipe_reads(self):
        packet = message({"type": "sample", "pts_seconds": 12.345}, b"\x00\n\xff\r")
        value, data = _read_message(ShortReads(packet))
        self.assertEqual(value["pts_seconds"], 12.345)
        self.assertEqual(data, b"\x00\n\xff\r")

    def test_rejects_oversized_payload_before_reading_or_allocating(self):
        header = json.dumps({"type": "sample"}).encode()
        packet = struct.pack("<I", len(header)) + header + struct.pack("<I", MAX_PAYLOAD + 1)
        with self.assertRaises(CaptureError):
            _read_message(io.BytesIO(packet))

    def test_truncated_payload_is_a_disconnect(self):
        with self.assertRaises(EOFError):
            _read_message(io.BytesIO(message({"type": "sample"}, b"abc")[:-1]))

    def test_slow_analysis_cannot_block_reader_and_only_keeps_recent_samples(self):
        backend = CaptureBackend()
        data = b"".join(message({"type": "sample", "pts_seconds": index, "metadata": {}}) for index in range(20))
        backend.process = SimpleNamespace(stdout=io.BytesIO(data))
        backend._on_sample = Mock()
        backend._read()
        self.assertEqual(backend._samples.qsize(), 2)
        self.assertEqual(backend._callback_drops, 18)
        self.assertEqual(backend.current_time(), 19)
        self.assertEqual(backend._samples.get_nowait()[0]["pts_seconds"], 18)
        backend._on_sample.assert_not_called()
        self.assertTrue(backend._ended.is_set())
        self.assertIn("disconnected", backend._error)

    def test_stopped_ack_preserves_error_and_actual_muxer_duration(self):
        backend = CaptureBackend()
        data = message({"type": "error", "message": "disk write failed"})
        data += message({"type": "stopped", "stopped": True, "graceful": True,
                         "duration_seconds": 10.5, "elapsed_seconds": 10.4667})
        process = Mock(stdout=io.BytesIO(data))
        process.poll.return_value = 0
        backend.process = process
        backend._read()
        result = backend.stop(timeout=.1)
        self.assertEqual(result["duration_seconds"], 10.5)
        self.assertEqual(result["error"], "disk write failed")
        process.terminate.assert_not_called()

    def queued_backend(self, callback):
        backend = CaptureBackend()
        backend.process = Mock()
        backend.process.poll.return_value = 0
        backend._stopped.set()
        backend._ended.set()
        backend._stop_ack = {"stopped": True, "graceful": True, "duration_seconds": 1}
        backend._on_sample = callback
        for pts in (0.9, 1.0):
            backend._samples.put(({"pts_seconds": pts, "metadata": {"width": 1, "height": 1}}, b"\x00\x00\x00\xff"))
        return backend

    def test_stop_drains_tail_samples_before_returning(self):
        received = []
        backend = self.queued_backend(lambda frame, pts, meta: received.append(pts))
        thread = threading.Thread(target=backend._dispatch_samples, daemon=True)
        backend._threads["samples"] = thread
        thread.start()
        result = backend.stop(timeout=2)
        self.assertEqual(received, [0.9, 1.0])
        self.assertTrue(result["callbacks_drained"])
        self.assertTrue(result["graceful"])
        self.assertFalse(thread.is_alive())

    def test_callback_timeout_disables_further_calls_and_reports_incomplete_cleanup(self):
        entered, release = threading.Event(), threading.Event()
        received = []
        def blocked(frame, pts, metadata):
            received.append(pts)
            entered.set()
            release.wait(2)
        backend = self.queued_backend(blocked)
        thread = threading.Thread(target=backend._dispatch_samples, daemon=True)
        backend._threads["samples"] = thread
        thread.start()
        self.assertTrue(entered.wait(2))
        result = backend.stop(timeout=.01)
        self.assertFalse(result["callbacks_drained"])
        self.assertFalse(result["graceful"])
        release.set()
        thread.join(2)
        self.assertEqual(received, [0.9])


if __name__ == "__main__":
    unittest.main()

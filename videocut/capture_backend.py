"""Windows capture adapter for the isolated, portable libobs host.

The recorder owns video/audio timing and encoding. This module only validates
commands and dispatches timestamped samples through bounded queues. No media
callback runs on the IPC reader thread or blocks the native recorder.
"""
from __future__ import annotations

from collections import deque
import json
import math
import os
from pathlib import Path
import queue
import struct
import subprocess
import threading
import time
from typing import Callable
import uuid

from .paths import RESOURCE_ROOT, INSTALL_ROOT

MAX_HEADER = 2 * 1024 * 1024
MAX_PAYLOAD = 64 * 1024 * 1024


class CaptureError(RuntimeError):
    pass


def runtime_root() -> Path:
    override = os.environ.get("VIDEOCUT_OBS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    for root in (RESOURCE_ROOT, INSTALL_ROOT):
        candidate = root / ".tools" / "obs"
        if (candidate / "bin" / "64bit" / "obs.dll").is_file():
            return candidate
    return RESOURCE_ROOT / ".tools" / "obs"


def backend_status() -> dict:
    runtime = runtime_root()
    missing = [name for name in ("obs.dll", "python.exe", "obs-ffmpeg-mux.exe")
               if not (runtime / "bin" / "64bit" / name).is_file()]
    host = RESOURCE_ROOT / "native" / "capture_host.py"
    if not host.is_file():
        missing.append("native/capture_host.py")
    manifest = {}
    try:
        manifest = json.loads((runtime / "capture-runtime.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    supported = os.name == "nt"
    ready = supported and not missing
    return {"available": ready, "ready": ready, "backend": "libobs",
            "version": manifest.get("version"), "runtime_path": str(runtime),
            "reason": "" if ready else ("Windows is required" if not supported else "Capture runtime is missing: " + ", ".join(missing)),
            "install_command": r".venv\Scripts\python.exe scripts\download_capture.py",
            "license": "GPL-2.0-or-later", "missing": missing,
            "features": {"window_capture": True, "monitor_capture": True,
                         "system_audio": True, "timestamped_roi": True,
                         "hardware_encoder_probe": True, "container": "mkv"}}


def _read_exact(stream, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise EOFError("Capture host closed its pipe")
        data.extend(chunk)
    return bytes(data)


def _read_message(stream):
    header_size = struct.unpack("<I", _read_exact(stream, 4))[0]
    if header_size > MAX_HEADER:
        raise CaptureError("Invalid capture host header")
    message = json.loads(_read_exact(stream, header_size))
    payload_size = struct.unpack("<I", _read_exact(stream, 4))[0]
    if payload_size > MAX_PAYLOAD:
        raise CaptureError("Invalid capture sample size")
    return message, _read_exact(stream, payload_size) if payload_size else b""


def _host_command(*args):
    status = backend_status()
    if not status["available"]:
        raise CaptureError(status["reason"])
    runtime = runtime_root()
    return [str(runtime / "bin" / "64bit" / "python.exe"), "-u",
            str(RESOURCE_ROOT / "native" / "capture_host.py"), "--runtime", str(runtime), *args]


def _new_process(*args):
    return subprocess.Popen(_host_command(*args), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                            bufsize=0)


def list_capture_devices(timeout: float = 25) -> dict:
    process = _new_process("--list")
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise CaptureError("Capture source enumeration timed out") from None
    import io
    messages = io.BytesIO(stdout)
    last_error = None
    while messages.tell() < len(stdout):
        message, _ = _read_message(messages)
        if message.get("type") == "sources":
            return message
        if message.get("type") == "error":
            last_error = message.get("message")
    detail = last_error or stderr.decode("utf-8", "replace")[-2000:]
    raise CaptureError("Unable to enumerate capture sources: " + detail)


def list_sources(timeout: float = 25) -> list[dict]:
    return list_capture_devices(timeout)["sources"]


def normalize_options(options: dict) -> dict:
    if not isinstance(options, dict):
        raise ValueError("Capture options must be an object")
    result = dict(options)
    source = result.get("source")
    if source is None:
        source = {"kind": result.get("source_kind", "window"), "id": result.get("source_id", "")}
    if not isinstance(source, dict) or source.get("kind") not in ("window", "monitor"):
        raise ValueError("Select a window or display capture source")
    if not isinstance(source.get("id"), str) or not source["id"] or len(source["id"]) > 4096:
        raise ValueError("A capture source must be explicitly selected")
    result["source"] = dict(source)
    for key, default, low, high in (("width", 1920, 640, 3840), ("height", 1080, 360, 2160),
                                  ("fps", 60, 15, 60), ("sample_fps", 15, 1, 30),
                                  ("bitrate_kbps", 16000, 1000, 80000)):
        value = result.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or int(value) != value or not low <= value <= high:
            raise ValueError(f"Invalid {key}: expected an integer between {low} and {high}")
        result[key] = int(value)
    if result["width"] % 2 or result["height"] % 2:
        raise ValueError("Recording dimensions must be even")
    if not isinstance(result.get("audio", True), bool):
        raise ValueError("audio must be true or false")
    result.setdefault("audio", True)
    result.setdefault("audio_device", "default")
    result.setdefault("encoder", "auto")
    if not isinstance(result["audio_device"], str) or len(result["audio_device"]) > 1024:
        raise ValueError("Invalid audio device")
    if result["encoder"] not in ("auto", "obs_nvenc_h264_tex", "obs_nvenc_h264", "jim_nvenc", "obs_qsv11_v2", "h264_texture_amf", "h264_fallback_amf", "obs_x264"):
        raise ValueError("Unsupported video encoder")
    return result


class CaptureBackend:
    def __init__(self):
        self.process = None
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._started = threading.Event()
        self._stopped = threading.Event()
        self._ended = threading.Event()
        self._dispatch_end = threading.Event()
        self._samples = queue.Queue(maxsize=2)
        self._statuses = queue.Queue(maxsize=64)
        self._latest = {}
        self._start_ack = None
        self._stop_ack = None
        self._error = None
        self._logs = deque(maxlen=100)
        self._on_sample = None
        self._on_status = None
        self._last_sample_pts = 0.0
        self._callback_drops = 0
        self._threads = {}
        self._accept_callbacks = True
        self._stop_in_progress = threading.Event()
        self.session_id = None

    @staticmethod
    def backend_status():
        return backend_status()

    @staticmethod
    def list_sources(timeout=25):
        return list_sources(timeout)

    def snapshot(self):
        with self._lock:
            return {**self._latest, "session_id": self.session_id,
                    "callback_dropped_frames": self._callback_drops,
                    "error": self._error, "active": bool(self.process and self.process.poll() is None and not self._stopped.is_set())}

    def current_time(self):
        with self._lock:
            return max(self._last_sample_pts, float(self._latest.get("elapsed_seconds", 0)))

    def start(self, options: dict, output_path, on_sample: Callable | None = None,
              on_status: Callable | None = None, timeout: float = 45) -> dict:
        if self.process is not None:
            raise CaptureError("Create a new CaptureBackend for each recording")
        settings = normalize_options(options)
        path = Path(output_path).expanduser().resolve()
        if path.suffix.lower() != ".mkv":
            raise ValueError("The recording container must be MKV")
        if path.exists():
            raise FileExistsError("Refusing to overwrite an existing recording")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.session_id = uuid.uuid4().hex
        self._on_sample, self._on_status = on_sample, on_status
        self.process = _new_process()
        for name, target in (("reader", self._read), ("logs", self._read_logs),
                             ("samples", self._dispatch_samples), ("statuses", self._dispatch_statuses)):
            thread = threading.Thread(target=target, daemon=True, name="capture-" + name)
            self._threads[name] = thread
            thread.start()
        self._send({"command": "start", "options": settings, "output_path": str(path)})
        deadline = time.monotonic() + timeout
        while not self._started.wait(.05):
            if self._error or self._ended.is_set():
                self.stop(timeout=5)
                raise CaptureError(self._error or "Capture host exited before recording started")
            if time.monotonic() > deadline:
                self.stop(timeout=5)
                raise CaptureError("Timed out waiting for the first encoded video frame")
        return {**self._start_ack, "session_id": self.session_id}

    def _send(self, message):
        with self._write_lock:
            if not self.process or self.process.poll() is not None:
                return
            try:
                self.process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    def stop(self, timeout: float = 20) -> dict:
        if not self.process:
            return {"stopped": True, "graceful": True, "duration_seconds": 0}
        # A callback may request stop while another thread is already joining
        # that callback. Never introduce a self-join or reciprocal-join deadlock.
        current = threading.current_thread()
        if self._stop_in_progress.is_set() and current in self._threads.values():
            self._send({"command": "stop"})
            return {**self.snapshot(), "stopped": self._stopped.is_set(), "stopping": True}
        self._stop_in_progress.set()
        deadline = time.monotonic() + max(0, timeout)
        if not self._stopped.is_set():
            self._send({"command": "stop"})
        while not self._stopped.wait(.05):
            if self._ended.is_set() or time.monotonic() >= deadline:
                break
        forced = False
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=max(.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                forced = True
                self.process.kill()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
        # The child drains its two transport samples on exit. Join the reader
        # first, so no producer can append after the dispatchers drain their tail.
        for name in ("reader", "logs"):
            thread = self._threads.get(name)
            if thread and thread is not current:
                thread.join(timeout=max(0, deadline - time.monotonic()))
        self._dispatch_end.set()
        for name in ("samples", "statuses"):
            thread = self._threads.get(name)
            if thread and thread is not current:
                thread.join(timeout=max(0, deadline - time.monotonic()))
        callbacks_drained = all(not thread.is_alive() for name, thread in self._threads.items()
                                if name in ("samples", "statuses"))
        self._accept_callbacks = False
        if self._stop_ack:
            result = {**self._stop_ack, "session_id": self.session_id, "error": self._error,
                      "callbacks_drained": callbacks_drained}
            if forced or not callbacks_drained:
                result.update(graceful=False, error=self._error or "Capture cleanup timed out; verify the saved recording")
            return result
        return {**self.snapshot(), "stopped": True, "graceful": False,
                "duration_seconds": self.current_time(),
                "callbacks_drained": callbacks_drained,
                "error": self._error or "The capture host stopped without a finalization acknowledgement; recover the MKV"}

    def _offer_status(self, message):
        try:
            self._statuses.put_nowait(message)
        except queue.Full:
            try:
                self._statuses.get_nowait()
                self._statuses.put_nowait(message)
            except queue.Empty:
                pass

    def _read(self):
        try:
            while True:
                message, payload = _read_message(self.process.stdout)
                message["capture_session_id"] = self.session_id
                kind = message.get("type")
                if kind == "sample":
                    with self._lock:
                        self._last_sample_pts = float(message["pts_seconds"])
                    try:
                        self._samples.put_nowait((message, payload))
                    except queue.Full:
                        try:
                            self._samples.get_nowait()
                        except queue.Empty:
                            pass
                        self._callback_drops += 1
                        try:
                            self._samples.put_nowait((message, payload))
                        except queue.Full:
                            self._callback_drops += 1
                    continue
                with self._lock:
                    if kind in ("stats", "started", "stopped"):
                        self._latest.update(message)
                    elif kind == "error":
                        self._error = message.get("message", "Capture failed")
                    if kind == "started":
                        self._start_ack = message
                        self._started.set()
                    elif kind == "stopped":
                        self._stop_ack = message
                        self._stopped.set()
                self._offer_status(message)
        except EOFError:
            if not self._stopped.is_set() and not self._error:
                self._error = "Capture process disconnected unexpectedly"
                self._offer_status({"type": "error", "message": self._error})
        except Exception as exc:
            self._error = str(exc)
            self._offer_status({"type": "error", "message": self._error})
        finally:
            self._ended.set()
            self._dispatch_end.set()

    def _read_logs(self):
        for line in iter(self.process.stderr.readline, b""):
            self._logs.append(line.decode("utf-8", "replace").rstrip())

    def diagnostics(self):
        return "\n".join(self._logs)

    def _dispatch_samples(self):
        import numpy as np
        while not self._dispatch_end.is_set() or not self._samples.empty():
            try:
                message, payload = self._samples.get(timeout=.1)
            except queue.Empty:
                continue
            if not self._on_sample or not self._accept_callbacks:
                continue
            try:
                meta = message["metadata"]
                meta["capture_session_id"] = self.session_id
                frame = np.frombuffer(payload, dtype=np.uint8).reshape(meta["height"], meta["width"], 4)[:, :, :3].copy()
                self._on_sample(frame, message["pts_seconds"], meta)
            except Exception as exc:
                self._offer_status({"type": "warning", "message": "Analysis callback failed: " + str(exc)})

    def _dispatch_statuses(self):
        while not self._dispatch_end.is_set() or not self._statuses.empty():
            try:
                message = self._statuses.get(timeout=.1)
            except queue.Empty:
                continue
            if self._on_status and self._accept_callbacks:
                try:
                    self._on_status(message)
                except Exception:
                    pass


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    print(json.dumps(list_capture_devices() if args.list else backend_status(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

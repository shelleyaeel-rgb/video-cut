"""Recording orchestration. Capture owns its clock and graceful-stop lifecycle.

No capture process is attached to a render Context: cancelling an export must
never kill a recording. Imports of the native backend and OCR are deliberately
lazy, so offline editing remains usable without the capture runtime.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import shutil
import threading
import time
import uuid

from . import logs
from .media import Context, ffmpeg_args, probe, run
from .paths import DATA_ROOT
from .project_store import ProjectStore


BUSY_STATES = {'starting', 'recording', 'stopping', 'finalizing'}


class RecordingLease:
    """Cross-process lock for one recorder per writable data directory."""
    def __init__(self, root):
        self.path = Path(root) / 'data' / 'recording.lock'
        self.stream = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.stream.seek(0)
                if not self.stream.read(1):
                    self.stream.write(b'0')
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.release()
            raise ValueError('另一个窗口正在录制，请先停止该录制。') from exc

    def release(self):
        if self.stream:
            self.stream.close()
            self.stream = None


class MarkerHotkey:
    """Ctrl+Alt+F8, scoped to a live recording; no keyboard hook/injection."""
    def __init__(self, callback):
        self.callback = callback
        self.thread = None
        self.thread_id = None
        self.ready = threading.Event()
        self.available = False

    def start(self):
        if os.name != 'nt':
            return False
        def listen():
            import ctypes
            from ctypes import wintypes
            user = ctypes.windll.user32
            self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
            self.available = bool(user.RegisterHotKey(None, 1, 0x4003, 0x77))
            self.ready.set()
            if not self.available:
                return
            msg = wintypes.MSG()
            try:
                while user.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    if msg.message == 0x0312:
                        try:
                            self.callback()
                        except Exception as exc:
                            logs.failure('runtime', 'recording_hotkey_failed', exc)
            finally:
                user.UnregisterHotKey(None, 1)
                self.available = False
        self.thread = threading.Thread(target=listen, daemon=True, name='recording-hotkey')
        self.thread.start()
        self.ready.wait(1)
        return self.available

    def close(self):
        if self.thread_id and self.available:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(2)


class RecordingManager:
    def __init__(self, root=None, store=None, backend_factory=None, detector_factory=None):
        self.root = Path(root or DATA_ROOT)
        self.store = store or ProjectStore(self.root / 'data' / 'projects.sqlite')
        self.backend_factory = backend_factory
        self.detector_factory = detector_factory
        self.lock = threading.RLock()
        self.lease = RecordingLease(self.root)
        self.current = None
        self.backend = None
        self.detector = None
        self.worker = None
        self.hotkey = None
        self.stop_requested = threading.Event()
        self.finished = threading.Event()
        self.finished.set()
        self.stats = {}
        self.warnings = []
        self.error = None
        self.state = 'idle'
        self.detector_state = 'idle'
        self.closed = False
        self.gap_keys = set()
        self.store_failed = False
        # Only the owner of the cross-process recording lease may reconcile old
        # sessions. Opening another browser/server must not interrupt a recorder.
        try:
            self.lease.acquire()
        except ValueError:
            pass
        else:
            try:
                self.store.recover_pending()
            finally:
                self.lease.release()

    @property
    def busy(self):
        with self.lock:
            return self.state in BUSY_STATES

    def capabilities(self):
        from .capture_backend import backend_status
        return backend_status()

    def sources(self):
        from .capture_backend import list_sources
        return list_sources()

    def _warn(self, message):
        with self.lock:
            if message not in self.warnings:
                self.warnings.append(message)
                self.warnings = self.warnings[-12:]

    def _persist(self, action, *args, **kwargs):
        try:
            return action(*args, **kwargs)
        except Exception as exc:
            self.store_failed = True
            self._warn('录像继续，但工程记录保存失败，请检查磁盘空间。')
            logs.failure('runtime', 'recording_store_failed', exc, session_id=self.current)
            return None

    def _marker(self, marker):
        if not self.current:
            return
        ident = marker.get('id') or marker.get('event_id') or uuid.uuid4().hex
        self._persist(self.store.upsert_marker, self.current, marker, marker_id=ident)

    def _gap(self, gap):
        if self.current:
            key = (gap['start'], gap['end'], gap.get('reason', '检测中断'))
            with self.lock:
                if key in self.gap_keys:
                    return
            saved = self._persist(self.store.add_gap, self.current, gap['start'], gap['end'], gap.get('reason', '检测中断'))
            if saved is not None:
                with self.lock:
                    self.gap_keys.add(key)

    def _detection_status(self, value):
        with self.lock:
            self.detector_state = value.get('state', self.detector_state)
        if value.get('error'):
            self._warn('自动标记异常：' + str(value['error']))

    def _capture_status(self, value):
        with self.lock:
            self.stats.update(value)
        if value.get('error') or value.get('type') == 'error':
            self.error = str(value.get('error') or value.get('message') or '录制引擎异常')
            self.stop_requested.set()
        for warning in value.get('warnings', []):
            self._warn(str(warning))
        if value.get('type') == 'warning':
            message = {
                'audio_reconnecting': '系统声音设备发生变化，正在重新连接，期间可能有静音。',
                'audio_unavailable': '当前声音设备不可用，录像可能没有系统声音。',
            }.get(value.get('code'), str(value.get('message') or '录制引擎提示异常，请检查录像。'))
            self._warn(message)

    def _sample(self, frame, pts, metadata):
        detector = self.detector
        if detector:
            try:
                detector.push(frame, pts, metadata)
            except Exception as exc:
                self._warn('自动标记暂不可用，录像仍在继续。')
                self._detection_status({'state': 'failed', 'error': str(exc)})

    def status(self):
        with self.lock:
            ident, backend = self.current, self.backend
            result = {'state': self.state, 'session_id': ident, 'busy': self.state in BUSY_STATES,
                      'detection_state': self.detector_state, 'stats': dict(self.stats),
                      'warnings': list(self.warnings), 'error': self.error,
                      'hotkey': bool(self.hotkey and self.hotkey.available)}
        try:
            result['elapsed_seconds'] = max(0., float(backend.current_time())) if backend else float(result['stats'].get('duration_seconds', 0))
        except Exception:
            result['elapsed_seconds'] = float(result['stats'].get('duration_seconds', 0))
        if ident:
            session = self.store.get_session(ident)
            markers = session.get('markers', [])
            result['marker_count'] = sum(m.get('state') != 'rejected' for m in markers)
            result['gap_count'] = len(session.get('coverage', []))
        else:
            result.update(marker_count=0, gap_count=0)
        return result

    def start(self, options):
        if not isinstance(options, dict):
            raise ValueError('录制参数无效。')
        if options.get('fps', 60) not in (30, 60):
            raise ValueError('录制帧率仅支持 30 或 60。')
        source = options.get('source')
        if not isinstance(source, dict) or source.get('kind') not in ('window', 'monitor') or not source.get('id'):
            raise ValueError('请先选择录制窗口。')
        options = {**options, 'width': 1920, 'height': 1080, 'sample_fps': 15}
        with self.lock:
            if self.closed:
                raise ValueError('应用正在关闭。')
            if self.busy:
                raise ValueError('已有录制正在运行或收尾。')
            self.lease.acquire()
            try:
                free = shutil.disk_usage(self.root).free
                if free < 512 * 1024 * 1024:
                    raise ValueError('录制目录可用空间不足 512 MiB，请先腾出空间。')
                ident = uuid.uuid4().hex
                directory = self.root / 'recordings' / ident
                directory.mkdir(parents=True, exist_ok=False)
                source_path = directory / 'source.mkv'
                self.store.create_session(options, source_path, session_id=ident)
                self.store.update_session(ident, state='starting')
            except Exception:
                self.lease.release()
                raise
            self.current, self.state = ident, 'starting'
            self.stats, self.warnings, self.error = {}, [], None
            self.gap_keys.clear()
            self.store_failed = False
            self.detector_state = 'warming' if options.get('detect', True) else 'disabled'
            self.stop_requested.clear()
            self.finished.clear()
            self.worker = threading.Thread(target=self._run, args=(options, source_path), daemon=True, name='recording-session')
            self.worker.start()
        return {'session_id': ident, 'state': 'starting'}

    def _run(self, options, source_path):
        terminal_state = 'failed'
        session_id = self.current

        def session_callback(callback):
            # A timed-out worker may return late. Never let its callbacks mutate
            # a later session, even if a backend cannot cancel a blocked callback.
            def dispatch(*args):
                with self.lock:
                    if self.current == session_id and self.state in BUSY_STATES:
                        return callback(*args)
            return dispatch

        try:
            if self.backend_factory is None:
                from .capture_backend import CaptureBackend
                factory = CaptureBackend
            else:
                factory = self.backend_factory
            self.backend = factory()
            if options.get('detect', True):
                try:
                    if self.detector_factory is None:
                        from .realtime import Detector
                        detector_factory = Detector
                    else:
                        detector_factory = self.detector_factory
                    self.detector = detector_factory(on_marker=session_callback(self._marker),
                        on_gap=session_callback(self._gap), on_status=session_callback(self._detection_status))
                    self.detector.start()
                except Exception as exc:
                    self.detector_state = 'failed'
                    self._warn('自动标记启动失败，仍可录制并手动标记：' + str(exc))
            ack = self.backend.start(options, source_path, on_sample=session_callback(self._sample),
                                     on_status=session_callback(self._capture_status))
            self.stats.update(ack or {})
            with self.lock:
                self.state = 'recording'
            self._persist(self.store.update_session, self.current, state='recording')
            self.hotkey = MarkerHotkey(self.manual_marker)
            self.hotkey.start()
            logs.event('runtime', 'recording_started', session_id=self.current, output=str(source_path))
            while not self.stop_requested.wait(.5):
                if shutil.disk_usage(source_path.parent).free < 256 * 1024 * 1024:
                    self._warn('可用空间低于 256 MiB，已停止录制以保存文件。')
                    self.stop_requested.set()
                snapshot = self.backend.snapshot()
                if snapshot.get('state') in ('failed', 'stopped', 'error') or snapshot.get('error'):
                    self.error = str(snapshot.get('error') or '录制引擎意外停止，正在检查录像。')
                    self.stop_requested.set()
            with self.lock:
                self.state = 'stopping'
            self._persist(self.store.update_session, self.current, state='stopping')
            stopped = self.backend.stop(timeout=20)
            self.stats.update(stopped or {})
            if stopped and (stopped.get('stopped') is False or stopped.get('graceful') is False):
                raise RuntimeError(stopped.get('error') or '录制未正常结束，请恢复已写入的录像。')
            with self.lock:
                self.state = 'finalizing'
            self._persist(self.store.update_session, self.current, state='finalizing')
            if self.detector:
                try:
                    report = self.detector.stop(timeout=10)
                    self.detector_state = report.get('state', 'complete')
                    for marker in report.get('markers', []):
                        self._marker(marker)
                    for gap in report.get('gaps', []):
                        self._gap(gap)
                    self.detector = None
                except Exception as exc:
                    self.detector_state = 'needs_rescan'
                    self._warn('识别收尾失败，录像仍会保存，可录后补查提示区。')
                    logs.failure('runtime', 'detection_drain_failed', exc, session_id=self.current)
            metadata = probe(source_path)
            if metadata['duration'] <= 0 or not metadata['width']:
                raise RuntimeError('录制文件没有有效画面，请从历史录制中恢复。')
            if options.get('audio', True) and not metadata['audio_tracks']:
                self._warn('录像已保存，但未发现声音轨道。')
            self.store.finalize_asset(self.current, source_path, metadata)
            if self.store_failed:
                self.detector_state = 'needs_rescan'
            if not options.get('detect', True) or self.detector_state in ('failed', 'needs_rescan'):
                self._gap({'start': 0., 'end': metadata['duration'], 'reason': '自动标记未覆盖完整录制，可录后补查'})
            self.stats['duration_seconds'] = metadata['duration']
            self._persist(self.store.update_session, self.current, detection_state=self.detector_state, warnings=self.warnings)
            terminal_state = 'ready'
        except Exception as exc:
            self.error = str(exc)
            logs.failure('runtime', 'recording_failed', exc, session_id=self.current)
            terminal_state = 'interrupted' if source_path.is_file() and source_path.stat().st_size else 'failed'
            self._persist(self.store.update_session, self.current, state=terminal_state, error=str(exc))
        finally:
            if self.hotkey:
                self.hotkey.close()
            if self.backend:
                try:
                    self.backend.stop(timeout=10)
                except Exception as exc:
                    logs.failure('runtime', 'recording_cleanup_failed', exc, session_id=self.current)
                try:
                    diagnostics = getattr(self.backend, 'diagnostics', lambda: '')()
                    if diagnostics:
                        logs.event('runtime', 'recording_engine_diagnostics', session_id=self.current,
                                   text=diagnostics[-32000:])
                except Exception as exc:
                    logs.failure('runtime', 'recording_diagnostics_failed', exc, session_id=self.current)
            if self.detector:
                try:
                    self.detector.stop(timeout=3)
                except Exception:
                    pass
            self.detector = None
            self.backend = None
            self._persist(self.store.update_session, self.current, warnings=list(self.warnings))
            with self.lock:
                self.lease.release()
                self.state = terminal_state
                self.finished.set()

    def stop(self):
        with self.lock:
            if self.state in BUSY_STATES:
                self.stop_requested.set()
                if self.state == 'recording':
                    self.state = 'stopping'
        return self.status()

    def manual_marker(self):
        with self.lock:
            if self.state != 'recording' or self.backend is None:
                raise ValueError('请在录制开始后添加标记。')
            pts = float(self.backend.current_time())
            if not math.isfinite(pts) or pts < 0:
                raise ValueError('首帧尚未就绪，请稍后标记。')
            return self.store.upsert_marker(self.current, {'time': pts, 'end': pts, 'source': 'manual',
                'state': 'confirmed', 'event_types': ['other'], 'confidence': 1., 'reason': '手动标记'})

    def close(self):
        with self.lock:
            self.closed = True
            self.stop_requested.set()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(90)
            if self.worker.is_alive():
                logs.event('runtime', 'recording_shutdown_incomplete', 'ERROR', session_id=self.current)


def session_source(session):
    source = session.get('source') or {}
    path = (source.get('path') if isinstance(source, dict) else source) or session.get('source_path')
    if not path:
        raise ValueError('录制尚未生成媒体文件。')
    return Path(path)


def preview_recording(session, start, end, ctx=None):
    """Short browser-compatible proxy, never transcode the entire source to preview."""
    ctx = ctx or Context()
    source = session_source(session)
    meta = probe(source, ctx)
    start, end = float(start), float(end)
    if not all(math.isfinite(v) for v in (start, end)) or not 0 <= start < end <= meta['duration'] + .01 or end-start > 45:
        raise ValueError('预览范围必须位于录像内，且不超过 45 秒。')
    destination = source.parent / 'previews' / f'{uuid.uuid4().hex}.mp4'
    destination.parent.mkdir(exist_ok=True)
    partial = destination.with_suffix('.partial.mp4')
    ctx.update('准备片段预览', 10)
    try:
        run(ffmpeg_args() + ['-ss', str(start), '-i', source, '-t', str(end-start), '-map', '0:v:0',
            '-map', '0:a:0?', '-vf', 'scale=960:-2', '-c:v', 'libx264', '-preset', 'veryfast',
            '-crf', '24', '-threads', '2', '-c:a', 'aac', '-movflags', '+faststart', partial], ctx, timeout=120)
        metadata = probe(partial, ctx)
        ctx.check()
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)
    ctx.update('预览已就绪', 100)
    return {'output': str(destination), 'start': start, 'end': end, 'duration': metadata['duration']}


def recover_recording(store, ident, ctx=None):
    ctx = ctx or Context()
    session = store.get_session(ident)
    if session.get('state') not in ('interrupted', 'failed'):
        raise ValueError('只有异常中断的录制需要恢复。')
    source = session_source(session)
    if not source.is_file():
        raise ValueError('找不到原始录制文件。')
    destination = source.with_name('recovered-' + uuid.uuid4().hex[:8] + '.mkv')
    ctx.update('检查并恢复录像', 10)
    run(ffmpeg_args() + ['-err_detect', 'ignore_err', '-i', source, '-map', '0:v:0', '-map', '0:a?',
                        '-c', 'copy', destination], ctx, timeout=600)
    metadata = probe(destination, ctx)
    if metadata['duration'] <= 0 or not metadata['width']:
        raise ValueError('尚未恢复出有效画面，原始文件已保留。')
    result = store.finalize_asset(ident, destination, metadata)
    ctx.update('录像已恢复', 100)
    return result


def rescan_recording(store, ident, ctx=None):
    from .realtime import scan_recording
    ctx = ctx or Context()
    session = store.get_session(ident)
    if session.get('state') != 'ready':
        raise ValueError('请先停止录制或恢复录像。')
    source = session_source(session)
    report = scan_recording(source, ctx)
    ctx.check()
    if report.get('state') != 'complete':
        raise RuntimeError('提示区补查未完整结束，原有标记已保留。')
    store.replace_automatic_markers(ident, report['markers'])
    store.update_session(ident, detection_state=report.get('state', 'complete'))
    return store.get_session(ident)

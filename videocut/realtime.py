"""Bounded, process-isolated Delta HUD detection using recording timestamps.

The initial preset covers the observed Chinese, 16:9, default-HUD notification
layout. It is deliberately not a claim to support every Delta Force game mode.
``push`` accepts BGR source images, or original-resolution crops with ``roi``
and ``source_width/source_height`` metadata. It never invokes OCR or callbacks.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import math
import multiprocessing
import os
import queue
import threading
import time
import uuid

import cv2
import numpy as np

from .autodetect import _engine, _same_region, candidates_from_ocr
from .media import Context, probe

PROFILE = 'delta_16_9_v1'
SAMPLE_FPS = 15
ROI = (.35, .59, .66, .78)


def roi_for_size(width, height):
    width, height = int(width), int(height)
    if min(width, height) <= 0 or abs(width / height - 16 / 9) > .04:
        raise ValueError('实时提示识别目前仅支持 16:9 游戏画面。')
    if height < 720:
        raise ValueError('实时提示识别需要至少 720p 的游戏画面。')
    return (math.floor(width*ROI[0]), math.floor(height*ROI[1]),
            math.ceil(width*ROI[2]), math.ceil(height*ROI[3]))


def prepare_sample(frame, meta=None):
    meta = dict(meta or {})
    if not isinstance(frame, np.ndarray) or frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8:
        raise ValueError('识别帧必须为 uint8 BGR 图像。')
    width = int(meta.get('source_width', frame.shape[1]))
    height = int(meta.get('source_height', frame.shape[0]))
    expected = roi_for_size(width, height)
    if 'roi' in meta:
        rect = tuple(map(int, meta['roi']))
        if rect != expected or frame.shape[:2] != (rect[3]-rect[1], rect[2]-rect[0]):
            raise ValueError('提示区域必须匹配源画面尺寸，且不得提前缩小。')
        crop = frame
    else:
        if frame.shape[:2] != (height, width):
            raise ValueError('源画面尺寸与图像不一致。')
        x0, y0, x1, y1 = expected
        crop = frame[y0:y1, x0:x1]
    meta.update(source_width=width, source_height=height, roi=list(expected))
    # Own the small crop: capture providers reuse their frame buffers.
    return np.array(crop, copy=True, order='C'), meta


def gate_score(image):
    """Cheap permissive white/gold glyph gate, not an event classifier."""
    low, high = image.min(axis=2), image.max(axis=2)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    gold = ((hsv[:, :, 0] >= 12) & (hsv[:, :, 0] <= 38) &
            (hsv[:, :, 1] >= 75) & (hsv[:, :, 2] >= 150))
    mask = (((low >= 145) & ((high.astype(np.int16)-low) <= 80)) | gold).astype(np.uint8)
    scale = image.shape[0] / (1080 * (ROI[3]-ROI[1]))
    # Both the notification's fixed text band and its central icon must contain
    # small glyphs. Bright gun edges elsewhere in the crop are not enough.
    height, width = image.shape[:2]
    text_mask = mask[round(height*.57):round(height*.83), round(width*.15):round(width*.83)]
    icon_mask = mask[round(height*.33):round(height*.64), round(width*.38):round(width*.59)]
    _, _, stats, _ = cv2.connectedComponentsWithStats(text_mask, 8)
    glyphs = [row for row in stats[1:] if
              2*scale <= row[2] <= 90*scale and 3*scale <= row[3] <= 60*scale
              and 6*scale*scale <= row[4] <= 2000*scale*scale]
    if len(glyphs) < 3:
        return 0.
    _, _, icons, _ = cv2.connectedComponentsWithStats(icon_mask, 8)
    if not any(7*scale <= row[2] <= 85*scale and 5*scale <= row[3] <= 60*scale
               and 24*scale*scale <= row[4] <= 2200*scale*scale for row in icons[1:]):
        return 0.
    # A near-white loading/flash frame is not useful OCR evidence.
    coverage = float(mask.mean())
    if coverage > .65:
        return 0.
    return min(1., len(glyphs)/12) * min(1., coverage/.012)


class MarkerTracker:
    """Stable live IDs; only an observed blank ends a visible notification."""
    def __init__(self, emit):
        self.emit = emit
        self.active = []
        self.markers = {}
        self.blank_since = None

    def _publish(self, track, final=False, uncertain=False):
        cue = track['cue']
        state = 'confirmed' if (track['samples'] >= 2 or
                cue.confidence >= .88 and cue.icon_score >= .65) else 'candidate'
        if final and state == 'candidate' or uncertain:
            state = 'needs_review'
        if track['end'] - track['time'] > 8:
            state = 'rejected'
        marker = {'id': track['id'], 'event_id': track['id'],
                  'time': round(track['time'], 6), 'end': round(track['end'], 6),
                  'event_types': [cue.event_type], 'state': state,
                  'confidence': cue.confidence, 'samples': track['samples'],
                  'classification': 'unknown', 'included': state != 'rejected',
                  'source': 'realtime', 'profile': PROFILE,
                  'reason': '提示文字与邻近图标；玩家与小兵待确认',
                  'matches': [{'id': 'realtime-ocr', 'name': cue.text, 'confidence': cue.confidence}],
                  'evidence': list(track['evidence']), 'final': final}
        self.markers[track['id']] = marker
        self.emit('marker', deepcopy(marker))

    def _new(self, pts, cue):
        if len(self.active) >= 8:
            self._publish(self.active.pop(0), final=True, uncertain=True)
        track = {'id': str(uuid.uuid4()), 'time': pts, 'end': pts,
                 'cue': cue, 'samples': 1, 'evidence': [], 'pending': None}
        self._evidence(track, pts, cue)
        self.active.append(track)
        self._publish(track)
        return track

    @staticmethod
    def _evidence(track, pts, cue):
        if len(track['evidence']) < 5:
            track['evidence'].append({'time': round(pts, 6), 'box': list(cue.box),
                                      'text': cue.text, 'confidence': cue.confidence,
                                      'icon_score': cue.icon_score})

    @staticmethod
    def _same(a, b):
        if a.event_type != b.event_type:
            return False
        # Animation/fading often produces confident-looking but wrong names.
        # A name can split live events only when BOTH readings are strong.
        def reliable(cue):
            score = cue.ocr_confidence if cue.ocr_confidence is not None else cue.confidence
            return cue.target if score >= .9 else ''
        left, right = reliable(a), reliable(b)
        return not left or not right or left == right

    def observe(self, pts, cues):
        if not cues:
            # An OCR miss is not evidence that a notification disappeared.
            return
        self.blank_since = None
        used = set()
        for cue in cues[:8]:
            track = next((item for item in self.active if item['id'] not in used and
                          _same_region(item['cue'].box, cue.box)), None)
            if track is None:
                track = self._new(pts, cue)
            elif not self._same(track['cue'], cue):
                pending = track['pending']
                if pending and self._same(pending[1], cue) and pts-pending[0] <= .8:
                    self._publish(track, final=True)
                    self.active.remove(track)
                    track = self._new(pending[0], pending[1])
                    track['samples'] = 2
                    track['end'] = pts
                    self._evidence(track, pts, cue)
                    self._publish(track)
                else:
                    track['pending'] = (pts, cue)
            else:
                track['end'] = max(pts, track['end'])
                track['samples'] += 1
                track['pending'] = None
                if cue.confidence > track['cue'].confidence:
                    track['cue'] = cue
                self._evidence(track, pts, cue)
                self._publish(track)
            used.add(track['id'])

    def blank(self, pts):
        if self.blank_since is None:
            self.blank_since = pts
        if pts-self.blank_since >= .4:
            self.finish()

    def finish(self, uncertain=False):
        tracks, self.active = self.active, []
        for track in tracks:
            self._publish(track, final=True, uncertain=uncertain)
            if track['pending']:
                pts, cue = track['pending']
                pending = self._new(pts, cue)
                self.active.remove(pending)
                self._publish(pending, final=True, uncertain=uncertain)


class RealtimeCore:
    """Synchronous detector core; only run inside the isolated worker/offline job."""
    def __init__(self, emit=None, recognize=None):
        self.emit = emit or (lambda *unused: None)
        self.recognize = recognize or self._recognize
        self.engine = None
        self.ring = deque(maxlen=SAMPLE_FPS*3)
        self.tracker = MarkerTracker(self.emit)
        self.last_pts = None
        self.last_ocr = -math.inf
        self.geometry = None
        self.gaps = []
        self.stats = {'frames': 0, 'ocr_calls': 0, 'gated_frames': 0,
                      'ocr_seconds': 0., 'profile': PROFILE}

    def _recognize(self, image, meta):
        self.engine = self.engine or _engine(Context())
        rows, _ = self.engine(image, use_cls=False)
        cues = candidates_from_ocr(image, rows, source_size=(meta['source_width'], meta['source_height']))
        x, y = meta['roi'][:2]
        for cue in cues:
            a, b, c, d = cue.box
            cue.box = (a+x, b+y, c+x, d+y)
        return cues

    def gap(self, start, end, reason):
        gap = {'start': max(0., start), 'end': max(start, end), 'reason': reason}
        self.gaps.append(gap)
        self.emit('gap', gap)
        self.tracker.finish(uncertain=True)
        self.ring.clear()

    def process(self, image, pts, meta):
        geometry = (meta['source_width'], meta['source_height'], tuple(meta['roi']), meta.get('geometry_revision', 0))
        if self.last_pts is not None:
            if pts <= self.last_pts:
                self.gap(pts, self.last_pts, 'non_monotonic_pts')
                return
            if pts-self.last_pts > .25:
                self.gap(self.last_pts, pts, 'sample_gap')
            if geometry != self.geometry:
                self.gap(self.last_pts, pts, 'geometry_changed')
        self.geometry, self.last_pts = geometry, pts
        self.stats['frames'] += 1
        score = gate_score(image)
        self.ring.append((pts, image, meta, score))
        while self.ring and pts-self.ring[0][0] > 3:
            self.ring.popleft()
        if not score:
            self.tracker.blank(pts)
            return
        self.stats['gated_frames'] += 1
        if pts-self.last_ocr < .18:
            return
        # Prefer clear recent samples, but never back-date a subsequent update.
        choices = [s for s in self.ring if s[0] > self.last_ocr and pts-s[0] <= .2 and s[3]]
        choices.sort(key=lambda s: (s[3], s[0]), reverse=True)
        # Keep up to three eligible images in the ring, but read only the best
        # one now. The next sampling opportunity retries without tripling OCR
        # work on every bright background false candidate.
        for sample_pts, crop, sample_meta, _ in choices[:1]:
            started = time.perf_counter()
            cues = self.recognize(crop, sample_meta)
            self.stats['ocr_seconds'] += time.perf_counter()-started
            self.stats['ocr_calls'] += 1
            if cues:
                self.tracker.observe(sample_pts, cues)
                break
        self.last_ocr = pts

    def finish(self):
        self.tracker.finish()
        return {'markers': list(self.tracker.markers.values()), 'gaps': self.gaps, 'stats': dict(self.stats)}


def _worker(samples, results, stopping):
    if os.name == 'nt':
        # Detection yields CPU scheduling priority to the game/capture worker.
        # Failure to change priority is not a reason to interrupt recording.
        try:
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
        except (AttributeError, OSError):
            pass
    dropped_results = False
    def emit(kind, value):
        nonlocal dropped_results
        try:
            results.put((kind, value), timeout=.1)
        except queue.Full:
            dropped_results = True
    core = RealtimeCore(emit)
    emit('status', {'state': 'running', 'profile': PROFILE, 'sample_fps': SAMPLE_FPS})
    last_status = time.monotonic()
    try:
        while True:
            try:
                item = samples.get(timeout=.1)
            except queue.Empty:
                if stopping.is_set():
                    break
                continue
            if item[0] == 'gap':
                core.gap(*item[1:])
            else:
                _, image, pts, meta = item
                core.process(image, pts, meta)
            if time.monotonic()-last_status > 1:
                emit('status', {'state': 'running', 'processed_pts': core.last_pts, 'stats': dict(core.stats)})
                last_status = time.monotonic()
        result = core.finish()
        result.update(state='stopped', processed_pts=core.last_pts, dropped_updates=dropped_results)
        # The final snapshot restores coalesced/dropped marker updates.
        results.put(('complete', result), timeout=2)
    except BaseException as exc:
        emit('status', {'state': 'failed', 'error': str(exc), 'processed_pts': core.last_pts})


class Detector:
    def __init__(self, on_marker=None, on_gap=None, on_status=None, *, queue_size=45, profile=PROFILE):
        if profile != PROFILE:
            raise ValueError('不支持的实时提示配置。')
        self.callbacks = {'marker': on_marker, 'gap': on_gap, 'status': on_status}
        self.queue_size = max(1, min(90, int(queue_size)))
        self.lock = threading.RLock()
        self.markers, self.gaps, self.stats = {}, [], {}
        self.state = 'idle'
        self.last_pts = None
        self.processed_pts = None
        self.last_enqueued = -math.inf
        self._gap = None
        self._last_gap_notify = {}
        self._complete = threading.Event()
        self._notifications = queue.Queue(maxsize=128)

    def start(self):
        with self.lock:
            if self.state != 'idle':
                raise RuntimeError('识别服务不可重复启动。')
            mp = multiprocessing.get_context('spawn')
            self.samples = mp.Queue(maxsize=self.queue_size)
            self.results = mp.Queue(maxsize=256)
            self.stopping = mp.Event()
            self.process = mp.Process(target=_worker, args=(self.samples, self.results, self.stopping), daemon=True)
            self.process.start()
            self.state = 'running'
            self.listener = threading.Thread(target=self._listen, daemon=True, name='realtime-results')
            self.listener.start()
        return self

    def _notify(self, kind, payload):
        try:
            self._notifications.put_nowait(('notify:'+kind, payload))
        except queue.Full:
            pass  # State is retained; callbacks are only change notifications.

    def _record_gap(self, start, end, reason):
        gap = {'start': max(0., float(start)), 'end': max(float(start), float(end)), 'reason': reason}
        if self.gaps and self.gaps[-1]['reason'] == reason and start <= self.gaps[-1]['end']+.25:
            self.gaps[-1]['end'] = max(self.gaps[-1]['end'], end)
            gap = dict(self.gaps[-1])
        else:
            self.gaps.append(gap)
        if reason not in self._last_gap_notify or end-self._last_gap_notify[reason] >= 1:
            self._last_gap_notify[reason] = end
            self._notify('gap', gap)

    def push(self, frame, pts, meta=None):
        # No OCR, callback, pipe write or waiting for queue capacity on this path.
        with self.lock:
            if self.state not in ('running', 'failed'):
                return False
            try:
                pts = float(pts)
                if not math.isfinite(pts) or pts < 0:
                    raise ValueError('录像时间必须为非负有限秒数。')
                if self.last_pts is not None and pts <= self.last_pts:
                    self._record_gap(max(0, pts), self.last_pts, 'non_monotonic_pts')
                    return False
                self.last_pts = pts
                if self.state == 'failed':
                    self._record_gap(self.processed_pts or 0., pts, 'worker_failed')
                    return False
                if pts-self.last_enqueued < 1/SAMPLE_FPS-1e-5:
                    return False
                crop, metadata = prepare_sample(frame, meta)
            except (TypeError, ValueError, OverflowError) as exc:
                t = self.last_pts or 0.
                self._record_gap(t, t, 'unsupported_frame')
                self._notify('status', {'state': 'degraded', 'error': str(exc)})
                return False
            if not self.process.is_alive():
                self._record_gap(self.processed_pts or 0., pts, 'worker_failed')
                return False
            try:
                self.samples.put_nowait(('sample', crop, pts, metadata))
            except queue.Full:
                self._record_gap(pts, pts, 'queue_full')
                return False
            self.last_enqueued = pts
            return True

    def _consume(self, kind, payload):
        if kind.startswith('notify:'):
            callback = self.callbacks.get(kind[7:])
            if callback:
                try:
                    callback(deepcopy(payload))
                except Exception:
                    pass
            return
        with self.lock:
            if kind == 'marker':
                self.markers[payload['id']] = payload
            elif kind == 'gap':
                self._record_gap(**payload)
            elif kind == 'status':
                self.processed_pts = payload.get('processed_pts', self.processed_pts)
                self.stats = payload.get('stats', self.stats)
                if payload.get('state') == 'failed':
                    self.state = 'failed'
            elif kind == 'complete':
                for marker in payload['markers']:
                    self.markers[marker['id']] = marker
                self.stats = payload['stats']
                for gap in payload.get('gaps', []):
                    if gap not in self.gaps:
                        self._record_gap(**gap)
                self.processed_pts = payload.get('processed_pts', self.processed_pts)
                self.state = payload['state']
                self._complete.set()
                return
        callback = self.callbacks.get(kind)
        if callback:
            try:
                callback(deepcopy(payload))
            except Exception:
                pass  # Callback failure cannot bring down capture or OCR.

    def _listen(self):
        while not self._complete.is_set():
            try:
                kind, payload = self._notifications.get_nowait()
            except queue.Empty:
                try:
                    kind, payload = self.results.get(timeout=.1)
                except (EOFError, OSError, ValueError):
                    return
                except queue.Empty:
                    if not self.process.is_alive():
                        with self.lock:
                            if self.state != 'stopped':
                                self.state = 'failed'
                        return
                    continue
            self._consume(kind, payload)

    def stop(self, timeout=10):
        with self.lock:
            if self.state == 'idle':
                self.state = 'stopped'
                return self.snapshot()
            if self.state == 'stopped':
                return self.snapshot()
            self.state = 'draining'
            self.stopping.set()
        self._complete.wait(max(0, timeout))
        with self.lock:
            if not self._complete.is_set():
                self._record_gap(self.processed_pts or 0., self.last_pts or 0., 'drain_timeout')
                self.state = 'needs_rescan'
                if self.process.is_alive():
                    self.process.terminate()
                self._complete.set()
        self.process.join(timeout=1)
        self.samples.cancel_join_thread()
        self.samples.close()
        self.results.close()
        return self.snapshot()

    def snapshot(self):
        with self.lock:
            return deepcopy({'state': self.state, 'markers': list(self.markers.values()),
                             'gaps': self.gaps, 'stats': self.stats,
                             'processed_pts': self.processed_pts})


def rescan_file(path, intervals, *, on_marker=None, ctx=None):
    """Offline job entry: recheck only requested ranges, using source frame PTS.

    Uses OpenCV's decoded-frame timestamp, not OCR elapsed time. Run outside the
    capture callback. This does not discover unreported gate false negatives.
    """
    ctx = ctx or Context()
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise ValueError('无法打开待补扫录像。')
    markers, gaps, stats = [], [], []
    try:
        ranges = []
        for interval in intervals:
            start = float(interval['start'] if isinstance(interval, dict) else interval[0])
            end = float(interval['end'] if isinstance(interval, dict) else interval[1])
            if not math.isfinite(start+end) or start < 0 or end < start:
                raise ValueError('补扫范围无效。')
            ranges.append((max(0., start-1), end+1))
        merged = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        total = sum(end-start for start, end in merged)
        completed = 0.
        for start, end in merged:
            capture.set(cv2.CAP_PROP_POS_MSEC, start*1000)
            def emit(kind, value):
                if kind == 'marker' and on_marker:
                    on_marker(dict(value, source='rescan'))
                if kind == 'gap':
                    gaps.append(value)
            core, next_pts = RealtimeCore(emit), start
            updated = time.monotonic()
            decoded_pts, repeated_pts = None, 0
            while True:
                ctx.check()
                ok, frame = capture.read()
                if not ok:
                    break
                pts = capture.get(cv2.CAP_PROP_POS_MSEC)/1000
                if not math.isfinite(pts) or pts < 0:
                    raise ValueError('录像帧时间无效，不能可靠生成标记。')
                repeated_pts = repeated_pts+1 if decoded_pts is not None and pts <= decoded_pts else 0
                if repeated_pts >= 3:
                    raise ValueError('无法读取递增的录像帧时间，补查已停止，原有标记不变。')
                decoded_pts = pts
                if pts > end:
                    break
                if pts < next_pts-1e-4:
                    continue
                crop, meta = prepare_sample(frame)
                core.process(crop, pts, meta)
                next_pts = pts+1/SAMPLE_FPS
                if time.monotonic()-updated > 1:
                    ctx.update('补查三角洲提示区', 100*min(total, completed+pts-start)/max(total, .001),
                               f'已分析到 {pts:.1f} 秒')
                    updated = time.monotonic()
            result = core.finish()
            markers.extend(dict(marker, source='rescan') for marker in result['markers'])
            stats.append(result['stats'])
            completed += end-start
    finally:
        capture.release()
    return {'markers': markers, 'events': markers, 'gaps': gaps, 'stats': stats, 'state': 'complete'}


def scan_recording(video, ctx=None, intervals=None):
    """Public offline job API; no screen capture, only the finalized recording."""
    ctx = ctx or Context()
    meta = probe(video, ctx)
    roi_for_size(meta['width'], meta['height'])
    if meta.get('color_transfer') in ('smpte2084', 'arib-std-b67'):
        raise ValueError('实时识别首版仅支持 SDR 录像。')
    selected = intervals if intervals is not None else [[0., meta['duration']]]
    ctx.update('补查三角洲提示区', 0)
    result = rescan_file(video, selected, ctx=ctx)
    result.update(duration=meta['duration'], profile=PROFILE)
    ctx.update('提示区补查完成', 100, f"找到 {len(result['markers'])} 处提示")
    return result

"""Local, template-free discovery of Delta Force personal event notifications.

The packaged Chinese OCR models detect text on the whole image. They are not a
game-trained object detector. A short notification line, its white/gold icon,
and temporal persistence are combined before a candidate is exported. No screen
coordinates or user calibration are used to find notifications.
"""
from __future__ import annotations

import importlib.util
import math
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .media import Context, LoggedProcess, ffmpeg_args, probe

SCAN_FPS = 4
DISCOVERY_STRIDE = 2
MAX_REGIONS = 8
REGION_LIFETIME = 30.
_ENGINE_LOCK = threading.Lock()
_ENGINE = None
_ENGINE_ERROR = None
_PREFIX = re.compile(r'^(?:你已)?((?:击中头部|爆头)?(?:击倒|击败|击杀))(.*)$')
_MENU_WORDS = ('次数', '任务', '累计', '完成', '奖励', '助攻', '队友', '敌人后', '使用', '目标', '统计', '记录', '挑战')


def _objective_text(text):
    return any(word in text for word in _MENU_WORDS) or bool(re.search(r'\d+[名个次]', text))


def status():
    """Cheap dependency/model check; does not load ONNX or fetch anything."""
    reason = _ENGINE_ERROR
    try:
        spec = importlib.util.find_spec('rapidocr_onnxruntime')
        if spec is None or importlib.util.find_spec('onnxruntime') is None:
            reason = '缺少本地中文识别组件，请运行 scripts/setup.ps1 更新依赖。'
        elif not spec.submodule_search_locations:
            reason = '无法定位本地中文识别模型。'
        else:
            models = Path(next(iter(spec.submodule_search_locations))) / 'models'
            if not all(any(models.glob(f'*{kind}*.onnx')) for kind in ('det', 'rec')):
                reason = '本地中文识别模型缺失，请重新安装识别组件。'
    except (ImportError, OSError, ValueError) as exc:
        reason = f'无法检查本地中文识别组件：{exc}'
    return {'available': reason is None, 'reason': reason, 'backend': 'rapidocr-onnxruntime',
            'scan_fps': SCAN_FPS, 'discovery_fps': SCAN_FPS / DISCOVERY_STRIDE,
            'mode': 'delta_auto', 'name': '三角洲自动识别'}


def _engine(ctx):
    global _ENGINE, _ENGINE_ERROR
    # Checking while waiting keeps concurrent task cancellation responsive.
    while not _ENGINE_LOCK.acquire(timeout=.2):
        ctx.check()
    try:
        ctx.check()
        if _ENGINE is None:
            available = status()
            if not available['available']:
                raise RuntimeError(available['reason'])
            try:
                from rapidocr_onnxruntime import RapidOCR
                _ENGINE = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1,
                                   det_limit_type='max', det_box_thresh=.3,
                                   det_thresh=.2, text_score=.4)
            except Exception as exc:
                _ENGINE_ERROR = f'本地中文识别组件加载失败：{exc}'
                raise RuntimeError(_ENGINE_ERROR) from exc
        return _ENGINE
    finally:
        _ENGINE_LOCK.release()


def _plain(text):
    return re.sub(r'[^\w\u4e00-\u9fff]', '', str(text)).replace('_', '')


def _box(points):
    values = np.asarray(points, dtype=float)
    if values.shape != (4, 2) or not np.isfinite(values).all():
        raise ValueError('Invalid OCR box')
    return (int(values[:, 0].min()), int(values[:, 1].min()),
            int(math.ceil(values[:, 0].max())), int(math.ceil(values[:, 1].max())))


def _same_region(a, b):
    ax, ay, ar, ab = a
    bx, by, br, bb = b
    height = max(ab-ay, bb-by, 1)
    return (abs((ay+ab-by-bb)/2) <= height * .8 and
            min(ar, br)-max(ax, bx) >= min(ar-ax, br-bx) * .35)


def _icon_score(frame, box):
    """Check a compact white/gold glyph above text, relative to its own size."""
    x0, y0, x1, y1 = box
    height = max(1, y1-y0)
    left, right = max(0, x0-height), min(frame.shape[1], x1+height)
    top, bottom = max(0, y0-round(3.3*height)), max(0, y0-round(.15*height))
    if right <= left or bottom <= top:
        return 0.
    crop = frame[top:bottom, left:right]
    low, high = crop.min(axis=2), crop.max(axis=2)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    gold = ((hsv[:, :, 0] >= 12) & (hsv[:, :, 0] <= 38) &
            (hsv[:, :, 1] >= 90) & (hsv[:, :, 2] >= 170))
    white = (((low >= 155) & ((high.astype(np.int16)-low) <= 75)) | gold).astype(np.uint8)
    # One-pixel compression gaps should not split a notification glyph.
    white = cv2.morphologyEx(white, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
    count, _, stats, centers = cv2.connectedComponentsWithStats(white, 8)
    best = 0.
    for idx in range(1, count):
        _, _, width, ch, area = stats[idx]
        cx, cy = centers[idx]
        if not (.65*height <= width <= 4*height and .28*height <= ch <= 2*height):
            continue
        if not (.09*height*height <= area <= 4*height*height and .8 <= width/max(ch, 1) <= 5):
            continue
        if not x0-.5*height <= left+cx <= x1+.5*height:
            continue
        # Text from another line tends to consist of many small separate glyphs.
        density = area / max(1, width*ch)
        if .15 <= density <= .94:
            best = max(best, min(1., .55 + area / (3*height*height)))
    return best


@dataclass
class Cue:
    text: str
    event_type: str
    confidence: float
    box: tuple
    icon_score: float
    target: str
    ocr_confidence: float | None = None


def candidates_from_ocr(frame, rows, offset=(0, 0), *, source_size=None):
    """Accept short personal prompts, not keyword mentions or feed sentences."""
    parsed = []
    for row in rows or []:
        try:
            box, text, score = _box(row[0]), str(row[1]).strip(), float(row[2])
            if not math.isfinite(score) or score < .4:
                continue
            box = (box[0]+offset[0], box[1]+offset[1], box[2]+offset[0], box[3]+offset[1])
            parsed.append((box, text, score))
        except (ValueError, TypeError, IndexError):
            continue
    candidates = []
    for box, text, score in parsed:
        plain = _plain(text)
        match = _PREFIX.match(plain)
        if match is None or len(plain) > 24 or _objective_text(plain):
            continue
        x0, y0, x1, y1 = box
        height = y1-y0
        # A caller may supply only a HUD crop. Text size is relative to the
        # source image, while icon coordinates remain relative to ``frame``.
        source_height = source_size[1] if source_size is not None else frame.shape[0]
        if height < 6 or height > source_height * .08 or x1 <= x0:
            continue
        # The OCR detector may split the verb and the colored target name.
        for other_box, other_text, other_score in sorted(parsed, key=lambda row: row[0][0]):
            ox0, oy0, ox1, oy1 = other_box
            if (other_box != box and 0 <= ox0-x1 <= height*1.5 and
                    abs((oy0+oy1-y0-y1)/2) <= height*.4 and
                    .5*height <= oy1-oy0 <= 1.7*height):
                text += ' ' + other_text
                x1 = ox1
                score = min(score, other_score)
        plain = _plain(text)
        if len(plain) > 24 or _objective_text(plain):
            continue
        box = (x0, y0, x1, y1)
        icon = _icon_score(frame, box)
        if icon < .55:
            continue
        # A very wide row is more likely a notification/feed sentence.
        if (x1-x0) / max(1, height) > 22:
            continue
        match = _PREFIX.match(plain)
        verb, target = match.groups()
        event_type = 'knockdown' if verb.endswith('击倒') else 'kill'
        if verb.startswith(('爆头', '击中头部')) and event_type == 'kill':
            event_type = 'headshot'
        # Colored names often fade sooner than the verb. Do not interpret an
        # uncertain suffix as a new opponent; retain the raw text as evidence.
        cue = Cue(text, event_type, min(1., score*.85+icon*.15), box, icon,
                  target if score >= .72 else '', score)
        if not any(_same_region(cue.box, old.box) for old in candidates):
            candidates.append(cue)
    return candidates


def _same_target(a, b):
    if a.event_type != b.event_type:
        return False
    # Missing suffixes do not establish that a different opponent appeared.
    return not a.target or not b.target or a.target == b.target


class EventTracker:
    """Bounded active state; no per-frame score arrays for long recordings."""
    def __init__(self):
        self.active = []
        self.events = []
        self.rejected_static = 0

    @staticmethod
    def _new(cue, t):
        return {'cue': cue, 'time': t, 'end': t, 'samples': 1, 'best': cue.confidence,
                'evidence': [(t, cue)], 'pending': None}

    def _finish(self, track):
        if track['end']-track['time'] > 8.:
            self.rejected_static += 1
            return
        cue = track['cue']
        if track['samples'] < 2 and (track['best'] < .88 or cue.icon_score < .65):
            return
        confidence = float(track['best'])
        evidence = [{'id': 'auto-ocr', 'name': item.text, 'role': 'trigger',
                     'text': item.text, 'time': round(t, 3), 'box': list(item.box),
                     'confidence': round(item.confidence, 4), 'icon_score': round(item.icon_score, 4),
                     'ocr_confidence': round(item.ocr_confidence if item.ocr_confidence is not None else item.confidence, 4),
                     'samples': track['samples']} for t, item in track['evidence']]
        self.events.append({'time': track['time'], 'end': track['end'], 'confidence': confidence,
                            'samples': track['samples'], 'event_types': [cue.event_type],
                            'matches': [{'id': 'auto-ocr', 'name': cue.text, 'confidence': confidence}],
                            'evidence': evidence, 'classification': 'unknown', 'included': True,
                            'reason': '自动发现短提示文字及邻近图标；玩家与小兵未区分，保留待确认'})

    def _retire(self, track):
        self._finish(track)
        if track.get('pending'):
            self._finish(track['pending'])

    def observe(self, t, cues):
        expired = [track for track in self.active if t-track['end'] > .75]
        for track in expired:
            self._retire(track)
            self.active.remove(track)
        used = set()
        for cue in cues[:MAX_REGIONS]:
            track = next((item for item in self.active if id(item) not in used and
                          _same_region(item['cue'].box, cue.box)), None)
            if track is None:
                track = self._new(cue, t)
                self.active.append(track)
                used.add(id(track))
                continue
            used.add(id(track))
            if not _same_target(track['cue'], cue):
                pending = track['pending']
                if pending is not None and t-pending['end'] <= .5 and _same_target(pending['cue'], cue):
                    self._finish(track)
                    self.active.remove(track)
                    pending.update(end=t, samples=pending['samples']+1,
                                   best=max(pending['best'], cue.confidence))
                    pending['evidence'].append((t, cue))
                    self.active.append(pending)
                else:
                    track['pending'] = self._new(cue, t)
                continue
            track.update(end=t, samples=track['samples']+1, best=max(track['best'], cue.confidence), pending=None)
            if cue.confidence > track['cue'].confidence:
                track['cue'] = cue
            if len(track['evidence']) < 4 and cue.text != track['evidence'][-1][1].text:
                track['evidence'].append((t, cue))
        # An adversarial/menu frame cannot grow active state without bound.
        while len(self.active) > MAX_REGIONS:
            self._retire(self.active.pop(0))

    def finish(self):
        for track in self.active:
            self._retire(track)
        self.active.clear()
        return sorted(self.events, key=lambda event: event['time'])


def _read_ocr(engine, image, ctx):
    ctx.check()
    height, width = image.shape[:2]
    if width*height <= 500_000:
        # ORT execution itself cannot be interrupted, so keep inference small.
        rows, _ = engine(image, use_cls=False)
        ctx.check()
        return rows
    # Detect text over the entire image first, then recognize original-resolution
    # crops near a notification glyph. Reading every HUD counter wastes most of
    # the OCR time and harms small translucent notification text.
    scale = min(1., 960/max(width, height))
    small = cv2.resize(image, (round(width*scale), round(height*scale)))
    boxes, _ = engine(small, use_cls=False, use_rec=False)
    ctx.check()
    regions, rows = [], []
    for points in boxes or []:
        box = tuple(round(value/scale) for value in _box(points))
        x0, y0, x1, y1 = box
        h = y1-y0
        if h < 6 or h > height*.08 or x1-x0 > 24*h:
            continue
        # Detection may return just the two-character white verb and omit the
        # colored name. Include room to its right when looking for the icon.
        if _icon_score(image, (x0, y0, min(width, x1+4*h), y1)) < .55:
            continue
        if any(_same_region(box, old) for old in regions):
            continue
        regions.append(box)
        cx0, cy0, cx1, cy1 = _region(box, width, height)
        found, _ = engine(image[cy0:cy1, cx0:cx1], use_cls=False)
        ctx.check()
        for points, text, score, *_ in found or []:
            shifted = [[x+cx0, y+cy0] for x, y in points]
            rows.append([shifted, text, score])
        if len(regions) >= 32:
            break
    return rows


def _region(box, width, height):
    x0, y0, x1, y1 = box
    h = max(8, y1-y0)
    return (max(0, x0-2*h), max(0, y0-4*h), min(width, x1+5*h), min(height, y1+2*h))


def detect_auto_events(video, ctx=None, *, start=0., duration=None, progress=(5, 47)):
    """Scan all frames as a stream; discover globally and track learned locations."""
    ctx = ctx or Context()
    meta = probe(video, ctx)
    if not math.isfinite(start) or start < 0 or start >= meta['duration']:
        raise ValueError('识别起点必须位于录像时长内。')
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        raise ValueError('识别时长必须大于 0。')
    if not meta['width'] or not meta['height']:
        raise ValueError('所选文件没有视频画面。')
    scan_duration = min(duration, meta['duration']-start) if duration is not None else meta['duration']-start
    ctx.update('自动识别击倒 / 击败', progress[0], '加载本地中文模型，无需框选提示区域')
    engine = _engine(ctx)
    # Preserve 1080p notification glyphs. Larger recordings are uniformly scaled.
    scale = min(1., 1920 / max(meta['width'], meta['height']))
    width, height = max(2, round(meta['width']*scale)), max(2, round(meta['height']*scale))
    args = ffmpeg_args()
    if start:
        args += ['-ss', str(start)]
    args += ['-i', video, '-t', str(scan_duration), '-frames:v', str(math.ceil(scan_duration*SCAN_FPS)),
             '-map', '0:v:0', '-an', '-sn', '-vf', f'fps={SCAN_FPS},scale={width}:{height}',
             '-pix_fmt', 'bgr24', '-f', 'rawvideo', '-']
    tracker, regions = EventTracker(), []
    frame_count, global_count, local_count = 0, 0, 0
    last_update = time.monotonic()
    with LoggedProcess(args, ctx) as child:
        frame_size = width*height*3
        while True:
            ctx.check()
            raw = child.process.stdout.read(frame_size)
            ctx.check()
            if not raw:
                break
            if len(raw) != frame_size:
                raise RuntimeError('检测视频帧不完整。')
            frame = np.frombuffer(raw, np.uint8).reshape(height, width, 3)
            t = start+frame_count/SCAN_FPS
            regions = [(box, seen) for box, seen in regions if t-seen < REGION_LIFETIME]
            cues = []
            # Full discovery keeps running even after the first notification.
            if regions:
                for box, _ in regions:
                    x0, y0, x1, y1 = _region(box, width, height)
                    rows = _read_ocr(engine, frame[y0:y1, x0:x1], ctx)
                    local_count += 1
                    for cue in candidates_from_ocr(frame, rows, (x0, y0)):
                        if not any(_same_region(cue.box, old.box) for old in cues):
                            cues.append(cue)
            if not regions or frame_count % DISCOVERY_STRIDE == 0:
                discovered = candidates_from_ocr(frame, _read_ocr(engine, frame, ctx))
                global_count += 1
                for cue in discovered:
                    existing = next((old for old in cues if _same_region(cue.box, old.box)), None)
                    if existing is None:
                        cues.append(cue)
                    elif cue.confidence > existing.confidence:
                        cues.remove(existing)
                        cues.append(cue)
            for cue in cues:
                regions = [(box, seen) for box, seen in regions if not _same_region(box, cue.box)]
                regions.append((cue.box, t))
            regions = regions[-MAX_REGIONS:]
            tracker.observe(t, cues)
            frame_count += 1
            now = time.monotonic()
            if now-last_update >= 2:
                done = min(scan_duration, frame_count/SCAN_FPS)
                ctx.update('自动识别击倒 / 击败', progress[0]+(progress[1]-progress[0])*done/scan_duration,
                           f'已扫描 {done:.1f} / {scan_duration:.1f} 秒 · 已确认 {len(tracker.events)} 个提示')
                last_update = now
        child.process.wait(timeout=15)
        child.check_result()
    events = tracker.finish()
    ctx.record('detection_complete', mode='delta_auto', backend='rapidocr-onnxruntime',
               frames=frame_count, candidate_count=len(events), start=start, duration=scan_duration,
               scan_fps=SCAN_FPS, discovery_fps=SCAN_FPS/DISCOVERY_STRIDE,
               global_ocr_count=global_count, local_ocr_count=local_count,
               rejected_static_count=tracker.rejected_static, evidence_size=[width, height])
    ctx.update('自动识别击倒 / 击败', progress[1], f'扫描完成，找到 {len(events)} 个候选事件')
    return events

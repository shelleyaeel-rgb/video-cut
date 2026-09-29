from __future__ import annotations

import json
import uuid
from pathlib import Path

import cv2
import numpy as np

from . import logs
from .media import ROOT, Context, LoggedProcess, ffmpeg_args, preview_frame, probe

PROFILES = ROOT / 'data' / 'profiles'


def save_profile(video, time, rect, name, threshold=.82):
    image = cv2.imdecode(np.frombuffer(preview_frame(video, time), np.uint8), cv2.IMREAD_GRAYSCALE)
    h, w = image.shape
    x, y, rw, rh = [float(n) for n in rect]
    if not all(np.isfinite([x, y, rw, rh])) or min(x, y) < 0 or min(rw, rh) <= 0 or x + rw > 1.001 or y + rh > 1.001:
        raise ValueError('框选区域必须位于画面内。')
    x0, y0, x1, y1 = int(x*w), int(y*h), min(w, int((x+rw)*w)), min(h, int((y+rh)*h))
    if x1-x0 < 8 or y1-y0 < 8:
        raise ValueError('区域太小，请框选完整且稳定的提示图案。')
    template = image[y0:y1, x0:x1]
    if float(template.std()) < 8:
        raise ValueError('这个区域几乎是纯色，请框选带轮廓或文字的提示图案。')
    ident = uuid.uuid4().hex
    directory = PROFILES / ident
    directory.mkdir(parents=True)
    cv2.imencode('.png', template)[1].tofile(str(directory / 'template.png'))
    margin = 12
    rx, ry = max(0, x0-margin), max(0, y0-margin)
    right, bottom = min(w, x1+margin), min(h, y1+margin)
    config = {'id': ident, 'name': str(name).strip()[:80] or '我的提示模板', 'reference_size': [w, h], 'roi': [rx, ry, right-rx, bottom-ry], 'threshold': float(threshold), 'scan_fps': 8, 'rearm_seconds': .35, 'min_visible_seconds': .12, 'kind': 'template', 'source_note': '用户标定；需在真实录像上验证'}
    (directory / 'profile.json').write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    return config


def get_profile(ident):
    if not isinstance(ident, str) or not ident or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789_-' for c in ident):
        raise ValueError('检测配置无效。')
    path = PROFILES / ident / 'profile.json'
    if not path.is_file():
        raise ValueError('找不到检测配置。请先标定提示图案。')
    return json.loads(path.read_text(encoding='utf-8'))


def list_profiles():
    result = []
    for path in PROFILES.glob('*/profile.json'):
        try:
            item = json.loads(path.read_text(encoding='utf-8'))
            result.append({k: item[k] for k in ('id', 'name', 'kind')})
        except (ValueError, KeyError) as exc:
            logs.failure('runtime', 'profile_skipped', exc, path=str(path))
            continue
    return result


def scores_to_events(scores, fps, threshold=.82, rearm=.35, min_visible=.12):
    events, active = [], None
    last_seen = -100.
    for i, score in enumerate(scores):
        t = i / fps
        if score >= threshold:
            if active is None:
                active = {'time': t, 'end': t, 'confidence': float(score), 'samples': 0}
            active['end'] = t
            active['confidence'] = max(active['confidence'], float(score))
            active['samples'] += 1
            last_seen = t
        elif active is not None and t-last_seen >= rearm:
            if active['samples']/fps >= min_visible:
                events.append(active)
            active = None
    if active is not None and active['samples']/fps >= min_visible:
        events.append(active)
    return events


def detect_events(video, profile, ctx=None, threshold=None):
    ctx = ctx or Context()
    meta = probe(video, ctx)
    w, h = profile['reference_size']
    if abs(meta['width']/max(1, meta['height']) - w/h) > .03:
        raise ValueError('录像宽高比与检测配置不同，请使用该录像重新标定。')
    template = cv2.imdecode(np.fromfile(PROFILES / profile['id'] / 'template.png', dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if template is None:
        raise ValueError('模板图片损坏，请重新标定。')
    x, y, cw, ch = profile['roi']
    if min(cw, ch) <= 0 or template.shape[0] > ch or template.shape[1] > cw or x < 0 or y < 0 or x+cw > w or y+ch > h:
        raise ValueError('检测区域与模板不匹配。')
    fps = profile.get('scan_fps', 8)
    filt = f'fps={fps},scale={w}:{h},format=gray,crop={cw}:{ch}:{x}:{y}'
    scores = []
    with LoggedProcess(ffmpeg_args() + ['-i', video, '-map', '0:v:0', '-an', '-sn', '-vf', filt, '-pix_fmt', 'gray', '-f', 'rawvideo', '-'], ctx) as child:
        frame_size = cw*ch
        while True:
            ctx.check()
            raw = child.process.stdout.read(frame_size)
            ctx.check()
            if not raw:
                break
            if len(raw) != frame_size:
                raise RuntimeError('检测视频帧不完整。')
            frame = np.frombuffer(raw, np.uint8).reshape(ch, cw)
            score = float(cv2.minMaxLoc(cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED))[1])
            scores.append(score)
            if len(scores) % (fps * 3) == 0:
                t = len(scores)/fps
                ctx.update('识别候选事件', 5 + 42 * min(1, t/meta['duration']), f'已扫描 {t:.0f} / {meta["duration"]:.0f} 秒')
        child.process.wait(timeout=15)
        child.check_result()
    events = scores_to_events(scores, fps, threshold if threshold is not None else profile['threshold'], profile.get('rearm_seconds', .35), profile.get('min_visible_seconds', .12))
    ctx.record('detection_complete', frames=len(scores), candidate_count=len(events), profile=profile['id'])
    return events

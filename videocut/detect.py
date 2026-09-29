from __future__ import annotations

import json
import math
import threading
import uuid
from pathlib import Path

import cv2
import numpy as np

from . import logs
from .media import ROOT, Context, LoggedProcess, ffmpeg_args, preview_frame, probe

PROFILES = ROOT / 'data' / 'profiles'
PROFILE_LOCK = threading.RLock()
EVENT_TYPES = {'knockdown', 'kill', 'headshot', 'other'}
ROLES = {'trigger', 'player', 'npc'}


def prepare_template(video, time, rect, mask_mode='edges', cutoff=180, erase=()):
    if not math.isfinite(float(time)) or float(time) < 0:
        raise ValueError('标定时间必须是非负数。')
    image = cv2.imdecode(np.frombuffer(preview_frame(video, time), np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError('无法读取标定画面。')
    h, w = image.shape
    if not isinstance(rect, (list, tuple)) or len(rect) != 4:
        raise ValueError('请框选提示图案。')
    x, y, rw, rh = [float(n) for n in rect]
    if not all(np.isfinite([x, y, rw, rh])) or min(x, y) < 0 or min(rw, rh) <= 0 or x + rw > 1.001 or y + rh > 1.001:
        raise ValueError('框选区域必须位于画面内。')
    x0, y0, x1, y1 = int(x*w), int(y*h), min(w, int((x+rw)*w)), min(h, int((y+rh)*h))
    if x1-x0 < 8 or y1-y0 < 8:
        raise ValueError('区域太小，请框选完整且稳定的提示图案。')
    template = image[y0:y1, x0:x1]
    if float(template.std()) < 8:
        raise ValueError('这个区域几乎是纯色，请框选带轮廓或文字的提示图案。')
    if mask_mode not in {'edges', 'light', 'dark', 'full'}:
        raise ValueError('蒙版方式无效。')
    cutoff = float(cutoff)
    if not math.isfinite(cutoff) or not 0 <= cutoff <= 255:
        raise ValueError('前景亮度必须在 0 到 255 之间。')
    edges = cv2.Canny(template, 50, 140)
    foreground = edges if mask_mode == 'edges' else (template >= cutoff if mask_mode == 'light' else template <= cutoff).astype(np.uint8)*255
    mask = np.full_like(template, 255) if mask_mode == 'full' else cv2.dilate(foreground, np.ones((5, 5), np.uint8))
    if not isinstance(erase, (list, tuple)) or len(erase) > 64:
        raise ValueError('蒙版擦除区域过多，请重置后重新框选。')
    for area in erase:
        if not isinstance(area, (list, tuple)) or len(area) != 4:
            raise ValueError('蒙版擦除区域无效。')
        ex, ey, ew, eh = map(float, area)
        if not all(np.isfinite([ex, ey, ew, eh])) or min(ex, ey) < 0 or min(ew, eh) <= 0 or ex+ew > 1.001 or ey+eh > 1.001:
            raise ValueError('蒙版擦除区域必须位于图标内。')
        th, tw = template.shape
        mask[int(ey*th):math.ceil((ey+eh)*th), int(ex*tw):math.ceil((ex+ew)*tw)] = 0
    if np.count_nonzero(mask) < 16 or np.count_nonzero(edges[mask > 0]) < 8 or float(template[mask > 0].std()) < 4:
        raise ValueError('保留的图案太少或缺少轮廓，请调整蒙版或重新框选。')
    margin = 12
    rx, ry = max(0, x0-margin), max(0, y0-margin)
    right, bottom = min(w, x1+margin), min(h, y1+margin)
    return template, mask, [w, h], [rx, ry, right-rx, bottom-ry]


def profile_templates(profile):
    """Legacy files remain readable without rewriting the user's configuration."""
    if 'templates' in profile:
        return profile['templates']
    return [{'id': 'legacy', 'name': profile['name'], 'role': 'trigger', 'event_type': 'other',
             'roi': profile['roi'], 'image': 'template.png', 'mask': None, 'match_mode': 'gray',
             'threshold': None, 'enabled': True}]


def _threshold(value):
    if value is None or value == '':
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError('模板阈值必须为 0.5–0.99，或留空跟随默认值。')
    if not math.isfinite(value) or not .5 <= value <= .99:
        raise ValueError('模板阈值必须为 0.5–0.99。')
    return value


def _write_profile(config):
    config['revision'] = uuid.uuid4().hex
    directory = PROFILES / config['id']
    temporary = directory / 'profile.tmp'
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(directory / 'profile.json')


def save_profile(video, time, rect, name, threshold=.82, *, profile_id=None, template_name='',
                 role='trigger', event_type='kill', mask_mode='edges', cutoff=180, erase=(), sample_threshold=None):
    if role not in ROLES or event_type not in EVENT_TYPES | {'all'} or role == 'trigger' and event_type == 'all':
        raise ValueError('模板用途或事件类型无效。')
    sample_threshold = _threshold(sample_threshold)
    threshold = _threshold(threshold) or .82
    template, mask, size, roi = prepare_template(video, time, rect, mask_mode, cutoff, erase)
    with PROFILE_LOCK:
        if profile_id:
            config = get_profile(profile_id)
            if config.get('kind') == 'demo':
                raise ValueError('请新建游戏配置，合成演示配置不支持修改。')
            if config['reference_size'] != size:
                raise ValueError('画面比例与已有配置不同，请新建配置。')
            config['templates'] = profile_templates(config)
        else:
            if role != 'trigger':
                raise ValueError('请先创建事件图标模板，再添加辅助标记。')
            config = {'id': uuid.uuid4().hex, 'name': str(name).strip()[:80] or '我的提示配置',
                      'kind': 'multi_template', 'reference_size': size, 'templates': [], 'threshold': threshold,
                      'scan_fps': 8, 'rearm_seconds': .35, 'min_visible_seconds': .12,
                      'source_note': '用户标定；辅助标记必须属于本人的同次事件，需实测验证'}
        if len(config['templates']) >= 32:
            raise ValueError('一个配置最多保存 32 个模板，请新建配置。')
        ident = uuid.uuid4().hex
        directory = PROFILES / config['id']
        directory.mkdir(parents=True, exist_ok=True)
        cv2.imencode('.png', template)[1].tofile(str(directory / f'{ident}.png'))
        cv2.imencode('.png', mask)[1].tofile(str(directory / f'{ident}-mask.png'))
        config.update(version=2, kind='multi_template')
        config['templates'].append({'id': ident, 'name': str(template_name).strip()[:80] or str(name).strip()[:80] or '提示样本',
                                    'role': role, 'event_type': event_type, 'roi': roi, 'image': f'{ident}.png',
                                    'mask': f'{ident}-mask.png', 'match_mode': 'gray' if mask_mode == 'full' else 'edges',
                                    'mask_mode': mask_mode, 'threshold': sample_threshold, 'enabled': True})
        _write_profile(config)
        return config


def update_template(ident, template_id, enabled, threshold):
    if not isinstance(enabled, bool):
        raise ValueError('启用状态无效。')
    threshold = _threshold(threshold)
    with PROFILE_LOCK:
        config = get_profile(ident)
        if config.get('kind') == 'demo':
            raise ValueError('合成演示配置不支持修改。')
        config['templates'] = profile_templates(config)
        item = next((t for t in config['templates'] if t['id'] == template_id), None)
        if item is None:
            raise ValueError('找不到模板。')
        item.update(enabled=enabled, threshold=threshold)
        if not any(t['role'] == 'trigger' and t.get('enabled', True) for t in config['templates']):
            raise ValueError('请至少启用一个事件图标模板。')
        config.update(version=2, kind='multi_template')
        _write_profile(config)
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
            result.append({**{k: item[k] for k in ('id', 'name', 'kind')}, 'templates': profile_templates(item)})
        except (ValueError, KeyError) as exc:
            logs.failure('runtime', 'profile_skipped', exc, path=str(path))
            continue
    return result


def mask_preview(template, mask):
    image = cv2.cvtColor(template, cv2.COLOR_GRAY2BGRA)
    image[:, :, 3] = mask
    return cv2.imencode('.png', image)[1].tobytes()


def saved_template_preview(ident, template_id):
    config = get_profile(ident)
    item = next((t for t in profile_templates(config) if t['id'] == template_id), None)
    if item is None:
        raise ValueError('找不到模板。')
    images = []
    for filename in (item['image'], item.get('mask')):
        if filename is None:
            images.append(None)
            continue
        if Path(filename).name != filename:
            raise ValueError('模板文件名无效。')
        path = PROFILES / ident / filename
        if not path.is_file():
            raise ValueError('模板图片缺失。')
        image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ValueError('模板图片损坏。')
        images.append(image)
    template, mask = images
    if mask is not None and mask.shape != template.shape:
        raise ValueError('模板蒙版尺寸无效。')
    return mask_preview(template, mask if mask is not None else np.full_like(template, 255))


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


def edge_features(image):
    # Soften one-pixel edge shifts caused by compression and translucent HUDs.
    return cv2.GaussianBlur(cv2.Canny(image, 50, 140), (3, 3), .8)


def template_score(frame, template, mask=None, mode='gray'):
    if mode == 'edges':
        frame = edge_features(frame)
        method = cv2.TM_CCORR_NORMED
    else:
        method = cv2.TM_CCOEFF_NORMED
    values = cv2.matchTemplate(frame, template, method, mask=mask)
    # Masked normalized matching can produce NaN/Inf on empty regions.
    values = np.where(np.isfinite(values), values, -1.)
    return float(np.clip(values.max(), -1., 1.))


def _same_region(a, b):
    x, y, w, h = a
    bx, by, bw, bh = b
    overlap = max(0, min(x+w, bx+bw)-max(x, bx))*max(0, min(y+h, by+bh)-max(y, by))
    return overlap >= .5*min(w*h, bw*bh)


def _sustained(indices, count=2):
    run, previous = 0, -2
    for i in sorted(indices):
        run = run+1 if i == previous+1 else 1
        if run >= count:
            return True
        previous = i
    return False


def combine_detections(templates, score_tracks, fps, threshold, rearm=.35, min_visible=.12):
    """Deduplicate overlapping prompt samples; classify only with concurrent evidence."""
    candidates = []
    for item, scores in zip(templates, score_tracks):
        if item['role'] != 'trigger':
            continue
        cutoff = item.get('threshold') or threshold
        for event in scores_to_events(scores, fps, cutoff, rearm, min_visible):
            indices = {i for i in range(round(event['time']*fps), round(event['end']*fps)+1) if scores[i] >= cutoff}
            candidates.append({**event, '_indices': indices, '_roi': item['roi'],
                               'event_types': [item['event_type']],
                               'matches': [{'id': item['id'], 'name': item['name'], 'confidence': event['confidence']}]})
    groups, active = [], []
    for event in sorted(candidates, key=lambda e: e['time']):
        active = [g for g in active if g['end'] >= event['time']]
        group = next((g for g in reversed(active) if _same_region(g['_roi'], event['_roi']) and
                      g['_indices'] & event['_indices']), None)
        if group is None:
            groups.append(event)
            active.append(event)
            continue
        group['end'] = max(group['end'], event['end'])
        group['confidence'] = max(group['confidence'], event['confidence'])
        group['_indices'].update(event['_indices'])
        group['matches'].extend(event['matches'])
        group['event_types'] = sorted(set(group['event_types'] + event['event_types']))
    for group in groups:
        group['evidence'] = []
    for item, scores in zip(templates, score_tracks):
        if item['role'] == 'trigger':
            continue
        cutoff = item.get('threshold') or threshold
        applicable = [g for g in groups if item['event_type'] == 'all' or item['event_type'] in g['event_types']]
        owners = {}
        for group in applicable:
            for i in group['_indices']:
                owners[i] = owners.get(i, 0)+1
        for group in applicable:
            # One helper cannot identify the target of two simultaneous prompts.
            hits = [i for i in group['_indices'] if owners[i] == 1 and scores[i] >= cutoff]
            if _sustained(hits):
                group['evidence'].append({'id': item['id'], 'name': item['name'], 'role': item['role'],
                                          'confidence': max(float(scores[i]) for i in hits), 'samples': len(hits)})
    for group in groups:
        roles = {e['role'] for e in group['evidence']}
        group['classification'] = next(iter(roles)) if len(roles) == 1 else 'unknown'
        group['reason'] = ('玩家与小兵依据冲突，保留待确认' if len(roles) > 1 else
                           '同时持续命中玩家辅助标记' if roles == {'player'} else
                           '同时持续命中小兵辅助标记' if roles == {'npc'} else
                           '没有可唯一关联且持续命中的辅助标记，保留待确认')
        group['included'] = group['classification'] != 'npc'
        group['samples'] = len(group.pop('_indices'))
        group.pop('_roi')
    return groups


def select_events(events, excluded=()):
    """Keep unknowns. Manual exclusions apply only to the reviewed time spans."""
    selected = []
    for event in events:
        manual = any(event['time'] <= end+.125 and event['end'] >= begin-.125 for begin, end in excluded)
        event['included'] = event.get('classification') != 'npc' and not manual
        event['exclusion_reason'] = '手动排除' if manual else '小兵辅助规则' if not event['included'] else None
        if event['included']:
            selected.append(event)
    return selected


def detect_events(video, profile, ctx=None, threshold=None, *, start=0., duration=None, progress=(5, 47)):
    ctx = ctx or Context()
    meta = probe(video, ctx)
    if not math.isfinite(start) or start < 0 or start >= meta['duration']:
        raise ValueError('识别起点必须位于录像时长内。')
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        raise ValueError('识别时长必须大于 0。')
    scan_duration = min(duration, meta['duration']-start) if duration is not None else meta['duration']-start
    w, h = profile['reference_size']
    if abs(meta['width']/max(1, meta['height']) - w/h) > .03:
        raise ValueError('录像宽高比与检测配置不同，请使用该录像重新标定。')
    templates = [t for t in profile_templates(profile) if t.get('enabled', True)]
    if not any(t['role'] == 'trigger' for t in templates):
        raise ValueError('请至少启用一个事件图标模板。')
    loaded = []
    for item in templates:
        def read_image(filename):
            if not isinstance(filename, str) or Path(filename).name != filename:
                raise ValueError('模板文件名无效。')
            path = PROFILES / profile['id'] / filename
            if not path.is_file():
                raise ValueError('模板图片缺失，请重新标定。')
            image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            if image is None:
                raise ValueError('模板图片损坏，请重新标定。')
            return image
        template = read_image(item['image'])
        mask = read_image(item['mask']) if item.get('mask') else None
        if mask is not None and (mask.shape != template.shape or np.count_nonzero(mask) < 16):
            raise ValueError('模板蒙版无效，请重新标定。')
        x, y, cw, ch = item['roi']
        if min(cw, ch) <= 0 or template.shape[0] > ch or template.shape[1] > cw or x < 0 or y < 0 or x+cw > w or y+ch > h:
            raise ValueError('检测区域与模板不匹配。')
        if item.get('match_mode') == 'edges':
            template = edge_features(template)
        loaded.append((template, mask))
    # Decode the union once; all templates share the same timeline and frames.
    x, y = min(t['roi'][0] for t in templates), min(t['roi'][1] for t in templates)
    cw = max(t['roi'][0]+t['roi'][2] for t in templates)-x
    ch = max(t['roi'][1]+t['roi'][3] for t in templates)-y
    fps = profile.get('scan_fps', 8)
    filt = f'fps={fps},scale={w}:{h},format=gray,crop={cw}:{ch}:{x}:{y}'
    score_tracks = [[] for _ in templates]
    frames = 0
    args = ffmpeg_args()
    if start:
        args += ['-ss', str(start)]
    args += ['-i', video]
    if duration is not None:
        args += ['-t', str(scan_duration), '-frames:v', str(math.ceil(scan_duration*fps))]
    args += ['-map', '0:v:0', '-an', '-sn', '-vf', filt, '-pix_fmt', 'gray', '-f', 'rawvideo', '-']
    ctx.update('识别候选事件', progress[0], f'扫描原片 {start:.1f}–{start+scan_duration:.1f} 秒')
    with LoggedProcess(args, ctx) as child:
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
            for item, (template, mask), scores in zip(templates, loaded, score_tracks):
                tx, ty, tw, th = item['roi']
                crop = frame[ty-y:ty-y+th, tx-x:tx-x+tw]
                scores.append(template_score(crop, template, mask, item.get('match_mode', 'gray')))
            frames += 1
            if frames % (fps * 3) == 0:
                t = frames/fps
                ctx.update('识别候选事件', progress[0] + (progress[1]-progress[0]) * min(1, t/scan_duration), f'已扫描 {min(t, scan_duration):.1f} / {scan_duration:.1f} 秒')
        child.process.wait(timeout=15)
        child.check_result()
    events = combine_detections(templates, score_tracks, fps, threshold if threshold is not None else profile['threshold'], profile.get('rearm_seconds', .35), profile.get('min_visible_seconds', .12))
    for event in events:
        event['time'] += start
        event['end'] += start
    ctx.record('detection_complete', frames=frames, candidate_count=len(events), template_count=len(templates), profile=profile['id'], start=start, duration=scan_duration)
    return events

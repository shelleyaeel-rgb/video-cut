"""Event previews, for a bounded interval or the complete recording."""
from __future__ import annotations

import time

from .detect import AUTO_PROFILE_ID, detect_events, get_profile
from .media import Context, local_file, probe
from .pipeline import finite_number
from .review import review_signature


def validate_profile_test(spec):
    duration = finite_number(spec, 'duration', 30, 0, 60)
    if 0 < duration < 5:
        raise ValueError('扫描时长须为 5–60 秒，或选择整段录像。')
    cfg = {
        'video': local_file(spec.get('video', '')),
        'profile': get_profile(spec.get('profile') or AUTO_PROFILE_ID),
        'start': finite_number(spec, 'start', 0, 0, 86400*365),
        'duration': duration,
        'threshold': finite_number(spec, 'threshold', .82, .5, .99),
    }
    if cfg['profile'].get('kind') == 'delta_auto':
        cfg['threshold'] = .82
    return cfg


def test_profile(spec, ctx=None):
    ctx = ctx or Context()
    started = time.perf_counter()
    cfg = validate_profile_test(spec)
    signature = review_signature(cfg['video'], cfg['profile'], cfg['threshold'])
    ctx.update('检查测试范围', 1)
    meta = probe(cfg['video'], ctx)
    if not meta['width']:
        raise ValueError('原片没有视频轨道。')
    if meta['color_transfer'] in ('smpte2084', 'arib-std-b67'):
        raise ValueError('首版暂不处理 HDR 录像，请提供 SDR 素材后再试。')
    if cfg['start'] >= meta['duration']:
        raise ValueError('测试起点已到录像末尾，请向前调整。')
    remaining = meta['duration']-cfg['start']
    duration = min(cfg['duration'], remaining) if cfg['duration'] else remaining
    events = detect_events(cfg['video'], cfg['profile'], ctx, cfg['threshold'],
                           start=cfg['start'], duration=duration, progress=(5, 98))
    if signature != review_signature(cfg['video'], cfg['profile'], cfg['threshold']):
        raise ValueError('录像在扫描期间发生变化，请等待录制或复制完成后重新扫描。')
    result = {
        'video': str(cfg['video']), 'profile': cfg['profile']['id'],
        'profile_name': cfg['profile']['name'], 'threshold': cfg['threshold'],
        'start': cfg['start'], 'duration': duration, 'requested_duration': cfg['duration'],
        'full_scan': cfg['start'] == 0 and duration >= meta['duration'],
        'source_duration': meta['duration'], 'detector': cfg['profile'].get('kind'),
        'events': events, 'elapsed_seconds': round(time.perf_counter()-started, 2),
        'review_signature': signature,
    }
    ctx.update('配置测试完成', 100, f'找到 {len(events)} 个候选事件')
    ctx.record('profile_test_complete', **result)
    return result

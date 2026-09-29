"""Bounded detection checks; no rendered montage or persistent frame cache."""
from __future__ import annotations

import time

from .detect import detect_events, get_profile
from .media import Context, local_file, probe
from .pipeline import finite_number
from .review import review_signature


def validate_profile_test(spec):
    return {
        'video': local_file(spec.get('video', '')),
        'profile': get_profile(spec.get('profile')),
        'start': finite_number(spec, 'start', 0, 0, 86400*365),
        'duration': finite_number(spec, 'duration', 30, 5, 60),
        'threshold': finite_number(spec, 'threshold', .82, .5, .99),
    }


def test_profile(spec, ctx=None):
    ctx = ctx or Context()
    started = time.perf_counter()
    cfg = validate_profile_test(spec)
    ctx.update('检查测试范围', 1)
    meta = probe(cfg['video'], ctx)
    if not meta['width']:
        raise ValueError('原片没有视频轨道。')
    if meta['color_transfer'] in ('smpte2084', 'arib-std-b67'):
        raise ValueError('首版暂不处理 HDR 录像，请提供 SDR 素材后再试。')
    if cfg['start'] >= meta['duration']:
        raise ValueError('测试起点已到录像末尾，请向前调整。')
    duration = min(cfg['duration'], meta['duration']-cfg['start'])
    events = detect_events(cfg['video'], cfg['profile'], ctx, cfg['threshold'],
                           start=cfg['start'], duration=duration, progress=(5, 98))
    result = {
        'video': str(cfg['video']), 'profile': cfg['profile']['id'],
        'profile_name': cfg['profile']['name'], 'threshold': cfg['threshold'],
        'start': cfg['start'], 'duration': duration, 'requested_duration': cfg['duration'],
        'events': events, 'elapsed_seconds': round(time.perf_counter()-started, 2),
        'review_signature': review_signature(cfg['video'], cfg['profile'], cfg['threshold']),
    }
    ctx.update('配置测试完成', 100, f'找到 {len(events)} 个候选事件')
    ctx.record('profile_test_complete', **result)
    return result

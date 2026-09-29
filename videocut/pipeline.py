from __future__ import annotations

import json
import math
import time
import uuid
from pathlib import Path

from . import logs
from .audio import mix_and_mux, prepare_audio, validate_modes, validate_tracks
from .detect import detect_events, get_profile, select_events
from .edit import music_peaks, plan_clips, write_impacts
from .media import ROOT, Cancelled, Context, ffmpeg_args, local_file, probe, run
from .review import review_exclusions


def finite_number(spec, key, default, low, high):
    try:
        value = float(spec.get(key, default))
    except (ValueError, TypeError):
        raise ValueError(f'{key} 必须是有效数值。')
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{key} 必须在 {low} 到 {high} 之间。')
    return value


def validate(spec):
    music_path = spec.get('music')
    music = local_file(music_path, audio=True) if music_path is not None and str(music_path).strip() else None
    cfg = {
        'video': local_file(spec.get('video', '')),
        'music': music,
        'profile': get_profile(spec.get('profile')),
        'target': finite_number(spec, 'target', 90, 5, 120),
        'music_start': finite_number(spec, 'music_start', 0, 0, 86400) if music else 0,
        'game_volume': finite_number(spec, 'game_volume', .65, 0, 2),
        'music_volume': finite_number(spec, 'music_volume', .65, 0, 2) if music else 0,
        'strength': finite_number(spec, 'strength', .5, 0, 1),
        'threshold': finite_number(spec, 'threshold', .82, .5, .99),
        'audio_track': int(finite_number(spec, 'audio_track', 0, 0, 63)),
        'encoder': 'nvenc' if spec.get('encoder') == 'nvenc' else 'cpu',
        **validate_modes(spec, has_music=music is not None),
    }
    cfg['excluded_events'] = review_exclusions(spec, cfg['video'], cfg['profile'], cfg['threshold'])
    return cfg


def encoding_args(encoder):
    if encoder == 'nvenc':
        return ['-c:v', 'h264_nvenc', '-preset', 'p4', '-cq', '22', '-b:v', '0']
    return ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '21', '-threads', '2']


def render_clip(cfg, meta, clip, dest, encoder, ctx):
    width, height = meta['width'], meta['height']
    ratio = min(1920/width, 1080/height, 1.)
    ow, oh = max(2, int(width*ratio)//2*2), max(2, int(height*ratio)//2*2)
    duration = clip['duration']
    args = ffmpeg_args() + ['-ss', str(clip['start']), '-i', cfg['video']]
    filters = [f'scale={ow}:{oh}', 'setsar=1', 'fps=30', 'setpts=PTS-STARTPTS']
    strength = cfg['strength']
    for event in clip['events']:
        hit = max(0., event['time']-clip['start'])
        if strength:
            filters.append(f"drawbox=x=0:y=0:w=iw:h=ih:color=white@{.18*strength:.3f}:t=fill:enable='between(t,{hit:.4f},{hit+.065:.4f})'")
    if strength:
        # A brief fixed-canvas punch zoom. Kept small to preserve game readability.
        expr = '+'.join(f'between(t,{e["time"]-clip["start"]:.4f},{e["time"]-clip["start"]+.18:.4f})' for e in clip['events'])
        zoom = f'1+{.045*strength:.4f}*min(1,{expr})'
        filters += [f"scale=w='trunc({ow}*({zoom})/2)*2':h='trunc({oh}*({zoom})/2)*2':eval=frame", f'crop={ow}:{oh}', 'setsar=1']
    filters.append('format=yuv420p')
    args += ['-map', '0:v:0', '-an', '-vf', ','.join(filters), '-t', str(duration)]
    args += encoding_args(encoder) + ['-video_track_timescale', '15360', '-movflags', '+faststart', dest]
    run(args, ctx, 900)


def generate(spec, ctx=None, ident=None):
    ctx = ctx or Context()
    ident = ident or ctx.task_id or uuid.uuid4().hex
    ctx.task_id = ident
    cfg = validate(spec)
    ctx.record('settings', settings=cfg)
    output = ROOT / 'outputs' / ident
    scratch = ROOT / 'work' / 'jobs' / ident
    output.mkdir(parents=True, exist_ok=False)
    scratch.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    ctx.update('检查素材', 1)
    meta = probe(cfg['video'], ctx)
    music_meta = probe(cfg['music'], ctx) if cfg['music'] else None
    ctx.record('input_metadata', video=meta, music=music_meta, video_bytes=cfg['video'].stat().st_size, music_bytes=cfg['music'].stat().st_size if cfg['music'] else None)
    if not meta['width']:
        raise ValueError('原片没有视频轨道。')
    if music_meta is not None and not music_meta['audio_tracks']:
        raise ValueError('BGM 文件没有音频轨道。')
    validate_tracks(cfg, meta)
    budget = cfg['target']
    warnings = []
    if music_meta is not None:
        available = music_meta['duration']-cfg['music_start']
        if available < 1:
            raise ValueError('音乐起点已到末尾，请向前调整。')
        budget = min(budget, available)
        if available < cfg['target']:
            warnings.append('所选音乐余量较短，成片上限已按音乐长度缩短。')
    if cfg['profile'].get('kind') == 'demo':
        warnings.append('这是合成素材演示，检测结果不代表真实游戏准确率。')
    if meta['color_transfer'] in ('smpte2084', 'arib-std-b67'):
        raise ValueError('首版暂不处理 HDR 录像，请提供 SDR 素材后再试。')
    candidates = detect_events(cfg['video'], cfg['profile'], ctx, cfg['threshold'])
    events = select_events(candidates, cfg['excluded_events'])
    (output / 'events.json').write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding='utf-8')
    if not candidates:
        raise ValueError('没有找到候选事件。请确认检测配置匹配这份录像，或调整阈值；不会生成虚构高光。')
    if not events:
        raise ValueError('候选事件已全部被小兵规则或手动排除过滤，请测试并检查辅助标记。')
    filtered = len(candidates)-len(events)
    if filtered:
        warnings.append(f'已过滤 {filtered} 个小兵或手动排除事件；依据保留在 events.json。')
    unknown = sum(e.get('classification', 'unknown') == 'unknown' for e in events)
    if unknown and cfg['profile'].get('kind') != 'demo':
        warnings.append(f'{unknown} 个事件缺少明确的目标类型依据，已作为待确认保留。')
    ctx.update('分析音乐节奏' if cfg['music'] else '规划剪辑镜头', 49, f'找到 {len(events)} 个候选事件')
    beats = music_peaks(cfg['music'], cfg['music_start'], budget, ctx) if cfg['music'] else []
    if cfg['music'] and not beats:
        warnings.append('没有稳定的音乐瞬态，按镜头原有节奏剪辑。')
    clips = plan_clips(events, meta['duration'], budget, beats)
    ctx.record('clips_planned', candidate_count=len(events), clip_count=len(clips), music_peak_count=len(beats), duration=sum(c['duration'] for c in clips))
    if not clips:
        raise ValueError('当前时长或音乐余量不足以容纳候选片段，请增加时长或提前音乐起点。' if cfg['music'] else '当前时长不足以容纳候选片段，请增加目标时长。')
    duration = sum(c['duration'] for c in clips)
    if duration < cfg['target']*.75:
        warnings.append('有效候选素材不足以填满目标时长，已输出较短集锦。')
    if not meta['audio_tracks']:
        if cfg['music']:
            warnings.append('原片无音轨，成片使用 BGM 和效果音。')
        else:
            warnings.append('原片无音轨，成片仅使用效果音。' if cfg['strength'] else '原片无音轨，未添加 BGM 且效果音已关闭，成片为静音。')
    game_audio, music_audio, audio_report, audio_warnings = prepare_audio(cfg,meta,clips,duration,scratch,ctx)
    warnings.extend(audio_warnings)
    encoder = cfg['encoder']
    segments = []
    for i, clip in enumerate(clips):
        ctx.update('渲染镜头', 75 + 16*i/len(clips), f'{i+1} / {len(clips)}')
        dest = scratch / f'segment-{i:03}.mp4'
        try:
            render_clip(cfg, meta, clip, dest, encoder, ctx)
        except Cancelled:
            raise
        except RuntimeError as exc:
            if encoder != 'nvenc':
                raise
            logs.failure('tasks', 'encoder_fallback', exc, task_id=ctx.task_id, stage=ctx.stage, from_encoder='nvenc', to_encoder='cpu', clip_index=i)
            encoder = 'cpu'
            warnings.append('NVENC 本次不可用，已回退到 CPU 编码。')
            render_clip(cfg, meta, clip, dest, encoder, ctx)
        segments.append(dest)
    concat = scratch / 'concat.txt'
    concat.write_text(''.join(f"file '{p.name}'\n" for p in segments), encoding='utf-8')
    impacts = scratch / 'impacts.wav'
    write_impacts(impacts, clips, duration, cfg['strength'])
    ctx.update('混音并封装成片', 93)
    partial, final = output / 'montage.partial.mp4', output / 'montage.mp4'
    mix_and_mux(concat,game_audio,music_audio,impacts,duration,cfg,partial,ctx)
    ctx.update('验证输出', 96)
    result_meta = probe(partial, ctx)
    if abs(result_meta['duration']-duration) > .25 or result_meta['duration'] > cfg['target']+.15 or not result_meta['audio_tracks']:
        raise RuntimeError('输出时长或音轨校验失败，保留中间文件供排查。')
    ctx.check()
    partial.replace(final)
    manifest = {'id': ident, 'status': 'complete', 'demo': cfg['profile'].get('kind') == 'demo', 'output': str(final), 'duration': result_meta['duration'], 'target': cfg['target'], 'elapsed_seconds': round(time.perf_counter()-started, 2), 'encoder': encoder, 'events': events, 'clips': clips, 'music_peaks': beats, 'warnings': warnings, 'settings': {k: str(v) if isinstance(v, Path) else v for k, v in cfg.items()}, 'output_meta': result_meta}
    manifest['audio_processing'] = audio_report
    manifest['candidate_count'] = len(candidates)
    manifest['excluded_count'] = filtered
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    ctx.update('已完成', 100)
    ctx.record('output_verified', output=str(final), output_meta=result_meta, warnings=warnings, elapsed_seconds=manifest['elapsed_seconds'])
    return manifest

"""Deterministic synthetic media, never presented as gameplay validation."""
from __future__ import annotations

import json
import wave

import cv2
import numpy as np

from .detect import PROFILES
from .media import ROOT, Context, ffmpeg_args, run


def make_demo(ctx=None):
    ctx = ctx or Context()
    folder = ROOT / 'work' / 'demo'
    folder.mkdir(parents=True, exist_ok=True)
    video, music = folder / '演示 原片.mp4', folder / '演示 BGM.wav'
    config_path = PROFILES / 'demo' / 'profile.json'
    if video.is_file() and music.is_file() and config_path.is_file():
        return {'video': str(video), 'music': str(music), 'profile': 'demo', 'target': 16, 'expected_events': [5, 10, 15, 20]}
    ctx.update('生成合成测试素材', 10, '包含四个已知时间的提示图案')
    cue = np.full((60, 280, 3), (26, 35, 30), dtype=np.uint8)
    cv2.rectangle(cue, (2, 2), (277, 57), (85, 235, 180), 2)
    cv2.line(cue, (14, 16), (37, 42), (225, 245, 245), 4)
    cv2.line(cue, (37, 16), (14, 42), (225, 245, 245), 4)
    cv2.putText(cue, 'ELIM +100', (52, 40), cv2.FONT_HERSHEY_SIMPLEX, .92, (240, 240, 240), 2, cv2.LINE_AA)
    cue_file = folder / 'cue.png'
    cv2.imencode('.png', cue)[1].tofile(str(cue_file))
    banner = np.zeros((50, 960, 3), dtype=np.uint8)
    cv2.putText(banner, 'SYNTHETIC TEST / NOT GAMEPLAY', (22, 34), cv2.FONT_HERSHEY_SIMPLEX, .8, (230, 230, 230), 2, cv2.LINE_AA)
    banner_file = folder / 'banner.png'
    cv2.imencode('.png', banner)[1].tofile(str(banner_file))
    expected = [5, 10, 15, 20]
    visible = '+'.join(f'between(t,{t},{t+.9})' for t in expected)
    graph = f"[0:v][1:v]overlay=600:90:enable='{visible}'[v];[v][2:v]overlay=0:0[out]"
    tmp_video = folder / 'demo.partial.mp4'
    run(ffmpeg_args() + ['-f', 'lavfi', '-i', 'testsrc2=size=960x540:rate=30:duration=24', '-loop', '1', '-i', cue_file, '-loop', '1', '-i', banner_file, '-f', 'lavfi', '-i', 'sine=frequency=180:sample_rate=48000:duration=24', '-filter_complex', graph, '-map', '[out]', '-map', '3:a', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-threads', '2', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-t', '24', '-movflags', '+faststart', tmp_video], ctx, 180)
    ctx.update('生成测试节拍', 80)
    rate, duration = 48000, 30
    timeline = np.arange(rate*duration)/rate
    song = (.035*np.sin(2*np.pi*220*timeline) + .025*np.sin(2*np.pi*330*timeline)).astype(np.float32)
    for beat in np.arange(.25, duration-.2, .5):
        t = np.arange(int(rate*.16))/rate
        kick = .4*np.sin(2*np.pi*(95*t-130*t*t))*np.exp(-t*24)
        begin = int(beat*rate)
        song[begin:begin+len(kick)] += kick
    with wave.open(str(music), 'wb') as f:
        f.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        f.writeframes((np.clip(song, -1, 1)*32767).astype('<i2').tobytes())
    directory = PROFILES / 'demo'
    directory.mkdir(parents=True, exist_ok=True)
    gray = cv2.cvtColor(cue, cv2.COLOR_BGR2GRAY)
    cv2.imencode('.png', gray)[1].tofile(str(directory / 'template.png'))
    profile = {'id': 'demo', 'name': '合成演示专用 · 非游戏配置', 'kind': 'demo', 'reference_size': [960, 540], 'roi': [588, 78, 304, 84], 'threshold': .82, 'scan_fps': 8, 'rearm_seconds': .35, 'min_visible_seconds': .12, 'source_note': '合成测试；不能用于验证三角洲真实准确率'}
    config_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding='utf-8')
    ctx.check()
    tmp_video.replace(video)
    ctx.update('演示素材就绪', 100)
    return {'video': str(video), 'music': str(music), 'profile': 'demo', 'target': 16, 'expected_events': expected}

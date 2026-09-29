from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np

from .media import ffmpeg_args, run


def music_peaks(music, start, duration, ctx=None):
    raw = run(ffmpeg_args() + ['-ss', str(start), '-i', music, '-map', '0:a:0', '-t', str(duration), '-vn', '-ac', '1', '-ar', '16000', '-f', 'f32le', '-'], ctx, 120)
    samples = np.frombuffer(raw, dtype='<f4')
    block = 320  # 20 ms energy envelope; no claim to infer bars or song structure.
    n = len(samples)//block
    if n < 4:
        return []
    rms = np.sqrt(np.mean(samples[:n*block].reshape(n, block)**2, axis=1))
    if float(rms.max()) < .0001:
        return []
    onset = np.maximum(0, rms - np.roll(rms, 2))
    onset[:2] = 0
    threshold = max(float(np.quantile(onset, .8)), float(onset.max())*.18, .0001)
    peaks = [i for i in range(1, n-1) if onset[i] >= threshold and onset[i] >= onset[i-1] and onset[i] > onset[i+1]]
    picked = []
    for i in sorted(peaks, key=lambda k: float(onset[k]), reverse=True):
        if all(abs(i-j) >= 11 for j in picked):
            picked.append(i)
    return [round(i*.02, 3) for i in sorted(picked)]


def plan_clips(events, source_duration, target, beats=(), pre=3.2, post=.8):
    if not events:
        return []
    groups = []
    for event in sorted(events, key=lambda e: e['time']):
        time = float(event['time'])
        if time < 0 or time >= source_duration:
            continue
        start, end = max(0., time-pre), min(source_duration, time+post)
        if groups and start <= groups[-1]['end']+.35 and end-groups[-1]['start'] <= 10:
            groups[-1]['end'] = max(groups[-1]['end'], end)
            groups[-1]['events'].append(event)
        else:
            # Do not reuse the end of the previous candidate in the next clip.
            if groups:
                start = max(start, groups[-1]['end'])
            if end-start >= .4:
                groups.append({'start': start, 'end': end, 'events': [event]})
    ranked = sorted(groups, key=lambda c: (len(c['events']), sum(e['confidence'] for e in c['events'])), reverse=True)
    selected, used = [], 0.
    # Reserve small per-clip room for beat alignment; never pad weak footage.
    for clip in ranked:
        length = clip['end']-clip['start']
        if used + length <= target + 1e-8:
            selected.append(dict(clip))
            used += length
    selected.sort(key=lambda c: c['start'])
    output, cursor, prev_end = [], 0., 0.
    for clip in selected:
        anchor = clip['events'][0]['time']
        start, end = clip['start'], clip['end']
        intended = cursor + anchor-start
        nearby = [b for b in beats if abs(b-intended) <= .45]
        shift = 0.
        if nearby:
            beat = min(nearby, key=lambda b: abs(b-intended))
            new_start = cursor+anchor-beat
            if max(prev_end, start-.45, 0) <= new_start <= min(start+.45, anchor-.35):
                shift = new_start-start
                start = new_start
        # Frame-aligned boundaries keep manifest and rendered montage in sync.
        start = math.ceil(start*30-1e-6)/30
        frames = int(math.floor((end-start)*30 + 1e-6))
        remaining = int(math.floor((target-cursor)*30 + 1e-6))
        frames = min(frames, remaining)
        end = start+frames/30
        included = [e for e in clip['events'] if start <= e['time'] < end-.05]
        if frames < 12 or not included:
            continue
        output.append({'start': round(start, 6), 'end': round(end, 6), 'duration': frames/30, 'output_start': round(cursor, 6), 'events': included, 'beat_shift': round(shift, 4)})
        cursor += frames/30
        prev_end = end
    return output


def write_impacts(path: Path, clips, duration, strength):
    rate = 48000
    data = np.zeros(max(1, int(math.ceil(duration*rate))), dtype=np.float32)
    if strength > 0:
        t = np.arange(int(rate*.13))/rate
        hit = np.sin(2*np.pi*(145*t-250*t*t))*np.exp(-t*34)*.16*strength
        for clip in clips:
            for event in clip['events']:
                begin = int((clip['output_start'] + event['time']-clip['start'])*rate)
                count = min(len(hit), len(data)-begin)
                if begin >= 0 and count > 0:
                    data[begin:begin+count] += hit[:count]
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        output.writeframes((np.clip(data, -1, 1)*32767).astype('<i2').tobytes())

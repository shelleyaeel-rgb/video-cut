from __future__ import annotations

import json
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


def plan_collection(events, source_duration, pre=5., post=2., fps=30):
    """Keep every selected marker, merging overlapping source intervals.

    This is the non-destructive assembly, with no montage budget or maximum
    shot length. Boundaries expand to output frames so per-clip rounding cannot
    accumulate audio drift. Only a final partial source frame requires padding.
    """
    try:
        source_duration, pre, post, fps = map(float, (source_duration, pre, post, fps))
    except (ValueError, TypeError):
        return []
    if (not all(math.isfinite(v) for v in (source_duration, pre, post, fps))
            or min(source_duration, fps) <= 0 or min(pre, post) < 0):
        return []
    unique = {}
    for original in events:
        try:
            begin = float(original['time'])
            end = float(original.get('end', begin))
            confidence = float(original.get('confidence', 0))
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(begin) or not 0 <= begin < source_duration:
            continue
        end = min(source_duration, max(begin, end)) if math.isfinite(end) else begin
        event = dict(original, time=begin, end=end,
                     confidence=confidence if math.isfinite(confidence) else 0.)
        key = ('id', event['event_id']) if event.get('event_id') else (
            'content', json.dumps(event, sort_keys=True, ensure_ascii=False))
        if key in unique:
            previous = unique[key]
            best = event if event['confidence'] > previous['confidence'] else previous
            event = dict(best, time=min(begin, previous['time']), end=max(end, previous['end']))
        unique[key] = event

    clips = []
    for event in sorted(unique.values(), key=lambda item: item['time']):
        start = math.floor(max(0., event['time']-pre)*fps+1e-9)/fps
        desired_end = max(event['end']+post, event['time']+1/fps)
        end = min(source_duration, math.ceil(desired_end*fps-1e-9)/fps)
        if clips and start <= clips[-1]['end']+1e-9:
            clips[-1]['end'] = max(clips[-1]['end'], end)
            clips[-1]['events'].append(event)
        else:
            clips.append({'start': start, 'end': end, 'events': [event]})
    cursor = 0
    for clip in clips:
        source_length = clip['end']-clip['start']
        frames = max(1, math.ceil(source_length*fps-1e-9))
        clip['duration'] = frames/fps
        clip['source_duration'] = source_length
        clip['tail_padding'] = max(0., clip['duration']-source_length)
        clip['output_start'] = cursor/fps
        clip['beat_shift'] = 0.
        cursor += frames
    return clips


def plan_clips(events, source_duration, target, beats=(), pre=5., post=2.):
    """Keep complete event windows, then select non-overlapping fights to budget.

    Visibility is not an event count: a single persistent indicator contributes
    at most three seconds of tail. Distinct later events extend the fight. Long
    fights split at event boundaries rather than dropping events in the overlap.
    All boundaries use output frames so alignment cannot overrun the budget.
    """
    fps, max_frames, minimum_frames = 30, 300, 12
    try:
        source_duration, target, pre, post = map(float, (source_duration, target, pre, post))
    except (TypeError, ValueError):
        return []
    if not all(math.isfinite(v) for v in (source_duration, target, pre, post)):
        return []
    if min(source_duration, target) <= 0 or min(pre, post) < 0:
        return []
    source_frames = math.floor(source_duration * fps + 1e-6)
    budget = math.floor(target * fps + 1e-6)
    if min(source_frames, budget) < minimum_frames:
        return []

    # Deduplicate event facts, not source frames: simultaneous events stay distinct.
    unique = {}
    for original in events:
        try:
            time = float(original['time'])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(time) or not 0 <= time < source_frames / fps:
            continue
        event = dict(original, time=time)
        try:
            end = float(event.get('end', time))
        except (TypeError, ValueError):
            end = time
        event['end'] = min(source_duration, max(time, end)) if math.isfinite(end) else time
        try:
            confidence = float(event.get('confidence', 0))
        except (TypeError, ValueError):
            confidence = 0.
        event['confidence'] = confidence if math.isfinite(confidence) else 0.
        key = ('id', event['event_id']) if event.get('event_id') else ('content', json.dumps(event, sort_keys=True, ensure_ascii=False))
        if key in unique:
            previous = unique[key]
            best = event if event['confidence'] > previous['confidence'] else previous
            event = dict(best, time=min(time, previous['time']), end=max(event['end'], previous['end']))
        unique[key] = event

    # One planning item per frame prevents a split between simultaneous events.
    frame_items = {}
    for event in sorted(unique.values(), key=lambda item: item['time']):
        frame = math.floor(event['time'] * fps)
        # Bound one detection's window even when stale UI persists for minutes.
        tail = min(event['end'] - event['time'], 3.) + min(post, 3.)
        end = min(source_frames, math.ceil((event['time'] + tail) * fps - 1e-6))
        end = max(frame + 1, end)
        start = max(0, math.floor((event['time'] - pre) * fps + 1e-6), end - max_frames)
        if frame in frame_items:
            item = frame_items[frame]
            item['end'] = max(item['end'], end)
            item['start'] = max(min(item['start'], start), item['end'] - max_frames)
            item['events'].append(event)
        else:
            frame_items[frame] = {'frame': frame, 'events': [event], 'start': start, 'end': end}

    groups = []
    for item in frame_items.values():
        start, end = item['start'], item['end']
        if groups and start <= groups[-1]['end'] + round(.35 * fps):
            groups[-1]['end'] = max(groups[-1]['end'], end)
            groups[-1]['items'].append(item)
        else:
            groups.append({'start': start, 'end': end, 'items': [item]})

    candidates = []
    for group in groups:
        start, end, items = group['start'], group['start'], []
        for item in group['items']:
            if items and item['end'] - start > max_frames:
                # Cut before the next event, retaining it in the following shot.
                # The following shot continues the prior action without replay.
                boundary = min(end, item['frame'])
                candidates.append({'start': start, 'end': boundary, 'items': items})
                start = max(boundary, item['end'] - max_frames)
                items = []
                end = start
            end = max(end, item['end'])
            items.append(item)
        if items:
            candidates.append({'start': start, 'end': end, 'items': items})

    def rank(clip):
        return (sum(len(item['events']) for item in clip['items']),
                sum(event['confidence'] for item in clip['items'] for event in item['events']))

    selected, used = [], 0
    for clip in sorted(candidates, key=rank, reverse=True):
        remaining = budget - used
        length = clip['end'] - clip['start']
        if length < minimum_frames:
            continue
        if length > remaining:
            # A short requested montage should still include the event, rather
            # than silently returning nothing because its full pre-roll won't fit.
            if remaining < min(budget, 2 * fps):
                continue
            choices = []
            for item in clip['items']:
                end = min(clip['end'], item['end'], item['frame'] + remaining - 1)
                start = max(clip['start'], end - remaining)
                end = min(clip['end'], start + remaining)
                included = [i for i in clip['items'] if start <= i['frame'] < end]
                if included:
                    choices.append({'start': start, 'end': end, 'items': included})
            if not choices:
                continue
            clip = max(choices, key=rank)
            length = clip['end'] - clip['start']
        selected.append(clip)
        used += length
    selected.sort(key=lambda c: c['start'])

    beat_frames = []
    for beat in beats:
        try:
            beat = float(beat)
        except (TypeError, ValueError):
            continue
        if math.isfinite(beat) and beat >= 0:
            beat_frames.append(round(beat * fps))
    output, cursor, previous_end = [], 0, 0
    for index, clip in enumerate(selected):
        start, end = clip['start'], clip['end']
        anchor = clip['items'][0]['frame']
        intended = cursor + anchor - start
        nearby = sorted((b for b in beat_frames if abs(b - intended) <= .45 * fps),
                        key=lambda b: abs(b - intended))
        shift = 0
        next_start = selected[index + 1]['start'] if index + 1 < len(selected) else source_frames
        for beat in nearby:
            proposed = intended - beat
            new_start, new_end = start + proposed, end + proposed
            if (new_start >= previous_end and new_end <= next_start
                    and new_start <= anchor and clip['items'][-1]['frame'] < new_end):
                shift = proposed
                start, end = new_start, new_end
                break
        frames = end - start
        output.append({'start': round(start / fps, 6), 'end': round(end / fps, 6),
                       'duration': frames / fps, 'output_start': round(cursor / fps, 6),
                       'events': [event for item in clip['items'] for event in item['events']],
                       'beat_shift': round(shift / fps, 4)})
        cursor += frames
        previous_end = end
    return output


def unique_effect_events(events):
    """One effect per 30 fps source frame; leave the event facts untouched."""
    frames, result = set(), []
    for event in sorted(events, key=lambda item: item['time']):
        frame = math.floor(event['time'] * 30)
        if frame not in frames:
            frames.add(frame)
            result.append(event)
    return result


def write_impacts(path: Path, clips, duration, strength):
    rate = 48000
    data = np.zeros(max(1, int(math.ceil(duration*rate))), dtype=np.float32)
    if strength > 0:
        t = np.arange(int(rate*.13))/rate
        hit = np.sin(2*np.pi*(145*t-250*t*t))*np.exp(-t*34)*.16*strength
        output_frames = set()
        for clip in clips:
            for event in unique_effect_events(clip['events']):
                output_time = clip['output_start'] + event['time'] - clip['start']
                frame = math.floor(output_time * 30 + 1e-6)
                if frame in output_frames:
                    continue
                output_frames.add(frame)
                begin = int(output_time * rate)
                count = min(len(hit), len(data)-begin)
                if begin >= 0 and count > 0:
                    data[begin:begin+count] += hit[:count]
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        output.writeframes((np.clip(data, -1, 1)*32767).astype('<i2').tobytes())

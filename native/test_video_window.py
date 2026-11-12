"""Replay an existing video in an owned, non-activating validation window.

--validate records ONLY this uniquely titled window, then persists the detector
markers and exports their collection. It never selects a monitor or records
system audio. This is a replay integration test, not a live-game benchmark.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

TITLE = 'FenghuoClip Delta Replay Validation'
ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def replay_window(args):
    import cv2
    from PIL import Image, ImageTk
    import tkinter as tk

    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    capture = cv2.VideoCapture(str(args.video))
    if not capture.isOpened():
        raise RuntimeError('Cannot open replay fixture')
    fps = capture.get(cv2.CAP_PROP_FPS)
    root = tk.Tk()
    root.withdraw()
    root.title(TITLE)
    # Fit the whole 1080p canvas on a 1080p monitor without a non-client title
    # bar pushing the final 38 rows outside the composited desktop surface.
    root.overrideredirect(True)
    root.geometry('1920x1080+0+0')
    root.maxsize(4096, 2160)
    root.resizable(False, False)
    root.attributes('-topmost', False)
    canvas = tk.Canvas(root, width=1920, height=1080, highlightthickness=0, background='black')
    canvas.pack(fill='both', expand=True)
    canvas_image = canvas.create_image(0, 0, anchor='nw')
    label = canvas.create_text(12, 12, anchor='nw', text='Replay validation — waiting for recorder', fill='white', font=('Consolas', 15))
    root.update_idletasks()
    root.deiconify()
    root.lower()
    root.update()
    root.lower()
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
    user = ctypes.windll.user32
    user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    # Tk marks override-redirect windows as tools; libobs correctly excludes
    # those from selectable application windows. This is our own test window.
    extended = user.GetWindowLongPtrW(hwnd, -20)
    user.SetWindowLongPtrW(hwnd, -20, (extended & ~0x80) | 0x40000)
    user.SetWindowPos(hwnd, ctypes.c_void_p(1), 0, 0, 1920, 1080, 0x0020 | 0x0010)
    root.update()
    report = {'title': TITLE, 'hwnd': int(hwnd), 'client_size': [canvas.winfo_width(), canvas.winfo_height()],
              'fixture': str(args.video), 'ready_monotonic_ns': time.perf_counter_ns(), 'presented': []}
    write_json(args.report, report)
    started = None
    window_start = time.perf_counter()
    current_frame, current_pts, decoded_index = None, None, -1

    def draw():
        nonlocal started, current_frame, current_pts, decoded_index
        if (args.stop_signal and args.stop_signal.exists()) or time.perf_counter()-window_start > args.seconds:
            root.destroy()
            return
        if started is None:
            if args.signal and not args.signal.exists():
                root.after(20, draw)
                return
            started = time.perf_counter()
            report['play_start_monotonic_ns'] = time.perf_counter_ns()
            write_json(args.report, report)
        target = time.perf_counter()-started
        report['client_size'] = [canvas.winfo_width(), canvas.winfo_height()]
        # Decode all intervening frames, present only the newest frame needed
        # for real-time playback; decoder delay is measured, never hidden.
        goal = int(target*fps)
        while decoded_index < goal:
            ok, frame = capture.read()
            if not ok:
                report['playback_complete'] = True
                break
            decoded_index += 1
            current_frame = frame
            current_pts = capture.get(cv2.CAP_PROP_POS_MSEC)/1000
        if current_frame is not None:
            frame = current_frame
            if frame.shape[:2] != (1080, 1920):
                frame = cv2.resize(frame, (1920, 1080))
            picture = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
            canvas.itemconfigure(canvas_image, image=picture)
            canvas.picture = picture
            canvas.itemconfigure(label, text=f'EXISTING VIDEO REPLAY | source PTS {current_pts:07.3f}s')
            report['presented'].append({'source_pts': current_pts, 'monotonic_ns': time.perf_counter_ns()})
        if report.get('playback_complete'):
            write_json(args.report, report)
            root.after(100, idle)
        else:
            root.after(8, draw)

    def idle():
        if (args.stop_signal and args.stop_signal.exists()) or time.perf_counter()-window_start > args.seconds:
            root.destroy()
        else:
            root.after(100, idle)

    root.after(0, draw)
    try:
        root.mainloop()
    finally:
        report['closed_monotonic_ns'] = time.perf_counter_ns()
        write_json(args.report, report)
        capture.release()


def validate(args):
    run_root = args.output.resolve() / ('run-'+str(time.time_ns()))
    run_root.mkdir(parents=True, exist_ok=False)
    os.environ['VIDEOCUT_DATA_DIR'] = str(run_root)
    sys.path.insert(0, str(ROOT))
    import cv2
    import numpy as np
    from videocut.capture_backend import CaptureBackend, list_sources
    from videocut.realtime import Detector
    from videocut.project_store import ProjectStore
    from videocut.media import probe, run, ffmpeg_args
    from videocut.pipeline import generate

    store = ProjectStore(run_root/'data'/'projects.sqlite')
    video = run_root/'source.mkv'
    source_report, signal, stop_signal = run_root/'window.json', run_root/'play.signal', run_root/'stop.signal'
    stdout = (run_root/'window.stdout').open('w', encoding='utf-8')
    stderr = (run_root/'window.stderr').open('w', encoding='utf-8')
    fixture = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--video', str(args.video.resolve()),
        '--signal', str(signal), '--stop-signal', str(stop_signal), '--report', str(source_report), '--seconds', '180'],
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), stdout=stdout, stderr=stderr)
    backend = CaptureBackend()
    detector = None
    samples, marker_updates, push_timings = [], [], []
    report = {'kind': 'existing_video_window_replay', 'actual_game_test': False, 'system_audio': False,
              'fixture': str(args.video.resolve()), 'root': str(run_root)}
    session = None
    anchor = {}
    try:
        deadline = time.monotonic()+15
        while not source_report.exists():
            if fixture.poll() is not None:
                raise RuntimeError('Replay window exited before becoming ready')
            if time.monotonic() > deadline:
                raise TimeoutError('Replay window did not become ready')
            time.sleep(.1)
        matches = [item for item in list_sources() if item['kind']=='window' and TITLE in item['name']]
        if len(matches) != 1:
            raise RuntimeError(f'Expected exactly one owned replay window, found {len(matches)}; no other source will be recorded')
        source = matches[0]
        report['capture_source'] = source
        session = store.create_session({'source': source, 'audio': False}, source_path=video)
        ident = session['id']
        def marker(value):
            marker_updates.append({'received_monotonic_ns': time.perf_counter_ns(), 'marker': value})
            store.upsert_marker(ident, value, marker_id=value['id'])
        def gap(value):
            store.add_gap(ident, value['start'], value['end'], value['reason'])
        detector = Detector(on_marker=marker, on_gap=gap).start()
        def sample(frame, pts, metadata):
            began = time.perf_counter()
            accepted = detector.push(frame, pts, metadata)
            push_timings.append((time.perf_counter()-began)*1000)
            samples.append({'pts': pts, 'accepted': accepted, 'received_monotonic_ns': time.perf_counter_ns(), 'metadata': metadata})
            if not anchor and pts > 15:
                anchor.update(frame=frame.copy(), pts=pts, roi=metadata['roi'])
        ack = backend.start({'source': source, 'width':1920,'height':1080,'fps':30,'sample_fps':15,
                             'audio':False,'encoder':'auto'}, video, on_sample=sample)
        store.update_session(ident, state='recording')
        report['capture_start'] = ack
        print('START '+json.dumps({'root':str(run_root),'source':source['name'],'encoder':ack.get('encoder')}), flush=True)
        signal.write_text('play', encoding='utf-8')
        began = time.monotonic()
        next_report = 5
        while time.monotonic()-began < 45:
            elapsed = time.monotonic()-began
            if elapsed >= next_report:
                print('PROGRESS '+json.dumps({'elapsed':round(elapsed,2),'samples':len(samples),
                    'markers':len(detector.snapshot()['markers']),'capture':backend.snapshot().get('elapsed_seconds')}), flush=True)
                next_report += 5
            if fixture.poll() is not None:
                raise RuntimeError('Replay window exited during recording')
            if backend.snapshot().get('error'):
                raise RuntimeError(backend.snapshot()['error'])
            time.sleep(.1)
        report['capture_stop'] = backend.stop(timeout=25)
        detection = detector.stop(timeout=15)
        for value in detection['markers']:
            store.upsert_marker(ident, value, marker_id=value['id'])
        for value in detection['gaps']:
            store.add_gap(ident, value['start'], value['end'], value['reason'])
        report['detection'] = detection
        metadata = probe(video)
        store.finalize_asset(ident, metadata=metadata)
        report['source_meta'] = metadata
        stop_signal.write_text('stop', encoding='utf-8')
        fixture.wait(timeout=10)
        window = json.loads(source_report.read_text(encoding='utf-8'))
        report['window'] = {key:value for key,value in window.items() if key!='presented'}
        report['displayed_frames'] = len(window['presented'])
        report['sample_count'] = len(samples)
        report['accepted_samples'] = sum(item['accepted'] for item in samples)
        report['push_ms'] = {'p95':float(np.quantile(push_timings,.95)),'max':max(push_timings)}
        origin = ack['recording_pts_origin_ns']
        report['playback_offset_seconds'] = (window['play_start_monotonic_ns']-origin)/1e9
        first_updates = {}
        for update in marker_updates:
            first_updates.setdefault(update['marker']['id'], update)
        report['marker_latency_seconds'] = [
            (item['received_monotonic_ns']-origin)/1e9-item['marker']['time'] for item in first_updates.values()]
        report['markers_source_relative'] = [
            {'id':value['id'],'type':value['event_types'],'source_time':value['time']-report['playback_offset_seconds'],
             'recording_time':value['time'],'end':value['end'],'state':value['state']} for value in detection['markers']]
        if anchor:
            recorded = cv2.VideoCapture(str(video))
            recorded.set(cv2.CAP_PROP_POS_MSEC, anchor['pts']*1000)
            ok, frame = recorded.read()
            decoded_pts = recorded.get(cv2.CAP_PROP_POS_MSEC)/1000
            recorded.release()
            if ok:
                x0,y0,x1,y1 = anchor['roi']
                crop = frame[y0:y1,x0:x1]
                report['sample_pts_verification'] = {'sample_pts':anchor['pts'],'decoded_pts':decoded_pts,
                    'mean_absolute_pixel_error':float(np.abs(crop.astype(float)-anchor['frame'].astype(float)).mean()),
                    'correlation':float(np.corrcoef(crop.reshape(-1),anchor['frame'].reshape(-1))[0,1])}
                cv2.imwrite(str(run_root/'recorded-anchor.png'), frame)
                np.savez_compressed(run_root/'sample-anchor.npz', **anchor)
        report['project'] = store.get_session(ident)
        write_json(run_root/'report.json', report)
        if not report['capture_stop'].get('graceful'):
            raise RuntimeError('Recorder did not finalize gracefully')
        if not detection['markers']:
            raise RuntimeError('No automatic markers detected in recorded replay')
        spec, recording_source = store.build_generation_spec(ident, mode='collection')
        collection = generate(spec, recording_source=recording_source)
        run(ffmpeg_args()+['-i',collection['output'],'-f','null','-'])
        report['collection'] = {key:collection[key] for key in ('output','duration','clips','selected_event_count','output_meta')}
        report['ok'] = True
        write_json(run_root/'report.json', report)
        print('RESULT '+json.dumps({'ok':True,'report':str(run_root/'report.json'),
            'markers':report['markers_source_relative'],'latency':report['marker_latency_seconds'],
            'collection':collection['output'],'duration':collection['duration'],
            'drops':{key:report['capture_stop'].get(key) for key in ('dropped_frames','analysis_dropped_frames')},
            'gaps':detection['gaps']}), flush=True)
    except BaseException as exc:
        report.update(ok=False,error=str(exc))
        write_json(run_root/'report.json', report)
        raise
    finally:
        backend.stop(timeout=5)
        if detector and detector.state not in ('stopped','needs_rescan'):
            detector.stop(timeout=3)
        stop_signal.write_text('stop', encoding='utf-8')
        try:
            fixture.wait(timeout=5)
        except subprocess.TimeoutExpired:
            fixture.terminate()
            fixture.wait(timeout=3)
        (run_root/'capture.log').write_text(backend.diagnostics(), encoding='utf-8')
        write_json(run_root/'samples.json', samples)
        write_json(run_root/'marker-updates.json', marker_updates)
        stdout.close()
        stderr.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--validate', action='store_true')
    parser.add_argument('--video', type=Path, default=ROOT/'work'/'automatic-real-sample.mp4')
    parser.add_argument('--output', type=Path, default=ROOT/'work'/'capture-real-replay')
    parser.add_argument('--signal', type=Path)
    parser.add_argument('--stop-signal', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--seconds', type=float, default=180)
    args = parser.parse_args()
    if args.validate:
        validate(args)
    else:
        if args.report is None:
            args.output.mkdir(parents=True, exist_ok=True)
            args.report = args.output / ('window-'+str(time.time_ns())+'.json')
        replay_window(args)


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    main()

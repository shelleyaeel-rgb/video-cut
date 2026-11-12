"""Packaged desktop entry point, local workers and frozen-build diagnostics."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import threading
import webbrowser


class InstanceLock:
    """One desktop process per data directory; Windows releases locks on exit."""
    def __init__(self, root):
        self.path = Path(root) / 'work' / 'desktop.lock'
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.file.seek(0)
                if not self.file.read(1):
                    self.file.write(b'0')
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise RuntimeError('烽火剪辑已经运行，请使用已打开的窗口。') from exc
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.close()


def message(text):
    if os.name == 'nt':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, '烽火剪辑', 0x40)
    else:
        print(text, file=sys.stderr)


def _self_test(ai=False):
    """Exercise the actual frozen imports, HTTP resources and video pipeline."""
    import urllib.request
    from . import __version__
    from .audio_model import require_model
    from .autodetect import _engine, status as detection_status
    from .demo import make_demo
    from .media import Context, probe, run, tool_path
    from .paths import DATA_ROOT, RESOURCE_ROOT, worker_command
    from .pipeline import generate
    from .server import App, LocalHTTPServer, handler_for
    from . import logs
    require_model(verify=True)
    report = {'version': __version__, 'data_dir': str(DATA_ROOT), 'resources': str(RESOURCE_ROOT)}
    import numpy as np
    _engine(Context())(np.zeros((96, 320, 3), dtype=np.uint8), use_cls=False)
    report['automatic_detection'] = detection_status()
    from .capture_backend import backend_status, list_capture_devices
    report['capture'] = backend_status()
    if not report['capture']['available']:
        raise RuntimeError(report['capture']['reason'])
    devices = list_capture_devices()
    report['capture']['source_count'] = len(devices['sources'])
    from .realtime import Detector
    detector = Detector().start()
    try:
        detector.push(np.zeros((540, 960, 3), dtype=np.uint8), 0.)
        detector_report = detector.stop(timeout=15)
        if detector_report.get('state') in ('failed', 'needs_rescan'):
            raise RuntimeError('Frozen detection worker failed: ' + str(detector_report))
        report['realtime_worker'] = detector_report.get('state')
    finally:
        detector.stop(timeout=2)
    report['ffmpeg'] = run([tool_path('ffmpeg'), '-version'], timeout=15).decode(errors='replace').splitlines()[0]
    app = App()
    server = LocalHTTPServer(('127.0.0.1', 0), handler_for(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f'http://127.0.0.1:{server.server_port}'
        with urllib.request.urlopen(url, timeout=10) as response:
            assert app.token.encode() in response.read(), 'Missing HTML/session resource'
        for asset in ('/app.js', '/style.css'):
            with urllib.request.urlopen(url + asset, timeout=10) as response:
                assert len(response.read()) > 100, 'Missing UI resource'
        request = urllib.request.Request(url + '/api/state', headers={'X-Session-Token': app.token})
        with urllib.request.urlopen(request, timeout=10) as response:
            report['api'] = json.load(response)['version']
        spec = make_demo()
        result = generate(spec, Context(), 'desktop-self-test')
        report['video'] = probe(result['output'])
        run([tool_path('ffmpeg'), '-v', 'error', '-i', result['output'], '-f', 'null', '-'], timeout=120)
        if ai:
            import wave
            source = DATA_ROOT / 'work' / 'self-test.wav'
            destination = source.with_name('self-test-separated.wav')
            progress = source.with_suffix('.json')
            with wave.open(str(source), 'wb') as output:
                output.setparams((2, 2, 44100, 0, 'NONE', 'not compressed'))
                output.writeframes(b'\0' * (44100 * 4 // 10))
            run(worker_command('separator', source, destination, progress), timeout=120)
            assert destination.is_file(), 'Frozen AI worker failed'
            report['ai_worker'] = json.loads(progress.read_text())['phase']
        report['ok'] = True
        return report
    finally:
        app.close()
        server.shutdown()
        server.server_close()
        thread.join(5)
        logs.store().close()


def _run_window(args):
    from . import __version__, logs
    from .paths import DATA_ROOT
    from .server import App, LocalHTTPServer, handler_for
    from .media import tool_path, run
    from .updater import mark_update_healthy
    for name in ('ffmpeg', 'ffprobe'):
        run([tool_path(name), '-version'], timeout=15)
    app = App()
    app.desktop = True
    server = LocalHTTPServer(('127.0.0.1', args.port), handler_for(app))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stopped = threading.Event()
    app.request_exit = stopped.set
    url = f'http://127.0.0.1:{server.server_port}'
    logs.event('startup', 'desktop_ready', version=__version__, url=url, pid=os.getpid())
    def healthy(*unused):
        logs.event('startup', 'desktop_window_loaded', version=__version__)
        if args.update_health:
            mark_update_healthy(args.update_health)
    try:
        if not args.browser:
            try:
                import webview
                window = webview.create_window('烽火剪辑', url, width=1320, height=900, min_size=(940, 640))

                def closing(*unused):
                    with app.lock:
                        if app.active or app.recording_busy:
                            message('任务仍在处理中。请先结束录制，或等待/取消剪辑任务后再退出。')
                            return False
                        app.closing = True
                    return True

                window.events.closing += closing
                window.events.loaded += healthy
                app.request_exit = window.destroy
                webview.start(gui='edgechromium' if os.name == 'nt' else None,
                              private_mode=False, storage_path=str(DATA_ROOT / 'work' / 'webview'))
                return 0
            except Exception as exc:
                logs.failure('startup', 'desktop_webview_unavailable', exc)
                app.request_exit = stopped.set
                # WebView2 is supplied by Windows on most machines. The same
                # packaged backend remains usable through the default browser.
        webbrowser.open(url)
        healthy()
        stopped.wait()
        return 0
    finally:
        app.close()
        server.shutdown()
        server.server_close()
        thread.join(5)
        logs.event('startup', 'desktop_stopped')


def main(argv=None):
    import multiprocessing
    multiprocessing.freeze_support()
    args_list = list(sys.argv[1:] if argv is None else argv)
    # Windowed PyInstaller intentionally gives us no console streams. Worker
    # results use files, while exceptions are recorded by the desktop logger.
    for stream in ('stdout', 'stderr'):
        if getattr(sys, stream) is None:
            setattr(sys, stream, open(os.devnull, 'w', encoding='utf-8'))
    if len(args_list) >= 2 and args_list[0] == '--worker':
        name = args_list[1]
        if name not in ('picker', 'separator'):
            return 2
        sys.argv = [f'videocut.{name}', *args_list[2:]]
        runpy.run_module(f'videocut.{name}', run_name='__main__')
        return 0
    parser = argparse.ArgumentParser(description='烽火剪辑 Windows 桌面版')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--browser', action='store_true')
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--self-test-ai', action='store_true')
    parser.add_argument('--result', type=Path)
    parser.add_argument('--update-health', type=Path)
    args = parser.parse_args(args_list)
    if args.self_test or args.self_test_ai:
        with tempfile.TemporaryDirectory(prefix='fenghuo-smoke-') as directory:
            os.environ['VIDEOCUT_DATA_DIR'] = directory
            try:
                report = _self_test(args.self_test_ai)
            except Exception:
                import traceback
                report = {'ok': False, 'error': traceback.format_exc()}
            if args.result:
                args.result.parent.mkdir(parents=True, exist_ok=True)
                args.result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(report, ensure_ascii=True))
            return 0 if report['ok'] else 1
    from . import logs
    from .paths import DATA_ROOT
    try:
        with InstanceLock(DATA_ROOT):
            return _run_window(args)
    except Exception as exc:
        logs.failure('startup', 'desktop_failed', exc)
        message(f'{exc}\n\n日志目录：{logs.store().directory}')
        return 1
    finally:
        logs.store().close()


if __name__ == '__main__':
    raise SystemExit(main())

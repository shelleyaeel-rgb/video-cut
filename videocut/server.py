from __future__ import annotations

# Initialize diagnostics before importing optional media dependencies, including
# when the older `python -m videocut.server` command is used.
if __name__ == '__main__':
    from .launcher import main as launch
    raise SystemExit(launch())

import argparse
import base64
import json
import mimetypes
import os
import re
import secrets
import socket
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, logs
from .audio_model import status as audio_status
from .demo import make_demo
from .detect import list_profiles, save_profile, prepare_template, mask_preview, saved_template_preview, update_template
from .media import ROOT, Cancelled, Context, local_file, preview_frame, probe, run, tool_path
from .pipeline import generate, validate
from .profile_test import test_profile, validate_profile_test


class App:
    def __init__(self):
        self.token = secrets.token_urlsafe(32)
        logs.store().add_secret(self.token)
        self.lock = threading.RLock()
        self.jobs = {}
        self.active = None
        self.media = {}
        self.closing = False
        self.client_reports = []

    def register(self, path):
        with self.lock:
            for ident, existing in self.media.items():
                if existing == path:
                    return ident
            ident = uuid.uuid4().hex
            self.media[ident] = path
            return ident

    def start(self, kind, spec=None):
        with self.lock:
            if self.closing:
                raise ValueError('服务正在关闭，请稍后重新启动。')
            if self.active:
                raise ValueError('已有任务正在处理，请等待完成或取消后重试。')
            ident = uuid.uuid4().hex
            self.active = ident
            job = {'id': ident, 'kind': kind, 'state': 'running', 'stage': '准备中', 'progress': 0, 'detail': '', 'result': None, 'error': None}
            self.jobs[ident] = job

            def update(stage, progress, detail):
                with self.lock:
                    job.update(stage=stage, progress=round(progress, 1), detail=detail)
            ctx = Context(update, task_id=ident)
            job['_ctx'] = ctx

            parent = logs.context()
            def worker():
                started = time.perf_counter()
                with logs.scope(**{**parent, 'task_id': ident}):
                    logs.event('tasks', 'task_started', kind=kind, settings=diagnostic_inputs(spec or {}))
                    try:
                        if kind == 'demo':
                            result = make_demo(ctx)
                        elif kind == 'profile_test':
                            result = test_profile(spec, ctx)
                        else:
                            result = generate(spec, ctx, ident)
                        ctx.check()
                        if kind == 'generate':
                            result['media_id'] = self.register(Path(result['output']))
                        with self.lock:
                            job.update(state='complete', progress=100, result=result)
                    except Cancelled as exc:
                        with self.lock:
                            job.update(state='cancelled', stage='已取消', error=str(exc))
                    except Exception as exc:
                        logs.failure('tasks', 'task_failed', exc, stage=ctx.stage)
                        with self.lock:
                            job.update(state='failed', stage='处理失败', error=logs.store().clean(str(exc)))
                    finally:
                        elapsed = round(time.perf_counter()-started, 3)
                        logs.event('tasks', 'task_finished', outcome=job['state'], last_stage=ctx.stage, elapsed_seconds=elapsed)
                        with self.lock:
                            job['elapsed_seconds'] = elapsed
                            self.active = None
            thread = threading.Thread(target=worker, daemon=True, name=f'task-{ident}')
            job['_thread'] = thread
            thread.start()
            return {'id': ident}

    def snapshot(self, ident):
        with self.lock:
            if ident not in self.jobs:
                raise ValueError('任务不存在。')
            return {k: v for k, v in self.jobs[ident].items() if not k.startswith('_')}

    def cancel(self, ident, reason='user'):
        with self.lock:
            job = self.jobs.get(ident)
            if job and job['state'] == 'running':
                logs.event('tasks', 'cancel_requested', task_id=ident, reason=reason, stage=job['_ctx'].stage)
                job['_ctx'].cancel()

    def close(self):
        with self.lock:
            self.closing = True
            threads = [job['_thread'] for job in self.jobs.values()]
            for ident in self.jobs:
                self.cancel(ident, 'shutdown')
        deadline = time.monotonic() + 5
        for thread in threads:
            thread.join(max(0, deadline-time.monotonic()))
        remaining = sum(thread.is_alive() for thread in threads)
        logs.event('runtime', 'workers_stopped', remaining=remaining)

    def report_client_error(self, data):
        # Per-server bound; all clients are local and authenticated.
        with self.lock:
            now = time.monotonic()
            self.client_reports = [t for t in self.client_reports if now-t < 60]
            if len(self.client_reports) >= 30:
                raise ValueError('页面错误上报过于频繁，请稍后重试。')
            self.client_reports.append(now)
        fields = {k: logs.store().clean(str(data.get(k, '')))[:limit] for k, limit in {'message': 2000, 'stack': 6000, 'source': 300, 'task_id': 64, 'client_id': 64, 'client_time': 64}.items()}
        logs.event('frontend', 'client_error', 'ERROR', **fields)
        return logs.store().status()


def diagnostic_inputs(data):
    """Only include configuration fields, never image data or request headers."""
    keys = ('video', 'music', 'path', 'audio', 'profile', 'time', 'rect', 'name', 'target', 'music_start', 'game_volume', 'music_volume', 'strength', 'threshold', 'audio_track', 'encoder', 'kind', 'id', 'voice_mode', 'music_voice_mode', 'game_audio_track', 'start', 'duration', 'profile_id', 'template_id', 'template_name', 'role', 'event_type', 'mask_mode', 'cutoff', 'sample_threshold', 'enabled')
    return {k: data[k] if isinstance(data[k], (int, float, bool, type(None))) else logs.store().clean(str(data[k]))[:2048] for k in keys if k in data}


class LocalHTTPServer(ThreadingHTTPServer):
    # HTTPServer enables SO_REUSEADDR by default. On Windows that can let a
    # second copy bind the same port instead of producing a startup error.
    allow_reuse_address = os.name != 'nt'

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'LocalVideoCut/0.1'

        def log_message(self, *args):
            pass  # Request URLs may contain local session tokens.

        def send_bytes(self, data, mime, code=200, extra=None):
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def json(self, obj, code=200):
            if code >= 400:
                obj = {**obj, 'error': logs.store().clean(obj.get('error', '操作失败')), 'request_id': self.request_id}
                logs.event('runtime', 'request_rejected', status=code, message=obj['error'])
            self.send_bytes(json.dumps(obj, ensure_ascii=False).encode('utf-8'), 'application/json; charset=utf-8', code)

        def allowed_host(self):
            return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}

        def authenticated(self, query=None):
            token = self.headers.get('X-Session-Token', '') or (query or {}).get('token', [''])[0]
            origin = self.headers.get('Origin')
            allowed = {f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'}
            return self.allowed_host() and (not origin or origin in allowed) and secrets.compare_digest(token, app.token)

        def do_GET(self):
            self.dispatch(self.get)

        def dispatch(self, action):
            self.request_id = uuid.uuid4().hex[:12]
            self.inputs = {}
            started = time.perf_counter()
            # The query string may contain a session token; only log the route.
            with logs.scope(request_id=self.request_id, method=self.command, route=urlparse(self.path).path):
                try:
                    action()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
                    logs.event('runtime', 'client_disconnected', 'WARNING', error_type=type(exc).__name__)
                except Exception as exc:
                    logs.failure('runtime', 'request_failed', exc, inputs=self.inputs, elapsed_seconds=round(time.perf_counter()-started, 3))
                    code = 400 if isinstance(exc, (ValueError, TypeError, KeyError, OSError, RuntimeError)) else 500
                    try:
                        self.json({'error': str(exc)}, code)
                    except OSError:
                        pass

        def get(self):
            if not self.allowed_host():
                self.json({'error': '仅接受本机访问。'}, 403)
                return
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path == '/':
                html = (ROOT / 'web' / 'index.html').read_text(encoding='utf-8').replace('__SESSION_TOKEN__', app.token)
                self.send_bytes(html.encode('utf-8'), 'text/html; charset=utf-8')
                return
            if parsed.path in ('/app.js', '/style.css'):
                file = ROOT / 'web' / parsed.path[1:]
                self.send_bytes(file.read_bytes(), 'text/javascript; charset=utf-8' if file.suffix == '.js' else 'text/css; charset=utf-8')
                return
            if not self.authenticated(query):
                self.json({'error': '本地会话已失效，请刷新页面。'}, 403)
                return
            if parsed.path == '/api/state':
                self.json({'version': __version__, 'profiles': list_profiles(), 'active': app.active, 'output_dir': str(ROOT / 'outputs'), 'logs': logs.store().status(), 'audio': audio_status()})
            elif parsed.path == '/api/job':
                self.json(app.snapshot(query.get('id', [''])[0]))
            elif parsed.path == '/api/template-image':
                self.send_bytes(saved_template_preview(query.get('profile', [''])[0], query.get('template', [''])[0]), 'image/png')
            elif parsed.path == '/api/logs.zip':
                self.send_bytes(logs.store().archive(), 'application/zip', extra={'Content-Disposition': 'attachment; filename="videocut-logs.zip"'})
            elif parsed.path == '/media':
                self.serve_media(query.get('id', [''])[0])
            else:
                self.json({'error': '接口不存在。'}, 404)

        def serve_media(self, ident):
            with app.lock:
                path = app.media.get(ident)
            if path is None or not path.is_file():
                self.json({'error': '媒体不存在。'}, 404)
                return
            size = path.stat().st_size
            start, end, code = 0, size-1, 200
            value = self.headers.get('Range')
            if value:
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', value)
                if not match or not any(match.groups()):
                    self.send_bytes(b'', 'application/octet-stream', 416, {'Content-Range': f'bytes */{size}'})
                    return
                a, b = match.groups()
                if not a:
                    start = max(0, size-int(b))
                else:
                    start = int(a)
                    end = min(end, int(b)) if b else end
                if start > end or start >= size:
                    self.send_bytes(b'', 'application/octet-stream', 416, {'Content-Range': f'bytes */{size}'})
                    return
                code = 206
            self.send_response(code)
            self.send_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'video/mp4')
            self.send_header('Content-Length', str(end-start+1))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Cache-Control', 'no-store')
            if code == 206:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.end_headers()
            with path.open('rb') as f:
                f.seek(start)
                remaining = end-start+1
                while remaining > 0:
                    chunk = f.read(min(256*1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def do_POST(self):
            self.dispatch(self.post)

        def post(self):
            length = int(self.headers.get('Content-Length', 0))
            if not self.authenticated():
                # Consume bounded input before closing, so Windows can deliver
                # the 403 response instead of resetting a socket with unread data.
                if 0 < length <= 65536:
                    self.rfile.read(length)
                self.json({'error': '请求未授权，请刷新本机页面。'}, 403)
                return
            if not 0 < length <= 65536:
                raise ValueError('请求大小无效。')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('请求格式无效。')
            self.inputs = diagnostic_inputs(data)
            route = urlparse(self.path).path
            if route == '/api/probe':
                path = local_file(data.get('path', ''), data.get('audio', False))
                result = probe(path)
                result['media_id'] = app.register(path)
                self.json(result)
            elif route == '/api/frame':
                path = local_file(data.get('video', ''))
                self.json({'image': 'data:image/png;base64,' + base64.b64encode(preview_frame(path, float(data.get('time', 0)))).decode('ascii')})
            elif route == '/api/profile':
                path = local_file(data.get('video', ''))
                with app.lock:
                    if app.active:
                        raise ValueError('任务运行中，请完成或取消后修改配置。')
                    result = save_profile(path, float(data.get('time', 0)), data.get('rect', []), data.get('name', ''), .82,
                                          profile_id=data.get('profile_id'), template_name=data.get('template_name', ''),
                                          role=data.get('role', 'trigger'), event_type=data.get('event_type', 'kill'),
                                          mask_mode=data.get('mask_mode', 'edges'), cutoff=data.get('cutoff', 180),
                                          erase=data.get('erase', []), sample_threshold=data.get('sample_threshold'))
                self.json(result)
            elif route == '/api/template-preview':
                path = local_file(data.get('video', ''))
                template, mask, _, _ = prepare_template(path, float(data.get('time', 0)), data.get('rect', []),
                                                        data.get('mask_mode', 'edges'), data.get('cutoff', 180), data.get('erase', []))
                self.json({'image': 'data:image/png;base64,' + base64.b64encode(mask_preview(template, mask)).decode('ascii')})
            elif route == '/api/template-update':
                with app.lock:
                    if app.active:
                        raise ValueError('任务运行中，请完成或取消后修改配置。')
                    self.json(update_template(data.get('profile'), data.get('template_id'), data.get('enabled'), data.get('threshold')))
            elif route == '/api/demo':
                self.json(app.start('demo'), 202)
            elif route == '/api/generate':
                validate(data)
                self.json(app.start('generate', data), 202)
            elif route == '/api/test-profile':
                validate_profile_test(data)
                self.json(app.start('profile_test', data), 202)
            elif route == '/api/cancel':
                app.cancel(data.get('id'))
                self.json({'ok': True})
            elif route == '/api/pick':
                output = run([os.sys.executable, '-m', 'videocut.picker', 'music' if data.get('kind') == 'music' else 'video'], timeout=180)
                self.json(json.loads(output))
            elif route == '/api/open-logs':
                path = logs.store().directory
                path.mkdir(parents=True, exist_ok=True)
                opened = False
                try:
                    if os.name == 'nt':
                        os.startfile(path)
                        opened = True
                except OSError as exc:
                    logs.failure('runtime', 'open_logs_failed', exc)
                self.json({'path': str(path), 'opened': opened})
            elif route == '/api/open-output':
                path = ROOT / 'outputs'
                path.mkdir(parents=True, exist_ok=True)
                if os.name == 'nt':
                    os.startfile(path)
                self.json({'path': str(path)})
            elif route == '/api/client-error':
                status = app.report_client_error(data)
                self.json({'ok': not status['write_error'], 'request_id': self.request_id, 'logs': status}, 503 if status['write_error'] else 200)
            else:
                self.json({'error': '接口不存在。'}, 404)

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--open', action='store_true')
    args = parser.parse_args(argv)
    tool_path('ffmpeg')
    tool_path('ffprobe')
    app = App()
    try:
        server = LocalHTTPServer(('127.0.0.1', args.port), handler_for(app))
    except OSError as exc:
        raise RuntimeError(f'端口 {args.port} 无法使用，请关闭已有窗口或使用 --port 指定其他端口。{exc}') from exc
    server.daemon_threads = True
    url = f'http://127.0.0.1:{server.server_port}'
    print(f'Local VideoCut {__version__}: {url}', flush=True)
    logs.event('startup', 'server_ready', url=url, pid=os.getpid())
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
        server.server_close()
        logs.event('startup', 'server_stopped')

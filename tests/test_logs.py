import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer

from videocut import logs, launcher
from videocut.media import ROOT, Cancelled, Context, run
from videocut.server import App, LocalHTTPServer, handler_for


class IsolatedLogs(unittest.TestCase):
    def setUp(self):
        (ROOT/'work').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='log-test-', dir=ROOT/'work')
        self.directory = Path(self.tmp.name)
        self.store = logs.LogStore(self.directory/'logs')
        self.manager = patch.object(logs, '_manager', self.store)
        self.manager.start()

    def tearDown(self):
        self.store.close()
        self.manager.stop()
        self.tmp.cleanup()

    def records(self, category):
        path = self.store.directory/f'{category}.log'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []

    def script(self, source):
        path = self.directory/'child.py'
        path.write_text(source, encoding='utf-8')
        return [sys.executable, str(path)]


class LogTests(IsolatedLogs):
    def test_redacts_secrets_in_fields_urls_and_tracebacks(self):
        secret = 'session-private-value-123'
        self.store.add_secret(secret)
        try:
            raise ValueError(f'{secret} password=letmein https://user:pass@example.com token=hidden Bearer example-bearer')
        except ValueError as exc:
            logs.failure('runtime', 'example', exc, nested={'authorization':'another-private-value','path':'D:/录像.mp4'}, session_token=secret)
        raw = (self.store.directory/'runtime.log').read_text(encoding='utf-8')
        for value in (secret, 'letmein', 'user:pass', 'hidden', 'example-bearer', 'another-private-value'):
            self.assertNotIn(value, raw)
        self.assertIn('Traceback', raw)
        self.assertIn('D:/录像.mp4', raw)

    def test_rotation_retains_new_records_and_bounds_files(self):
        self.store.policy = {'runtime':(1024,2)}
        for i in range(100):
            logs.event('runtime', 'rotation', index=i, message='中文测试'*20)
        files = list(self.store.directory.glob('runtime.log*'))
        self.assertEqual(len(files), 3)
        self.assertLess(sum(f.stat().st_size for f in files), 4200)
        self.assertEqual(self.records('runtime')[-1]['index'], 99)
        self.assertNotIn('"index": 0,', ''.join(f.read_text(encoding='utf-8') for f in files))

    def test_write_failure_reports_health_without_breaking_work(self):
        with patch.object(Path, 'mkdir', side_effect=PermissionError('read only')):
            self.assertFalse(logs.event('runtime', 'write_failed'))
        self.assertTrue(self.store.status()['write_error'])

    def test_archive_contains_only_redacted_known_logs(self):
        self.store.add_secret('private-example-token')
        logs.event('tasks','export_test',message='private-example-token')
        (self.store.directory/'unrelated.txt').write_text('do not export')
        with zipfile.ZipFile(io.BytesIO(self.store.archive())) as archive:
            self.assertEqual(archive.namelist(), ['tasks.log'])
            self.assertNotIn(b'private-example-token', archive.read('tasks.log'))

    def test_long_stderr_and_binary_stdout(self):
        expected = 'BEGIN-' + '诊断'*12000 + '-END\n'
        args = self.script(f'import sys\nsys.stderr.buffer.write({expected.encode()!r})\nsys.stdout.buffer.write(b"BINARY-MEDIA-OUTPUT\\x00\\xff")\n')
        self.assertEqual(run(args), b'BINARY-MEDIA-OUTPUT\x00\xff')
        rows = self.records('ffmpeg')
        self.assertEqual(''.join(r['text'] for r in rows if r['event']=='stderr'), expected)
        self.assertEqual(rows[-1]['outcome'], 'complete')
        self.assertNotIn('BINARY-MEDIA-OUTPUT', (self.store.directory/'ffmpeg.log').read_text(encoding='utf-8'))
        self.assertEqual(len({r['command_id'] for r in rows}), 1)

    def test_failed_process_retains_early_diagnostics(self):
        args = self.script('import sys\nsys.stderr.write("EARLY-CAUSE\\n"+"x"*8000+"\\nLATE-ERROR\\n")\nsys.exit(7)\n')
        with self.assertRaisesRegex(RuntimeError, 'LATE-ERROR'):
            run(args, Context(task_id='failure-task'))
        rows = self.records('ffmpeg')
        self.assertIn('EARLY-CAUSE', ''.join(r.get('text','') for r in rows))
        self.assertEqual(rows[-1]['returncode'], 7)
        self.assertTrue(all(r['task_id']=='failure-task' for r in rows))

    def test_timeout_spawn_failure_and_cancellation(self):
        with self.assertRaisesRegex(RuntimeError, '超时'):
            run(self.script('import sys,time\nprint("BEFORE-TIMEOUT",file=sys.stderr,flush=True)\ntime.sleep(30)\n'), timeout=.3)
        self.assertEqual(self.records('ffmpeg')[-1]['outcome'], 'timeout')
        self.assertIn('BEFORE-TIMEOUT', ''.join(r.get('text','') for r in self.records('ffmpeg')))
        with self.assertRaises(OSError):
            run([self.directory/'missing-program.exe'])
        self.assertEqual(self.records('ffmpeg')[-1]['event'], 'spawn_failed')
        ctx = Context(task_id='cancel-test')
        results = []
        def worker():
            try:
                run(self.script('import time\ntime.sleep(30)\n'), ctx)
            except Exception as exc:
                results.append(exc)
        thread = threading.Thread(target=worker)
        thread.start()
        deadline = time.monotonic()+3
        while not ctx.processes and thread.is_alive() and time.monotonic()<deadline:
            time.sleep(.01)
        ctx.cancel()
        thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(results[0], Cancelled)
        self.assertEqual(self.records('ffmpeg')[-1]['outcome'], 'cancelled')
        self.assertFalse(ctx.processes)

    def test_cancel_between_spawn_and_attach(self):
        ctx = Context()
        def attach(process):
            ctx.cancelled.set()
            Context.attach(ctx, process)
        with patch.object(ctx, 'attach', side_effect=attach):
            with self.assertRaises(Cancelled):
                run(self.script('import time\ntime.sleep(30)\n'), ctx)
        self.assertEqual(self.records('ffmpeg')[-1]['outcome'], 'cancelled')


class TaskLogTests(IsolatedLogs):
    def test_task_success_and_failure_have_ids_stage_and_traceback(self):
        app = App()
        def success(ctx):
            ctx.update('测试阶段', 50)
            return {'ready':True}
        with patch('videocut.server.make_demo', side_effect=success):
            ident = app.start('demo')['id']
            app.jobs[ident]['_thread'].join(3)
        self.assertEqual(app.snapshot(ident)['state'], 'complete')
        def failure(ctx):
            ctx.update('失败之前', 12)
            raise ValueError(f'test-only {app.token}')
        with patch('videocut.server.make_demo', side_effect=failure):
            failed = app.start('demo')['id']
            app.jobs[failed]['_thread'].join(3)
        rows = self.records('tasks')
        self.assertEqual({r['task_id'] for r in rows}, {ident, failed})
        row = next(r for r in rows if r['event']=='task_failed')
        self.assertEqual(row['stage'], '失败之前')
        self.assertIn('Traceback', row['traceback'])
        self.assertNotIn(app.token, json.dumps(rows))
        self.assertNotIn(app.token, app.snapshot(failed)['error'])
        self.assertEqual(rows[-1]['outcome'], 'failed')
        self.assertIn('elapsed_seconds', rows[-1])
        app.close()

    def test_shutdown_cancels_and_waits_for_task_log(self):
        app = App()
        def waiting(ctx):
            ctx.update('等待取消', 20)
            ctx.cancelled.wait(3)
            ctx.check()
        with patch('videocut.server.make_demo', side_effect=waiting):
            ident = app.start('demo')['id']
            app.close()
        self.assertFalse(app.jobs[ident]['_thread'].is_alive())
        self.assertEqual(app.snapshot(ident)['state'], 'cancelled')
        self.assertEqual(self.records('tasks')[-1]['outcome'], 'cancelled')
        self.assertTrue(any(r['event']=='cancel_requested' for r in self.records('tasks')))


class HttpLogTests(IsolatedLogs):
    def setUp(self):
        super().setUp()
        self.app = App()
        self.server = ThreadingHTTPServer(('127.0.0.1',0), handler_for(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.app.close()
        super().tearDown()

    def request(self, path, data=None, auth=True):
        request = urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}'+path, data=json.dumps(data).encode() if data is not None else None, headers={'X-Session-Token':self.app.token} if auth else {})
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    def test_invalid_input_has_correlated_diagnostics(self):
        code, body = self.request('/api/probe', {'path':str(self.directory/'missing.mp4')})
        self.assertEqual(code, 400)
        rows = self.records('runtime')
        self.assertEqual(rows[0]['request_id'], body['request_id'])
        self.assertEqual(rows[0]['route'], '/api/probe')
        self.assertIn('missing.mp4', rows[0]['inputs']['path'])
        self.assertIn('Traceback', rows[0]['traceback'])
        code, body = self.request('/missing?token='+self.app.token)
        self.assertEqual(code, 404)
        self.assertNotIn(self.app.token, (self.store.directory/'runtime.log').read_text(encoding='utf-8'))

    def test_unexpected_request_error_and_profile_failure_logged(self):
        with patch('videocut.server.list_profiles', side_effect=AssertionError('unexpected-test')):
            code, body = self.request('/api/state')
        self.assertEqual(code, 500)
        self.assertIn('request_id', body)
        with patch('videocut.server.local_file', return_value=self.directory/'video.mp4'), patch('videocut.server.save_profile', side_effect=ValueError('invalid rectangle')):
            code, body = self.request('/api/profile', {'video':'test.mp4','rect':[0,0,0,0]})
        self.assertEqual(code, 400)
        self.assertEqual(self.records('runtime')[-2]['request_id'], body['request_id'])

    def test_frontend_auth_redaction_limits_and_log_health(self):
        self.assertEqual(self.request('/api/client-error', {'message':'test'}, auth=False)[0], 403)
        self.assertFalse(self.records('frontend'))
        code, body = self.request('/api/client-error', {'client_id':'ui-123','message':'x'*1990+self.app.token,'stack':'token='+self.app.token,'ignored':'not-saved'})
        self.assertEqual(code, 200)
        row = self.records('frontend')[-1]
        self.assertEqual(row['client_id'], 'ui-123')
        self.assertNotIn(self.app.token[:10], row['message'])
        self.assertNotIn('ignored', row)
        self.assertLessEqual(len(row['message']), 2000)
        self.app.client_reports = [time.monotonic()]*30
        self.assertEqual(self.request('/api/client-error', {'message':'rate-test'})[0], 400)
        self.store.failed = True
        self.assertTrue(self.request('/api/state')[1]['logs']['write_error'])

    def test_open_logs_opens_only_fixed_directory(self):
        with patch('videocut.server.os.startfile', create=True) as opened:
            code, body = self.request('/api/open-logs', {'path':'Z:/untrusted'})
        self.assertEqual(code, 200)
        self.assertEqual(body['path'], str(self.store.directory))
        if os.name == 'nt':
            opened.assert_called_once_with(self.store.directory)

    def test_open_logs_falls_back_to_path_when_shell_is_unavailable(self):
        with patch('videocut.server.os.startfile', side_effect=PermissionError('shell unavailable'), create=True):
            code, body = self.request('/api/open-logs', {})
        self.assertEqual(code, 200)
        self.assertFalse(body['opened'])
        self.assertEqual(body['path'], str(self.store.directory))
        if os.name == 'nt':
            self.assertEqual(self.records('runtime')[-1]['event'], 'open_logs_failed')

    def test_download_logs_requires_auth_and_returns_valid_zip(self):
        self.assertEqual(self.request('/api/logs.zip', auth=False)[0],403)
        request = urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}/api/logs.zip',headers={'X-Session-Token':self.app.token})
        with urllib.request.urlopen(request,timeout=5) as response:
            self.assertEqual(response.headers['Content-Type'], 'application/zip')
            self.assertIn('attachment', response.headers['Content-Disposition'])
            with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                self.assertIn('runtime.log', archive.namelist())
                self.assertNotIn(self.app.token.encode(), archive.read('runtime.log'))


class StartupLogTests(IsolatedLogs):
    @unittest.skipUnless(os.name == 'nt', 'Windows launcher')
    def test_missing_python_is_recorded_before_python_can_start(self):
        fixture = self.directory/'bootstrap-case'
        (fixture/'scripts').mkdir(parents=True)
        shutil.copyfile(ROOT/'start.cmd', fixture/'start.cmd')
        shutil.copyfile(ROOT/'scripts'/'bootstrap-error.ps1', fixture/'scripts'/'bootstrap-error.ps1')
        result = subprocess.run(['cmd.exe','/d','/c',str(fixture/'start.cmd')], cwd=fixture, input=b'', capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        path = fixture/'work'/'logs'/'bootstrap.log'
        self.assertTrue(path.exists(), result.stdout.decode(errors='replace')+result.stderr.decode(errors='replace'))
        record = json.loads(path.read_text(encoding='utf-8-sig').strip())
        self.assertEqual(record['event'], 'python_unavailable')

    def test_missing_dependency_and_tool(self):
        with patch('videocut.launcher.importlib.import_module', side_effect=ImportError('missing-cv2')), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(launcher.main([]), 1)
        self.assertIn('missing-cv2', self.records('startup')[-1]['traceback'])
        with patch('videocut.media.tool_path', side_effect=RuntimeError('missing-ffmpeg')), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(launcher.main([]), 1)
        self.assertIn('missing-ffmpeg', self.records('startup')[-1]['traceback'])

    def test_real_port_conflict_is_logged(self):
        with LocalHTTPServer(('127.0.0.1',0), handler_for(App())) as occupied:
            port = occupied.server_port
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(launcher.main(['--port',str(port)]), 1)
        failure = self.records('startup')[-1]
        self.assertEqual(failure['event'], 'startup_failed')
        self.assertIn(str(port), failure['error'])
        self.assertIn('Traceback', failure['traceback'])


@unittest.skipUnless(os.environ.get('VIDEOCUT_INTEGRATION')=='1', 'Requires real FFmpeg')
class StreamLogTests(IsolatedLogs):
    def test_actual_corrupt_media_is_logged(self):
        from videocut.media import probe
        broken = self.directory/'broken.mp4'
        broken.write_bytes(b'this is not an mp4')
        with self.assertRaises(RuntimeError):
            probe(broken)
        rows = self.records('ffmpeg')
        self.assertTrue(any(r['event']=='stderr' for r in rows))
        self.assertEqual(rows[-1]['outcome'], 'failed')

    def test_detection_stream_failure_is_logged(self):
        from videocut.demo import make_demo
        from videocut.detect import detect_events, get_profile
        from videocut.media import probe
        spec = make_demo()
        meta = probe(spec['video'])
        with patch('videocut.detect.probe', return_value=meta):
            with self.assertRaises(RuntimeError):
                detect_events(self.directory/'missing-stream.mp4', get_profile('demo'), Context(task_id='stream-test'))
        rows = [r for r in self.records('ffmpeg') if r.get('task_id')=='stream-test']
        self.assertTrue(any(r['event']=='stderr' for r in rows))
        self.assertEqual(rows[-1]['outcome'], 'failed')


if __name__ == '__main__':
    unittest.main()

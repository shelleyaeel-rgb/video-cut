"""Authenticated HTTP boundaries for recording and trusted render snapshots."""
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

from videocut.project_store import ProjectStore
from videocut.server import App, handler_for
from videocut.timeline import RecordingSource


class RecordingHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root / 'projects.sqlite')
        self.source = self.root / 'original.mkv'
        self.source.write_bytes(b'fixture source')
        self.session = self.store.create_session(source_path=self.source)
        self.marker = self.store.upsert_marker(self.session['id'], {'time': 3, 'end': 3.5, 'source': 'manual'})
        self.store.finalize_asset(self.session['id'], metadata={'duration': 10., 'width': 1920, 'height': 1080, 'audio_tracks': []})
        self.app = App()
        self.recorder = SimpleNamespace(
            store=self.store, busy=False, close=Mock(), start=Mock(return_value={'state': 'starting', 'session_id': 'started'}),
            stop=Mock(return_value={'state': 'stopping'}), manual_marker=Mock(return_value=self.marker),
            status=Mock(return_value={'state': 'idle', 'busy': False}),
            capabilities=Mock(return_value={'available': False}), sources=Mock(return_value=[]))
        self.app._recording = self.recorder
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.app))
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join()
        self.app.active = None
        self.app.close()
        self.temporary.cleanup()

    def request(self, path, body=None, *, authenticated=True, origin=None):
        headers = {'X-Session-Token': self.app.token} if authenticated else {}
        if origin is not None:
            headers['Origin'] = origin
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url+path, data=data, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            payload = response.read()
            return response.status, json.loads(payload) if payload else None

    def test_all_recording_routes_require_session_token(self):
        for route in ('/api/recording', '/api/recordings', '/api/recording/project?id=x',
                      '/api/recording/sources', '/api/recording/capabilities'):
            with self.subTest(route=route):
                self.assertEqual(self.request(route, authenticated=False)[0], 403)
        for action in ('start', 'stop', 'mark', 'selection', 'add-marker', 'preview', 'recover', 'rescan', 'generate'):
            with self.subTest(action=action):
                self.assertEqual(self.request('/api/recording/'+action, {}, authenticated=False)[0], 403)
        self.recorder.start.assert_not_called()
        self.recorder.stop.assert_not_called()

    def test_cross_origin_cannot_start_or_stop_recording(self):
        for action in ('start', 'stop', 'mark', 'selection'):
            self.assertEqual(self.request('/api/recording/'+action, {}, origin='https://example.com')[0], 403)
        self.recorder.start.assert_not_called()
        self.recorder.stop.assert_not_called()

    def test_export_uses_store_snapshot_and_ignores_browser_events_and_video(self):
        output = self.root / 'export.mp4'
        output.write_bytes(b'output fixture')
        with patch('videocut.server.generate', return_value={'output': str(output)}) as render:
            status, result = self.request('/api/recording/generate', {
                'session_id': self.session['id'], 'mode': 'montage', 'events': [{'time': 999}],
                'recording_source': {'video': 'forged'},
                'options': {'video': 'forged.mp4', 'events': [{'time': 999}], 'target': 5}})
            self.assertEqual(status, 202)
            self.app.jobs[result['id']]['_thread'].join(3)
            self.assertEqual(self.app.snapshot(result['id'])['state'], 'complete')
            source = render.call_args.kwargs['recording_source']
            self.assertIsInstance(source, RecordingSource)
            self.assertEqual(source.video, str(self.source.resolve()))
            self.assertEqual(source.selected_ids, (self.marker['id'],))
            self.assertEqual(source.candidates()[0]['time'], 3)
            self.assertNotIn('events', render.call_args.args[0])

    def test_recording_blocks_render_and_review_and_active_render_blocks_recording(self):
        self.recorder.busy = True
        for action, body in (
            ('generate', {'session_id': self.session['id']}),
            ('selection', {'session_id': self.session['id'], 'selected_ids': []}),
            ('add-marker', {'session_id': self.session['id'], 'time': 4}),
            ('preview', {'session_id': self.session['id'], 'start': 1, 'end': 4})):
            with self.subTest(action=action):
                self.assertEqual(self.request('/api/recording/'+action, body)[0], 400)
        self.recorder.busy = False
        self.app.active = 'existing-render'
        self.assertEqual(self.request('/api/recording/start', {})[0], 400)
        self.recorder.start.assert_not_called()
        self.app.active = None

    def test_selection_rejects_foreign_ids_and_stale_revision(self):
        for body in ({'session_id': self.session['id'], 'selected_ids': ['foreign']},
                     {'session_id': self.session['id'], 'selected_ids': [], 'revision': -1}):
            self.assertEqual(self.request('/api/recording/selection', body)[0], 400)
        self.assertEqual(self.store.get_session(self.session['id'])['selection'], [self.marker['id']])

    def test_selection_without_padding_preserves_saved_boundaries(self):
        self.store.update_selection(self.session['id'], [self.marker['id']], pre=8, post=4)
        status, session = self.request('/api/recording/selection', {'session_id': self.session['id'], 'selected_ids': []})
        self.assertEqual(status, 200)
        self.assertEqual(session['settings']['pre'], 8)
        self.assertEqual(session['settings']['post'], 4)

    def test_ready_source_required_and_bad_manual_times_rejected(self):
        incomplete = self.store.create_session(source_path=self.source)
        self.assertEqual(self.request('/api/recording/generate', {'session_id': incomplete['id']})[0], 400)
        for position in (-1, 10, float('nan'), float('inf')):
            self.assertEqual(self.request('/api/recording/add-marker', {'session_id': self.session['id'], 'time': position})[0], 400)
        self.assertEqual(len(self.store.get_session(self.session['id'])['markers']), 1)

    def test_cancel_render_never_stops_recorder(self):
        started, release = threading.Event(), threading.Event()
        def render(spec, ctx, ident, **kwargs):
            started.set()
            release.wait(3)
            ctx.check()
            return {'output': str(self.source)}
        with patch('videocut.server.generate', side_effect=render):
            status, result = self.request('/api/recording/generate', {'session_id': self.session['id']})
            self.assertEqual(status, 202)
            self.assertTrue(started.wait(2))
            self.assertEqual(self.request('/api/cancel', {'id': result['id']})[0], 200)
            release.set()
            self.app.jobs[result['id']]['_thread'].join(3)
            self.assertEqual(self.app.snapshot(result['id'])['state'], 'cancelled')
        self.recorder.stop.assert_not_called()


if __name__ == '__main__':
    unittest.main()

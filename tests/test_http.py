import json
import threading
import unittest
import tempfile
from pathlib import Path
import cv2
import numpy as np
import urllib.error
import urllib.request
from unittest.mock import patch
from http.server import ThreadingHTTPServer

from videocut.media import ROOT
from videocut.server import App, handler_for


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=App()
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(cls.app))
        cls.url=f'http://127.0.0.1:{cls.server.server_port}'
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def request(self,path,headers=None,body=None):
        data=json.dumps(body).encode() if body is not None else None
        req=urllib.request.Request(self.url+path,data=data,headers=headers or {})
        try:return urllib.request.urlopen(req,timeout=5)
        except urllib.error.HTTPError as e:return e
    def test_api_requires_token(self):
        with self.request('/api/state') as r: self.assertEqual(r.status,403)
    def test_ui_and_authenticated_api(self):
        with self.request('/') as r:
            self.assertIn(self.app.token.encode(),r.read())
            self.assertEqual(r.headers['X-Frame-Options'],'DENY')
        with self.request('/api/state',{'X-Session-Token':self.app.token}) as r:self.assertEqual(r.status,200)
    def test_reject_cross_origin_post(self):
        with self.request('/api/demo',{'X-Session-Token':self.app.token,'Origin':'https://example.com'},{}) as r:self.assertEqual(r.status,403)
    def test_reject_foreign_host(self):
        with self.request('/',{'Host':'example.com'}) as r:self.assertEqual(r.status,403)
    def test_range_and_invalid_range(self):
        # Register a known non-sensitive test file; media routes never accept filesystem paths.
        path=ROOT/'work'/'http-fixture.bin'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'0123456789')
        ident=self.app.register(path)
        url=f'/media?id={ident}&token={self.app.token}'
        with self.request(url,{'Range':'bytes=2-5'}) as r:
            self.assertEqual(r.status,206);self.assertEqual(r.read(),b'2345')
        with self.request(url,{'Range':'bytes=-3'}) as r:self.assertEqual(r.read(),b'789')
        with self.request(url,{'Range':'bytes=100-'}) as r:self.assertEqual(r.status,416)
    def test_invalid_input_fails_before_job(self):
        with self.request('/api/generate',{'X-Session-Token':self.app.token},{'video':'Z:/missing.mp4'}) as r:self.assertEqual(r.status,400)
        self.assertIsNone(self.app.active)

    def test_update_routes_require_authentication_and_source_cannot_install(self):
        headers = {'X-Session-Token': self.app.token}
        for route in ('/api/update', '/api/preferences'):
            with self.request(route) as r:
                self.assertEqual(r.status, 403)
        for route in ('/api/update/check', '/api/update/download', '/api/update/install', '/api/quit', '/api/preferences'):
            with self.request(route, body={}) as r:
                self.assertEqual(r.status, 403)
        with patch.object(self.app.updater, 'check', return_value={'state': 'checking'}) as check:
            with self.request('/api/update', headers) as r:
                self.assertEqual(r.status, 200)
            check.assert_not_called()
            with self.request('/api/update/check', headers, {}) as r:
                self.assertEqual(r.status, 202)
                self.assertEqual(json.load(r)['state'], 'checking')
            check.assert_called_once()
        with self.request('/api/update/install', headers, {}) as r:
            self.assertEqual(r.status, 400)
        self.assertFalse(self.app.closing)

    def test_profile_test_auth_and_validation(self):
        with self.request('/api/test-profile', body={'video':'missing.mp4'}) as r:
            self.assertEqual(r.status, 403)
        with self.request('/api/test-profile', {'X-Session-Token':self.app.token}, {'video':'Z:/missing.mp4'}) as r:
            self.assertEqual(r.status, 400)
        self.assertIsNone(self.app.active)

    def test_profile_test_runs_as_cancellable_job_without_montage(self):
        result = {'video':'test.mp4', 'events':[], 'start':0, 'duration':30}
        spec = {'video':'test.mp4', 'profile':'demo', 'duration':30}
        media_before = dict(self.app.media)
        with patch('videocut.server.validate_profile_test'), patch('videocut.server.test_profile', return_value=result) as test, patch('videocut.server.generate') as generate:
            with self.request('/api/test-profile', {'X-Session-Token':self.app.token}, spec) as r:
                self.assertEqual(r.status, 202)
                ident = json.load(r)['id']
            self.app.jobs[ident]['_thread'].join(2)
            snapshot = self.app.snapshot(ident)
            self.assertEqual(snapshot['state'], 'complete')
            self.assertEqual(snapshot['kind'], 'profile_test')
            self.assertEqual(snapshot['result'], result)
            self.assertEqual(test.call_args.args[0], spec)
            generate.assert_not_called()
        self.assertEqual(self.app.media, media_before)

    def test_template_preview_append_update_image_and_busy_guard(self):
        image = np.full((100,160),30,np.uint8)
        cv2.putText(image,'K',(30,70),cv2.FONT_HERSHEY_SIMPLEX,1.5,240,3)
        png = cv2.imencode('.png',image)[1].tobytes()
        headers = {'X-Session-Token':self.app.token}
        spec = {'video':'fixture.mp4','time':1,'rect':[.1,.2,.5,.6],'name':'HTTP配置','template_name':'共同图标'}
        with tempfile.TemporaryDirectory(prefix='videocut-http-') as directory, patch('videocut.detect.PROFILES',Path(directory)), patch('videocut.server.local_file',return_value=Path('fixture.mp4')), patch('videocut.detect.preview_frame',return_value=png):
            with self.request('/api/template-preview',body=spec) as r:self.assertEqual(r.status,403)
            with self.request('/api/template-preview',headers,spec) as r:
                self.assertEqual(r.status,200);self.assertTrue(json.load(r)['image'].startswith('data:image/png;base64,'))
            with self.request('/api/profile',headers,spec) as r:
                self.assertEqual(r.status,200);profile=json.load(r)
            with self.request('/api/profile',headers,{**spec,'profile_id':profile['id'],'role':'npc','event_type':'all','template_name':'小兵标记'}) as r:
                self.assertEqual(r.status,200);updated=json.load(r)
            self.assertEqual(len(updated['templates']),2)
            helper=updated['templates'][-1]
            with self.request('/api/template-update',headers,{'profile':profile['id'],'template_id':helper['id'],'enabled':False,'threshold':.9}) as r:
                self.assertEqual(r.status,200);self.assertFalse(json.load(r)['templates'][-1]['enabled'])
            image_url=f"/api/template-image?profile={profile['id']}&template={helper['id']}"
            with self.request(image_url) as r:self.assertEqual(r.status,403)
            with self.request(image_url,headers) as r:
                self.assertEqual(r.status,200);self.assertEqual(r.headers['Content-Type'],'image/png')
            self.app.active='test-busy'
            try:
                with self.request('/api/profile',headers,spec) as r:self.assertEqual(r.status,400)
                with self.request('/api/template-update',headers,{'profile':profile['id'],'template_id':helper['id'],'enabled':True}) as r:self.assertEqual(r.status,400)
            finally:self.app.active=None


if __name__=='__main__':unittest.main()

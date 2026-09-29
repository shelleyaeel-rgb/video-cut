import json
import threading
import unittest
import urllib.error
import urllib.request
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


if __name__=='__main__':unittest.main()

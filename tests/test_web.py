import json
import threading
import unittest
import urllib.error
import urllib.request

from helpers import make_monitor

from pkgwatch.web import serve


class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = make_monitor()
        cls.httpd = serve(cls.m, port=0)
        cls.port = cls.httpd.server_address[1]
        # Host check uses the real port
        cls.httpd.RequestHandlerClass = __import__("pkgwatch.web", fromlist=["make_handler"]).make_handler(cls.m, cls.port)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def call(self, method, path, body=None, headers=None):
        h = {"Content-Type": "application/json", "X-PkgWatch": "1"}
        h.update(headers or {})
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, headers=h,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_index_and_state(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as r:
            self.assertIn(b"PkgWatch", r.read())
        code, st = self.call("GET", "/api/state")
        self.assertEqual(code, 200)
        self.assertIn("packages", st)
        self.assertTrue(any(c["id"] == "asl" for c in st["carriers"]))

    def test_csrf_and_host_guard(self):
        code, _ = self.call("POST", "/api/ack", {}, headers={"X-PkgWatch": ""})
        self.assertEqual(code, 403)
        code, _ = self.call("GET", "/api/state", headers={"Host": "evil.example:80"})
        self.assertEqual(code, 403)

    def test_add_check_remove(self):
        code, p = self.call("POST", "/api/packages", {"input": "SIM-OFD,DEL", "name": "Test"})
        self.assertEqual(code, 200, p)
        self.assertEqual(p["carrier"], "sim")
        code, _ = self.call("POST", f"/api/packages/{p['id']}/check")
        self.assertEqual(code, 200)
        code, _ = self.call("DELETE", f"/api/packages/{p['id']}")
        self.assertEqual(code, 200)
        self.assertIsNone(self.m.store.get(p["id"]))

    def test_bad_input(self):
        code, err = self.call("POST", "/api/packages", {"input": ""})
        self.assertEqual(code, 400)
        code, err = self.call("POST", "/api/settings", {"nope": 1})
        self.assertEqual(code, 400)
        code, err = self.call("POST", "/api/settings", {"alert_statuses": ["bogus"]})
        self.assertEqual(code, 400)
        code, err = self.call("POST", "/api/pushover", {"app_token": 'a" ; rm -rf', "user_key": "x"})
        self.assertEqual(code, 400)

    def test_settings_clamped(self):
        code, s = self.call("POST", "/api/settings", {"pushover_retry": 5, "default_interval": 1})
        self.assertEqual(code, 200)
        self.assertEqual(s["pushover_retry"], 30)
        self.assertEqual(s["default_interval"], 5)


if __name__ == "__main__":
    unittest.main()

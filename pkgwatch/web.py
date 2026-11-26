"""Local web UI + JSON API (127.0.0.1 only)."""

from __future__ import annotations

import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import carriers, keychain
from .models import Package
from .monitor import Monitor

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class ApiError(Exception):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


def make_handler(monitor: Monitor, port: int):
    store = monitor.store
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "PkgWatch"

        def log_message(self, fmt, *args):
            log.debug("http " + fmt, *args)

        # ---- plumbing ----
        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj, default=str).encode(), "application/json")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 100_000:
                raise ApiError("body too large", 413)
            if not n:
                return {}
            try:
                data = json.loads(self.rfile.read(n))
            except ValueError:
                raise ApiError("invalid JSON") from None
            if not isinstance(data, dict):
                raise ApiError("expected a JSON object")
            return data

        def _guard(self, mutating: bool):
            # DNS-rebinding protection: only answer to our own host name.
            if self.headers.get("Host") not in allowed_hosts:
                raise ApiError("forbidden host", 403)
            # CSRF protection: a custom header forces a CORS preflight that we never approve,
            # so other websites open in the browser cannot drive this API.
            if mutating and self.headers.get("X-PkgWatch") != "1":
                raise ApiError("missing X-PkgWatch header", 403)

        def _dispatch(self, method: str):
            try:
                self._guard(method != "GET")
                self._route(method, self.path.split("?")[0])
            except ApiError as e:
                self._json(e.code, {"error": str(e)})
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                log.exception("api error")
                self._json(500, {"error": f"internal error: {e}"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")

        # ---- routes ----
        def _route(self, method: str, path: str):
            if method == "GET" and path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if method == "GET" and path == "/api/state":
                return self._json(200, monitor.state())
            if method == "GET" and path == "/api/events":
                return self._json(200, store.recent_events(500, include_polls=True))
            if method == "POST" and path == "/api/packages":
                return self._add(self._body())
            m = re.fullmatch(r"/api/packages/([0-9a-f]{10})(/check|/resume)?", path)
            if m:
                pkg = store.get(m.group(1))
                if not pkg:
                    raise ApiError("no such package", 404)
                if method == "DELETE" and not m.group(2):
                    monitor.acknowledge(pkg.id, "removed")
                    store.remove(pkg.id)
                    store.log_event("removed", pkg)
                    return self._json(200, {"ok": True})
                if method == "POST" and m.group(2) == "/check":
                    monitor.check_now(pkg.id)
                    return self._json(200, {"ok": True})
                if method == "POST" and m.group(2) == "/resume":
                    store.update(pkg.id, active=True, consecutive_errors=0)
                    store.log_event("resumed", pkg)
                    monitor.check_now(pkg.id)
                    return self._json(200, {"ok": True})
            if method == "POST" and path == "/api/ack":
                b = self._body()
                n = monitor.acknowledge(b.get("id") or None, "ui")
                return self._json(200, {"ok": True, "acknowledged": n})
            if method == "POST" and path == "/api/test":
                chans = [c for c in self._body().get("channels", []) if c in ("alarm", "mac", "pushover")]
                return self._json(200, monitor.test_channels(chans or ["alarm", "mac", "pushover"]))
            if method == "POST" and path == "/api/settings":
                return self._json(200, store.update_settings(self._body()))
            if path == "/api/pushover":
                if method == "POST":
                    b = self._body()
                    keychain.set("app_token", (b.get("app_token") or "").strip())
                    keychain.set("user_key", (b.get("user_key") or "").strip())
                elif method == "DELETE":
                    keychain.delete("app_token")
                    keychain.delete("user_key")
                monitor.pushover.refresh()
                store.log_event("pushover_credentials", None, action=method)
                return self._json(200, {"configured": monitor.pushover.configured})
            raise ApiError("not found", 404)

        def _add(self, b: dict):
            carrier_id, number, url = carriers.resolve(str(b.get("input", "")), str(b.get("carrier") or ""))
            interval = b.get("interval")
            options = {}
            if b.get("delivered_text"):
                options["delivered_text"] = str(b["delivered_text"])[:200]
            pkg = Package(tracking_number=number, carrier=carrier_id, url=url,
                          name=str(b.get("name") or "")[:80],
                          interval=float(interval) if interval else None, options=options)
            saved = store.add(pkg)
            if saved is pkg:
                store.log_event("added", pkg, url=url)
            monitor.check_now(saved.id)
            return self._json(200, saved.to_dict())

    return Handler


def serve(monitor: Monitor, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("refusing to bind the UI to a non-loopback address")
    httpd = ThreadingHTTPServer((host, port), make_handler(monitor, port))
    httpd.daemon_threads = True
    return httpd

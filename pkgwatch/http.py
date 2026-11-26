"""Tiny stdlib HTTP client that turns every transport problem into TrackingError."""

from __future__ import annotations

import http.cookiejar
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .models import TrackingError

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "PkgWatch/1.0 (personal delivery monitor; low-rate polling)"
)
DEFAULT_TIMEOUT = 30

_BOT_WALL_MARKERS = ("g-recaptcha", "h-captcha", "cf-challenge", "challenge-platform",
                     "Just a moment...", "Attention Required!", "px-captcha")


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes
    url: str

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self):
        try:
            return json.loads(self.body)
        except ValueError as e:
            raise TrackingError(f"invalid JSON from {self.url}: {e}") from e


def looks_blocked(text: str) -> bool:
    return any(m in text for m in _BOT_WALL_MARKERS)


class Session:
    """Per-adapter session with its own cookie jar."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        self.timeout = timeout
        self.cookies = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def request(self, method: str, url: str, *, data: dict | None = None, json_body=None,
                headers: dict | None = None, ok_statuses=(200,)) -> Response:
        hdrs = {"User-Agent": USER_AGENT, "Accept-Language": "en-CA,en;q=0.9"}
        hdrs.update(headers or {})
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        elif data is not None:
            body = urllib.parse.urlencode(data).encode()
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                resp = Response(r.status, dict(r.headers), r.read(), r.geturl())
        except urllib.error.HTTPError as e:
            resp = Response(e.code, dict(e.headers or {}), e.read() or b"", url)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise TrackingError(f"network error: {reason}") from e

        if resp.status in ok_statuses:
            return resp
        if resp.status == 429:
            raise TrackingError("rate limited (HTTP 429)", retry_after=_retry_after(resp.headers))
        if resp.status in (403, 503) and looks_blocked(resp.text):
            raise TrackingError(f"carrier bot protection / CAPTCHA (HTTP {resp.status}); not bypassing",
                                blocked=True)
        if resp.status >= 500:
            raise TrackingError(f"carrier server error (HTTP {resp.status})",
                                retry_after=_retry_after(resp.headers))
        raise TrackingError(f"unexpected HTTP {resp.status} from {urllib.parse.urlsplit(url).netloc}")

    def get(self, url: str, **kw) -> Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> Response:
        return self.request("POST", url, **kw)


def _retry_after(headers: dict) -> float | None:
    v = {k.lower(): val for k, val in headers.items()}.get("retry-after")
    try:
        return float(v) if v else None
    except ValueError:
        return None

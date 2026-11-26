"""Notification channels: macOS Notification Center and Pushover."""

from __future__ import annotations

import json
import logging
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from . import keychain

log = logging.getLogger(__name__)

PUSHOVER_API = "https://api.pushover.net/1"


def mac_notification(title: str, message: str, sound: str = "Glass") -> bool:
    script = ["-e", "on run argv",
              "-e", "display notification (item 2 of argv) with title (item 1 of argv) sound name (item 3 of argv)",
              "-e", "end run"]
    try:
        r = subprocess.run(["osascript", *script, title, message, sound], capture_output=True, timeout=15)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("mac notification failed: %s", e)
        return False


class PushoverError(Exception):
    pass


class Pushover:
    """Minimal Pushover client with retries (network blips must not eat a delivery alert)."""

    def __init__(self, creds=keychain.pushover_credentials):
        self._load = creds
        self._cache: tuple[float, tuple[str, str]] = (0.0, ("", ""))

    def _creds(self) -> tuple[str, str]:
        at, val = self._cache
        if time.time() - at > 60:  # Keychain lookups spawn a process; don't do it on every UI poll
            val = self._load()
            self._cache = (time.time(), val)
        return val

    def refresh(self):
        self._cache = (0.0, ("", ""))

    @property
    def configured(self) -> bool:
        token, user = self._creds()
        return bool(token and user)

    def _call(self, method: str, path: str, params: dict, attempts: int = 4) -> dict:
        token, user = self._creds()
        if not (token and user):
            raise PushoverError("Pushover not configured")
        params = {"token": token, **params}
        if path == "/messages.json":
            params["user"] = user
        url = f"{PUSHOVER_API}{path}"
        body = None
        if method == "GET":
            url += "?" + urllib.parse.urlencode(params)
        else:
            body = urllib.parse.urlencode(params).encode()
        delay = 2.0
        last = ""
        for i in range(attempts):
            try:
                req = urllib.request.Request(url, data=body, method=method)
                with urllib.request.urlopen(req, timeout=15) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")
                if 400 <= e.code < 500 and e.code != 429:
                    raise PushoverError(f"Pushover rejected request (HTTP {e.code}): {detail}") from e
                last = f"HTTP {e.code}"
            except (urllib.error.URLError, OSError, ValueError) as e:
                last = str(getattr(e, "reason", e))
            if i < attempts - 1:
                time.sleep(delay)
                delay *= 2
        raise PushoverError(f"Pushover unreachable after {attempts} attempts: {last}")

    def send(self, title: str, message: str, *, priority: int = 0, url: str = "", url_title: str = "",
             sound: str = "", retry: int = 30, expire: int = 3600) -> str:
        """Returns the emergency receipt (priority 2) or ''."""
        p = {"title": title[:250], "message": message[:1024], "priority": priority}
        if url:
            p.update(url=url[:512], url_title=(url_title or "Track")[:100])
        if sound:
            p["sound"] = sound
        if priority == 2:
            p.update(retry=max(30, retry), expire=min(10800, expire))
        res = self._call("POST", "/messages.json", p)
        if res.get("status") != 1:
            raise PushoverError(f"Pushover error: {res}")
        return res.get("receipt", "")

    def acknowledged(self, receipt: str) -> bool:
        res = self._call("GET", f"/receipts/{receipt}.json", {}, attempts=1)
        return bool(res.get("acknowledged"))

    def cancel(self, receipt: str) -> None:
        self._call("POST", f"/receipts/{receipt}/cancel.json", {}, attempts=2)

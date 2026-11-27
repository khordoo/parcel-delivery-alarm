"""Persistent state (packages + settings) in one JSON file, plus a JSONL event log."""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from pathlib import Path

from .models import Package, Status

log = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("PKGWATCH_HOME", Path.home() / "Library/Application Support/PkgWatch"))

DEFAULT_SETTINGS = {
    "default_interval": 30,  # seconds; carriers may enforce a higher floor
    "alert_statuses": [Status.DELIVERED.value, Status.AVAILABLE_FOR_PICKUP.value],
    "notify_changes": True,  # macOS/Pushover heads-up for out-for-delivery, attempted, exception
    "alarm_sound": "",  # empty = built-in siren
    "alarm_volume": 100,  # system output volume during alarm (0 = don't touch)
    "alarm_output": "builtin",  # builtin | default | part of a device name
    "mac_dialog": True,  # modal "Acknowledge" alert on the Mac
    "pushover_enabled": True,
    "pushover_retry": 30,  # emergency: re-alert phone every N s (min 30)
    "pushover_expire": 3600,  # stop re-alerting after N s (max 10800)
    "pushover_sound": "persistent",
    "prevent_sleep": True,
    "error_alert_after": 10,  # consecutive failures before warning you tracking is broken
}


class Store:
    def __init__(self, data_dir: Path = DATA_DIR):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "state.json"
        self.events_path = self.dir / "events.jsonl"
        self.lock = threading.RLock()
        self.settings = dict(DEFAULT_SETTINGS)
        self.packages: dict[str, Package] = {}
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError) as e:
            backup = self.path.with_suffix(f".corrupt-{int(time.time())}.json")
            self.path.rename(backup)
            log.error("state file unreadable (%s); moved to %s and starting fresh", e, backup)
            return
        self.settings.update(raw.get("settings") or {})
        for d in raw.get("packages") or []:
            p = Package.from_dict(d)
            self.packages[p.id] = p

    def save(self):
        with self.lock:
            data = {"version": 1, "settings": self.settings,
                    "packages": [p.to_dict() for p in self.packages.values()]}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1))
            os.replace(tmp, self.path)  # atomic: never a half-written state file

    # -- packages --
    def add(self, pkg: Package) -> Package:
        with self.lock:
            for p in self.packages.values():
                if p.carrier == pkg.carrier and p.tracking_number == pkg.tracking_number:
                    return p
            self.packages[pkg.id] = pkg
            self.save()
            return pkg

    def remove(self, pkg_id: str) -> Package | None:
        with self.lock:
            p = self.packages.pop(pkg_id, None)
            self.save()
            return p

    def get(self, pkg_id: str) -> Package | None:
        with self.lock:
            return self.packages.get(pkg_id)

    def update(self, pkg_id: str, **fields) -> Package | None:
        with self.lock:
            p = self.packages.get(pkg_id)
            if p is None:
                return None
            for k, v in fields.items():
                setattr(p, k, v)
            self.save()
            return p

    def snapshot(self) -> list[Package]:
        with self.lock:
            return [copy.deepcopy(p) for p in self.packages.values()]

    def active(self) -> list[Package]:
        return [p for p in self.snapshot() if p.active]

    def update_settings(self, changes: dict) -> dict:
        with self.lock:
            for k, v in changes.items():
                if k not in DEFAULT_SETTINGS:
                    raise ValueError(f"unknown setting {k}")
                default = DEFAULT_SETTINGS[k]
                if isinstance(default, bool):
                    v = bool(v)
                elif isinstance(default, int):
                    v = int(v)
                elif isinstance(default, list):
                    if not isinstance(v, list) or not all(x in Status._value2member_map_ for x in v):
                        raise ValueError(f"{k} must be a list of statuses")
                else:
                    v = str(v)
                self.settings[k] = v
            self.settings["default_interval"] = max(5, self.settings["default_interval"])
            self.settings["pushover_retry"] = max(30, self.settings["pushover_retry"])
            self.settings["pushover_expire"] = min(10800, max(60, self.settings["pushover_expire"]))
            self.save()
            return dict(self.settings)

    # -- event log --
    def log_event(self, kind: str, pkg: Package | None = None, **data):
        rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%d %H:%M:%S%z"), "kind": kind}
        if pkg is not None:
            rec.update(pkg=pkg.id, number=pkg.tracking_number, carrier=pkg.carrier)
        rec.update(data)
        line = json.dumps(rec, default=str)
        with self.lock:
            with self.events_path.open("a") as f:
                f.write(line + "\n")
        log.info("%s %s %s", kind, pkg.tracking_number if pkg else "", data)

    def recent_events(self, n: int = 200, include_polls: bool = False) -> list[dict]:
        try:
            with self.events_path.open("rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 400_000))
                lines = f.read().decode(errors="replace").splitlines()[1:]
        except OSError:
            return []
        out = []
        for line in reversed(lines):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if include_polls or rec.get("kind") != "poll":
                out.append(rec)
            if len(out) >= n:
                break
        return out

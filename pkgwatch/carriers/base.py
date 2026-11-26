"""Carrier adapter interface + registry.

To add a carrier: create pkgwatch/carriers/<name>.py with a Carrier subclass
decorated with @register. Modules in this package are auto-imported, so the
monitor, UI and detection pick it up with no other changes.
"""

from __future__ import annotations

import re
import urllib.parse

from ..models import Package, TrackingResult

REGISTRY: dict[str, "Carrier"] = {}


def register(cls):
    inst = cls()
    REGISTRY[inst.id] = inst
    return cls


class Carrier:
    id: str = ""
    name: str = ""
    # Floor for polling interval regardless of user settings (politeness / CDN cache).
    min_interval: float = 30.0
    # Regexes matched against the normalized tracking number for auto-detection.
    patterns: tuple[str, ...] = ()
    # Hostnames whose URLs this adapter understands, and query params holding the number.
    url_hosts: tuple[str, ...] = ()
    url_params: tuple[str, ...] = ()
    # Try this adapter when no pattern matches (one cheap request).
    probe: bool = False
    # Shown in the manual carrier picker.
    selectable: bool = True
    # Set by the monitor: call on_update(pkg_id) when background work found news worth an immediate re-check.
    on_update = None

    def detect(self, number: str) -> bool:
        return any(re.fullmatch(p, number) for p in self.patterns)

    def parse_url(self, url: str) -> str | None:
        """Return the tracking number if this URL belongs to this carrier."""
        parts = urllib.parse.urlsplit(url)
        host = parts.netloc.lower().removeprefix("www.")
        if not any(host == h or host.endswith("." + h) for h in self.url_hosts):
            return None
        qs = urllib.parse.parse_qs(parts.query)
        for p in self.url_params:
            if qs.get(p):
                return normalize_number(qs[p][0])
        seg = [s for s in parts.path.split("/") if s]
        if seg and re.fullmatch(r"[A-Za-z0-9-]{8,40}", seg[-1]) and any(c.isdigit() for c in seg[-1]):
            return normalize_number(seg[-1])
        return None

    def public_url(self, pkg: Package) -> str:
        return pkg.url

    def track(self, pkg: Package) -> TrackingResult:
        """Fetch and normalize. Raise TrackingError on any failure to obtain data."""
        raise NotImplementedError

    def enrich(self, pkg: Package, result: TrackingResult) -> TrackingResult | None:
        """Optional slow follow-up run after alerting (e.g. exact timestamps). None = nothing added."""
        return None


def normalize_number(s: str) -> str:
    return re.sub(r"\s+", "", s.strip()).upper()

"""Intelcom / Dragonfly (Canada).

Uses the same public JSON endpoint as intelcom.ca/en/track-your-package.
Responses are Cloudflare-cached for 30 s (cache-control: max-age=30), so
polling faster than 30 s is pointless and impolite.
"""

from __future__ import annotations

import urllib.parse

from ..classify import classify
from ..http import Session
from ..models import Event, Package, Status, TrackingError, TrackingResult
from .base import Carrier, register

API = "https://intelcom.ca/cfworker/v3/tracking/{}/"
PUBLIC = "https://intelcom.ca/en/track-your-package/?tracking-id={}"

PICKUP_READY_CODES = {650}
STEP_FALLBACK = {1: Status.INFO_RECEIVED, 2: Status.IN_TRANSIT, 3: Status.OUT_FOR_DELIVERY}


def _label(s: dict) -> str:
    short = ((s.get("labels") or {}).get("shortLabel") or {}).get("en") or ""
    return s.get("label") or short


def map_status(s: dict) -> Status:
    code = s.get("status")
    text = f"{s.get('label') or ''} | {((s.get('labels') or {}).get('shortLabel') or {}).get('en') or ''}"
    if code in PICKUP_READY_CODES:
        return Status.AVAILABLE_FOR_PICKUP
    if s.get("isDelivered"):
        # Carrier's own delivered flag. Covers door delivery and pickup-point collection.
        return Status.DELIVERED
    st = classify(text)
    if st == Status.DELIVERED:
        # Text says delivered but the flag disagrees: trust the flag, don't alarm.
        st = Status.UNKNOWN
    if st == Status.UNKNOWN:
        st = STEP_FALLBACK.get(s.get("step"), Status.UNKNOWN)
    return st


def parse(data: dict) -> TrackingResult:
    if not data.get("success"):
        code = (data.get("data") or {}).get("code")
        if code == "not_found":
            return TrackingResult(Status.NOT_FOUND, "Tracking number not found", source="intelcom")
        raise TrackingError(f"intelcom returned error: {code or data}")
    result = (data.get("data") or {}).get("result")
    if not result or not result.get("last_status"):
        raise TrackingError("intelcom response missing result/last_status")
    last = result["last_status"]
    events = []
    for s in result.get("status_list") or []:
        if s.get("hidden"):
            continue
        ts = s.get("timestamp")
        events.append(Event(time=ts / 1000 if ts else None, label=_label(s),
                            status=map_status(s).value, code=str(s.get("status", ""))))
    events.sort(key=lambda e: e.time or 0, reverse=True)
    ts = last.get("timestamp")
    return TrackingResult(
        status=map_status(last),
        label=_label(last),
        event_time=ts / 1000 if ts else None,
        events=events,
        source="intelcom.ca",
        eta=result.get("public_eta") or result.get("eta") or "",
    )


@register
class Intelcom(Carrier):
    id = "intelcom"
    name = "Intelcom / Dragonfly"
    min_interval = 30.0
    patterns = (r"INTLCM[A-Z0-9]{6,}", r"DELCF[0-9]{6,}", r"DLV[A-Z0-9]{8,}")
    url_hosts = ("intelcom.ca", "dragonflyshipping.ca", "intelcom.com")
    url_params = ("tracking-id", "trackingId", "tracking")
    probe = True

    def __init__(self):
        self.session = Session(timeout=20)

    def public_url(self, pkg: Package) -> str:
        return PUBLIC.format(urllib.parse.quote(pkg.tracking_number))

    def track(self, pkg: Package) -> TrackingResult:
        r = self.session.get(API.format(urllib.parse.quote(pkg.tracking_number)),
                             headers={"Accept": "application/json"})
        return parse(r.json())

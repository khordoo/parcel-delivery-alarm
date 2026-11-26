"""Precision Parcel & Package Deliveries (Fastfrate group; ASL's final-mile carrier).

Same JSON API the public tracker at track.precisiondeliveries.com uses. The
site opens an anonymous session (POST /api/authentication with an empty body
sets a cookie) for every visitor; we do the same, nothing more.

The response contains recipient PII (name, address, phone). We only keep
timestamps, public status text and status codes.
"""

from __future__ import annotations

import calendar
import re
import threading
import time
import urllib.parse

from ..classify import classify
from ..http import Session
from ..models import Event, Package, Status, TrackingError, TrackingResult
from .base import Carrier, register

BASE = "https://track.precisiondeliveries.com/api"
PUBLIC = "https://track.precisiondeliveries.com/tracking?trackingNumber={}"


def _utc(ts: str) -> float | None:
    # scandatetime is UTC ("2026-11-26 13:11:05"); scandatetimelocal is not reliable.
    try:
        return float(calendar.timegm(time.strptime(ts, "%Y-%m-%d %H:%M:%S")))
    except (TypeError, ValueError):
        return None


def map_event(e: dict) -> Status:
    if (e.get("signedby") or "").strip() or e.get("deliveryimages") or (e.get("signatureimage") or "").strip():
        return Status.DELIVERED
    return classify(e.get("publicstatus") or "", e.get("statusdesc") or "")


def parse(data: dict) -> TrackingResult:
    raw = data.get("scanevents")
    if not isinstance(raw, list) or not raw:
        raise TrackingError("precision response has no scanevents")
    events = []
    for e in raw:
        label = e.get("publicstatus") or e.get("statusdesc") or str(e.get("status"))
        events.append(Event(time=_utc(e.get("scandatetime")), label=label,
                            status=map_event(e).value, code=str(e.get("status", ""))))
    events.sort(key=lambda ev: ev.time or 0, reverse=True)
    latest = events[0]
    return TrackingResult(
        status=Status(latest.status),
        label=latest.label,
        event_time=latest.time,
        events=events,
        source="precisiondeliveries.com",
        sub_carrier="Precision Parcel",
        eta=data.get("edd") or "",
    )


@register
class Precision(Carrier):
    id = "precision"
    name = "Precision Parcel"
    min_interval = 30.0
    url_hosts = ("precisiondeliveries.com",)
    url_params = ("trackingNumber", "tracking", "itemId", "id")
    probe = True

    def __init__(self):
        self.session = Session(timeout=40)  # this API is sometimes slow (~15 s observed)
        self._lock = threading.Lock()
        self._authed_at = 0.0

    def public_url(self, pkg: Package) -> str:
        return PUBLIC.format(urllib.parse.quote(pkg.tracking_number))

    def _auth(self, force=False):
        with self._lock:
            if force or time.time() - self._authed_at > 3600:
                self.session.post(f"{BASE}/authentication", json_body={})
                self._authed_at = time.time()

    def track(self, pkg: Package) -> TrackingResult:
        self._auth()
        url = f"{BASE}/shipment-itemId/{urllib.parse.quote(pkg.tracking_number)}"
        try:
            r = self.session.get(url, ok_statuses=(200, 404))
        except TrackingError as e:
            if "HTTP 401" not in str(e):
                raise
            self._auth(force=True)
            r = self.session.get(url, ok_statuses=(200, 404))
        if r.status == 404:
            body = r.json()
            if re.search(r"no (shipment|item)", str(body.get("error", "")), re.I):
                return TrackingResult(Status.NOT_FOUND, "Tracking number not found", source="precisiondeliveries.com")
            raise TrackingError(f"precision 404: {body}")
        return parse(r.json())

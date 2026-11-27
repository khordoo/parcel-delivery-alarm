"""ASL Distribution Services (Canada).

Primary source: the WordPress AJAX endpoint behind asldistribution.com/tracking
(returns an HTML fragment; no CAPTCHA on tracking). It only exposes event
*dates*, so when the final-mile carrier is Precision Parcel we enrich important
events with Precision's exact scan timestamps, and fall back to Precision
entirely if ASL's endpoint fails.

Race mode: while a package is out for delivery with Precision as final mile,
every poll also queries Precision in the background. Whichever source reports
a final status (delivered / ready for pickup) first wins; the monitor's
alerted_at guard guarantees a single alert. Precision is slow (up to ~25 s),
so its request never blocks the ASL poll: when it finishes with a final status
it wakes the monitor via on_update for an immediate re-check.
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
import urllib.parse
from concurrent.futures import Future, ThreadPoolExecutor
from html.parser import HTMLParser

from ..classify import classify
from ..http import Session
from ..models import Event, Package, Status, TrackingError, TrackingResult
from .base import REGISTRY, Carrier, register

log = logging.getLogger(__name__)

AJAX = "https://asldistribution.com/wp-admin/admin-ajax.php"
PAGE = "https://asldistribution.com/tracking/"
PUBLIC = "https://asldistribution.com/tracking/?tracking={}"

FINAL = (Status.DELIVERED, Status.AVAILABLE_FOR_PICKUP)
CATCH_UP_WINDOW = 600  # after an alert, keep watching the slower source this long (s)
CATCH_UP_EVERY = 15


class _ResultParser(HTMLParser):
    """Collects text by CSS class from the order-tracking-result fragment."""

    WANT = {"order-tracking-status-label": "label", "order-tracking-status-location": "location",
            "order-tracking-status-date": "date", "order-tracking-delivery-date": "eta",
            "order-tracking-carrier": "carrier", "order-tracking-updated": "updated"}

    def __init__(self):
        super().__init__()
        self.events: list[dict] = []
        self.meta: dict[str, str] = {}
        self._stack: list[str | None] = []  # field name per open tag
        self._buf: dict[str, list[str]] = {}

    def handle_starttag(self, tag, attrs):
        classes = (dict(attrs).get("class") or "").split()
        if tag == "li" and "order-tracking-status" in classes:
            self.events.append({"active": "active" in classes})
        field = next((self.WANT[c] for c in classes if c in self.WANT), None)
        if tag in ("img", "br", "input"):
            return
        self._stack.append(field)
        if field:
            self._buf[field] = []

    def handle_endtag(self, tag):
        if not self._stack:
            return
        field = self._stack.pop()
        if field:
            text = re.sub(r"\s+", " ", "".join(self._buf.pop(field, []))).strip()
            if field in ("label", "location", "date") and self.events:
                self.events[-1][field] = text
            else:
                self.meta[field] = text

    def handle_data(self, data):
        for f in self._buf:
            self._buf[f].append(data)


def _parse_date(s: str) -> float | None:
    try:
        return time.mktime(time.strptime(s, "%B %d, %Y"))
    except (TypeError, ValueError):
        return None


def parse(data: dict) -> TrackingResult:
    if not isinstance(data, dict):
        raise TrackingError("asl: unexpected response")
    if not data.get("success"):
        code = (data.get("data") or {}).get("code", "unknown") if isinstance(data.get("data"), dict) else "unknown"
        if code in ("rate_limited", "too_many_requests"):
            raise TrackingError("asl: rate limited", retry_after=120)
        if code in ("not_found", "invalid", "no_results"):
            return TrackingResult(Status.NOT_FOUND, "Not in ASL's system yet (normal before the carrier receives it)", source="asldistribution.com")
        raise TrackingError(f"asl returned error code '{code}'")
    frag = (data.get("data") or {}).get("html") or ""
    p = _ResultParser()
    p.feed(frag)
    events = [e for e in p.events if e.get("label")]
    if not events:
        if re.search(r"not found|no result|invalid", html.unescape(frag), re.I):
            return TrackingResult(Status.NOT_FOUND, "Not in ASL's system yet (normal before the carrier receives it)", source="asldistribution.com")
        raise TrackingError("asl: could not parse tracking result (page layout changed?)")
    norm = [Event(time=_parse_date(e.get("date", "")), label=e["label"], location=e.get("location", ""),
                  status=classify(e["label"]).value) for e in events]
    latest_idx = next((i for i, e in enumerate(events) if e.get("active")), 0)
    latest = norm[latest_idx]
    carrier = re.sub(r"^Carrier:\s*", "", p.meta.get("carrier", "")).strip()
    return TrackingResult(status=Status(latest.status), label=latest.label, event_time=latest.time,
                          events=norm, source="asldistribution.com", sub_carrier=carrier,
                          eta=p.meta.get("eta", ""), date_only=True)


@register
class ASL(Carrier):
    id = "asl"
    name = "ASL Distribution"
    min_interval = 30.0
    patterns = (r"ASL[0-9]{8,12}",)
    url_hosts = ("asldistribution.com",)
    url_params = ("tracking", "tracking_id", "tracking-id")
    probe = True

    def __init__(self):
        self.session = Session(timeout=30)
        self._nonce = ""
        self._nonce_at = 0.0
        self._race_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="precision-race")
        self._race: dict[str, Future] = {}  # pkg id -> in-flight/finished Precision lookup
        self._race_lock = threading.Lock()

    def public_url(self, pkg: Package) -> str:
        return PUBLIC.format(urllib.parse.quote(pkg.tracking_number))

    def _get_nonce(self) -> str:
        # The site embeds a WordPress nonce; it is not currently enforced, so this is best-effort.
        if time.time() - self._nonce_at > 6 * 3600:
            self._nonce_at = time.time()
            try:
                page = self.session.get(PAGE).text
                m = re.search(r'OA_ORDER_TRACKING\s*=\s*\{[^}]*"nonce":"([0-9a-f]+)"', page)
                self._nonce = m.group(1) if m else ""
            except TrackingError as e:
                log.info("asl nonce fetch failed: %s", e)
        return self._nonce

    def _track_asl(self, pkg: Package) -> TrackingResult:
        form = {"action": "order_tracking", "tracking_id": pkg.tracking_number}
        nonce = self._get_nonce()
        if nonce:
            form["nonce"] = nonce
        r = self.session.post(AJAX, data=form, headers={"Accept": "application/json",
                                                        "X-Requested-With": "XMLHttpRequest",
                                                        "Referer": PAGE},
                              ok_statuses=(200, 404))  # 404 + {"code":"not_found"} = not in ASL's system yet
        return parse(r.json())

    # ---------- race mode ----------
    def _racing(self, pkg: Package) -> bool:
        return (REGISTRY.get("precision") is not None
                and pkg.status == Status.OUT_FOR_DELIVERY.value
                and (not pkg.sub_carrier or "precision" in pkg.sub_carrier.lower()))

    def _start_race(self, pkg: Package):
        with self._race_lock:
            if pkg.id in self._race:
                return  # previous lookup still running (Precision can take ~25 s): don't pile up
            fut = self._race_pool.submit(REGISTRY["precision"].track, pkg)
            self._race[pkg.id] = fut

        def done(f: Future):
            if not f.exception() and f.result().status in FINAL and self.on_update:
                log.info("precision reports %s for %s; waking monitor", f.result().status.value,
                         pkg.tracking_number)
                self.on_update(pkg.id)
        fut.add_done_callback(done)

    def _take_race(self, pkg_id: str) -> TrackingResult | None:
        """Consume a finished Precision lookup; return it only if it reports a final status."""
        with self._race_lock:
            fut = self._race.get(pkg_id)
            if fut is None or not fut.done():
                return None
            del self._race[pkg_id]
        if fut.exception():
            log.info("precision race lookup failed: %s", fut.exception())
            return None
        r = fut.result()
        if r.status not in FINAL:
            return None
        r.source = "precisiondeliveries.com (beat asl)"
        return r

    def _drop_race(self, pkg_id: str):
        with self._race_lock:
            self._race.pop(pkg_id, None)

    def track(self, pkg: Package) -> TrackingResult:
        racing = self._racing(pkg)
        if racing:
            first = self._take_race(pkg.id)
            if first:
                return first
            self._start_race(pkg)
        try:
            result = self._track_asl(pkg)
        except TrackingError as asl_err:
            return self._fallback(pkg, asl_err)
        if racing and result.status not in FINAL:
            first = self._take_race(pkg.id)  # Precision may have finished while ASL answered
            if first:
                return first
        if result.status in FINAL:
            self._drop_race(pkg.id)
        result.source = "asldistribution.com"
        return result

    def _fallback(self, pkg: Package, asl_err: TrackingError) -> TrackingResult:
        precision = REGISTRY.get("precision")
        if not precision:
            raise asl_err
        with self._race_lock:
            fut = self._race.pop(pkg.id, None)
        try:
            # Reuse the in-flight race lookup if there is one instead of a second slow request.
            result = fut.result(timeout=45) if fut else precision.track(pkg)
        except Exception as p_err:
            raise TrackingError(f"{asl_err}; precision fallback: {p_err}",
                                retry_after=asl_err.retry_after, blocked=asl_err.blocked) from p_err
        if result.status == Status.NOT_FOUND:
            raise asl_err  # Precision doesn't know it: report the real (ASL) failure
        log.info("asl failed (%s); used precision fallback", asl_err)
        result.source = "precisiondeliveries.com (asl fallback)"
        return result

    # ---------- after the alert ----------
    def enrich(self, pkg: Package, result: TrackingResult) -> TrackingResult | None:
        """Runs after alerting. Adds Precision's exact scan time (ASL has dates only) and,
        when Precision won the race, measures how long ASL took to catch up."""
        precision = REGISTRY.get("precision")
        if not precision:
            return None
        if result.source.startswith("precisiondeliveries.com (beat asl)"):
            start = time.time()
            while time.time() - start < CATCH_UP_WINDOW:
                try:
                    if self._track_asl(pkg).status in FINAL:
                        result.source += f"; asl caught up after {time.time() - start:.0f}s"
                        return result
                except TrackingError as e:
                    log.info("asl catch-up check failed: %s", e)
                time.sleep(CATCH_UP_EVERY)
            result.source += f"; asl still not final after {CATCH_UP_WINDOW}s"
            return result
        if not result.date_only or "precision" not in result.sub_carrier.lower():
            return None
        p = precision.track(pkg)
        if p.status != result.status or not p.event_time:
            return None
        result.event_time, result.date_only, result.events = p.event_time, False, p.events
        result.source += " + precision timestamps (asl first)"
        return result

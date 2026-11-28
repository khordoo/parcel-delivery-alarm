"""Polling scheduler, status-change handling and alert fan-out. Carrier-agnostic."""

from __future__ import annotations

import copy
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Callable

from . import carriers
from .alarm import Alarm
from .models import NOTABLE_STATUSES, TERMINAL_STATUSES, Package, Status, TrackingError, TrackingResult
from .notify import Pushover, PushoverError, mac_notification
from .power import SleepGuard
from .store import Store

log = logging.getLogger(__name__)

BACKOFF_BASE = 10.0  # first retry after a failure (s); doubles each time
BACKOFF_MAX = 300.0
BLOCKED_BACKOFF = 900.0
RECEIPT_POLL = 15.0
RESUME_ALARM_WINDOW = 24 * 3600
NOT_FOUND_INTERVAL = 300.0


def fmt_time(ts: float | None, date_only: bool = False) -> str:
    if not ts:
        return "unknown time"
    return time.strftime("%b %d" if date_only else "%b %d %H:%M:%S", time.localtime(ts))


class Monitor:
    def __init__(self, store: Store, *, alarm: Alarm | None = None, pushover: Pushover | None = None,
                 mac_notify: Callable[[str, str], bool] = mac_notification,
                 sleep_guard: SleepGuard | None = None, get_carrier=carriers.get):
        self.store = store
        self.alarm = alarm or Alarm(store.dir, lambda: self.store.settings, lambda: self.acknowledge(None, "mac dialog"))
        self.pushover = pushover or Pushover()
        self.mac_notify = mac_notify
        self.sleep = sleep_guard or SleepGuard()
        self.get_carrier = get_carrier
        self._pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="track")
        self._next: dict[str, float] = {}
        self._inflight: set[str] = set()
        self._recheck: set[str] = set()  # check_now() arrived while a poll was in flight
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_receipt_poll = 0.0
        self._test_receipt = ""
        self.started_at = time.time()
        for c in carriers.REGISTRY.values():
            c.on_update = self.check_now

    # ---------- lifecycle ----------
    def start(self):
        self._resume_unacknowledged()
        self._thread = threading.Thread(target=self._loop, name="monitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stopping.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._pool.shutdown(wait=False, cancel_futures=True)
        self.alarm.stop()
        self.sleep.set(False)

    def _resume_unacknowledged(self):
        for p in self.store.snapshot():
            if p.alerted_at and not p.acknowledged_at and time.time() - p.alerted_at < RESUME_ALARM_WINDOW:
                log.info("resuming unacknowledged alarm for %s", p.tracking_number)
                self.alarm.start(p.id, self._alert_title(p), self._alert_message(p) + " (restarted)")

    def _loop(self):
        while not self._stopping.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("monitor tick failed")
            self._wake.wait(1.0)
            self._wake.clear()

    def tick(self):
        now = time.time()
        active = self.store.active()
        for p in active:
            with self._lock:
                if p.id in self._inflight or now < self._next.get(p.id, 0):
                    continue
                self._inflight.add(p.id)
            self._pool.submit(self._check_safe, p.id)
        self.sleep.set(bool(self.store.settings.get("prevent_sleep")) and (bool(active) or bool(self.alarm.active)))
        if now - self._last_receipt_poll >= RECEIPT_POLL:
            self._last_receipt_poll = now
            self._pool.submit(self._poll_receipts)

    def interval(self, p: Package) -> float:
        try:
            floor = self.get_carrier(p.carrier).min_interval
        except ValueError:
            floor = 30.0
        interval = max(float(p.interval or self.store.settings["default_interval"]), floor)
        if p.status == Status.NOT_FOUND.value:
            # Not in the carrier's system yet (label printed, not handed over). Delivery is hours
            # away at the earliest, so check gently; full speed resumes as soon as it appears.
            interval = max(interval, NOT_FOUND_INTERVAL)
        return interval

    def check_now(self, pkg_id: str):
        with self._lock:
            self._next[pkg_id] = 0
            if pkg_id in self._inflight:
                self._recheck.add(pkg_id)  # the running poll would otherwise reschedule 30 s out
        self._wake.set()

    # ---------- one poll ----------
    def _check_safe(self, pkg_id: str):
        try:
            self.check(pkg_id)
        except Exception:
            log.exception("check crashed for %s", pkg_id)
        finally:
            with self._lock:
                self._inflight.discard(pkg_id)
                if pkg_id in self._recheck:
                    self._recheck.discard(pkg_id)
                    if pkg_id in self._next:  # still active
                        self._next[pkg_id] = 0
            self._wake.set()

    def check(self, pkg_id: str):
        live = self.store.get(pkg_id)
        if live is None or not live.active:
            return
        pkg = copy.deepcopy(live)
        started = time.time()
        try:
            carrier = self.get_carrier(pkg.carrier)
            result = carrier.track(pkg)
        except TrackingError as e:
            self._on_error(pkg, e, started)
            return
        except Exception as e:  # adapter bug / layout change: still a *failed request*, not "no change"
            log.exception("adapter %s crashed", pkg.carrier)
            self._on_error(pkg, TrackingError(f"adapter error: {e!r}"), started)
            return
        self._on_result(pkg, carrier, result, started)

    def _schedule(self, pkg_id: str, delay: float):
        with self._lock:
            self._next[pkg_id] = time.time() + delay * random.uniform(0.95, 1.05)

    def _on_error(self, pkg: Package, err: TrackingError, started: float):
        n = pkg.consecutive_errors + 1
        delay = min(BACKOFF_BASE * 2 ** (n - 1), BACKOFF_MAX)
        if err.retry_after:
            delay = max(delay, err.retry_after)
        if err.blocked:
            delay = max(delay, BLOCKED_BACKOFF)
        self.store.update(pkg.id, last_checked_at=time.time(), consecutive_errors=n, last_error=str(err))
        self._schedule(pkg.id, delay)
        self.store.log_event("error", pkg, error=str(err), consecutive=n, retry_in_s=round(delay),
                             duration_s=round(time.time() - started, 2))
        threshold = int(self.store.settings.get("error_alert_after", 10))
        if (n >= threshold or err.blocked) and not pkg.error_notified:
            self.store.update(pkg.id, error_notified=True)
            title = "Tracking is failing"
            msg = f"{pkg.name or pkg.tracking_number}: {err}. Status may be stale — check manually."
            self._notify_async(title, msg, priority=1, pkg=pkg)

    def _on_result(self, pkg: Package, carrier, res: TrackingResult, started: float):
        now = time.time()
        settings = self.store.settings
        changed = res.status.value != pkg.status or res.label != pkg.status_label
        alert = res.status.value in settings["alert_statuses"] and not pkg.alerted_at
        fields = dict(
            last_checked_at=now, last_success_at=now, consecutive_errors=0, last_error="",
            error_notified=False, status=res.status.value, status_label=res.label,
            sub_carrier=res.sub_carrier or pkg.sub_carrier, eta=res.eta or pkg.eta,
            carrier_event_time=res.event_time, date_only=res.date_only,
            events=self._stamp_seen(pkg, res, now),
        )
        if changed:
            fields["last_change_at"] = now
        if alert:
            fields.update(alerted_at=now, alert_status=res.status.value, acknowledged_at=None)
        if res.status in TERMINAL_STATUSES:
            fields["active"] = False
        # Persist (incl. alerted_at) BEFORE notifying: a crash/restart can never re-alert.
        updated = self.store.update(pkg.id, **fields)
        if updated is None:
            return  # removed while we were polling
        if pkg.consecutive_errors:
            self.store.log_event("recovered", pkg, after_errors=pkg.consecutive_errors)
        self.store.log_event("poll", pkg, status=res.status.value, duration_s=round(now - started, 2),
                             source=res.source)
        if changed:
            latency = None if (res.date_only or not res.event_time) else round(now - res.event_time, 1)
            self.store.log_event("status_change", pkg, old=pkg.status, new=res.status.value, label=res.label,
                                 carrier_event_time=res.event_time, detected_at=now,
                                 carrier_to_detection_s=latency, source=res.source)
        if alert:
            self._fire_alert(updated)
            self._enrich_async(updated, carrier, res)
        elif changed and res.status in NOTABLE_STATUSES and settings.get("notify_changes"):
            prio = 1 if res.status in (Status.ATTEMPTED, Status.EXCEPTION) else 0
            self._notify_async(f"{res.status.label}", f"{updated.name or updated.tracking_number}: {res.label}",
                               priority=prio, pkg=updated)
        if not fields.get("active", True):
            self.store.log_event("polling_stopped", pkg, reason=res.status.value)
            with self._lock:
                self._next.pop(pkg.id, None)
        else:
            self._schedule(pkg.id, self.interval(updated))

    @staticmethod
    def _stamp_seen(pkg: Package, res: TrackingResult, now: float) -> list[dict]:
        """Carry each event's first-seen time across polls (ASL only gives dates, so this is
        the best "when" we have). Events already present on the very first successful poll
        happened before monitoring started and get no stamp."""
        prev = {(e.get("label"), e.get("location"), e.get("time")): e for e in pkg.events}
        legacy = bool(pkg.events) and all("seen_at" not in e for e in pkg.events)
        out = []
        for e in res.events[:25]:
            d = asdict(e)
            old = prev.get((d["label"], d["location"], d["time"]))
            if old is not None and "seen_at" in old:
                d["seen_at"] = old["seen_at"]
            elif old is not None or pkg.last_success_at is None:
                # Seen before this feature existed, or already there when monitoring started.
                d["seen_at"] = pkg.last_change_at if (legacy and d["label"] == pkg.status_label) else None
            else:
                d["seen_at"] = now
            out.append(d)
        return out

    # ---------- alerts ----------
    def _alert_title(self, p: Package) -> str:
        return "📦 Package ready for pickup" if p.alert_status == Status.AVAILABLE_FOR_PICKUP.value \
            else "📦 Package DELIVERED"

    def _alert_message(self, p: Package) -> str:
        who = p.name or p.tracking_number
        via = p.sub_carrier or self._carrier_name(p)
        return f"{who} ({via}): {p.status_label} — carrier time {fmt_time(p.carrier_event_time, p.date_only)}"

    def _carrier_name(self, p: Package) -> str:
        try:
            return self.get_carrier(p.carrier).name
        except ValueError:
            return p.carrier

    def _public_url(self, p: Package) -> str:
        try:
            return self.get_carrier(p.carrier).public_url(p)
        except ValueError:
            return p.url

    def _fire_alert(self, p: Package):
        title, msg = self._alert_title(p), self._alert_message(p)
        self.store.log_event("alert", p, status=p.alert_status, alerted_at=p.alerted_at,
                             carrier_event_time=p.carrier_event_time)
        # All channels in parallel: a slow Pushover call must not delay the siren and vice versa.
        threading.Thread(target=self._alarm_channel, args=(p, title, msg), daemon=True).start()
        threading.Thread(target=self._mac_channel, args=(p, title, msg), daemon=True).start()
        threading.Thread(target=self._pushover_emergency, args=(p, title, msg), daemon=True).start()

    def _alarm_channel(self, p, title, msg):
        self.alarm.start(p.id, title, msg)
        self.store.log_event("notification_sent", p, channel="alarm")

    def _mac_channel(self, p, title, msg):
        ok = self.mac_notify(title, msg)
        self.store.log_event("notification_sent" if ok else "notification_failed", p, channel="mac")

    def _pushover_emergency(self, p: Package, title: str, msg: str):
        s = self.store.settings
        if not s.get("pushover_enabled") or not self.pushover.configured:
            self.store.log_event("notification_skipped", p, channel="pushover", reason="not configured/disabled")
            return
        try:
            receipt = self.pushover.send(title, msg, priority=2, url=self._public_url(p), url_title="Tracking page",
                                         sound=s.get("pushover_sound", ""), retry=s["pushover_retry"],
                                         expire=s["pushover_expire"])
            self.store.update(p.id, pushover_receipt=receipt)
            self.store.log_event("notification_sent", p, channel="pushover", receipt=receipt)
            if self.store.get(p.id) and self.store.get(p.id).acknowledged_at:
                self.pushover.cancel(receipt)  # acknowledged while we were sending
        except PushoverError as e:
            self.store.log_event("notification_failed", p, channel="pushover", error=str(e))

    def _notify_async(self, title: str, msg: str, priority: int = 0, pkg: Package | None = None):
        def run():
            ok = self.mac_notify(title, msg)
            self.store.log_event("notification_sent" if ok else "notification_failed", pkg, channel="mac", title=title)
            s = self.store.settings
            if s.get("pushover_enabled") and self.pushover.configured:
                try:
                    self.pushover.send(title, msg, priority=priority, url=self._public_url(pkg) if pkg else "")
                    self.store.log_event("notification_sent", pkg, channel="pushover", title=title)
                except PushoverError as e:
                    self.store.log_event("notification_failed", pkg, channel="pushover", error=str(e))
        threading.Thread(target=run, daemon=True).start()

    def _enrich_async(self, p: Package, carrier, res: TrackingResult):
        def run():
            try:
                r2 = carrier.enrich(p, res)
            except TrackingError as e:
                log.info("enrich failed: %s", e)
                return
            if r2 and r2.event_time:
                self.store.update(p.id, carrier_event_time=r2.event_time, date_only=r2.date_only,
                                  events=[asdict(e) for e in r2.events[:25]])
                self.store.log_event("enriched", p, carrier_event_time=r2.event_time,
                                     carrier_to_detection_s=round(p.alerted_at - r2.event_time, 1),
                                     source=r2.source)
        threading.Thread(target=run, daemon=True).start()

    def acknowledge(self, pkg_id: str | None = None, source: str = "ui") -> int:
        self.alarm.stop(pkg_id)
        now = time.time()
        n = 0
        receipts = []
        for p in self.store.snapshot():
            if (pkg_id is None or p.id == pkg_id) and p.alerted_at and not p.acknowledged_at:
                self.store.update(p.id, acknowledged_at=now)
                self.store.log_event("acknowledged", p, via=source, after_s=round(now - p.alerted_at, 1))
                if p.pushover_receipt and source != "pushover":
                    receipts.append(p.pushover_receipt)
                n += 1
        if pkg_id is None or pkg_id == "test":
            if self._test_receipt and source != "pushover":
                receipts.append(self._test_receipt)
            self._test_receipt = ""
        for r in receipts:
            threading.Thread(target=self._cancel_receipt, args=(r,), daemon=True).start()
        return n

    def _cancel_receipt(self, receipt: str):
        try:
            self.pushover.cancel(receipt)
        except PushoverError as e:
            log.warning("could not cancel pushover receipt: %s", e)

    def _poll_receipts(self):
        """Acknowledging on the iPhone also silences the Mac."""
        if not self.alarm.active or not self.pushover.configured:
            return
        for p in self.store.snapshot():
            if p.alerted_at and not p.acknowledged_at and p.pushover_receipt:
                try:
                    if self.pushover.acknowledged(p.pushover_receipt):
                        self.acknowledge(p.id, "pushover")
                except PushoverError:
                    pass
        if self._test_receipt:
            try:
                if self.pushover.acknowledged(self._test_receipt):
                    self._test_receipt = ""
                    self.alarm.stop("test")
                    self.store.log_event("acknowledged", None, via="pushover", test=True)
            except PushoverError:
                pass

    # ---------- tests from the UI ----------
    def test_channels(self, channels: list[str]) -> dict:
        title, msg = "📦 PkgWatch TEST alarm", "This is a test. Acknowledge to silence."
        out = {}
        self.store.log_event("test", None, channels=channels)
        if "alarm" in channels:
            self.alarm.start("test", title, msg)
            out["alarm"] = "playing — press Acknowledge"
        if "mac" in channels:
            out["mac"] = "sent" if self.mac_notify(title, msg) else "failed"
        if "pushover" in channels:
            if not self.pushover.configured:
                out["pushover"] = "not configured"
            else:
                s = self.store.settings
                try:
                    self._test_receipt = self.pushover.send(title, msg, priority=2, sound=s.get("pushover_sound", ""),
                                                            retry=s["pushover_retry"], expire=600)
                    out["pushover"] = "sent (emergency priority; acknowledge on phone or here)"
                except PushoverError as e:
                    out["pushover"] = f"failed: {e}"
        return out

    def state(self) -> dict:
        pkgs = []
        for p in self.store.snapshot():
            d = p.to_dict()
            d["status_text"] = Status(p.status).label if p.status in Status._value2member_map_ else p.status
            d["carrier_name"] = self._carrier_name(p)
            d["public_url"] = self._public_url(p)
            d["interval_effective"] = self.interval(p)
            with self._lock:
                d["next_check_at"] = self._next.get(p.id) if p.active else None
                d["checking"] = p.id in self._inflight
            pkgs.append(d)
        pkgs.sort(key=lambda d: (not d["active"], -d["added_at"]))
        return {
            "packages": pkgs,
            "alarm": self.alarm.active,
            "sleep_prevented": self.sleep.held,
            "pushover_configured": self.pushover.configured,
            "settings": self.store.settings,
            "carriers": carriers.choices(),
            "events": self.store.recent_events(80),
            "now": time.time(),
            "started_at": self.started_at,
        }

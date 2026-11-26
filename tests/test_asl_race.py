import threading
import time
import unittest

from helpers import make_monitor, settle

from pkgwatch.carriers import REGISTRY
from pkgwatch.carriers.asl import ASL
from pkgwatch.models import Package, Status, TrackingError, TrackingResult

OFD, DEL = Status.OUT_FOR_DELIVERY, Status.DELIVERED


class FakePrecision:
    """Stands in for the Precision adapter; can be made slow with a gate."""

    def __init__(self, status=OFD, gate: threading.Event | None = None, error=None):
        self.status, self.gate, self.error, self.calls = status, gate, error, 0

    def track(self, pkg):
        self.calls += 1
        if self.gate:
            self.gate.wait(5)
        if self.error:
            raise self.error
        return TrackingResult(self.status, self.status.label, event_time=time.time() - 60,
                              source="precisiondeliveries.com", sub_carrier="Precision Parcel")


def ofd_pkg(**kw):
    p = Package(tracking_number="ASL0000000099", carrier="asl", status=OFD.value, sub_carrier="Precision Parcel")
    for k, v in kw.items():
        setattr(p, k, v)
    return p


class RaceTest(unittest.TestCase):
    def setUp(self):
        self.real_precision = REGISTRY["precision"]
        self.asl = ASL()
        self.asl_status = OFD
        self.asl_error = None
        self.asl._track_asl = self._fake_asl
        self.woken = []
        self.asl.on_update = self.woken.append

    def tearDown(self):
        REGISTRY["precision"] = self.real_precision

    def _fake_asl(self, pkg):
        if self.asl_error:
            raise self.asl_error
        return TrackingResult(self.asl_status, self.asl_status.label, source="x", sub_carrier="Precision Parcel",
                              date_only=True)

    def _wait_race_done(self, pkg_id):
        fut = self.asl._race.get(pkg_id)
        if fut:
            try:
                fut.result(5)
            except Exception:
                pass
        time.sleep(0.05)

    def test_precision_first_wins_and_wakes_monitor(self):
        gate = threading.Event()
        REGISTRY["precision"] = FakePrecision(DEL, gate=gate)
        pkg = ofd_pkg()
        r = self.asl.track(pkg)  # Precision still in flight: ASL answer returned without waiting
        self.assertEqual(r.status, OFD)
        gate.set()
        self._wait_race_done(pkg.id)
        self.assertEqual(self.woken, [pkg.id], "monitor woken for immediate re-check")
        r = self.asl.track(pkg)
        self.assertEqual(r.status, DEL)
        self.assertTrue(r.source.startswith("precisiondeliveries.com (beat asl)"))

    def test_asl_first_wins(self):
        REGISTRY["precision"] = FakePrecision(OFD, gate=threading.Event())
        self.asl_status = DEL
        r = self.asl.track(ofd_pkg())
        self.assertEqual((r.status, r.source), (DEL, "asldistribution.com"))
        self.assertEqual(self.asl._race, {}, "pending race dropped")

    def test_no_pileup_while_precision_slow(self):
        gate = threading.Event()
        fake = REGISTRY["precision"] = FakePrecision(OFD, gate=gate)
        pkg = ofd_pkg()
        for _ in range(5):
            self.asl.track(pkg)
        gate.set()
        self._wait_race_done(pkg.id)
        self.assertEqual(fake.calls, 1)

    def test_precision_non_final_is_ignored(self):
        REGISTRY["precision"] = FakePrecision(Status.ATTEMPTED)
        pkg = ofd_pkg()
        self.asl.track(pkg)
        self._wait_race_done(pkg.id)
        self.assertEqual(self.asl.track(pkg).status, OFD, "only final statuses may override ASL")
        self.assertEqual(self.woken, [])

    def test_precision_error_does_not_affect_asl(self):
        REGISTRY["precision"] = FakePrecision(error=TrackingError("slow"))
        pkg = ofd_pkg()
        self.asl.track(pkg)
        self._wait_race_done(pkg.id)
        self.assertEqual(self.asl.track(pkg).status, OFD)

    def test_not_racing_unless_out_for_delivery_with_precision(self):
        fake = REGISTRY["precision"] = FakePrecision(DEL)
        self.asl.track(ofd_pkg(status=Status.IN_TRANSIT.value))
        self.asl.track(ofd_pkg(sub_carrier="Other Courier"))
        time.sleep(0.05)
        self.assertEqual(fake.calls, 0)

    def test_asl_failure_reuses_inflight_precision(self):
        gate = threading.Event()
        fake = REGISTRY["precision"] = FakePrecision(DEL, gate=gate)
        pkg = ofd_pkg()
        self.asl_error = TrackingError("asl down")
        threading.Timer(0.1, gate.set).start()
        r = self.asl.track(pkg)
        self.assertEqual(r.status, DEL)
        self.assertIn("fallback", r.source)
        self.assertEqual(fake.calls, 1)


class MonitorWakeTest(unittest.TestCase):
    def test_check_now_during_inflight_poll_is_not_lost(self):
        m = make_monitor([OFD])
        p = m.store.add(Package(tracking_number="W1", carrier="scripted"))
        with m._lock:
            m._inflight.add(p.id)  # a poll is running...
        m.check_now(p.id)  # ...when Precision wakes us
        m._check_safe(p.id)  # the running poll finishes and would reschedule 30 s out
        self.assertEqual(m._next[p.id], 0, "re-check requested during the poll runs right away")

    def test_normal_poll_reschedules(self):
        m = make_monitor([OFD])
        p = m.store.add(Package(tracking_number="W3", carrier="scripted"))
        with m._lock:
            m._inflight.add(p.id)
        m._check_safe(p.id)
        self.assertGreater(m._next[p.id], time.time())

    def test_single_alert_when_both_sources_report_delivered(self):
        m = make_monitor([DEL, DEL])
        p = m.store.add(Package(tracking_number="W2", carrier="scripted"))
        m.check(p.id)
        m.store.update(p.id, active=True)  # simulate the second source's late answer arriving
        m.check(p.id)
        settle()
        self.assertEqual(len(m.alarm.started), 1)
        self.assertEqual(len([s for s in m.pushover.sent if s[0] == 2]), 1)


if __name__ == "__main__":
    unittest.main()

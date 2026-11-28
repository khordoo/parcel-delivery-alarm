import time
import unittest

from helpers import make_monitor, settle

from pkgwatch.models import Package, Status, TrackingError
from pkgwatch.store import Store

OFD, DEL, ATT = Status.OUT_FOR_DELIVERY, Status.DELIVERED, Status.ATTEMPTED


def add(m, number="X1"):
    return m.store.add(Package(tracking_number=number, carrier="scripted"))


class TransitionTest(unittest.TestCase):
    def test_out_for_delivery_then_delivered_alerts_once(self):
        m = make_monitor([OFD, OFD, DEL, DEL])
        p = add(m)
        m.check(p.id)
        settle()
        self.assertEqual(m.store.get(p.id).status, "out_for_delivery")
        self.assertEqual(m.alarm.started, [])
        self.assertEqual(len(m.mac_log), 1, "heads-up notification for out for delivery")
        self.assertEqual([s[0] for s in m.pushover.sent], [0])

        m.check(p.id)  # unchanged: no new notification
        settle()
        self.assertEqual(len(m.mac_log), 1)

        m.check(p.id)  # delivered
        settle()
        got = m.store.get(p.id)
        self.assertEqual(got.status, "delivered")
        self.assertFalse(got.active, "polling stops after delivery")
        self.assertIsNotNone(got.alerted_at)
        self.assertEqual(len(m.alarm.started), 1)
        self.assertEqual([s[0] for s in m.pushover.sent], [0, 2], "emergency priority push")
        self.assertEqual(got.pushover_receipt, "r2")

        calls = m.carrier.calls
        m.check(p.id)  # inactive: no request, no alert
        settle()
        self.assertEqual(m.carrier.calls, calls)
        self.assertEqual(len(m.alarm.started), 1)

    def test_attempted_and_exception_do_not_alarm(self):
        m = make_monitor([ATT, Status.EXCEPTION])
        p = add(m)
        m.check(p.id)
        m.check(p.id)
        settle()
        self.assertEqual(m.alarm.started, [])
        self.assertTrue(m.store.get(p.id).active)
        self.assertEqual([s[0] for s in m.pushover.sent], [1, 1], "high-priority heads-up")

    def test_already_delivered_when_added_alerts(self):
        m = make_monitor([DEL])
        p = add(m)
        m.check(p.id)
        settle()
        self.assertEqual(len(m.alarm.started), 1)

    def test_ready_for_pickup_alerts_and_can_be_disabled(self):
        m = make_monitor([Status.AVAILABLE_FOR_PICKUP])
        p = add(m)
        m.check(p.id)
        settle()
        self.assertEqual(len(m.alarm.started), 1)
        self.assertIn("pickup", m.alarm.started[0][1])

        m2 = make_monitor([Status.AVAILABLE_FOR_PICKUP])
        m2.store.update_settings({"alert_statuses": ["delivered"]})
        p2 = add(m2)
        m2.check(p2.id)
        settle()
        self.assertEqual(m2.alarm.started, [])
        self.assertFalse(m2.store.get(p2.id).active, "terminal status still stops polling")


class ErrorHandlingTest(unittest.TestCase):
    def test_failure_is_not_a_status_change(self):
        m = make_monitor([OFD, TrackingError("network error: boom"), TrackingError("network error: boom"), OFD])
        p = add(m)
        m.check(p.id)
        ok_at = m.store.get(p.id).last_success_at
        m.check(p.id)
        got = m.store.get(p.id)
        self.assertEqual(got.status, "out_for_delivery", "status kept, not reset to unknown")
        self.assertEqual(got.consecutive_errors, 1)
        self.assertEqual(got.last_success_at, ok_at)
        self.assertGreater(got.last_checked_at, ok_at)
        self.assertIn("boom", got.last_error)

        m.check(p.id)
        self.assertEqual(m.store.get(p.id).consecutive_errors, 2)
        m.check(p.id)
        got = m.store.get(p.id)
        self.assertEqual(got.consecutive_errors, 0)
        self.assertEqual(got.last_error, "")
        kinds = [e["kind"] for e in m.store.recent_events(50, include_polls=True)]
        self.assertIn("recovered", kinds)
        self.assertEqual(kinds.count("error"), 2)

    def test_exponential_backoff_and_retry_after(self):
        m = make_monitor([TrackingError("x")] * 4 + [TrackingError("slow down", retry_after=200)])
        p = add(m)
        delays = []
        for _ in range(5):
            t0 = time.time()
            m.check(p.id)
            delays.append(m._next[p.id] - t0)
        for got, want in zip(delays, [10, 20, 40, 80, 200]):
            self.assertAlmostEqual(got, want, delta=want * 0.06 + 0.5)

    def test_error_warning_sent_once(self):
        m = make_monitor([TrackingError("down")])
        m.store.update_settings({"error_alert_after": 3})
        p = add(m)
        for _ in range(6):
            m.check(p.id)
        settle()
        warnings = [t for t, _ in m.mac_log if "failing" in t]
        self.assertEqual(len(warnings), 1)

    def test_blocked_backs_off_hard(self):
        m = make_monitor([TrackingError("captcha", blocked=True)])
        p = add(m)
        t0 = time.time()
        m.check(p.id)
        self.assertGreater(m._next[p.id] - t0, 800)

    def test_adapter_crash_counts_as_failure(self):
        m = make_monitor([OFD])
        p = add(m)
        m.carrier.track = lambda pkg: 1 / 0
        m.check(p.id)
        got = m.store.get(p.id)
        self.assertEqual(got.consecutive_errors, 1)
        self.assertIn("ZeroDivisionError", got.last_error)


class AckAndPersistenceTest(unittest.TestCase):
    def test_acknowledge_stops_alarm_and_cancels_push(self):
        m = make_monitor([DEL])
        p = add(m)
        m.check(p.id)
        settle()
        self.assertTrue(m.alarm.active)
        m.acknowledge(None, "ui")
        settle()
        self.assertFalse(m.alarm.active)
        self.assertIsNotNone(m.store.get(p.id).acknowledged_at)
        self.assertEqual(m.pushover.cancelled, ["r1"])

    def test_phone_acknowledgement_silences_mac(self):
        m = make_monitor([DEL])
        p = add(m)
        m.check(p.id)
        settle()
        m.pushover.acked.add("r1")
        m._poll_receipts()
        settle()
        self.assertFalse(m.alarm.active)
        self.assertEqual(m.pushover.cancelled, [], "no need to cancel an acknowledged receipt")

    def test_restart_resumes_unacked_alarm_without_new_push(self):
        m = make_monitor([OFD, DEL])
        p = add(m)
        m.check(p.id)
        m.check(p.id)
        settle()
        sent = len(m.pushover.sent)

        m2 = make_monitor([DEL], data_dir=m.store.dir)
        m2._resume_unacknowledged()
        got = m2.store.get(p.id)
        self.assertEqual(got.status, "delivered")
        self.assertFalse(got.active)
        self.assertEqual(len(m2.alarm.started), 1, "alarm resumes after restart until acknowledged")
        self.assertEqual(m2.pushover.sent, [], "no duplicate push after restart")
        m2.tick()
        settle()
        self.assertEqual(m2.carrier.calls, 0, "delivered package is not polled again")
        self.assertEqual(len(m.pushover.sent), sent)

    def test_resume_after_delivery_never_realerts(self):
        m = make_monitor([DEL])
        p = add(m)
        m.check(p.id)
        settle()
        m.acknowledge()
        m.store.update(p.id, active=True)
        m.check(p.id)
        settle()
        self.assertEqual(len(m.alarm.started), 1)

    def test_state_persists_across_restart(self):
        m = make_monitor([OFD])
        p = add(m, "PERSIST1")
        m.check(p.id)
        s2 = Store(m.store.dir)
        got = s2.get(p.id)
        self.assertEqual((got.tracking_number, got.status, got.active), ("PERSIST1", "out_for_delivery", True))

    def test_duplicate_add_returns_existing(self):
        m = make_monitor([OFD])
        a = add(m, "DUP1")
        b = add(m, "DUP1")
        self.assertEqual(a.id, b.id)
        self.assertEqual(len(m.store.packages), 1)


class SchedulingTest(unittest.TestCase):
    def test_sleep_guard_follows_active_packages(self):
        m = make_monitor([OFD, DEL])
        m.tick()
        self.assertFalse(m.sleep.held)
        p = add(m)
        m.tick()
        self.assertTrue(m.sleep.held)
        m.check(p.id)
        m.check(p.id)
        settle()
        m.acknowledge()
        m.tick()
        self.assertFalse(m.sleep.held, "released once nothing is active and alarm is silenced")

    def test_interval_floor(self):
        m = make_monitor([OFD])
        p = m.store.add(Package(tracking_number="DELCF000000001", carrier="intelcom", interval=5))
        self.assertEqual(m.interval(p), 30, "Intelcom CDN cache is 30 s; faster polling is pointless")
        m.store.update_settings({"default_interval": 45})
        p2 = add(m)
        self.assertEqual(m.interval(p2), 45)


class NotFoundYetTest(unittest.TestCase):
    def test_not_found_is_not_an_error_and_polls_gently_then_speeds_up(self):
        m = make_monitor([Status.NOT_FOUND, Status.NOT_FOUND, OFD])
        p = add(m)
        for _ in range(2):
            t0 = time.time()
            m.check(p.id)
            got = m.store.get(p.id)
            self.assertEqual((got.status, got.consecutive_errors, got.error_notified), ("not_found", 0, False))
            self.assertGreater(m._next[p.id] - t0, 280, "checks every ~5 min while not in carrier's system")
        t0 = time.time()
        m.check(p.id)
        self.assertEqual(m.store.get(p.id).status, "out_for_delivery")
        self.assertLess(m._next[p.id] - t0, 35, "back to normal interval once the carrier has it")
        settle()
        self.assertEqual(m.alarm.started, [])


class SeenAtTest(unittest.TestCase):
    def _res(self, *labels):
        from pkgwatch.models import Event, TrackingResult
        return TrackingResult(OFD, labels[0], events=[Event(time=1.0, label=lab) for lab in labels], date_only=True)

    def test_first_seen_is_stamped_once_and_carried(self):
        from pkgwatch.monitor import Monitor
        pkg = Package(tracking_number="S1", carrier="scripted")
        ev = Monitor._stamp_seen(pkg, self._res("Manifest Scan"), now=100.0)
        self.assertIsNone(ev[0]["seen_at"], "existed before monitoring started")
        pkg.events, pkg.last_success_at = ev, 100.0
        ev = Monitor._stamp_seen(pkg, self._res("Out for Delivery", "Manifest Scan"), now=200.0)
        self.assertEqual([e["seen_at"] for e in ev], [200.0, None])
        pkg.events = ev
        ev = Monitor._stamp_seen(pkg, self._res("Out for Delivery", "Manifest Scan"), now=300.0)
        self.assertEqual([e["seen_at"] for e in ev], [200.0, None], "not re-stamped on later polls")

    def test_first_appearance_after_not_found_is_stamped(self):
        from pkgwatch.monitor import Monitor
        pkg = Package(tracking_number="S2", carrier="scripted", status="not_found", last_success_at=50.0)
        ev = Monitor._stamp_seen(pkg, self._res("Manifest Scan"), now=100.0)
        self.assertEqual(ev[0]["seen_at"], 100.0)

    def test_legacy_events_backfilled_from_last_change(self):
        from pkgwatch.monitor import Monitor
        pkg = Package(tracking_number="S3", carrier="scripted", status_label="Manifest Scan",
                      last_success_at=90.0, last_change_at=42.0,
                      events=[{"time": 1.0, "label": "Manifest Scan", "location": "", "status": "x", "code": ""}])
        ev = Monitor._stamp_seen(pkg, self._res("Manifest Scan"), now=100.0)
        self.assertEqual(ev[0]["seen_at"], 42.0)


if __name__ == "__main__":
    unittest.main()

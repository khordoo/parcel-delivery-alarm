import tempfile
import time

from pkgwatch import carriers
from pkgwatch.models import Status, TrackingError, TrackingResult
from pkgwatch.monitor import Monitor
from pkgwatch.store import Store


class ScriptedCarrier(carriers.Carrier):
    """Returns the next item of a script per call (Status, TrackingError or (Status, label))."""
    id = "scripted"
    name = "Scripted"
    min_interval = 0

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def track(self, pkg):
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(item, TrackingError):
            raise item
        status, label = item if isinstance(item, tuple) else (item, item.label)
        return TrackingResult(status, label, event_time=time.time() - 42, source="scripted")


class FakeAlarm:
    def __init__(self):
        self.started, self.stopped, self._active = [], [], {}

    @property
    def active(self):
        return dict(self._active)

    def start(self, key, title, message):
        self.started.append((key, title, message))
        self._active[key] = message

    def stop(self, key=None):
        keys = list(self._active) if key is None else [k for k in [key] if k in self._active]
        for k in keys:
            self._active.pop(k)
        self.stopped.append(key)
        return keys


class FakePushover:
    configured = True

    def __init__(self):
        self.sent, self.cancelled, self.acked = [], [], set()

    def send(self, title, message, priority=0, **kw):
        self.sent.append((priority, title, message))
        return f"r{len(self.sent)}" if priority == 2 else ""

    def acknowledged(self, receipt):
        return receipt in self.acked

    def cancel(self, receipt):
        self.cancelled.append(receipt)

    def refresh(self):
        pass


class FakeSleep:
    def __init__(self):
        self.held = False
        self.history = []

    def set(self, want):
        self.held = want
        self.history.append(want)


def make_monitor(script=(), data_dir=None):
    data_dir = data_dir or tempfile.mkdtemp(prefix="pkgwatch-test-")
    store = Store(data_dir)
    carrier = ScriptedCarrier(script)
    mac = []
    m = Monitor(store, alarm=FakeAlarm(), pushover=FakePushover(),
                mac_notify=lambda t, msg: mac.append((t, msg)) or True,
                sleep_guard=FakeSleep(),
                get_carrier=lambda cid: carrier if cid == "scripted" else carriers.get(cid))
    m.mac_log = mac
    m.carrier = carrier
    return m


def settle(seconds=0.3):
    """Let fire-and-forget notification threads run (all fakes are instant)."""
    time.sleep(seconds)

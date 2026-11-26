"""CLI entry point: python3 -m pkgwatch <command>."""

from __future__ import annotations

import argparse
import getpass
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
import webbrowser

from . import carriers, keychain
from .models import Package, TrackingError
from .store import DATA_DIR, Store


def setup_logging(data_dir, verbose=False):
    data_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(data_dir / "pkgwatch.log", maxBytes=5_000_000, backupCount=3)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers[:] = [fh, sh]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def cmd_serve(args):
    from .monitor import Monitor
    from .web import serve

    setup_logging(DATA_DIR, args.verbose)
    store = Store()
    monitor = Monitor(store)
    httpd = serve(monitor, port=args.port)
    store.log_event("startup", None, port=args.port, pid=os.getpid(),
                    active=[p.tracking_number for p in store.active()])
    monitor.start()
    url = f"http://127.0.0.1:{args.port}/"
    logging.info("PkgWatch running at %s (data: %s)", url, DATA_DIR)

    def shutdown(*_):
        logging.info("shutting down")
        store.log_event("shutdown", None)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    if args.open:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        monitor.stop()


def cmd_check(args):
    """One-shot lookup (no state, no alerts) — handy for testing adapters."""
    setup_logging(DATA_DIR / "cli", args.verbose)
    cid, number, url = carriers.resolve(args.input, args.carrier or "")
    c = carriers.get(cid)
    t = time.time()
    try:
        r = c.track(Package(tracking_number=number, carrier=cid, url=url))
    except TrackingError as e:
        print(f"REQUEST FAILED ({c.name}, {time.time() - t:.1f}s): {e}")
        return 2
    fmt = "%Y-%m-%d" if r.date_only else "%Y-%m-%d %H:%M:%S"
    print(f"{c.name} {number}  [{time.time() - t:.1f}s via {r.source}]")
    print(f"  status: {r.status.value} — {r.label}" + (f"  (final mile: {r.sub_carrier})" if r.sub_carrier else ""))
    for e in r.events:
        ts = time.strftime(fmt, time.localtime(e.time)) if e.time else "?"
        print(f"  {ts:19}  {e.status:22} {e.label}" + (f" — {e.location}" if e.location else ""))
    return 0


def cmd_timeline(args):
    """Status changes, alerts and acknowledgements with timestamps (compare with Amazon's email/app)."""
    store = Store()
    keep = {"added", "status_change", "alert", "notification_sent", "notification_failed", "enriched",
            "acknowledged", "polling_stopped", "error", "recovered", "startup", "shutdown"}
    rows = [e for e in reversed(store.recent_events(5000)) if e["kind"] in keep
            and (not args.number or args.number.upper() in str(e.get("number", "")))]
    for e in rows:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["ts"]))
        extra = ""
        if e["kind"] in ("status_change", "enriched"):
            ct = e.get("carrier_event_time")
            extra = f"{e['old']} -> {e['new']} \"{e.get('label', '')}\"" if "new" in e else "exact carrier time"
            if ct and e.get("carrier_to_detection_s") is not None:
                extra += f" carrier@{time.strftime('%m-%d %H:%M:%S', time.localtime(ct))}"
            elif ct:
                extra += f" carrier@{time.strftime('%m-%d', time.localtime(ct))} (date only)"
            if e.get("carrier_to_detection_s") is not None:
                extra += f" (+{e['carrier_to_detection_s']}s)"
            if e.get("source"):
                extra += f" [{e['source']}]"
        elif e["kind"].startswith("notification"):
            extra = e.get("channel", "") + (f" {e['error']}" if e.get("error") else "")
        elif e["kind"] == "error":
            extra = e.get("error", "")
        elif e["kind"] == "acknowledged":
            extra = f"via {e.get('via')} after {e.get('after_s')}s"
        print(f"{ts}  {e['kind']:20} {e.get('number', ''):16} {extra}")
    return 0


def cmd_set_pushover(args):
    print("Pushover credentials are stored in your macOS login Keychain (service 'pkgwatch.pushover').")
    token = getpass.getpass("Application API token: ").strip()
    user = getpass.getpass("User key: ").strip()
    keychain.set("app_token", token)
    keychain.set("user_key", user)
    print("Saved. Restart not required; use the UI's 'Test Pushover' button to verify.")
    return 0


def cmd_test_alarm(args):
    from .alarm import Alarm

    done = threading.Event()
    a = Alarm(DATA_DIR, lambda: {"alarm_volume": args.volume, "mac_dialog": True}, done.set)
    a.start("test", "PkgWatch test", "Click Acknowledge (or Ctrl-C) to stop")
    try:
        done.wait(args.seconds)
    except KeyboardInterrupt:
        pass
    a.stop()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pkgwatch", description="Low-latency package delivery monitor")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run monitor + web UI")
    s.add_argument("--port", type=int, default=int(os.environ.get("PKGWATCH_PORT", 8787)))
    s.add_argument("--open", action="store_true", help="open the UI in a browser")
    s.set_defaults(fn=cmd_serve)
    c = sub.add_parser("check", help="one-shot status lookup")
    c.add_argument("input", help="tracking number or URL")
    c.add_argument("--carrier", choices=sorted(carriers.REGISTRY))
    c.set_defaults(fn=cmd_check)
    tl = sub.add_parser("timeline", help="show detection/notification timestamps")
    tl.add_argument("number", nargs="?", help="filter by tracking number")
    tl.set_defaults(fn=cmd_timeline)
    p = sub.add_parser("set-pushover", help="store Pushover credentials in the Keychain")
    p.set_defaults(fn=cmd_set_pushover)
    t = sub.add_parser("test-alarm", help="play the alarm locally")
    t.add_argument("--seconds", type=float, default=15)
    t.add_argument("--volume", type=int, default=0, help="force system volume (0 = leave as is)")
    t.set_defaults(fn=cmd_test_alarm)
    args = ap.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())

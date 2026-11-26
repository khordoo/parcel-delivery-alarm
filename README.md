# PkgWatch

PkgWatch is a small package-delivery monitor for macOS. It polls carriers directly. When a package is delivered, it plays a loud, repeating siren on the Mac and sends an emergency-priority Pushover push to the iPhone. Both keep going until you acknowledge one of them.

- Pure Python standard library: no `pip install`, no cloud, no subscriptions (Pushover is a one-time ~US$5).
- Runs as a LaunchAgent, independent of any terminal or Claude Code session.
- Local web UI at <http://127.0.0.1:8787/>.

## Quick start

```bash
git clone https://github.com/khordoo/pkgwatch.git && cd pkgwatch
./scripts/install-launchagent.sh          # starts now and at every login, restarts on crash
open http://127.0.0.1:8787/                # add packages, test alarm, configure
python3 -m pkgwatch set-pushover           # or use the UI form (stored in Keychain)
```

Other commands:

```bash
python3 -m pkgwatch check ASL0012345678     # one-shot lookup, no state/alerts
python3 -m pkgwatch timeline                # detection + notification timestamps
python3 -m pkgwatch test-alarm --seconds 5  # hear the siren
python3 -m pkgwatch serve --port 8787       # run in foreground instead of launchd
./scripts/uninstall-launchagent.sh
PYTHONPATH=.:tests python3 -m unittest discover -s tests   # 53 tests
```

Data lives in `~/Library/Application Support/PkgWatch/`:
- `state.json`: packages, last status, settings. Written atomically.
- `events.jsonl`: every poll, status change, error, alert, notification and acknowledgement, with epoch and local timestamps.
- `pkgwatch.log`: rotating application log.

## 1. Investigation summary

Tested in November 2026 with a real ASL package (out for delivery → delivered) and a real Intelcom package (delivered to a pickup point). Numbers below are illustrative.

| Approach | Latency | Reliability | Coverage | Cost / auth | Verdict |
|---|---|---|---|---|---|
| **Intelcom public JSON** `GET intelcom.ca/cfworker/v3/tracking/{id}/` | ~0.1 s response; **Cloudflare cache max-age=30 s** | Structured, has carrier's own `isDelivered` flag and status codes | Intelcom/Dragonfly | Free, no auth | **Used** |
| **ASL site AJAX** `POST asldistribution.com/wp-admin/admin-ajax.php action=order_tracking` | ~2 s, uncached (`no-store`) | HTML fragment (parser can break on redesign); dates only, no times | ASL incl. its final-mile partners | Free, no auth, no CAPTCHA (page nonce not enforced) | **Used (primary for ASL)** |
| **Precision Parcel JSON** `track.precisiondeliveries.com/api/shipment-itemId/{id}` | 0.3–25 s (slow, variable; occasional transient upstream errors) | Structured; exact UTC scan times, signature/photo flags | ASL packages whose final mile is Precision | Anonymous session cookie, the same one the public site creates for every visitor | **Raced against ASL while out for delivery; fallback; exact timestamps** |
| Carrier webhooks | n/a | Neither carrier offers a public developer webhook. Precision has SMS/e-mail opt-in only | — | — | Not usable programmatically |
| 17TRACK API | Refreshes every **6–12 h** (up to 24 h) | Good | Both carriers listed (keys 100555, 100989) | $19 / 300 shipments, API key | Too slow as a primary source |
| AfterShip | Refreshes every **3–12 h** | Good | Broad | Paid plans | Too slow |
| Ship24 | Not published (free: 10 shipments/mo) | Good | Broad | API key | Too slow / too limited |
| Playwright / browser automation | Same data as above, heavier | — | — | — | Not needed: every page is backed by an API |
| Amazon tracking page | — | Needs Amazon login (auth wall) | — | — | Skipped: we don't automate authenticated sessions |
| LLM page interpretation | — | — | — | — | Not needed: everything is deterministic. A small model (e.g. Claude Haiku 4.5) could later classify unfamiliar pages, but it is not used for routine detection. |

**Decision.** Use direct carrier endpoints. They beat the universal trackers by hours. The universal services are worth adding only as an optional slow backstop for carriers with no adapter.

## 2. Architecture

```
pkgwatch/
  models.py        Status enum, TrackingResult, Package, TrackingError (failure ≠ unchanged)
  classify.py      ordered, deterministic text → Status rules (EN/FR); negatives win over "delivered"
  http.py          stdlib HTTP session; transport failures, 429, 5xx, bot walls → TrackingError
  carriers/        one file per carrier, auto-discovered via @register
    intelcom.py  asl.py  precision.py  generic.py (custom URL)  simulator.py (testing)
  monitor.py       scheduler, backoff, dedup, alert fan-out, ack, Pushover receipt polling
  alarm.py         generated siren via afplay loop + modal "Acknowledge" dialog, volume boost/restore
  notify.py        macOS Notification Center + Pushover client (with retries)
  keychain.py      Pushover secrets in the login Keychain
  power.py         caffeinate -i -w <pid>
  web.py + static/ local UI and JSON API (127.0.0.1 only, Host check + CSRF header)
```

**Adding a carrier.** Drop `pkgwatch/carriers/foo.py` with a `Carrier` subclass decorated with `@register`. It must implement `track(pkg) -> TrackingResult` and raise `TrackingError` on failure. Optional parts: `patterns` (auto-detect), `url_hosts`/`url_params` (URL parsing), `min_interval`, `enrich()`. You don't need to touch the core.

**Adding a package.** Paste a tracking number or URL.
- Detection order: URL host, then number pattern, then a cheap probe of the probe-capable carriers, then manual choice.
- An unrecognised URL becomes a *custom URL* package. It gets a plain GET plus strict delivery phrases, and you can give it your own "delivered" phrase. Pages that only render with JavaScript, need a login, or show a CAPTCHA are reported as errors. They are never bypassed.

**Statuses.** `unknown, not_found, info_received, in_transit, out_for_delivery, attempted, exception, available_for_pickup, delivered, returned`.
- Alarm statuses (configurable): `delivered` and `available_for_pickup`.
- Heads-up only (no alarm): out for delivery, attempted, exception.
- Polling stops at: delivered, ready-for-pickup, returned.

**ASL race mode.**
- While an ASL package is *out for delivery* and Precision Parcel is the final-mile carrier, every poll also queries Precision in the background.
- Whichever source reports *delivered / ready for pickup* first triggers the alert:
  - The fast ASL poll (~1–2 s) never waits for Precision, which can take ~25 s.
  - When Precision finishes with a final status, it wakes the monitor for an immediate re-check instead of waiting for the next 30 s tick.
- Only final statuses from Precision may override ASL, so the displayed status never flip-flops. The persisted `alerted_at` guard keeps it to a single alert even when both sources agree.
- After the alert, the slower source is watched for up to 10 min. The log then records which source won and by how much (`python3 -m pkgwatch timeline`).

**Reliability rules.**
- **Failed request ≠ unchanged status.** On a failure the last known status is kept. `consecutive_errors`, `last_error` and `last_success_at` are shown in the UI ("status may be stale").
- **Backoff.** Retries wait 10 s, 20, 40, 80, 160, then cap at 300 s. A `Retry-After` header is honoured. A bot wall or CAPTCHA backs off 15 min. After 10 consecutive failures (configurable) you get one "Tracking is failing" warning.
- **Duplicate prevention.** `alerted_at` is persisted *before* any notification goes out, so a restart or crash can't send a second push. If the app restarts with an alarm still unacknowledged (within 24 h), the local siren resumes but no new push is sent.
- **Acknowledgement.** Acknowledging anywhere stops everything:
  - The UI button, the Mac dialog, or the **Acknowledge button in Pushover on the phone** (receipt polled every 15 s) all count.
  - A local ack cancels the Pushover emergency retries.
- **Fan-out.** Siren, Mac notification and Pushover are fired on separate threads, so a slow network doesn't delay the siren.
- **Polling.** Default is 30 s with ±5 % jitter. Each adapter has a floor: Intelcom 30 s (its CDN cache), custom URLs 60 s. The UI's "Check now" forces an immediate poll.

## Real-world result

The first live ASL delivery went like this:
- The driver's scanner logged *Delivered – Safe location* at 14:15:30.
- ASL's tracking site first showed it at 14:17:36.
- PkgWatch fired the Mac siren, the Mac notification and an emergency Pushover push in the same second.
- The alert was acknowledged 6 s later.
- Amazon's site had **not** updated yet.

Roughly 2 min of the gap was upstream: the scanner sync plus ASL's site, which kept saying "Out for Delivery" on three polls after the scan time. PkgWatch's own share was under 30 s. Race mode (above) was added to find out whether Precision's tracker is ahead of ASL's.

## 3. Pushover setup (iPhone)

1. Install **Pushover** from the App Store and create an account. After the 30-day trial it is a one-time purchase, ~US$5.
2. At <https://pushover.net> copy your **User Key**.
3. Click **Create an Application/API Token**, name it `PkgWatch`, and copy the **API Token**.
4. Store both, using either:
   - the UI → *Pushover* → *Save to Keychain*, or
   - `python3 -m pkgwatch set-pushover` (hidden prompt).

   They are stored in the macOS **login Keychain** (service `pkgwatch.pushover`) through `security -i` on stdin, so they never show up in process lists, `state.json` or logs. To inspect or delete them: Keychain Access → search "PkgWatch". Environment variables `PUSHOVER_APP_TOKEN` / `PUSHOVER_USER_KEY` override the Keychain if set.
5. On the iPhone, open Settings → Notifications → Pushover and allow notifications, sounds and **Critical Alerts** (Pushover asks the first time). Emergency-priority messages then ring through Silent mode and Focus.
6. In the UI press **Test both channels**. The siren plays on the Mac and an emergency push repeats on the phone every 30 s. Acknowledge on the phone and confirm the Mac goes quiet within about 15 s.

Delivery alerts use Pushover **priority 2 (emergency)** with `retry=30 s`, `expire=3600 s` and sound `persistent`, all configurable.

## 4. Limitations that affect latency or reliability

- **Detection latency** is roughly poll interval + carrier propagation + cache.
  - Intelcom's endpoint is CDN-cached for 30 s, so worst case is ~60 s after Intelcom records the event.
  - ASL is uncached. Worst case is ~30 s plus ASL's response time (~2 s).
  - What can't be measured from here is how long the *driver's scanner* takes to sync to the carrier. That is usually seconds, but can be minutes in poor cell coverage.
- **ASL timestamps are date-only.** For Precision-delivered ASL packages, PkgWatch fetches Precision's exact scan time *after* sounding the alarm, so the slow Precision API never delays the alert. The UI shows carrier-time → detection latency where it is known.
- **Endpoints are unofficial.** These are the same public endpoints the carriers' own tracking pages use, not documented APIs. A redesign can break a parser. That shows up as a visible error (and a warning after N failures), never as a silent "no change". Polling stays low-rate (one request per package per 30 s) and identifies itself in the User-Agent.
- **Sleep, lid and power.**
  - `caffeinate -i` blocks *idle* system sleep while any package is active or an alarm is ringing. The display still sleeps. It is released when nothing is active, and it dies with the app (`-w pid`).
  - **Closing the MacBook lid sleeps the Mac anyway.** The exception is clamshell mode (power + external display + keyboard/mouse). While asleep, nothing polls and the siren can't play. Keep the lid open, or rely on…
  - …Pushover. It only reaches the phone if the Mac was awake when the delivery was detected. A sleeping Mac detects nothing.
  - Low battery or the  menu → Sleep also override caffeinate.
- **Network loss.** Polls fail and back off (capped at 5 min, so recovery is quick), and the UI marks the status stale. Pushover sends retry 4× with backoff. If the Mac is offline, the push can't be sent, but the local siren still plays once the delivery is detected.
- **Restarts and logouts.**
  - The LaunchAgent starts at *login*, not at boot. After a reboot nothing runs until you log in. Enable automatic login if you need unattended recovery.
  - State persists, so monitoring resumes where it left off. Unacknowledged alarms resume locally.
- **Audio.**
  - The siren goes to the current output device. If headphones or Bluetooth are connected, that's where it plays.
  - Volume is raised during the alarm and restored afterwards, but only on devices with software volume. Outputs without software volume (e.g. HDMI/DisplayPort audio, some interfaces) play at the device's own level.
  - Focus modes don't block `afplay`. The Mac notification itself can be suppressed by Focus.
- **Dialog and notification permissions.** The first time, macOS may ask to allow notifications from "Script Editor"/`osascript`. Allow it in System Settings → Notifications.
- **Amazon** isn't monitored. Its tracking requires your Amazon login, which this app deliberately doesn't automate.

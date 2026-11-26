"""Fake carrier for end-to-end testing of alarms, errors and dedup.

Tracking number "SIM-<steps>" where steps are comma separated tokens, one per
poll; the last token repeats forever. Tokens:
  NF not found   INFO label created   TRANSIT in transit   OFD out for delivery
  ATT attempted  EXC exception        PICKUP ready for pickup  DEL delivered
  ERR request failure (network)       429 rate limited
Example: SIM-TRANSIT,OFD,ERR,ERR,OFD,DEL
Default (just "SIM"): SIM-OFD,OFD,DEL
"""

from __future__ import annotations

import threading
import time

from ..models import Package, Status, TrackingError, TrackingResult
from .base import Carrier, register

TOKENS = {
    "NF": Status.NOT_FOUND, "INFO": Status.INFO_RECEIVED, "TRANSIT": Status.IN_TRANSIT,
    "OFD": Status.OUT_FOR_DELIVERY, "ATT": Status.ATTEMPTED, "EXC": Status.EXCEPTION,
    "PICKUP": Status.AVAILABLE_FOR_PICKUP, "DEL": Status.DELIVERED,
}


@register
class Simulator(Carrier):
    id = "sim"
    name = "Simulator (testing)"
    min_interval = 2.0
    patterns = (r"SIM(-[A-Z0-9,]+)?",)

    def __init__(self):
        self._step: dict[str, int] = {}
        self._lock = threading.Lock()

    def steps(self, number: str) -> list[str]:
        spec = number.partition("-")[2]
        return [t for t in spec.split(",") if t] or ["OFD", "OFD", "DEL"]

    def track(self, pkg: Package) -> TrackingResult:
        steps = self.steps(pkg.tracking_number)
        with self._lock:
            i = self._step.get(pkg.id, 0)
            self._step[pkg.id] = i + 1
        tok = steps[min(i, len(steps) - 1)]
        if tok == "ERR":
            raise TrackingError("network error: simulated failure")
        if tok == "429":
            raise TrackingError("rate limited (simulated)", retry_after=3)
        st = TOKENS.get(tok)
        if st is None:
            raise TrackingError(f"simulator: unknown token {tok}")
        return TrackingResult(st, f"Simulated: {st.label}", event_time=time.time(), source="simulator")

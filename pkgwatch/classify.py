"""Deterministic text -> Status classification (English + French).

Rules are ordered: the first match wins. Negative/qualified outcomes
(attempted, returned, exception) are checked before "delivered" so that
"not delivered" or "delivery attempted" never trips the alarm.
"""

from __future__ import annotations

import re

from .models import Status

_RULES: list[tuple[Status, re.Pattern]] = [
    (Status.ATTEMPTED, re.compile(
        r"attempt|not delivered|undeliverable|unable to deliver|could not (be )?deliver|"
        r"missed delivery|no one (was )?(home|available)|tentative|n'a pas pu|non livr", re.I)),
    (Status.RETURNED, re.compile(
        r"return(ed|ing)? to (sender|shipper|merchant)|being returned|retour (à|a) l'exp", re.I)),
    (Status.EXCEPTION, re.compile(
        r"exception|damaged|delay|held at|on hold|lost|refused|address (issue|problem|incorrect)|"
        r"incorrect address|cancel|problem|issue with|retard|endommag", re.I)),
    (Status.AVAILABLE_FOR_PICKUP, re.compile(
        r"(available|ready) for pick ?-?up|at (the )?pick ?-?up (point|location)|"
        r"disponible pour (le )?ramassage|pr[eê]t pour (le )?ramassage", re.I)),
    (Status.DELIVERED, re.compile(
        r"\bdelivered\b|delivery (complete|completed|confirmed)|picked up by (customer|recipient)|"
        r"\blivr[ée]e?\b|signed (for )?by", re.I)),
    (Status.OUT_FOR_DELIVERY, re.compile(
        r"out for delivery|onto (the )?delivery (truck|vehicle)|with (the )?(courier|driver) for delivery|"
        r"en (cours de )?livraison|on its way to you", re.I)),
    (Status.INFO_RECEIVED, re.compile(
        r"label created|data received|information received|manifest|pre-?advi|shipment created|"
        r"order (information|info) received|en attente", re.I)),
    (Status.IN_TRANSIT, re.compile(
        r"in transit|received|arrived|departed|scan|hub|terminal|on the move|sorted|processed|"
        r"shipped|picked up|en transit|re[çc]u|has your package", re.I)),
]


def classify(*texts: str) -> Status:
    joined = " | ".join(t for t in texts if t)
    for status, pattern in _RULES:
        if pattern.search(joined):
            return status
    return Status.UNKNOWN


# Stricter phrasing used when scanning a whole web page (generic adapter):
# pages contain FAQ text like "what if my package is not delivered", so plain
# keyword matching is too loose there.
PAGE_DELIVERED = re.compile(
    r"\b(has been delivered|was delivered|delivered (on|at|to)\b|status:?\s*delivered|"
    r"delivery (complete|completed|confirmed))", re.I)
PAGE_PICKUP = re.compile(r"\b(is )?(now )?(available|ready) for pick ?-?up\b", re.I)
PAGE_OFD = re.compile(r"\bout for delivery\b", re.I)
PAGE_ATTEMPTED = re.compile(r"\b(delivery attempted|attempted delivery|unable to deliver)\b", re.I)

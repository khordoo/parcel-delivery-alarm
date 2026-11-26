"""Carrier registry, auto-detection and URL parsing."""

from __future__ import annotations

import importlib
import logging
import pkgutil

from ..models import Status, TrackingError
from .base import REGISTRY, Carrier, normalize_number, register  # noqa: F401

log = logging.getLogger(__name__)

for _m in pkgutil.iter_modules(__path__):
    if _m.name != "base":
        importlib.import_module(f"{__name__}.{_m.name}")


def get(carrier_id: str) -> Carrier:
    try:
        return REGISTRY[carrier_id]
    except KeyError:
        raise ValueError(f"unknown carrier '{carrier_id}'") from None


def choices() -> list[dict]:
    return [{"id": c.id, "name": c.name} for c in REGISTRY.values() if c.selectable]


def resolve(user_input: str, carrier_id: str = "") -> tuple[str, str, str]:
    """Turn a tracking number or URL (+ optional manual carrier) into (carrier, number, url).

    Raises ValueError with a user-facing message if it cannot decide.
    """
    text = user_input.strip()
    if not text:
        raise ValueError("enter a tracking number or URL")

    if text.lower().startswith(("http://", "https://")):
        if carrier_id and carrier_id != "generic":
            num = get(carrier_id).parse_url(text)
            if not num:
                raise ValueError(f"could not find a tracking number for {get(carrier_id).name} in that URL")
            return carrier_id, num, text
        if not carrier_id:
            for c in REGISTRY.values():
                num = c.parse_url(text)
                if num:
                    return c.id, num, text
        # Unsupported carrier: monitor the page itself.
        return "generic", text, text

    number = normalize_number(text)
    if carrier_id:
        get(carrier_id)
        if carrier_id == "generic":
            raise ValueError("the custom-URL carrier needs a tracking page URL")
        return carrier_id, number, ""

    matches = [c for c in REGISTRY.values() if c.detect(number)]
    if len(matches) == 1:
        return matches[0].id, number, ""
    candidates = matches or [c for c in REGISTRY.values() if c.probe]

    # Ambiguous or unknown format: ask the candidate carriers (one request each).
    from ..models import Package
    for c in candidates:
        try:
            r = c.track(Package(tracking_number=number, carrier=c.id))
        except TrackingError as e:
            log.info("probe %s for %s failed: %s", c.id, number, e)
            continue
        if r.status != Status.NOT_FOUND:
            return c.id, number, ""
    raise ValueError("could not detect the carrier for this number; choose it manually "
                     "or paste the carrier's tracking URL")

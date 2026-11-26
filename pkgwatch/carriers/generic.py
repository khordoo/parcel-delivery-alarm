"""Custom tracking URL for carriers without an adapter.

Fetches the page with a plain GET and looks for delivery phrases in the
visible text. Deterministic, no LLM. Works only for server-rendered pages:
JavaScript-only tracking pages, logins and CAPTCHAs are reported as errors
rather than worked around.

Options (pkg.options):
  delivered_text: exact phrase that appears only once delivered (most reliable)
"""

from __future__ import annotations

import hashlib
import html
import re

from ..classify import PAGE_ATTEMPTED, PAGE_DELIVERED, PAGE_OFD, PAGE_PICKUP
from ..http import Session, looks_blocked
from ..models import Package, Status, TrackingError, TrackingResult
from .base import Carrier, register


def page_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", " ", markup)
    markup = re.sub(r"(?s)<!--.*?-->", " ", markup)
    text = html.unescape(re.sub(r"<[^>]+>", " ", markup))
    return re.sub(r"\s+", " ", text).strip()


def analyse(markup: str, delivered_text: str = "") -> TrackingResult:
    if looks_blocked(markup):
        raise TrackingError("page is behind a CAPTCHA / bot check; not bypassing", blocked=True)
    text = page_text(markup)
    if len(text) < 200:
        raise TrackingError("page has almost no server-rendered text (JavaScript app?); "
                            "this URL can't be monitored without a dedicated adapter", blocked=True)
    digest = hashlib.sha1(text.encode()).hexdigest()[:12]
    if delivered_text:
        if delivered_text.lower() in text.lower():
            return TrackingResult(Status.DELIVERED, f'Found "{delivered_text}"', source="custom url")
        return TrackingResult(Status.UNKNOWN, f"Waiting for \"{delivered_text}\" (page {digest})", source="custom url")
    for st, rx in ((Status.ATTEMPTED, PAGE_ATTEMPTED), (Status.DELIVERED, PAGE_DELIVERED),
                   (Status.AVAILABLE_FOR_PICKUP, PAGE_PICKUP), (Status.OUT_FOR_DELIVERY, PAGE_OFD)):
        m = rx.search(text)
        if m:
            snippet = text[max(0, m.start() - 40): m.end() + 40]
            return TrackingResult(st, f"…{snippet}…", source="custom url (heuristic)")
    return TrackingResult(Status.UNKNOWN, f"No delivery phrase found (page {digest})", source="custom url")


@register
class Generic(Carrier):
    id = "generic"
    name = "Custom tracking URL"
    min_interval = 60.0  # unknown site: be extra polite

    def __init__(self):
        self.session = Session(timeout=30)

    def detect(self, number: str) -> bool:
        return False

    def parse_url(self, url: str) -> str | None:
        return None

    def track(self, pkg: Package) -> TrackingResult:
        url = pkg.url or pkg.tracking_number
        if not url.lower().startswith(("http://", "https://")):
            raise TrackingError("custom carrier needs a tracking URL", blocked=True)
        r = self.session.get(url, headers={"Accept": "text/html,application/xhtml+xml"})
        return analyse(r.text, (pkg.options or {}).get("delivered_text", ""))

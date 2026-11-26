"""Normalized data model shared by every carrier adapter."""

from __future__ import annotations

import enum
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


class Status(str, enum.Enum):
    UNKNOWN = "unknown"
    NOT_FOUND = "not_found"
    INFO_RECEIVED = "info_received"
    IN_TRANSIT = "in_transit"
    OUT_FOR_DELIVERY = "out_for_delivery"
    ATTEMPTED = "attempted"
    EXCEPTION = "exception"
    AVAILABLE_FOR_PICKUP = "available_for_pickup"
    DELIVERED = "delivered"
    RETURNED = "returned"

    @property
    def label(self) -> str:
        return STATUS_LABELS[self]


STATUS_LABELS = {
    Status.UNKNOWN: "Unknown",
    Status.NOT_FOUND: "Not found yet",
    Status.INFO_RECEIVED: "Label created",
    Status.IN_TRANSIT: "In transit",
    Status.OUT_FOR_DELIVERY: "Out for delivery",
    Status.ATTEMPTED: "Delivery attempted",
    Status.EXCEPTION: "Exception",
    Status.AVAILABLE_FOR_PICKUP: "Ready for pickup",
    Status.DELIVERED: "Delivered",
    Status.RETURNED: "Returned to sender",
}

# Polling stops once one of these is reached.
TERMINAL_STATUSES = {Status.DELIVERED, Status.AVAILABLE_FOR_PICKUP, Status.RETURNED}

# Non-alarm statuses that still deserve a heads-up notification.
NOTABLE_STATUSES = {Status.OUT_FOR_DELIVERY, Status.ATTEMPTED, Status.EXCEPTION, Status.RETURNED}


class TrackingError(Exception):
    """The tracking request failed. This is NOT the same as "status unchanged".

    retry_after: seconds the carrier asked us to wait (rate limiting).
    blocked: carrier is refusing automated access (CAPTCHA/bot wall); we do not
             try to bypass it, we back off hard and tell the user.
    """

    def __init__(self, message: str, retry_after: float | None = None, blocked: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.blocked = blocked


@dataclass
class Event:
    time: float | None  # epoch seconds, carrier's timestamp if known
    label: str
    status: str = Status.UNKNOWN.value
    location: str = ""
    code: str = ""


@dataclass
class TrackingResult:
    status: Status
    label: str  # carrier's human text for the latest event
    event_time: float | None = None  # carrier timestamp of the latest event
    events: list[Event] = field(default_factory=list)  # newest first
    source: str = ""  # which endpoint produced this
    sub_carrier: str = ""  # e.g. ASL -> "Precision Parcel"
    eta: str = ""
    # True when event_time only has day precision (latency stats are meaningless then).
    date_only: bool = False


@dataclass
class Package:
    tracking_number: str
    carrier: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    name: str = ""
    url: str = ""  # custom tracking URL (generic adapter) or the URL the user pasted
    interval: float | None = None  # per-package override, seconds
    options: dict[str, Any] = field(default_factory=dict)  # adapter-specific (e.g. generic match phrase)
    added_at: float = field(default_factory=time.time)
    active: bool = True

    status: str = Status.UNKNOWN.value
    status_label: str = ""
    sub_carrier: str = ""
    eta: str = ""
    carrier_event_time: float | None = None
    date_only: bool = False
    events: list[dict] = field(default_factory=list)

    last_checked_at: float | None = None  # last attempt (success or failure)
    last_success_at: float | None = None  # last attempt that returned data
    last_change_at: float | None = None
    consecutive_errors: int = 0
    last_error: str = ""
    error_notified: bool = False

    alerted_at: float | None = None  # duplicate-alert guard; persisted
    alert_status: str = ""
    acknowledged_at: float | None = None
    pushover_receipt: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Package":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

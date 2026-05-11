from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class TicketStatus(str, Enum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    ON_HOLD = "on_hold"
    RESOLVED = "resolved"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class EmailMessage:
    id: str
    subject: str
    body: str
    sender: str
    received_at: datetime
    sender_name: str = ""
    to_addresses: list[str] = field(default_factory=list)
    cc_addresses: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Rule:
    category: str
    keywords: list[str]
    sender_contains: str | None = None


@dataclass(frozen=True)
class ClassificationResult:
    email_id: str
    category: str
    reason: str

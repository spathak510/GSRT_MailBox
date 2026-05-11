from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProcessedEmailRecord:
    email_id: str
    category: str
    folder: str
    reason: str
    processed_at: str

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.infrastructure.persistence.models import ProcessedEmailRecord


class ProcessedEmailRepository:
    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._is_postgres = self._conn.__class__.__module__.startswith("psycopg")

    def save(self, email_id: str, category: str, folder: str, reason: str) -> None:
        processed_at = datetime.now(timezone.utc).isoformat()
        if self._is_postgres:
            self._conn.execute(
                """
                INSERT INTO processed_emails(email_id, category, folder, reason, processed_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (email_id)
                DO UPDATE SET
                    category = EXCLUDED.category,
                    folder = EXCLUDED.folder,
                    reason = EXCLUDED.reason,
                    processed_at = EXCLUDED.processed_at
                """,
                (email_id, category, folder, reason, processed_at),
            )
        else:
            self._conn.execute(
                """
                INSERT INTO processed_emails(email_id, category, folder, reason, processed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(email_id)
                DO UPDATE SET
                    category = excluded.category,
                    folder = excluded.folder,
                    reason = excluded.reason,
                    processed_at = excluded.processed_at
                """,
                (email_id, category, folder, reason, processed_at),
            )
        self._conn.commit()

    def list_processed_ids(self) -> set[str]:
        rows = self._conn.execute("SELECT email_id FROM processed_emails").fetchall()
        return {row["email_id"] for row in rows}

    def all(self) -> list[ProcessedEmailRecord]:
        rows = self._conn.execute(
            "SELECT email_id, category, folder, reason, processed_at FROM processed_emails"
        ).fetchall()
        return [
            ProcessedEmailRecord(
                email_id=row["email_id"],
                category=row["category"],
                folder=row["folder"],
                reason=row["reason"],
                processed_at=row["processed_at"],
            )
            for row in rows
        ]

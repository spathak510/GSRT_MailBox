from __future__ import annotations

from typing import Any
import sqlite3
from pathlib import Path

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]


def _sqlite_path(database_url: str) -> Path:
    if not database_url.startswith("sqlite:///"):
        raise ValueError("Invalid sqlite URL. Expected format: sqlite:///path/to/file.db")
    return Path(database_url.replace("sqlite:///", "", 1))


def get_connection(database_url: str) -> Any:
    if database_url.startswith("sqlite:///"):
        db_path = _sqlite_path(database_url)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    if database_url.startswith("postgresql://"):
        if psycopg is None or dict_row is None:
            raise RuntimeError(
                "PostgreSQL URL detected, but psycopg is not installed. "
                "Install dependencies from requirements.txt."
            )
        return psycopg.connect(database_url, row_factory=dict_row)

    raise ValueError("Unsupported DATABASE_URL. Use sqlite:///... or postgresql://...")


def init_schema(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_emails (
            email_id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            folder TEXT NOT NULL,
            reason TEXT NOT NULL,
            processed_at TEXT NOT NULL
        )
        """
    )
    conn.commit()

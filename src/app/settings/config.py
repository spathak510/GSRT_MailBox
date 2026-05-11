from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class AppConfig:
    app_env: str
    log_level: str
    database_url: str
    audit_log_path: Path
    prompts_dir: Path
    rules_path: Path
    mapping_path: Path
    openai_api_key: str | None
    graph_tenant_id: str | None
    graph_client_id: str | None
    graph_client_secret: str | None
    graph_mailbox_user: str | None
    graph_mailbox_password: str | None
    graph_timeout_seconds: int
    support_engineer_emails: list[str]
    escalation_email: str | None
    vip_titles: list[str]
    general_categories: list[str]
    worker_interval_seconds: int       # background poller interval (0 = disabled)
    webhook_base_url: str | None       # public HTTPS base URL for Graph webhook callbacks
    webhook_client_state: str          # secret token to validate incoming webhook payloads


ROOT_DIR = Path(__file__).resolve().parents[3]


def load_config() -> AppConfig:
    load_dotenv(ROOT_DIR / ".env")

    return AppConfig(
        app_env=os.getenv("APP_ENV", "dev"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        database_url=os.getenv("DATABASE_URL", "sqlite:///data/email_segregation.db"),
        audit_log_path=ROOT_DIR / os.getenv("AUDIT_LOG_PATH", "data/audit_log.jsonl"),
        prompts_dir=ROOT_DIR / os.getenv("PROMPTS_DIR", "data/prompts"),
        rules_path=ROOT_DIR / os.getenv("RULES_PATH", "data/rules/classification_rules.yaml"),
        mapping_path=ROOT_DIR / os.getenv("MAPPING_PATH", "data/mappings/category_folder_map.yaml"),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        graph_tenant_id=os.getenv("GRAPH_TENANT_ID") or None,
        graph_client_id=os.getenv("GRAPH_CLIENT_ID") or None,
        graph_client_secret=os.getenv("GRAPH_CLIENT_SECRET") or None,
        graph_mailbox_user=os.getenv("GRAPH_MAILBOX_USER") or None,
        graph_mailbox_password=os.getenv("GRAPH_MAILBOX_PASSWORD") or None,
        graph_timeout_seconds=int(os.getenv("GRAPH_TIMEOUT_SECONDS", "20")),
        support_engineer_emails=[
            e.strip()
            for e in os.getenv("SUPPORT_ENGINEER_EMAILS", "").split(",")
            if e.strip()
        ],
        escalation_email=os.getenv("ESCALATION_EMAIL") or None,
        vip_titles=[
            t.strip()
            for t in os.getenv(
                "VIP_TITLES",
                "Director,VP,Vice President,Chief,CTO,CEO,COO,CFO,SVP,EVP",
            ).split(",")
            if t.strip()
        ],
        general_categories=[
            c.strip()
            for c in os.getenv(
                "GENERAL_CATEGORIES",
                "general,marketing,newsletter,junk",
            ).split(",")
            if c.strip()
        ],
        worker_interval_seconds=int(os.getenv("WORKER_INTERVAL_SECONDS", "12*60*60")),
        webhook_base_url=os.getenv("WEBHOOK_BASE_URL") or None,
        webhook_client_state=os.getenv("WEBHOOK_CLIENT_STATE", "mailbox-auto-assistant-secret"),
    )

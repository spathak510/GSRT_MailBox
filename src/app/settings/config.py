from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _parse_category_folder_map(raw_value: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in raw_value.split(","):
        entry = item.strip()
        if not entry:
            continue
        category, separator, folder = entry.partition(":")
        if not separator:
            continue
        category_name = category.strip()
        folder_name = folder.strip()
        if category_name and folder_name:
            mapping[category_name] = folder_name
    return mapping


@dataclass(frozen=True)
class AppConfig:
    app_env: str
    log_level: str
    log_file_path: Path
    database_url: str
    audit_log_path: Path
    prompts_dir: Path
    rules_path: Path
    category_folder_map: dict[str, str]
    openai_api_key: str | None
    servicenow_base_url: str | None
    servicenow_url: str | None
    servicenow_adhoc_url: str | None
    servicenow_portal_url: str | None
    servicenow_username: str | None
    servicenow_password: str | None
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
    is_unread_mail: bool                 # flag to indicate if mail should be marked as read

ROOT_DIR = Path(__file__).resolve().parents[3]


def load_config() -> AppConfig:
    load_dotenv(ROOT_DIR / ".env")

    return AppConfig(
        app_env=os.getenv("APP_ENV", "dev"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        log_file_path=ROOT_DIR / os.getenv("LOG_FILE_PATH", "data/logs/app.log"),
        database_url=os.getenv("DATABASE_URL", "sqlite:///data/email_segregation.db"),
        audit_log_path=ROOT_DIR / os.getenv("AUDIT_LOG_PATH", "data/audit_log.jsonl"),
        prompts_dir=ROOT_DIR / os.getenv("PROMPTS_DIR", "data/prompts"),
        rules_path=ROOT_DIR / os.getenv("RULES_PATH", "data/rules/classification_rules.yaml"),
        category_folder_map=_parse_category_folder_map(os.getenv("CATEGORY_FOLDER_MAP", "")),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        servicenow_base_url=os.getenv("IHG_SERVICENOW_BASE_URL") or None,
        servicenow_url=os.getenv("IHG_SERVICENOW_URL") or None,
        servicenow_adhoc_url=os.getenv("IHG_SERVICENOW_ADHOC_TABLE_URL") or None,
        servicenow_portal_url=os.getenv("IHG_SERVICENOW_PORTAL_URL") or None,
        servicenow_username=os.getenv("IHG_SERVICENOW_USERNAME") or None,
        servicenow_password=os.getenv("IHG_SERVICENOW_PASSWORD") or None,
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
                for t in os.getenv("VIP_TITLES", "").split(",")
            if t.strip()
        ],
        general_categories=[
            c.strip()
            for c in os.getenv(
                "GENERAL_CATEGORIES",
                "marketing,newsletter,junk",
            ).split(",")
            if c.strip()
        ],
        worker_interval_seconds=int(os.getenv("WORKER_INTERVAL_SECONDS", "12*60*60")),
        webhook_base_url=os.getenv("WEBHOOK_BASE_URL") or None,
        webhook_client_state=os.getenv("WEBHOOK_CLIENT_STATE", "mailbox-auto-assistant-secret"),
        is_unread_mail=os.getenv("IS_UNREAD_MAIL", "True").lower() in ("true", "1", "t")
    )

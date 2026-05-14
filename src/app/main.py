from __future__ import annotations

import logging
from pathlib import Path

import yaml

from app.application.pipeline import EmailSegregationPipeline
from app.domain.folder_mapper import FolderMapper
from app.domain.models import Rule
from app.infrastructure.ai.openai_client import OpenAIClient
from app.infrastructure.mailbox.microsoft_graph_client import MicrosoftGraphMailboxClient
from app.infrastructure.persistence.db import get_connection, init_schema
from app.infrastructure.persistence.repository import ProcessedEmailRepository
# from app.infrastructure.ticketing.stub_client import StubTicketingClient
from app.infrastructure.ticketing.base import ServiceNowTicketingClient
from app.observability.audit_logger import AuditLogger
from app.observability.metrics import Metrics
from app.settings.config import ROOT_DIR, load_config
from app.settings.logging import setup_logging


logger = logging.getLogger(__name__)


def _load_rules(path: Path) -> list[Rule]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    rules_data = data.get("rules", [])
    return [
        Rule(
            category=item["category"],
            keywords=item.get("keywords", []),
            sender_contains=item.get("sender_contains"),
        )
        for item in rules_data
    ]


def _load_mapping(path: Path) -> dict[str, str]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("mapping", {})


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def build_pipeline() -> EmailSegregationPipeline:
    cfg = load_config()
    setup_logging(cfg.log_level, cfg.log_file_path)
    logger.info(
        "Initializing mailbox assistant env=%s log_level=%s log_file=%s",
        cfg.app_env,
        cfg.log_level,
        cfg.log_file_path,
    )

    conn = get_connection(cfg.database_url)
    init_schema(conn)

    repository = ProcessedEmailRepository(conn)
    mailbox_client = MicrosoftGraphMailboxClient(
        tenant_id=cfg.graph_tenant_id,
        client_id=cfg.graph_client_id,
        client_secret=cfg.graph_client_secret,
        mailbox_user=cfg.graph_mailbox_user,
        mailbox_password=cfg.graph_mailbox_password,
        timeout_seconds=cfg.graph_timeout_seconds,
    )
    ai_client = OpenAIClient(api_key=cfg.openai_api_key)

    rules = _load_rules(cfg.rules_path)
    folder_mapper = FolderMapper(_load_mapping(cfg.mapping_path), default_folder="General")

    system_prompt = _read_text(cfg.prompts_dir / "classifier_system.txt")
    fewshot_prompt = _read_text(cfg.prompts_dir / "classifier_fewshot.txt")

    return EmailSegregationPipeline(
        mailbox_client=mailbox_client,
        ai_client=ai_client,
        repository=repository,
        folder_mapper=folder_mapper,
        rules=rules,
        metrics=Metrics(),
        audit_logger=AuditLogger(cfg.audit_log_path),
        system_prompt=system_prompt,
        fewshot_prompt=fewshot_prompt,
        ticketing_client=ServiceNowTicketingClient(),
        support_engineer_emails=cfg.support_engineer_emails,
        escalation_email=cfg.escalation_email,
        vip_titles=cfg.vip_titles,
        general_categories=cfg.general_categories,
    )


def run_once() -> int:
    pipeline = build_pipeline()
    processed = pipeline.process_unread_emails()
    logger.info("Run completed processed=%s", processed)
    return processed


if __name__ == "__main__":
    processed = run_once()
    print(f"Processed {processed} email(s). root={ROOT_DIR}")

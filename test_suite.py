from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from app.application.pipeline import EmailSegregationPipeline
from app.application.prompt_builder import build_classifier_prompt, mask_sensitive_text
from app.application.use_cases import classify_email
from app.domain.folder_mapper import FolderMapper
from app.domain.models import EmailMessage, Rule, TicketStatus
from app.domain.rules_engine import (
    classify_with_rules,
    extract_adhoc_number,
    extract_incident_number,
    extract_ref_message_id,
    extract_ticket_number,
    extract_ticket_numbers,
    is_auto_notification_email,
    is_vip_sender,
)
from app.infrastructure.ai.base import AIClient
from app.infrastructure.mailbox.base import MailboxClient
from app.infrastructure.mailbox.microsoft_graph_client import MicrosoftGraphMailboxClient
from app.infrastructure.persistence.db import init_schema
from app.infrastructure.persistence.repository import ProcessedEmailRepository
from app.infrastructure.ticketing.base import ServiceNowTicketingClient
from app.observability.audit_logger import AuditLogger
from app.observability.metrics import Metrics
from app.settings.logging import setup_logging


def assert_processed_once(result: object) -> None:
    assert isinstance(result, dict)
    assert result.get("processed_count") == 1


def build_client() -> MicrosoftGraphMailboxClient:
    client = MicrosoftGraphMailboxClient(
        tenant_id="tenant",
        client_id="client",
        client_secret="secret",
        mailbox_user="mailbox@example.com",
        mailbox_password="password",
    )
    client._emails = [
        client._emails[0] if client._emails else None,
    ]
    client._emails = [
        email for email in client._emails if email is not None
    ] or [
        __import__("app.domain.models", fromlist=["EmailMessage"]).EmailMessage(
            id="fallback-1",
            subject="Fallback subject",
            body="Fallback body",
            sender="fallback@example.com",
            received_at=datetime.now(timezone.utc),
        )
    ]
    return client


class RecordingAIClient(AIClient):
    def __init__(self) -> None:
        self.prompt: str | None = None

    def classify_email(self, email: EmailMessage, prompt: str) -> tuple[str, str]:
        self.prompt = prompt
        return "general", "AI classified"


class StubAIClient(AIClient):
    def classify_email(self, email: EmailMessage, prompt: str) -> tuple[str, str]:
        return "general", "stub ai"


class StubMailboxClient(MailboxClient):
    def __init__(self, unread_emails: list[EmailMessage] | None = None) -> None:
        self.moved: list[tuple[str, str]] = []
        self.replies: list[tuple[str, str, list[str]]] = []
        self.support_notifications: list[tuple[list[str], str, str, str | None, str | None]] = []
        self.unread_emails = unread_emails or []

    def fetch_unread(self, limit: int = 25) -> list[EmailMessage]:
        if self.unread_emails:
            return self.unread_emails
        return [
            EmailMessage(
                id="x1",
                subject="Invoice pending",
                body="Please pay this invoice",
                sender="billing@vendor.com",
                sender_name="",
                received_at=datetime.now(timezone.utc),
            )
        ]

    def move_email(self, email_id: str, folder_name: str) -> None:
        self.moved.append((email_id, folder_name))

    def reply_email(
        self,
        email_id: str,
        body: str,
        cc_addresses: list[str] | None = None,
    ) -> None:
        self.replies.append((email_id, body, cc_addresses or []))

    def create_folders(self, folders: list[str]) -> None:
        return None

    def send_support_notification(
        self,
        to_addresses: list[str],
        subject: str,
        body: str,
        attachment_name: str | None = None,
        attachment_content: str | None = None,
    ) -> None:
        self.support_notifications.append(
            (to_addresses, subject, body, attachment_name, attachment_content)
        )


class StubTicketingClient:
    def __init__(
        self,
        status: TicketStatus,
        status_by_ticket: dict[str, TicketStatus] | None = None,
        match_percent_by_ticket: dict[str, int] | None = None,
    ) -> None:
        self.status = status
        self.status_by_ticket = status_by_ticket or {}
        self.match_percent_by_ticket = match_percent_by_ticket or {}
        self.ticket_numbers: list[str] = []
        self.adhoc_ticket_numbers: list[str] = []
        self.comment_updates: list[tuple[str, str]] = []
        self.comment_accuracy_checks: list[tuple[str, str | None]] = []

    def get_inc_ticket_status(self, ticket_number: str) -> TicketStatus:
        self.ticket_numbers.append(ticket_number)
        return self.status_by_ticket.get(ticket_number, self.status)

    def get_adhoc_ticket_status(self, ticket_number: str) -> TicketStatus:
        self.adhoc_ticket_numbers.append(ticket_number)
        return self.status_by_ticket.get(ticket_number, self.status)

    def comment_accuracy_validation(
        self,
        incident_number: str,
        email: EmailMessage,
        ticket_type: str | None = None,
    ) -> dict[str, object]:
        self.comment_accuracy_checks.append((incident_number, ticket_type))
        match_percent = self.match_percent_by_ticket.get(incident_number, 100)
        return {
            "match_percent": match_percent,
            "matched_words": set(),
            "customer_comment": "",
            "match": match_percent >= 70,
        }

    def extract_email_body(self, body: str) -> str:
        return body

    def add_comment(
        self,
        incident_number: str,
        mail_body: str,
        ticket_type: str | None = None,
    ) -> bool:
        self.comment_updates.append((incident_number, mail_body))
        return True


def _build_ticket_pipeline(
    tmp_path,
    email: EmailMessage,
    ticketing: StubTicketingClient,
) -> tuple[EmailSegregationPipeline, StubMailboxClient]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    mailbox = StubMailboxClient(unread_emails=[email])
    pipeline = EmailSegregationPipeline(
        mailbox_client=mailbox,
        ai_client=StubAIClient(),
        repository=ProcessedEmailRepository(conn),
        folder_mapper=FolderMapper({}, default_folder="General"),
        rules=[],
        metrics=Metrics(),
        audit_logger=AuditLogger(tmp_path / f"audit-{email.id}.jsonl"),
        system_prompt="SYSTEM",
        fewshot_prompt="FEWSHOT",
        ticketing_client=ticketing,
        support_engineer_emails=["support@company.com"],
    )
    return pipeline, mailbox


def test_folder_mapper_uses_mapping_case_insensitive() -> None:
    mapper = FolderMapper({"finance": "Finance"}, default_folder="General")

    assert mapper.to_folder("FINANCE") == "Finance"


def test_folder_mapper_fallback_to_default() -> None:
    mapper = FolderMapper({"finance": "Finance"}, default_folder="General")

    assert mapper.to_folder("unknown") == "General"


def test_setup_logging_writes_to_file(tmp_path) -> None:
    root_logger = logging.getLogger()
    log_path = tmp_path / "app.log"

    setup_logging("INFO", log_path)
    logging.getLogger("tests.logging").info("logging smoke test")

    for handler in root_logger.handlers:
        handler.flush()

    assert log_path.exists()
    assert "logging smoke test" in log_path.read_text(encoding="utf-8")
    assert any(
        getattr(handler, "baseFilename", None) == str(log_path)
        for handler in root_logger.handlers
    )


def test_fetch_unread_falls_back_to_local_messages_on_graph_error() -> None:
    client = build_client()

    def raise_graph_error(endpoint: str) -> dict:
        raise RuntimeError("graph unavailable")

    client._graph_get = raise_graph_error  # type: ignore[method-assign]

    unread = client.fetch_unread(limit=1)

    assert len(unread) == 1
    assert unread[0].id == "fallback-1"


def test_reply_email_falls_back_to_local_behavior_on_graph_error(capsys) -> None:
    client = build_client()

    def raise_graph_error(endpoint: str, payload: dict) -> dict:
        raise RuntimeError("graph unavailable")

    client._graph_post = raise_graph_error  # type: ignore[method-assign]

    client.reply_email("email-123", "Hello", ["support@example.com"])

    captured = capsys.readouterr()
    assert "[mailbox] reply to email email-123 | cc: support@example.com" in captured.out


def test_reply_email_creates_threaded_reply_draft_before_sending() -> None:
    client = build_client()
    calls: list[tuple[str, str, dict | None]] = []

    def fake_graph_request(method: str, endpoint: str, payload: dict | None = None) -> dict:
        calls.append((method, endpoint, payload))
        if method == "POST" and endpoint.endswith("/createReply"):
            return {
                "id": "draft-123",
                "body": {
                    "contentType": "HTML",
                    "content": "<html><body><div>Original thread</div></body></html>",
                },
            }
        return {}

    client._graph_request = fake_graph_request  # type: ignore[method-assign]

    client.reply_email("email-123", "<p>Hello</p>", ["support@example.com"])

    assert calls == [
        (
            "POST",
            "/users/mailbox@example.com/messages/email-123/createReply",
            {},
        ),
        (
            "PATCH",
            "/users/mailbox@example.com/messages/draft-123",
            {
                "body": {
                    "contentType": "HTML",
                    "content": "<html><body><p>Hello</p><br/><br/><div>Original thread</div></body></html>",
                },
                "ccRecipients": [
                    {"emailAddress": {"address": "support@example.com"}},
                ],
            },
        ),
        (
            "POST",
            "/users/mailbox@example.com/messages/draft-123/send",
            {},
        ),
    ]


def test_prompt_builder_contains_all_sections() -> None:
    prompt = build_classifier_prompt(
        system_prompt="SYSTEM",
        fewshot_prompt="FEWSHOT",
        subject="subject",
        body="body",
    )

    assert "SYSTEM" in prompt
    assert "FEWSHOT" in prompt
    assert "Subject: subject" in prompt
    assert "Body: body" in prompt
    assert "Return JSON" in prompt


def test_mask_sensitive_text_redacts_known_patterns() -> None:
    text = (
        "Aadhaar 1234 5678 9012, PAN ABCDE1234F, passport K1234567, "
        "account 1234 5678 9012 3456, IFSC HDFC0123456, "
        "phone +91 9876543210, UPI user-1@oksbi"
    )

    masked = mask_sensitive_text(text)

    assert "1234 5678 9012" not in masked
    assert "ABCDE1234F" not in masked
    assert "K1234567" not in masked
    assert "1234 5678 9012 3456" not in masked
    assert "HDFC0123456" not in masked
    assert "9876543210" not in masked
    assert "user-1@oksbi" not in masked
    assert "[REDACTED_AADHAAR]" in masked
    assert "[REDACTED_PAN]" in masked
    assert "[REDACTED_PASSPORT]" in masked
    assert masked.count("[REDACTED_AADHAAR]") >= 2
    assert "[REDACTED_IFSC]" in masked
    assert "[REDACTED_PHONE]" in masked
    assert "[REDACTED_UPI_ID]" in masked


def test_prompt_builder_masks_sensitive_subject_and_body() -> None:
    prompt = build_classifier_prompt(
        system_prompt="SYSTEM",
        fewshot_prompt="FEWSHOT",
        subject="PAN ABCDE1234F, passport K1234567 and phone 9876543210 attached",
        body="Bank details 1234 5678 9012 3456, Aadhaar 123456789012, and UPI user-1@oksbi",
    )

    assert "ABCDE1234F" not in prompt
    assert "K1234567" not in prompt
    assert "9876543210" not in prompt
    assert "1234 5678 9012 3456" not in prompt
    assert "123456789012" not in prompt
    assert "user-1@oksbi" not in prompt
    assert "[REDACTED_PAN]" in prompt
    assert "[REDACTED_PASSPORT]" in prompt
    assert "[REDACTED_PHONE]" in prompt
    assert prompt.count("[REDACTED_AADHAAR]") >= 2
    assert "[REDACTED_AADHAAR]" in prompt
    assert "[REDACTED_UPI_ID]" in prompt


def test_rules_engine_matches_keyword_and_sender() -> None:
    email = EmailMessage(
        id="e1",
        subject="Invoice INV-100",
        body="Please release payment",
        sender="billing@vendor.com",
        received_at=datetime.now(timezone.utc),
    )
    rules = [Rule(category="finance", keywords=["invoice"], sender_contains="billing")]

    category, reason = classify_with_rules(email, rules)

    assert category == "finance"
    assert "keyword" in reason


def test_rules_engine_returns_none_when_no_match() -> None:
    email = EmailMessage(
        id="e2",
        subject="Hello",
        body="How are you?",
        sender="friend@example.com",
        received_at=datetime.now(timezone.utc),
    )
    rules = [Rule(category="finance", keywords=["invoice"])]

    category, reason = classify_with_rules(email, rules)

    assert category is None
    assert reason == "No rule matched"


def test_vip_sender_detected_in_display_name() -> None:
    email = EmailMessage(
        id="vip1",
        subject="Budget Review",
        body="Need approval on Q2 budget",
        sender="john.smith@company.com",
        sender_name="John Smith, VP of Operations",
        received_at=datetime.now(timezone.utc),
    )
    vip_titles = ["Director", "VP", "Chief", "CEO"]

    is_vip, detected_by = is_vip_sender(email, vip_titles)

    assert is_vip is True
    assert "sender_name" in detected_by
    assert "VP" in detected_by


def test_vip_sender_detected_in_email_body_signature() -> None:
    email = EmailMessage(
        id="vip2",
        subject="Strategic Initiative",
        body=(
            "Hello,\n\n"
            "Please review the attached proposal for Q3 initiatives.\n\n"
            "Best regards,\n"
            "Jane Doe\n"
            "VP Engineering\n"
            "jane.doe@company.com"
        ),
        sender="jane.doe@company.com",
        sender_name="Jane Doe",
        received_at=datetime.now(timezone.utc),
    )
    vip_titles = ["Director", "VP", "Chief", "CEO", "CTO"]

    is_vip, detected_by = is_vip_sender(email, vip_titles)

    assert is_vip is True
    assert "body_signature" in detected_by
    assert "VP" in detected_by


def test_vip_sender_detected_in_body_with_director_title() -> None:
    email = EmailMessage(
        id="vip3",
        subject="Important Decision",
        body=(
            "Team,\n\n"
            "The following action items are critical...\n\n"
            "---\n"
            "Michael Johnson\n"
            "Director of Sales\n"
            "michael@company.com"
        ),
        sender="michael@company.com",
        sender_name="Michael Johnson",
        received_at=datetime.now(timezone.utc),
    )
    vip_titles = ["Director", "VP", "Chief"]

    is_vip, detected_by = is_vip_sender(email, vip_titles)

    assert is_vip is True
    assert "body_signature" in detected_by


def test_non_vip_sender_no_title() -> None:
    email = EmailMessage(
        id="normal1",
        subject="Regular inquiry",
        body="Could you help with this?",
        sender="user@external.com",
        sender_name="John User",
        received_at=datetime.now(timezone.utc),
    )
    vip_titles = ["Director", "VP", "Chief", "CEO"]

    is_vip, detected_by = is_vip_sender(email, vip_titles)

    assert is_vip is False
    assert detected_by == ""


def test_vip_detection_case_insensitive() -> None:
    email = EmailMessage(
        id="vip4",
        subject="Request",
        body="Please find my signature at the bottom.\n\nBest,\nAlex\nvp COMPLIANCE\nalex@company.com",
        sender="alex@company.com",
        sender_name="Alex",
        received_at=datetime.now(timezone.utc),
    )
    vip_titles = ["Director", "VP", "Chief"]

    is_vip, detected_by = is_vip_sender(email, vip_titles)

    assert is_vip is True


def test_vip_detection_multiple_titles_in_rules() -> None:
    email = EmailMessage(
        id="vip5",
        subject="Announcement",
        body="Team announcement.\n\nRegards,\nSarah Lee\nChief Technology Officer\nsarah@company.com",
        sender="sarah@company.com",
        sender_name="Sarah Lee",
        received_at=datetime.now(timezone.utc),
    )
    vip_titles = ["Director", "VP", "CTO", "Chief", "CEO", "CFO"]

    is_vip, detected_by = is_vip_sender(email, vip_titles)

    assert is_vip is True


def test_vip_detection_with_partial_word_matches() -> None:
    email = EmailMessage(
        id="vip6",
        subject="Guidance",
        body="Please follow up with this.\n\nThanks,\nBob Martinez\nVP Sales Operations\nbob@company.com",
        sender="bob@company.com",
        sender_name="Bob Martinez",
        received_at=datetime.now(timezone.utc),
    )
    vip_titles = ["VP", "Director"]

    is_vip, detected_by = is_vip_sender(email, vip_titles)

    assert is_vip is True


def test_vip_detected_by_graph_job_title_in_sender_name() -> None:
    email = EmailMessage(
        id="vip7",
        subject="Operational Update",
        body="Please review the operational update.",
        sender="jane.doe@company.com",
        sender_name="Jane Doe, VP Engineering",
        received_at=datetime.now(timezone.utc),
    )
    vip_titles = ["Director", "VP", "Chief", "CEO"]

    is_vip, detected_by = is_vip_sender(email, vip_titles)

    assert is_vip is True
    assert "sender_name" in detected_by


def test_extract_incident_and_adhoc_numbers_separately() -> None:
    email = EmailMessage(
        id="ticket1",
        subject="Issue with INC7050808",
        body="Please check ADH123456 on priority.",
        sender="user@example.com",
        received_at=datetime.now(timezone.utc),
    )

    assert extract_incident_number(email) == ["INC7050808"]
    assert extract_adhoc_number(email) == ["ADH123456"]
    assert extract_ticket_number(email) == ["INC7050808"]


def test_extract_ticket_numbers_preserves_mixed_ticket_order() -> None:
    email = EmailMessage(
        id="ticket2",
        subject="Issue with INC123456 then ADH555555",
        body="Please also review INC999999 after the adhoc request.",
        sender="user@example.com",
        received_at=datetime.now(timezone.utc),
    )

    assert extract_ticket_numbers(email) == [
        {"ticket_number": "INC123456", "ticket_type": "incident"},
        {"ticket_number": "ADH555555", "ticket_type": "adhoc"},
        {"ticket_number": "INC999999", "ticket_type": "incident"},
    ]


def test_extract_ticket_numbers_supports_adhoc_prefix() -> None:
    email = EmailMessage(
        id="ticket3",
        subject="I have created incident INC7052380 and Adhoc ADHOC0531678",
        body="Please also provide an update for ADHOC0531696.",
        sender="user@example.com",
        received_at=datetime.now(timezone.utc),
    )

    assert extract_adhoc_number(email) == ["ADHOC0531678", "ADHOC0531696"]
    assert extract_ticket_numbers(email) == [
        {"ticket_number": "INC7052380", "ticket_type": "incident"},
        {"ticket_number": "ADHOC0531678", "ticket_type": "adhoc"},
        {"ticket_number": "ADHOC0531696", "ticket_type": "adhoc"},
    ]


def test_extract_ticket_numbers_supports_spaced_ticket_formats() -> None:
    email = EmailMessage(
        id="ticket4",
        subject="Incident INC 7052380 and Adhoc ADHOC 0531678",
        body="Please also review ADH OC0531696.",
        sender="user@example.com",
        received_at=datetime.now(timezone.utc),
    )

    assert extract_incident_number(email) == ["INC7052380"]
    assert extract_adhoc_number(email) == ["ADHOC0531678", "ADHOC0531696"]
    assert extract_ticket_numbers(email) == [
        {"ticket_number": "INC7052380", "ticket_type": "incident"},
        {"ticket_number": "ADHOC0531678", "ticket_type": "adhoc"},
        {"ticket_number": "ADHOC0531696", "ticket_type": "adhoc"},
    ]


def test_extract_ticket_numbers_supports_common_ticket_variants() -> None:
    email = EmailMessage(
        id="ticket5",
        subject=(
            "INC7052380 INC 7052381 INC-7052382 "
            "ADH0531677 ADH 0531678 ADH-0531679 "
            "ADHOC0531680 ADHOC 0531681 ADH-OC0531682 ADH OC 0531683"
        ),
        body="Please review all ticket variants.",
        sender="user@example.com",
        received_at=datetime.now(timezone.utc),
    )

    assert extract_incident_number(email) == [
        "INC7052380",
        "INC7052381",
        "INC7052382",
    ]
    assert extract_adhoc_number(email) == [
        "ADH0531677",
        "ADH0531678",
        "ADH0531679",
        "ADHOC0531680",
        "ADHOC0531681",
        "ADHOC0531682",
        "ADHOC0531683",
    ]


def test_extract_ref_message_id() -> None:
    email = EmailMessage(
        id="ref1",
        subject="Follow-up INC7050808 | Ref Msg: 1234",
        body="Please investigate this.",
        sender="user@example.com",
        received_at=datetime.now(timezone.utc),
    )

    assert extract_ref_message_id(email) == "1234"


def test_auto_notification_detected_from_sender_pattern() -> None:
    email = EmailMessage(
        id="bot1",
        subject="Daily report",
        body="Automated update",
        sender="no-reply@internal.example.com",
        received_at=datetime.now(timezone.utc),
    )

    is_bot, reason = is_auto_notification_email(email)

    assert is_bot is True
    assert "sender_pattern" in reason


def test_auto_notification_detected_from_keyword() -> None:
    email = EmailMessage(
        id="bot2",
        subject="Nightly Appsrv health check",
        body="All systems green.",
        sender="ops@example.com",
        received_at=datetime.now(timezone.utc),
    )

    is_bot, reason = is_auto_notification_email(email)

    assert is_bot is True
    assert reason == "keyword:appsrv"


def test_get_inc_ticket_status_uses_incident_table_query(monkeypatch) -> None:
    captured: dict = {}

    def fake_get(url, headers, auth, params, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["auth"] = auth
        captured["params"] = params
        captured["timeout"] = timeout

        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"result": [{"state": "2"}]},
        )

    monkeypatch.setenv("IHG_SERVICENOW_URL", "https://ihguat.service-now.com/api/now/table/incident?sysparm_query=number=")
    monkeypatch.setenv("IHG_SERVICENOW_USERNAME", "svc_user")
    monkeypatch.setenv("IHG_SERVICENOW_PASSWORD", "svc_password")
    monkeypatch.delenv("IHG_SERVICENOW_INCIDENT_TABLE_URL", raising=False)
    monkeypatch.delenv("IHG_SERVICENOW_BASIC_AUTH", raising=False)
    monkeypatch.delenv("IHG_SERVICENOW_COOKIE", raising=False)
    monkeypatch.setattr("app.infrastructure.ticketing.base.requests.get", fake_get)

    client = ServiceNowTicketingClient()

    status = client.get_inc_ticket_status("INC7050808")

    assert status == TicketStatus.IN_PROGRESS
    assert captured["url"] == "https://ihguat.service-now.com/api/now/table/incident"
    assert captured["auth"] == ("svc_user", "svc_password")
    assert captured["params"] == {
        "sysparm_query": "number=INC7050808",
        "sysparm_limit": "1",
    }
    assert captured["headers"] == {"Accept": "application/json"}
    assert captured["timeout"] == 5


def test_get_inc_ticket_status_prefers_explicit_headers(monkeypatch) -> None:
    captured: dict = {}

    def fake_get(url, headers, auth, params, timeout):
        captured["headers"] = headers
        captured["auth"] = auth
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"result": [{"state": "6"}]},
        )

    monkeypatch.setenv("IHG_SERVICENOW_URL", "https://ihguat.service-now.com")
    monkeypatch.setenv("IHG_SERVICENOW_USERNAME", "svc_user")
    monkeypatch.setenv("IHG_SERVICENOW_PASSWORD", "svc_password")
    monkeypatch.setenv("IHG_SERVICENOW_BASIC_AUTH", "Basic token-value")
    monkeypatch.setenv("IHG_SERVICENOW_COOKIE", "cookie=value")
    monkeypatch.setattr("app.infrastructure.ticketing.base.requests.get", fake_get)

    client = ServiceNowTicketingClient()

    status = client.get_inc_ticket_status("INC7050808")

    assert status == TicketStatus.RESOLVED
    assert captured["auth"] is None
    assert captured["headers"] == {
        "Accept": "application/json",
        "Authorization": "Basic token-value",
        "Cookie": "cookie=value",
    }


def test_get_adhoc_ticket_status_uses_adhoc_table_query(monkeypatch) -> None:
    captured: dict = {}

    def fake_get(url, headers, auth, params, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["auth"] = auth
        captured["params"] = params
        captured["timeout"] = timeout

        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"result": [{"state": "5"}]},
        )

    monkeypatch.setenv("IHG_SERVICENOW_ADHOC_TABLE_URL", "https://ihg.service-now.com/api/now/table/u_ad_hoc_request?sysparm_query=number=")
    monkeypatch.setenv("IHG_SERVICENOW_USERNAME", "svc_user")
    monkeypatch.setenv("IHG_SERVICENOW_PASSWORD", "svc_password")
    monkeypatch.delenv("IHG_SERVICENOW_BASIC_AUTH", raising=False)
    monkeypatch.delenv("IHG_SERVICENOW_COOKIE", raising=False)
    monkeypatch.setattr("app.infrastructure.ticketing.base.requests.get", fake_get)

    client = ServiceNowTicketingClient()

    status = client.get_adhoc_ticket_status("ADH123456")

    assert status == TicketStatus.PENDING
    assert captured["url"] == "https://ihg.service-now.com/api/now/table/u_ad_hoc_request"
    assert captured["auth"] == ("svc_user", "svc_password")
    assert captured["params"] == {
        "sysparm_query": "number=ADH123456",
        "sysparm_limit": "1",
    }
    assert captured["headers"] == {"Accept": "application/json"}
    assert captured["timeout"] == 5


def test_get_adhoc_ticket_status_maps_open_value(monkeypatch) -> None:
    def fake_get(url, headers, auth, params, timeout):
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"result": [{"state": "1"}]},
        )

    monkeypatch.setenv("IHG_SERVICENOW_ADHOC_TABLE_URL", "https://ihg.service-now.com/api/now/table/u_ad_hoc_request")
    monkeypatch.setattr("app.infrastructure.ticketing.base.requests.get", fake_get)

    client = ServiceNowTicketingClient()

    status = client.get_adhoc_ticket_status("ADH123456")

    assert status == TicketStatus.OPEN


def test_get_adhoc_ticket_status_normalizes_negative_value(monkeypatch) -> None:
    def fake_get(url, headers, auth, params, timeout):
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"result": [{"state": "-5"}]},
        )

    monkeypatch.setenv("IHG_SERVICENOW_ADHOC_TABLE_URL", "https://ihg.service-now.com/api/now/table/u_ad_hoc_request")
    monkeypatch.setattr("app.infrastructure.ticketing.base.requests.get", fake_get)

    client = ServiceNowTicketingClient()

    status = client.get_adhoc_ticket_status("ADH123456")

    assert status == TicketStatus.PENDING


def test_add_comment_uses_adhoc_table_urls(monkeypatch) -> None:
    captured: dict[str, object] = {"get_urls": [], "patch_urls": []}

    def fake_get(url, headers, auth, params, timeout):
        captured["get_urls"].append(url)
        captured["params"] = params
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"result": [{"sys_id": "adhoc-sys-id"}]},
        )

    def fake_patch(url, json, headers, auth, timeout):
        captured["patch_urls"].append(url)
        captured["payload"] = json
        return SimpleNamespace(
            raise_for_status=lambda: None,
        )

    monkeypatch.setenv("IHG_SERVICENOW_ADHOC_TABLE_URL", "https://ihg.service-now.com/api/now/table/u_ad_hoc_request")
    monkeypatch.setenv("IHG_SERVICENOW_USERNAME", "svc_user")
    monkeypatch.setenv("IHG_SERVICENOW_PASSWORD", "svc_password")
    monkeypatch.setattr("app.infrastructure.ticketing.base.requests.get", fake_get)
    monkeypatch.setattr("app.infrastructure.ticketing.base.requests.patch", fake_patch)

    client = ServiceNowTicketingClient()

    updated = client.add_comment("ADH123456", "Please add this comment.", "adhoc")

    assert updated is True
    assert captured["get_urls"] == ["https://ihg.service-now.com/api/now/table/u_ad_hoc_request"]
    assert captured["patch_urls"] == ["https://ihg.service-now.com/api/now/table/u_ad_hoc_request/adhoc-sys-id"]
    assert captured["params"] == {
        "sysparm_query": "number=ADH123456",
        "sysparm_limit": "1",
    }
    assert captured["payload"] == {"comments": "please add this comment."}


def test_clean_text_removes_header_labels_without_truncating_message() -> None:
    client = ServiceNowTicketingClient()

    cleaned = client.clean_text(
        "Please review this update. From: Alice Reply Bob Sent: Today Subject: Incident To: Team"
    )

    assert cleaned == "please review this update. alice bob today incident team"


def test_constructor_config_overrides_env_for_incident_table(monkeypatch) -> None:
    monkeypatch.setenv("IHG_SERVICENOW_URL", "https://env-instance.service-now.com/api/now/table/incident")

    client = ServiceNowTicketingClient(
        base_url="https://ctor-instance.service-now.com",
        username="svc_user",
        password="svc_password",
    )

    assert client._incident_table_base_url() == "https://ctor-instance.service-now.com/api/now/table/incident"


def test_classify_email_masks_sensitive_data_before_openai_fallback() -> None:
    email = EmailMessage(
        id="e1",
        subject="PAN ABCDE1234F and passport K1234567 shared from 9876543210",
        body="Aadhaar 1234 5678 9012, account 1234567890123456, and UPI user-1@oksbi are in this email.",
        sender="user@example.com",
        received_at=datetime.now(timezone.utc),
    )
    ai_client = RecordingAIClient()

    result = classify_email(
        email=email,
        rules=[],
        ai_client=ai_client,
        system_prompt="SYSTEM",
        fewshot_prompt="FEWSHOT",
    )

    assert result.category == "general"
    assert ai_client.prompt is not None
    assert "ABCDE1234F" not in ai_client.prompt
    assert "K1234567" not in ai_client.prompt
    assert "9876543210" not in ai_client.prompt
    assert "1234 5678 9012" not in ai_client.prompt
    assert "1234567890123456" not in ai_client.prompt
    assert "user-1@oksbi" not in ai_client.prompt
    assert "[REDACTED_PAN]" in ai_client.prompt
    assert "[REDACTED_PASSPORT]" in ai_client.prompt
    assert "[REDACTED_PHONE]" in ai_client.prompt
    assert "[REDACTED_AADHAAR]" in ai_client.prompt
    assert "[REDACTED_BANK_ACCOUNT]" in ai_client.prompt
    assert "[REDACTED_UPI_ID]" in ai_client.prompt


def test_pipeline_processes_and_persists(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    repo = ProcessedEmailRepository(conn)
    mailbox = StubMailboxClient()
    pipeline = EmailSegregationPipeline(
        mailbox_client=mailbox,
        ai_client=StubAIClient(),
        repository=repo,
        folder_mapper=FolderMapper({"finance": "Finance"}, default_folder="General"),
        rules=[Rule(category="finance", keywords=["invoice"], sender_contains="billing")],
        metrics=Metrics(),
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        system_prompt="SYSTEM",
        fewshot_prompt="FEWSHOT",
    )

    processed = pipeline.process_unread_emails()

    assert_processed_once(processed)


def test_vip_escalation_no_reply_no_folder_move(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    vip_email = EmailMessage(
        id="vip1",
        subject="Budget Review Request",
        body="Need approval on Q2 budget",
        sender="john.director@company.com",
        sender_name="John Smith, VP of Operations",
        received_at=datetime.now(timezone.utc),
    )

    repo = ProcessedEmailRepository(conn)
    mailbox = StubMailboxClient(unread_emails=[vip_email])
    pipeline = EmailSegregationPipeline(
        mailbox_client=mailbox,
        ai_client=StubAIClient(),
        repository=repo,
        folder_mapper=FolderMapper({}, default_folder="General"),
        rules=[],
        metrics=Metrics(),
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        system_prompt="SYSTEM",
        fewshot_prompt="FEWSHOT",
        vip_titles=["Director", "VP", "Chief", "CEO"],
        escalation_email="escalation@company.com",
    )

    processed = pipeline.process_unread_emails()

    assert_processed_once(processed)
    assert "vip1" in repo.list_processed_ids()


def test_vip_escalation_audit_logging(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    vip_email = EmailMessage(
        id="vip2",
        subject="Strategic Initiative Review",
        body="Requesting sign-off",
        sender="jane.cto@company.com",
        sender_name="Jane Doe, CTO",
        received_at=datetime.now(timezone.utc),
    )

    mailbox = StubMailboxClient(unread_emails=[vip_email])
    audit_logger = AuditLogger(tmp_path / "audit_vip.jsonl")
    pipeline = EmailSegregationPipeline(
        mailbox_client=mailbox,
        ai_client=StubAIClient(),
        repository=ProcessedEmailRepository(conn),
        folder_mapper=FolderMapper({}, default_folder="General"),
        rules=[],
        metrics=Metrics(),
        audit_logger=audit_logger,
        system_prompt="SYSTEM",
        fewshot_prompt="FEWSHOT",
        vip_titles=["Director", "VP", "Chief", "CTO"],
        escalation_email="escalation@company.com",
    )

    pipeline.process_unread_emails()

    audit_file = tmp_path / "audit_vip.jsonl"
    assert audit_file.exists()

    audit_lines = audit_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(audit_lines) == 1

    audit_entry = json.loads(audit_lines[0])
    assert audit_entry["action"] == "vip_escalation"
    assert audit_entry["email_id"] == "vip2"
    assert audit_entry["sender"] == "jane.cto@company.com"
    assert "requires discussion with" in audit_entry.get("note", "").lower()


def test_cc_support_engineers_on_reply(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    support_email = EmailMessage(
        id="support1",
        subject="Cannot access my account",
        body="I'm unable to log in to the system",
        sender="user@external.com",
        sender_name="John User",
        received_at=datetime.now(timezone.utc),
    )

    repo = ProcessedEmailRepository(conn)
    mailbox = StubMailboxClient(unread_emails=[support_email])
    support_engineers = ["support1@company.com", "support2@company.com", "engineer@company.com"]

    pipeline = EmailSegregationPipeline(
        mailbox_client=mailbox,
        ai_client=StubAIClient(),
        repository=repo,
        folder_mapper=FolderMapper({"support": "Support"}, default_folder="General"),
        rules=[Rule(category="support", keywords=["account", "login"])],
        metrics=Metrics(),
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        system_prompt="SYSTEM",
        fewshot_prompt="FEWSHOT",
        support_engineer_emails=support_engineers,
        general_categories=["general", "marketing"],
    )

    processed = pipeline.process_unread_emails()

    assert_processed_once(processed)


def test_cc_support_engineers_empty_list(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    email = EmailMessage(
        id="e1",
        subject="Help needed",
        body="Something is broken",
        sender="user@example.com",
        sender_name="User",
        received_at=datetime.now(timezone.utc),
    )

    mailbox = StubMailboxClient(unread_emails=[email])
    pipeline = EmailSegregationPipeline(
        mailbox_client=mailbox,
        ai_client=StubAIClient(),
        repository=ProcessedEmailRepository(conn),
        folder_mapper=FolderMapper({"support": "Support"}, default_folder="General"),
        rules=[Rule(category="support", keywords=["help"])],
        metrics=Metrics(),
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        system_prompt="SYSTEM",
        fewshot_prompt="FEWSHOT",
        support_engineer_emails=[],
        general_categories=["general"],
    )

    processed = pipeline.process_unread_emails()

    assert_processed_once(processed)
    assert len(mailbox.replies) == 1

    email_id, reply_body, cc_list = mailbox.replies[0]
    assert cc_list == []


def test_vip_escalation_takes_precedence_over_classification(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    vip_support_email = EmailMessage(
        id="vip_support",
        subject="Support needed for critical issue",
        body="Our system is down, need immediate help",
        sender="director@company.com",
        sender_name="Mike Johnson, VP of Engineering",
        received_at=datetime.now(timezone.utc),
    )

    mailbox = StubMailboxClient(unread_emails=[vip_support_email])
    pipeline = EmailSegregationPipeline(
        mailbox_client=mailbox,
        ai_client=StubAIClient(),
        repository=ProcessedEmailRepository(conn),
        folder_mapper=FolderMapper({"support": "Support"}, default_folder="General"),
        rules=[Rule(category="support", keywords=["support", "help"])],
        metrics=Metrics(),
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        system_prompt="SYSTEM",
        fewshot_prompt="FEWSHOT",
        support_engineer_emails=["support@company.com"],
        vip_titles=["VP", "Director", "Chief"],
        escalation_email="escalation@company.com",
    )

    processed = pipeline.process_unread_emails()

    assert_processed_once(processed)
    assert "vip_support" in ProcessedEmailRepository(conn).list_processed_ids()


def test_bot_auto_notification_is_skipped_without_reply_or_move(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    email = EmailMessage(
        id="bot1",
        subject="Nightly backup verification report Appsrv",
        body="Automated status update",
        sender="noreply@internal.example.com",
        received_at=datetime.now(timezone.utc),
    )

    mailbox = StubMailboxClient(unread_emails=[email])
    pipeline = EmailSegregationPipeline(
        mailbox_client=mailbox,
        ai_client=StubAIClient(),
        repository=ProcessedEmailRepository(conn),
        folder_mapper=FolderMapper({}, default_folder="General"),
        rules=[],
        metrics=Metrics(),
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        system_prompt="SYSTEM",
        fewshot_prompt="FEWSHOT",
    )

    processed = pipeline.process_unread_emails()

    assert_processed_once(processed)
    assert "bot1" in ProcessedEmailRepository(conn).list_processed_ids()


def test_open_ticket_no_action_when_recipient_and_ref_message_present(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    email = EmailMessage(
        id="snow1",
        subject="Question on INC7050808 | Ref Msg: 1234",
        body="Please review this issue.",
        sender="user@example.com",
        sender_name="User One",
        received_at=datetime.now(timezone.utc),
        to_addresses=["ihg@servicenow.com"],
    )

    mailbox = StubMailboxClient(unread_emails=[email])
    ticketing = StubTicketingClient(TicketStatus.ON_HOLD)
    pipeline = EmailSegregationPipeline(
        mailbox_client=mailbox,
        ai_client=StubAIClient(),
        repository=ProcessedEmailRepository(conn),
        folder_mapper=FolderMapper({}, default_folder="General"),
        rules=[],
        metrics=Metrics(),
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        system_prompt="SYSTEM",
        fewshot_prompt="FEWSHOT",
        ticketing_client=ticketing,
        support_engineer_emails=["support@company.com"],
    )

    processed = pipeline.process_unread_emails()

    assert_processed_once(processed)
    assert ticketing.ticket_numbers == ["INC7050808"]


def test_open_ticket_support_notification_when_ref_missing(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    email = EmailMessage(
        id="snow2",
        subject="Follow-up on INC7050808",
        body="Please investigate this issue.",
        sender="user@example.com",
        sender_name="User Two",
        received_at=datetime.now(timezone.utc),
        to_addresses=["ihg@servicenow.com"],
    )

    mailbox = StubMailboxClient(unread_emails=[email])
    support_engineers = ["support1@company.com", "support2@company.com"]
    ticketing = StubTicketingClient(TicketStatus.IN_PROGRESS)
    pipeline = EmailSegregationPipeline(
        mailbox_client=mailbox,
        ai_client=StubAIClient(),
        repository=ProcessedEmailRepository(conn),
        folder_mapper=FolderMapper({}, default_folder="General"),
        rules=[],
        metrics=Metrics(),
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        system_prompt="SYSTEM",
        fewshot_prompt="FEWSHOT",
        ticketing_client=ticketing,
        support_engineer_emails=support_engineers,
    )

    processed = pipeline.process_unread_emails()

    assert_processed_once(processed)
    assert ticketing.ticket_numbers == ["INC7050808"]
    assert mailbox.replies == []


def test_open_ticket_support_notification_when_recipient_missing_but_ref_present(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    email = EmailMessage(
        id="snow3",
        subject="Follow-up on INC7050808 | Ref Msg: 1234",
        body="Please review this issue.",
        sender="user@example.com",
        sender_name="User Three",
        received_at=datetime.now(timezone.utc),
    )

    mailbox = StubMailboxClient(unread_emails=[email])
    support_engineers = ["support@company.com"]
    ticketing = StubTicketingClient(TicketStatus.IN_PROGRESS)
    pipeline = EmailSegregationPipeline(
        mailbox_client=mailbox,
        ai_client=StubAIClient(),
        repository=ProcessedEmailRepository(conn),
        folder_mapper=FolderMapper({}, default_folder="General"),
        rules=[],
        metrics=Metrics(),
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        system_prompt="SYSTEM",
        fewshot_prompt="FEWSHOT",
        ticketing_client=ticketing,
        support_engineer_emails=support_engineers,
    )

    processed = pipeline.process_unread_emails()

    assert_processed_once(processed)
    assert ticketing.ticket_numbers == ["INC7050808"]
    assert mailbox.replies == []


def test_open_ticket_support_notification_when_recipient_and_ref_missing(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    email = EmailMessage(
        id="snow4",
        subject="Follow-up on INC7050808",
        body="Please investigate this query.",
        sender="user@example.com",
        sender_name="User Four",
        received_at=datetime.now(timezone.utc),
    )

    mailbox = StubMailboxClient(unread_emails=[email])
    support_engineers = ["support@company.com"]
    ticketing = StubTicketingClient(TicketStatus.NEW)
    pipeline = EmailSegregationPipeline(
        mailbox_client=mailbox,
        ai_client=StubAIClient(),
        repository=ProcessedEmailRepository(conn),
        folder_mapper=FolderMapper({}, default_folder="General"),
        rules=[],
        metrics=Metrics(),
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        system_prompt="SYSTEM",
        fewshot_prompt="FEWSHOT",
        ticketing_client=ticketing,
        support_engineer_emails=support_engineers,
    )

    processed = pipeline.process_unread_emails()

    assert_processed_once(processed)
    assert ticketing.ticket_numbers == ["INC7050808"]
    assert mailbox.replies == []


def test_servicenow_closed_ticket_replies_without_cc(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    email = EmailMessage(
        id="snow3",
        subject="Follow-up on INC7050808",
        body="Please review ADH123456 for the same query.",
        sender="user@example.com",
        sender_name="User Three",
        received_at=datetime.now(timezone.utc),
        to_addresses=["ihg@servicenow.com"],
    )

    mailbox = StubMailboxClient(unread_emails=[email])
    ticketing = StubTicketingClient(TicketStatus.RESOLVED)
    pipeline = EmailSegregationPipeline(
        mailbox_client=mailbox,
        ai_client=StubAIClient(),
        repository=ProcessedEmailRepository(conn),
        folder_mapper=FolderMapper({}, default_folder="General"),
        rules=[],
        metrics=Metrics(),
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        system_prompt="SYSTEM",
        fewshot_prompt="FEWSHOT",
        ticketing_client=ticketing,
        support_engineer_emails=["support@company.com"],
    )

    processed = pipeline.process_unread_emails()

    assert_processed_once(processed)
    assert len(mailbox.replies) == 1
    assert mailbox.replies[0][2] == []
    assert mailbox.support_notifications == []
    assert ticketing.comment_updates == []


def test_multiple_incidents_in_one_email_updates_open_ticket_without_reply(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    email = EmailMessage(
        id="snow-multi",
        subject="Follow-up on INC7050808 and INC7050809",
        body="Please review both incidents.",
        sender="user@example.com",
        sender_name="User Multi, Analyst",
        received_at=datetime.now(timezone.utc),
        to_addresses=["ihg@servicenow.com"],
    )

    mailbox = StubMailboxClient(unread_emails=[email])
    ticketing = StubTicketingClient(
        TicketStatus.RESOLVED,
        status_by_ticket={
            "INC7050808": TicketStatus.CANCELLED,
            "INC7050809": TicketStatus.ON_HOLD,
        },
        match_percent_by_ticket={"INC7050809": 10},
    )
    pipeline = EmailSegregationPipeline(
        mailbox_client=mailbox,
        ai_client=StubAIClient(),
        repository=ProcessedEmailRepository(conn),
        folder_mapper=FolderMapper({}, default_folder="General"),
        rules=[],
        metrics=Metrics(),
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        system_prompt="SYSTEM",
        fewshot_prompt="FEWSHOT",
        ticketing_client=ticketing,
        support_engineer_emails=["support@company.com"],
    )

    pipeline.process_unread_emails()

    assert ticketing.ticket_numbers == ["INC7050808", "INC7050809", "INC7050809"]
    assert mailbox.replies == []
    assert ticketing.comment_updates == [("INC7050809", "Please review both incidents.")]
    assert mailbox.support_notifications == []
    assert mailbox.moved == [("snow-multi", "General")]


def test_multiple_active_incidents_send_clarification_reply(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    email = EmailMessage(
        id="snow-open-multi",
        subject="Follow-up on INC7050901 and INC7050902",
        body="Please check both active incidents.",
        sender="user@example.com",
        sender_name="User Open, Analyst",
        received_at=datetime.now(timezone.utc),
        to_addresses=["ihg@servicenow.com"],
    )

    mailbox = StubMailboxClient(unread_emails=[email])
    ticketing = StubTicketingClient(
        TicketStatus.ON_HOLD,
        status_by_ticket={
            "INC7050901": TicketStatus.NEW,
            "INC7050902": TicketStatus.ON_HOLD,
        },
        match_percent_by_ticket={
            "INC7050901": 10,
            "INC7050902": 10,
        },
    )
    pipeline = EmailSegregationPipeline(
        mailbox_client=mailbox,
        ai_client=StubAIClient(),
        repository=ProcessedEmailRepository(conn),
        folder_mapper=FolderMapper({}, default_folder="General"),
        rules=[],
        metrics=Metrics(),
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        system_prompt="SYSTEM",
        fewshot_prompt="FEWSHOT",
        ticketing_client=ticketing,
        support_engineer_emails=["support@company.com"],
    )

    pipeline.process_unread_emails()

    assert ticketing.ticket_numbers == ["INC7050901", "INC7050902"]
    assert ticketing.comment_accuracy_checks == []
    assert ticketing.comment_updates == []
    assert len(mailbox.replies) == 1
    assert "could you please help us with the ticket number" in mailbox.replies[0][1].lower()
    assert "INC7050901" in mailbox.replies[0][1]
    assert "INC7050902" in mailbox.replies[0][1]


def test_two_active_and_one_resolved_incidents_still_send_clarification_reply(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    email = EmailMessage(
        id="snow-open-mixed",
        subject="Follow-up on INC7295029 INC7295190 and INC7295311",
        body="Please update the new incident and old incidents.",
        sender="user@example.com",
        sender_name="User Mixed, Analyst",
        received_at=datetime.now(timezone.utc),
        to_addresses=["ihg@servicenow.com"],
    )

    mailbox = StubMailboxClient(unread_emails=[email])
    ticketing = StubTicketingClient(
        TicketStatus.ON_HOLD,
        status_by_ticket={
            "INC7295029": TicketStatus.IN_PROGRESS,
            "INC7295190": TicketStatus.NEW,
            "INC7295311": TicketStatus.RESOLVED,
        },
        match_percent_by_ticket={
            "INC7295029": 10,
            "INC7295190": 10,
        },
    )
    pipeline = EmailSegregationPipeline(
        mailbox_client=mailbox,
        ai_client=StubAIClient(),
        repository=ProcessedEmailRepository(conn),
        folder_mapper=FolderMapper({}, default_folder="General"),
        rules=[],
        metrics=Metrics(),
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        system_prompt="SYSTEM",
        fewshot_prompt="FEWSHOT",
        ticketing_client=ticketing,
        support_engineer_emails=["support@company.com"],
    )

    pipeline.process_unread_emails()

    assert ticketing.ticket_numbers == ["INC7295029", "INC7295190", "INC7295311"]
    assert ticketing.comment_accuracy_checks == []
    assert ticketing.comment_updates == []
    assert len(mailbox.replies) == 1
    assert "INC7295029" in mailbox.replies[0][1]
    assert "INC7295190" in mailbox.replies[0][1]
    assert "INC7295311" not in mailbox.replies[0][1]


def test_multiple_incidents_all_terminal_send_single_new_case_reply(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    email = EmailMessage(
        id="snow-terminal",
        subject="Follow-up on INC7050810 and INC7050811",
        body="Please review both closed incidents.",
        sender="user@example.com",
        sender_name="User Closed, Analyst",
        received_at=datetime.now(timezone.utc),
    )

    mailbox = StubMailboxClient(unread_emails=[email])
    ticketing = StubTicketingClient(
        TicketStatus.RESOLVED,
        status_by_ticket={
            "INC7050810": TicketStatus.CANCELLED,
            "INC7050811": TicketStatus.RESOLVED,
        },
    )
    pipeline = EmailSegregationPipeline(
        mailbox_client=mailbox,
        ai_client=StubAIClient(),
        repository=ProcessedEmailRepository(conn),
        folder_mapper=FolderMapper({}, default_folder="General"),
        rules=[],
        metrics=Metrics(),
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        system_prompt="SYSTEM",
        fewshot_prompt="FEWSHOT",
        ticketing_client=ticketing,
    )

    pipeline.process_unread_emails()

    assert ticketing.ticket_numbers == ["INC7050810", "INC7050811"]
    assert len(mailbox.replies) == 1
    assert ticketing.comment_updates == []


def test_multiple_incidents_all_not_found_send_single_not_found_reply(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    email = EmailMessage(
        id="snow-missing",
        subject="Question on INC7050812 and INC7050813",
        body="Please check both incident numbers.",
        sender="user@example.com",
        sender_name="User Missing, Analyst",
        received_at=datetime.now(timezone.utc),
    )

    mailbox = StubMailboxClient(unread_emails=[email])
    ticketing = StubTicketingClient(
        TicketStatus.NOT_FOUND,
        status_by_ticket={
            "INC7050812": TicketStatus.NOT_FOUND,
            "INC7050813": TicketStatus.NOT_FOUND,
        },
    )
    pipeline = EmailSegregationPipeline(
        mailbox_client=mailbox,
        ai_client=StubAIClient(),
        repository=ProcessedEmailRepository(conn),
        folder_mapper=FolderMapper({}, default_folder="General"),
        rules=[],
        metrics=Metrics(),
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        system_prompt="SYSTEM",
        fewshot_prompt="FEWSHOT",
        ticketing_client=ticketing,
    )

    pipeline.process_unread_emails()

    assert ticketing.ticket_numbers == ["INC7050812", "INC7050813"]
    assert len(mailbox.replies) == 1
    assert "could not find them in servicenow" in mailbox.replies[0][1].lower()
    assert "INC7050812" in mailbox.replies[0][1]
    assert "INC7050813" in mailbox.replies[0][1]
    assert ticketing.comment_updates == []


def test_adhoc_ticket_routes_through_ticket_workflow(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    email = EmailMessage(
        id="adhoc1",
        subject="Follow-up on ADH123456",
        body="Please investigate this adhoc request.",
        sender="user@example.com",
        sender_name="User Adhoc",
        received_at=datetime.now(timezone.utc),
    )

    mailbox = StubMailboxClient(unread_emails=[email])
    ticketing = StubTicketingClient(
        TicketStatus.IN_PROGRESS,
        status_by_ticket={"ADH123456": TicketStatus.IN_PROGRESS},
        match_percent_by_ticket={"ADH123456": 10},
    )
    pipeline = EmailSegregationPipeline(
        mailbox_client=mailbox,
        ai_client=StubAIClient(),
        repository=ProcessedEmailRepository(conn),
        folder_mapper=FolderMapper({}, default_folder="General"),
        rules=[],
        metrics=Metrics(),
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        system_prompt="SYSTEM",
        fewshot_prompt="FEWSHOT",
        ticketing_client=ticketing,
        support_engineer_emails=["support@company.com"],
    )

    pipeline.process_unread_emails()

    assert ticketing.ticket_numbers == []
    assert ticketing.adhoc_ticket_numbers == ["ADH123456"]
    assert ticketing.comment_updates == [("ADH123456", "Please investigate this adhoc request.")]
    assert mailbox.replies == []


def test_ticket_routing_one_incident_zero_adhoc(tmp_path) -> None:
    email = EmailMessage(
        id="scenario-inc-only",
        subject="Follow-up on INC7001001",
        body="Please investigate incident INC7001001.",
        sender="user@example.com",
        sender_name="Incident User",
        received_at=datetime.now(timezone.utc),
    )
    ticketing = StubTicketingClient(
        TicketStatus.IN_PROGRESS,
        status_by_ticket={"INC7001001": TicketStatus.IN_PROGRESS},
        match_percent_by_ticket={"INC7001001": 10},
    )
    pipeline, mailbox = _build_ticket_pipeline(tmp_path, email, ticketing)

    pipeline.process_unread_emails()

    assert ticketing.ticket_numbers == ["INC7001001"]
    assert ticketing.adhoc_ticket_numbers == []
    assert ticketing.comment_updates == [
        ("INC7001001", "Please investigate incident INC7001001.")
    ]
    assert mailbox.replies == []


def test_ticket_routing_zero_incident_one_adhoc(tmp_path) -> None:
    email = EmailMessage(
        id="scenario-adh-only",
        subject="Follow-up on ADH7001002",
        body="Please investigate adhoc ADH7001002.",
        sender="user@example.com",
        sender_name="Adhoc User",
        received_at=datetime.now(timezone.utc),
    )
    ticketing = StubTicketingClient(
        TicketStatus.IN_PROGRESS,
        status_by_ticket={"ADH7001002": TicketStatus.IN_PROGRESS},
        match_percent_by_ticket={"ADH7001002": 10},
    )
    pipeline, mailbox = _build_ticket_pipeline(tmp_path, email, ticketing)

    pipeline.process_unread_emails()

    assert ticketing.ticket_numbers == []
    assert ticketing.adhoc_ticket_numbers == ["ADH7001002"]
    assert ticketing.comment_updates == [
        ("ADH7001002", "Please investigate adhoc ADH7001002.")
    ]
    assert mailbox.replies == []


def test_ticket_routing_one_incident_plus_one_adhoc(tmp_path) -> None:
    email = EmailMessage(
        id="scenario-inc-adh",
        subject="Follow-up on INC7001003 and ADH7001004",
        body="Please review INC7001003 and ADH7001004.",
        sender="user@example.com",
        sender_name="Mixed User",
        received_at=datetime.now(timezone.utc),
    )
    ticketing = StubTicketingClient(
        TicketStatus.IN_PROGRESS,
        status_by_ticket={
            "INC7001003": TicketStatus.NEW,
            "ADH7001004": TicketStatus.ON_HOLD,
        },
        match_percent_by_ticket={
            "INC7001003": 10,
            "ADH7001004": 10,
        },
    )
    pipeline, mailbox = _build_ticket_pipeline(tmp_path, email, ticketing)

    pipeline.process_unread_emails()

    assert ticketing.ticket_numbers == ["INC7001003"]
    assert ticketing.adhoc_ticket_numbers == ["ADH7001004"]
    assert ticketing.comment_accuracy_checks == []
    assert ticketing.comment_updates == []
    assert len(mailbox.replies) == 1
    assert "INC7001003" in mailbox.replies[0][1]
    assert "ADH7001004" in mailbox.replies[0][1]


def test_ticket_routing_multiple_incidents_only(tmp_path) -> None:
    email = EmailMessage(
        id="scenario-inc-multi",
        subject="Follow-up on INC7001005 INC7001006",
        body="Please review both incidents.",
        sender="user@example.com",
        sender_name="Incident Multi",
        received_at=datetime.now(timezone.utc),
    )
    ticketing = StubTicketingClient(
        TicketStatus.IN_PROGRESS,
        status_by_ticket={
            "INC7001005": TicketStatus.NEW,
            "INC7001006": TicketStatus.ON_HOLD,
        },
        match_percent_by_ticket={
            "INC7001005": 10,
            "INC7001006": 10,
        },
    )
    pipeline, mailbox = _build_ticket_pipeline(tmp_path, email, ticketing)

    pipeline.process_unread_emails()

    assert ticketing.ticket_numbers == ["INC7001005", "INC7001006"]
    assert ticketing.adhoc_ticket_numbers == []
    assert ticketing.comment_accuracy_checks == []
    assert ticketing.comment_updates == []
    assert len(mailbox.replies) == 1
    assert "INC7001005" in mailbox.replies[0][1]
    assert "INC7001006" in mailbox.replies[0][1]


def test_ticket_routing_multiple_adhoc_only(tmp_path) -> None:
    email = EmailMessage(
        id="scenario-adh-multi",
        subject="Follow-up on ADH7001007 ADH7001008",
        body="Please review both adhoc tickets.",
        sender="user@example.com",
        sender_name="Adhoc Multi",
        received_at=datetime.now(timezone.utc),
    )
    ticketing = StubTicketingClient(
        TicketStatus.IN_PROGRESS,
        status_by_ticket={
            "ADH7001007": TicketStatus.NEW,
            "ADH7001008": TicketStatus.IN_PROGRESS,
        },
        match_percent_by_ticket={
            "ADH7001007": 10,
            "ADH7001008": 10,
        },
    )
    pipeline, mailbox = _build_ticket_pipeline(tmp_path, email, ticketing)

    pipeline.process_unread_emails()

    assert ticketing.ticket_numbers == []
    assert ticketing.adhoc_ticket_numbers == ["ADH7001007", "ADH7001008"]
    assert ticketing.comment_accuracy_checks == []
    assert ticketing.comment_updates == []
    assert len(mailbox.replies) == 1
    assert "ADH7001007" in mailbox.replies[0][1]
    assert "ADH7001008" in mailbox.replies[0][1]


def test_ticket_routing_mixed_multiple_incident_and_adhoc(tmp_path) -> None:
    email = EmailMessage(
        id="scenario-mixed-multi",
        subject="Follow-up on INC7001009 ADH7001010 INC7001011 ADH7001012",
        body="Please review all open tickets.",
        sender="user@example.com",
        sender_name="Mixed Multi",
        received_at=datetime.now(timezone.utc),
    )
    ticketing = StubTicketingClient(
        TicketStatus.IN_PROGRESS,
        status_by_ticket={
            "INC7001009": TicketStatus.NEW,
            "ADH7001010": TicketStatus.ON_HOLD,
            "INC7001011": TicketStatus.RESOLVED,
            "ADH7001012": TicketStatus.IN_PROGRESS,
        },
        match_percent_by_ticket={
            "INC7001009": 10,
            "ADH7001010": 10,
            "ADH7001012": 10,
        },
    )
    pipeline, mailbox = _build_ticket_pipeline(tmp_path, email, ticketing)

    pipeline.process_unread_emails()

    assert ticketing.ticket_numbers == ["INC7001009", "INC7001011"]
    assert ticketing.adhoc_ticket_numbers == ["ADH7001010", "ADH7001012"]
    assert ticketing.comment_accuracy_checks == []
    assert ticketing.comment_updates == []
    assert len(mailbox.replies) == 1
    assert "INC7001009" in mailbox.replies[0][1]
    assert "ADH7001010" in mailbox.replies[0][1]
    assert "ADH7001012" in mailbox.replies[0][1]
    assert "INC7001011" not in mailbox.replies[0][1]


def test_two_incidents_one_closed_one_pending_checks_active_comment_accuracy(tmp_path) -> None:
    email = EmailMessage(
        id="scenario-two-incidents-mixed-status",
        subject="Follow-up on INC7001101 and INC7001102",
        body="Please check both incidents.",
        sender="user@example.com",
        sender_name="Two Incident User",
        received_at=datetime.now(timezone.utc),
    )
    ticketing = StubTicketingClient(
        TicketStatus.IN_PROGRESS,
        status_by_ticket={
            "INC7001101": TicketStatus.CLOSED,
            "INC7001102": TicketStatus.ON_HOLD,
        },
        match_percent_by_ticket={"INC7001102": 10},
    )
    pipeline, mailbox = _build_ticket_pipeline(tmp_path, email, ticketing)

    pipeline.process_unread_emails()

    assert ticketing.comment_accuracy_checks == [("INC7001102", "incident")]
    assert ticketing.comment_updates == [("INC7001102", "Please check both incidents.")]
    assert mailbox.replies == []


def test_two_adhoc_one_closed_one_pending_checks_active_comment_accuracy(tmp_path) -> None:
    email = EmailMessage(
        id="scenario-two-adhoc-mixed-status",
        subject="Follow-up on ADH7001103 and ADH7001104",
        body="Please check both adhoc tickets.",
        sender="user@example.com",
        sender_name="Two Adhoc User",
        received_at=datetime.now(timezone.utc),
    )
    ticketing = StubTicketingClient(
        TicketStatus.PENDING,
        status_by_ticket={
            "ADH7001103": TicketStatus.CANCELLED,
            "ADH7001104": TicketStatus.PENDING,
        },
        match_percent_by_ticket={"ADH7001104": 10},
    )
    pipeline, mailbox = _build_ticket_pipeline(tmp_path, email, ticketing)

    pipeline.process_unread_emails()

    assert ticketing.comment_accuracy_checks == [("ADH7001104", "adhoc")]
    assert ticketing.comment_updates == [("ADH7001104", "Please check both adhoc tickets.")]
    assert mailbox.replies == []


def test_one_incident_one_adhoc_with_one_closed_checks_active_comment_accuracy(tmp_path) -> None:
    email = EmailMessage(
        id="scenario-one-incident-one-adhoc-mixed-status",
        subject="Follow-up on INC7001105 and ADH7001106",
        body="Please check both tickets.",
        sender="user@example.com",
        sender_name="Mixed Pair User",
        received_at=datetime.now(timezone.utc),
    )
    ticketing = StubTicketingClient(
        TicketStatus.IN_PROGRESS,
        status_by_ticket={
            "INC7001105": TicketStatus.RESOLVED,
            "ADH7001106": TicketStatus.PENDING,
        },
        match_percent_by_ticket={"ADH7001106": 10},
    )
    pipeline, mailbox = _build_ticket_pipeline(tmp_path, email, ticketing)

    pipeline.process_unread_emails()

    assert ticketing.comment_accuracy_checks == [("ADH7001106", "adhoc")]
    assert ticketing.comment_updates == [("ADH7001106", "Please check both tickets.")]
    assert mailbox.replies == []
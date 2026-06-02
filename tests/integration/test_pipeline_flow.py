from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.application.pipeline import EmailSegregationPipeline
from app.domain.folder_mapper import FolderMapper
from app.domain.models import EmailMessage, Rule, TicketStatus
from app.infrastructure.mailbox.base import MailboxClient
from app.infrastructure.persistence.db import init_schema
from app.infrastructure.persistence.repository import ProcessedEmailRepository
from app.observability.audit_logger import AuditLogger
from app.observability.metrics import Metrics

from app.infrastructure.ai.base import AIClient


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
        self.comment_updates: list[tuple[str, str]] = []

    def get_ticket_status(self, ticket_number: str) -> TicketStatus:
        self.ticket_numbers.append(ticket_number)
        return self.status_by_ticket.get(ticket_number, self.status)

    def comment_accuracy_validation(self, incident_number: str, email: EmailMessage) -> dict[str, object]:
        match_percent = self.match_percent_by_ticket.get(incident_number, 100)
        return {
            "match_percent": match_percent,
            "matched_words": set(),
            "customer_comment": "",
            "match": match_percent >= 70,
        }

    def extract_email_body(self, body: str) -> str:
        return body

    def add_comment(self, incident_number: str, mail_body: str) -> bool:
        self.comment_updates.append((incident_number, mail_body))
        return True


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

    assert processed == 1
    assert mailbox.moved == [("x1", "Finance")]
    assert "x1" in repo.list_processed_ids()


def test_vip_escalation_no_reply_no_folder_move(tmp_path) -> None:
    """Test VIP sender: should NOT send reply, should NOT move email, should NOT process."""
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

    # VIP escalation should be counted as processed
    assert processed == 1
    # But NO reply should be sent
    assert len(mailbox.replies) == 0
    # And email should NOT be moved from Inbox
    assert len(mailbox.moved) == 0
    # Email should still be marked as processed in DB
    assert "vip1" in repo.list_processed_ids()


def test_vip_escalation_audit_logging(tmp_path) -> None:
    """Test that VIP escalation is properly logged in audit log."""
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

    # Check audit log contains VIP escalation action
    audit_file = tmp_path / "audit_vip.jsonl"
    assert audit_file.exists()
    
    audit_lines = audit_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(audit_lines) == 1
    
    import json
    audit_entry = json.loads(audit_lines[0])
    assert audit_entry["action"] == "vip_escalation"
    assert audit_entry["email_id"] == "vip2"
    assert audit_entry["sender"] == "jane.cto@company.com"
    assert "requires discussion with" in audit_entry.get("note", "").lower()


def test_cc_support_engineers_on_reply(tmp_path) -> None:
    """Test that replies are CC'd to all support engineer emails."""
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

    assert processed == 1
    assert len(mailbox.replies) == 1
    
    email_id, reply_body, cc_list = mailbox.replies[0]
    assert email_id == "support1"
    # All support engineers should be CC'd
    assert set(cc_list) == set(support_engineers)


def test_cc_support_engineers_empty_list(tmp_path) -> None:
    """Test that replies work correctly when no support engineers are configured."""
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
        support_engineer_emails=[],  # Empty list
        general_categories=["general"],
    )

    processed = pipeline.process_unread_emails()

    assert processed == 1
    assert len(mailbox.replies) == 1
    
    email_id, reply_body, cc_list = mailbox.replies[0]
    # CC list should be empty (no support engineers)
    assert cc_list == []


def test_vip_escalation_takes_precedence_over_classification(tmp_path) -> None:
    """Test that VIP escalation is checked BEFORE email classification and reply."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    # Email that matches support rules but is from a VIP
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

    assert processed == 1
    # VIP escalation takes precedence: NO reply sent
    assert len(mailbox.replies) == 0
    # Email stays in Inbox (not moved to Support folder)
    assert len(mailbox.moved) == 0


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

    assert processed == 1
    assert mailbox.replies == []
    assert mailbox.moved == []


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

    assert processed == 1
    assert mailbox.replies == []
    assert mailbox.support_notifications == []
    assert ticketing.comment_updates == []
    assert mailbox.moved == []


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

    assert processed == 1
    assert ticketing.ticket_numbers == ["INC7050808"]
    assert len(mailbox.support_notifications) == 1
    assert mailbox.support_notifications[0][0] == support_engineers
    assert "user-query-snow2.txt" == mailbox.support_notifications[0][3]
    assert len(ticketing.comment_updates) == 1
    assert ticketing.comment_updates[0] == ("INC7050808", "Please investigate this issue.")
    assert mailbox.replies == []
    assert mailbox.moved == []


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

    assert processed == 1
    assert ticketing.ticket_numbers == ["INC7050808"]
    assert len(mailbox.support_notifications) == 1
    assert mailbox.support_notifications[0][0] == support_engineers
    assert len(ticketing.comment_updates) == 1
    assert ticketing.comment_updates[0] == ("INC7050808", "Please review this issue.")
    assert mailbox.replies == []
    assert mailbox.moved == []


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

    assert processed == 1
    assert len(mailbox.support_notifications) == 1
    assert mailbox.support_notifications[0][0] == support_engineers
    assert len(ticketing.comment_updates) == 1
    assert ticketing.comment_updates[0] == ("INC7050808", "Please investigate this query.")
    assert mailbox.replies == []
    assert mailbox.moved == []


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

    assert processed == 1
    assert len(mailbox.replies) == 1
    assert mailbox.replies[0][2] == []
    assert "currently in a <strong>Resolved</strong> state" in mailbox.replies[0][1]
    assert mailbox.support_notifications == []
    assert ticketing.comment_updates == []
    assert mailbox.moved == []


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

    assert ticketing.ticket_numbers == ["INC7050808", "INC7050809"]
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
    assert ticketing.comment_updates == [
        ("INC7050901", "Please check both active incidents."),
        ("INC7050902", "Please check both active incidents."),
    ]
    assert len(mailbox.replies) == 1
    assert "which incident you want us to review" in mailbox.replies[0][1].lower()
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
    assert ticketing.comment_updates == [
        ("INC7295029", "Please update the new incident and old incidents."),
        ("INC7295190", "Please update the new incident and old incidents."),
    ]
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
    assert "each referenced incident is already in a closed state" in mailbox.replies[0][1].lower()
    assert "raise a new case" in mailbox.replies[0][1].lower()
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

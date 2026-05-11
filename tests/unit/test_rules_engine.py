from __future__ import annotations

from datetime import datetime, timezone

from app.domain.models import EmailMessage, Rule
from app.domain.rules_engine import (
    classify_with_rules,
    extract_adhoc_number,
    extract_incident_number,
    extract_ref_message_id,
    extract_ticket_number,
    is_auto_notification_email,
    is_vip_sender,
)


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


# ─── VIP Sender Detection Tests ──────────────────────────────────────────────

def test_vip_sender_detected_in_display_name() -> None:
    """VIP title found in sender's display name (sender_name field)."""
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
    """VIP title found in email body/signature (not in sender_name)."""
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
        sender_name="Jane Doe",  # No title in sender_name
        received_at=datetime.now(timezone.utc),
    )
    vip_titles = ["Director", "VP", "Chief", "CEO", "CTO"]

    is_vip, detected_by = is_vip_sender(email, vip_titles)

    assert is_vip is True
    assert "body_signature" in detected_by
    assert "VP" in detected_by


def test_vip_sender_detected_in_body_with_director_title() -> None:
    """VIP detection catches 'Director' title in email body."""
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
    """Non-VIP sender: no title in name or body."""
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
    """VIP title detection is case-insensitive."""
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
    """VIP detection works with multiple VIP title patterns."""
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
    """VIP detection uses substring matching (VP matches 'VP Engineering')."""
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
    """Graph-enriched sender_name (e.g. 'Jane Doe, VP Engineering') triggers VIP."""
    # This simulates what fetch_unread does when Graph returns jobTitle
    email = EmailMessage(
        id="vip7",
        subject="Operational Update",
        body="Please review the operational update.",
        sender="jane.doe@company.com",
        sender_name="Jane Doe, VP Engineering",  # enriched by Graph job title lookup
        received_at=datetime.now(timezone.utc),
    )
    vip_titles = ["Director", "VP", "Chief", "CEO"]

    is_vip, detected_by = is_vip_sender(email, vip_titles)

    assert is_vip is True
    assert "sender_name" in detected_by  # caught via sender_name (Graph-enriched)


def test_extract_incident_and_adhoc_numbers_separately() -> None:
    email = EmailMessage(
        id="ticket1",
        subject="Issue with INC7050808",
        body="Please check ADH123456 on priority.",
        sender="user@example.com",
        received_at=datetime.now(timezone.utc),
    )

    assert extract_incident_number(email) == "INC7050808"
    assert extract_adhoc_number(email) == "ADH123456"
    assert extract_ticket_number(email) == "INC7050808"


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

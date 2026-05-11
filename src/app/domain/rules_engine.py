from __future__ import annotations

import re
from typing import Iterable

from app.domain.models import EmailMessage, Rule

_INCIDENT_RE = re.compile(r"\bINC\d{5,15}\b", re.IGNORECASE)
_ADHOC_RE = re.compile(r"\bADH\d{5,15}\b", re.IGNORECASE)
_REF_MESSAGE_RE = re.compile(
    r"\b(?:ref(?:erence)?\s*(?:msg|message)?\s*(?:id|number|no\.?)*\s*[:#-]?\s*)([A-Z0-9-]{3,})\b",
    re.IGNORECASE,
)
_BOT_SENDER_PATTERNS = (
    "noreply",
    "no-reply",
    "do-not-reply",
    "donotreply",
    "mailer-daemon",
    "postmaster",
    "notification",
    "notifications",
    "alerts",
    "bot",
)
_BOT_KEYWORDS = (
    "fsprod",
    "unx",
    "fsprd",
    "appsrv",
    "websrvservice",
)


def classify_with_rules(email: EmailMessage, rules: Iterable[Rule]) -> tuple[str | None, str]:
    subject_body = f"{email.subject}\n{email.body}".lower()

    for rule in rules:
        if rule.sender_contains and rule.sender_contains.lower() not in email.sender.lower():
            continue

        for keyword in rule.keywords:
            if keyword.lower() in subject_body:
                return rule.category, f"Matched keyword '{keyword}'"

    return None, "No rule matched"


def _normalized_message_text(email: EmailMessage) -> str:
    text = f"{email.subject} {email.body}"
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_by_pattern(pattern: re.Pattern[str], email: EmailMessage) -> str | None:
    match = pattern.search(_normalized_message_text(email))
    return match.group(0).upper() if match else None


def extract_incident_number(email: EmailMessage) -> str | None:
    return _extract_by_pattern(_INCIDENT_RE, email)


def extract_adhoc_number(email: EmailMessage) -> str | None:
    return _extract_by_pattern(_ADHOC_RE, email)


def extract_ticket_number(email: EmailMessage) -> str | None:
    return extract_incident_number(email) or extract_adhoc_number(email)


def extract_ref_message_id(email: EmailMessage) -> str | None:
    match = _REF_MESSAGE_RE.search(_normalized_message_text(email))
    return match.group(1).upper() if match else None

# def extract_ticket_number(email: EmailMessage) -> str | None:
#     """Return the first ticket/reference number found in the subject or body, or None."""
#     text = f"{email.subject} {email.body}"
#     match = _TICKET_RE.search(text)
#     return match.group(1) if match else None


def is_vip_sender(email: EmailMessage, vip_titles: list[str]) -> tuple[bool, str]:
    """Return (True, source) if the sender is a VIP, (False, "") otherwise.

    Detection order (first match wins):
    1. sender_name  — display name from the email From: header / Azure AD
       (includes Graph-enriched job title when available)
    2. email body   — signature block containing a VIP title

    Examples of "source" values:
    - "sender_name: John Smith, VP Engineering"
    - "body_signature: Director of Sales"
    """
    name_lower = email.sender_name.lower()
    body_lower = email.body.lower()

    for title in vip_titles:
        t = title.lower()
        if name_lower and t in name_lower:
            return True, f"sender_name: {email.sender_name}"

    for title in vip_titles:
        t = title.lower()
        if body_lower and t in body_lower:
            return True, f"body_signature: matched '{title}'"

    return False, ""


def is_auto_notification_email(email: EmailMessage) -> tuple[bool, str]:
    sender_lower = email.sender.lower()
    text_lower = f"{email.subject} {email.body}".lower()

    for pattern in _BOT_SENDER_PATTERNS:
        if pattern in sender_lower:
            return True, f"sender_pattern:{pattern}"

    for keyword in _BOT_KEYWORDS:
        if keyword in sender_lower or keyword in text_lower:
            return True, f"keyword:{keyword}"

    return False, ""


def is_servicenow_cced(email: EmailMessage) -> bool:
    """Check if 'ihg@servicenow.com' is in the TO or CC addresses."""
    to_list = email.to_addresses or []
    cc_list = email.cc_addresses or []
    return "ihg@service-now.com" in to_list or "ihg@service-now.com" in cc_list

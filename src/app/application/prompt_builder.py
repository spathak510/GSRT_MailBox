from __future__ import annotations

import re


_SENSITIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d{4}\s\d{4}\s\d{4}\b"), "[REDACTED_AADHAAR]"),
    (re.compile(r"\b\d{12}\b"), "[REDACTED_AADHAAR]"),
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b", re.IGNORECASE), "[REDACTED_PAN]"),
    (re.compile(r"\b[A-PR-WYa-pr-wy]\d{7}\b"), "[REDACTED_PASSPORT]"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[REDACTED_BANK_ACCOUNT]"),
    (re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b", re.IGNORECASE), "[REDACTED_IFSC]"),
    (re.compile(r"\b(?:\+91[-\s]?)?[6-9]\d{9}\b"), "[REDACTED_PHONE]"),
    (re.compile(r"\b[a-z0-9._-]{2,}@[a-z][a-z0-9]{2,}(?!\.)\b", re.IGNORECASE), "[REDACTED_UPI_ID]"),
)


def mask_sensitive_text(text: str) -> str:
    masked_text = text
    for pattern, replacement in _SENSITIVE_PATTERNS:
        masked_text = pattern.sub(replacement, masked_text)
    return masked_text


def build_classifier_prompt(system_prompt: str, fewshot_prompt: str, subject: str, body: str) -> str:
    sanitized_subject = mask_sensitive_text(subject)
    sanitized_body = mask_sensitive_text(body)

    return (
        f"{system_prompt.strip()}\n\n"
        f"{fewshot_prompt.strip()}\n\n"
        "Classify the email into one category.\n"
        "Return JSON: {\"category\": \"...\", \"reason\": \"...\"}.\n\n"
        f"Subject: {sanitized_subject}\n"
        f"Body: {sanitized_body}\n"
    )

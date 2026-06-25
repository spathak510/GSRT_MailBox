from __future__ import annotations

from app.application.prompt_builder import build_classifier_prompt, mask_sensitive_text


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
    assert "[REDACTED_BANK_ACCOUNT]" in masked
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
    assert "[REDACTED_BANK_ACCOUNT]" in prompt
    assert "[REDACTED_AADHAAR]" in prompt
    assert "[REDACTED_UPI_ID]" in prompt

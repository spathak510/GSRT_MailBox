from __future__ import annotations

from datetime import datetime, timezone

from app.application.use_cases import classify_email
from app.domain.models import EmailMessage
from app.infrastructure.ai.base import AIClient


class RecordingAIClient(AIClient):
    def __init__(self) -> None:
        self.prompt: str | None = None

    def classify_email(self, email: EmailMessage, prompt: str) -> tuple[str, str]:
        self.prompt = prompt
        return "general", "AI classified"


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
from __future__ import annotations

from app.application.prompt_builder import build_classifier_prompt


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

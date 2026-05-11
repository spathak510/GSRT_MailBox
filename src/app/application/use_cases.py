from __future__ import annotations

from app.application.prompt_builder import build_classifier_prompt
from app.domain.models import ClassificationResult, EmailMessage, Rule
from app.domain.rules_engine import classify_with_rules
from app.infrastructure.ai.base import AIClient


def classify_email(
    email: EmailMessage,
    rules: list[Rule],
    ai_client: AIClient,
    system_prompt: str,
    fewshot_prompt: str,
) -> ClassificationResult:
    category, reason = classify_with_rules(email, rules)
    if category:
        return ClassificationResult(email_id=email.id, category=category, reason=reason)

    prompt = build_classifier_prompt(system_prompt, fewshot_prompt, email.subject, email.body)
    ai_category, ai_reason = ai_client.classify_email(email, prompt)
    return ClassificationResult(email_id=email.id, category=ai_category, reason=ai_reason)

from __future__ import annotations

import json
import logging

from openai import OpenAI

from app.domain.models import EmailMessage
from app.infrastructure.ai.base import AIClient

logger = logging.getLogger(__name__)


class OpenAIClient(AIClient):
    """Classifies emails using OpenAI GPT chat completion.

    Requires OPENAI_API_KEY to be set in .env.
    Falls back to 'general' category if the response cannot be parsed.
    """

    def __init__(self, model: str = "gpt-4o-mini", api_key: str | None = None) -> None:
        self._model = model
        self._client = OpenAI(api_key=api_key)

    def classify_email(self, email: EmailMessage, prompt: str) -> tuple[str, str]:
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=100,
            )
            raw = response.choices[0].message.content or "{}"
            data = json.loads(raw)
            category = data.get("category", "general").strip().lower()
            reason = data.get("reason", "AI classified")
            logger.debug("OpenAI classified email_id=%s as %s", email.id, category)
            return category, reason
        except Exception as exc:
            logger.warning("OpenAI classification failed for email_id=%s: %s", email.id, exc)
            return "general", f"AI fallback: {exc}"

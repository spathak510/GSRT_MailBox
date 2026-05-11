from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.models import EmailMessage


class AIClient(ABC):
    @abstractmethod
    def classify_email(self, email: EmailMessage, prompt: str) -> tuple[str, str]:
        """Return category and reason."""
        raise NotImplementedError

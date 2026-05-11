from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.models import EmailMessage


class MailboxClient(ABC):
    @abstractmethod
    def fetch_unread(self, limit: int = 25) -> list[EmailMessage]:
        raise NotImplementedError

    @abstractmethod
    def move_email(self, email_id: str, folder_name: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def reply_email(
        self,
        email_id: str,
        body: str,
        cc_addresses: list[str] | None = None,
    ) -> None:
        """Send a reply to the given email, optionally CC-ing additional recipients."""
        raise NotImplementedError

    @abstractmethod
    def create_folders(self, folders: list[str]) -> None:
        raise NotImplementedError

    @abstractmethod
    def send_support_notification(
        self,
        to_addresses: list[str],
        subject: str,
        body: str,
        attachment_name: str | None = None,
        attachment_content: str | None = None,
    ) -> None:
        """Send a support email, optionally with a text attachment."""
        raise NotImplementedError

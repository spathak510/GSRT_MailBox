from __future__ import annotations

from datetime import datetime, timezone

from app.infrastructure.mailbox.microsoft_graph_client import MicrosoftGraphMailboxClient


def build_client() -> MicrosoftGraphMailboxClient:
    client = MicrosoftGraphMailboxClient(
        tenant_id="tenant",
        client_id="client",
        client_secret="secret",
        mailbox_user="mailbox@example.com",
        mailbox_password="password",
    )
    client._emails = [
        client._emails[0] if client._emails else None,
    ]
    client._emails = [
        email for email in client._emails if email is not None
    ] or [
        __import__("app.domain.models", fromlist=["EmailMessage"]).EmailMessage(
            id="fallback-1",
            subject="Fallback subject",
            body="Fallback body",
            sender="fallback@example.com",
            received_at=datetime.now(timezone.utc),
        )
    ]
    return client


def test_fetch_unread_falls_back_to_local_messages_on_graph_error() -> None:
    client = build_client()

    def raise_graph_error(endpoint: str) -> dict:
        raise RuntimeError("graph unavailable")

    client._graph_get = raise_graph_error  # type: ignore[method-assign]

    unread = client.fetch_unread(limit=1)

    assert len(unread) == 1
    assert unread[0].id == "fallback-1"


def test_reply_email_falls_back_to_local_behavior_on_graph_error(capsys) -> None:
    client = build_client()

    def raise_graph_error(endpoint: str, payload: dict) -> dict:
        raise RuntimeError("graph unavailable")

    client._graph_post = raise_graph_error  # type: ignore[method-assign]

    client.reply_email("email-123", "Hello", ["support@example.com"])

    captured = capsys.readouterr()
    assert "[mailbox] reply to email email-123 | cc: support@example.com" in captured.out


def test_reply_email_creates_threaded_reply_draft_before_sending() -> None:
    client = build_client()
    calls: list[tuple[str, str, dict | None]] = []

    def fake_graph_request(method: str, endpoint: str, payload: dict | None = None) -> dict:
        calls.append((method, endpoint, payload))
        if method == "POST" and endpoint.endswith("/createReply"):
            return {
                "id": "draft-123",
                "body": {
                    "contentType": "HTML",
                    "content": "<html><body><div>Original thread</div></body></html>",
                },
            }
        return {}

    client._graph_request = fake_graph_request  # type: ignore[method-assign]

    client.reply_email("email-123", "<p>Hello</p>", ["support@example.com"])

    assert calls == [
        (
            "POST",
            "/users/mailbox@example.com/messages/email-123/createReply",
            {},
        ),
        (
            "PATCH",
            "/users/mailbox@example.com/messages/draft-123",
            {
                "body": {
                    "contentType": "HTML",
                    "content": "<html><body><p>Hello</p><br/><br/><div>Original thread</div></body></html>",
                },
                "ccRecipients": [
                    {"emailAddress": {"address": "support@example.com"}},
                ],
            },
        ),
        (
            "POST",
            "/users/mailbox@example.com/messages/draft-123/send",
            {},
        ),
    ]
from __future__ import annotations

from types import SimpleNamespace

from app.domain.models import TicketStatus
from app.infrastructure.ticketing.base import ServiceNowTicketingClient


def test_get_ticket_status_uses_incident_table_query(monkeypatch) -> None:
    captured: dict = {}

    def fake_get(url, headers, auth, params, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["auth"] = auth
        captured["params"] = params
        captured["timeout"] = timeout

        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"result": [{"state": "2"}]},
        )

    monkeypatch.setenv("IHG_SERVICENOW_URL", "https://ihguat.service-now.com/api/now/table/incident?sysparm_query=number=")
    monkeypatch.setenv("IHG_SERVICENOW_USERNAME", "svc_user")
    monkeypatch.setenv("IHG_SERVICENOW_PASSWORD", "svc_password")
    monkeypatch.delenv("IHG_SERVICENOW_INCIDENT_TABLE_URL", raising=False)
    monkeypatch.delenv("IHG_SERVICENOW_BASIC_AUTH", raising=False)
    monkeypatch.delenv("IHG_SERVICENOW_COOKIE", raising=False)
    monkeypatch.setattr("app.infrastructure.ticketing.base.requests.get", fake_get)

    client = ServiceNowTicketingClient()

    status = client.get_ticket_status("INC7050808")

    assert status == TicketStatus.IN_PROGRESS
    assert captured["url"] == "https://ihguat.service-now.com/api/now/table/incident"
    assert captured["auth"] == ("svc_user", "svc_password")
    assert captured["params"] == {
        "sysparm_query": "number=INC7050808",
        "sysparm_limit": "1",
    }
    assert captured["headers"] == {"Accept": "application/json"}
    assert captured["timeout"] == 5


def test_get_ticket_status_prefers_explicit_headers(monkeypatch) -> None:
    captured: dict = {}

    def fake_get(url, headers, auth, params, timeout):
        captured["headers"] = headers
        captured["auth"] = auth
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"result": [{"state": "6"}]},
        )

    monkeypatch.setenv("IHG_SERVICENOW_URL", "https://ihguat.service-now.com")
    monkeypatch.setenv("IHG_SERVICENOW_USERNAME", "svc_user")
    monkeypatch.setenv("IHG_SERVICENOW_PASSWORD", "svc_password")
    monkeypatch.setenv("IHG_SERVICENOW_BASIC_AUTH", "Basic token-value")
    monkeypatch.setenv("IHG_SERVICENOW_COOKIE", "cookie=value")
    monkeypatch.setattr("app.infrastructure.ticketing.base.requests.get", fake_get)

    client = ServiceNowTicketingClient()

    status = client.get_ticket_status("INC7050808")

    assert status == TicketStatus.RESOLVED
    assert captured["auth"] is None
    assert captured["headers"] == {
        "Accept": "application/json",
        "Authorization": "Basic token-value",
        "Cookie": "cookie=value",
    }
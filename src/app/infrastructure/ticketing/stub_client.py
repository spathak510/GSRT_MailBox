from __future__ import annotations

from app.domain.models import TicketStatus
from app.infrastructure.ticketing.base import TicketingClient


class StubTicketingClient(TicketingClient):
    def get_ticket_status(self, ticket_number: str) -> TicketStatus:
        if ticket_number.endswith("1"):
            return TicketStatus.RESOLVED
        elif ticket_number.endswith("2"):
            return TicketStatus.CLOSED
        elif ticket_number.endswith("3"):
            return TicketStatus.ON_HOLD
        return TicketStatus.NEW

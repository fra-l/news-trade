"""Mock contract provider for tests and dry-run scenarios."""

from __future__ import annotations

from datetime import datetime

from news_trade.models.contracts import ContractAwardEvent


class MockContractProvider:
    """Returns a fixed list of ``ContractAwardEvent`` objects.

    Satisfies the ``ContractProvider`` protocol without any network calls.
    Construct with a list of events; ``fetch()`` always returns that list
    regardless of the time window requested.

    Args:
        events: Events to return from every ``fetch()`` call.
    """

    def __init__(self, events: list[ContractAwardEvent] | None = None) -> None:
        self._events = events or []

    @property
    def name(self) -> str:
        return "mock"

    async def fetch(
        self,
        since: datetime,
        until: datetime | None = None,
    ) -> list[ContractAwardEvent]:
        return list(self._events)

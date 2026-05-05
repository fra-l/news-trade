"""Mock lobbying provider for tests and dry-run scenarios."""

from __future__ import annotations

from news_trade.models.lobbying import LobbyingFiling


class MockLobbyingProvider:
    """Returns a configurable list of ``LobbyingFiling`` objects.

    Satisfies the ``LobbyingDataProvider`` protocol without any network calls.

    Args:
        filings: Filings to return from every ``get_filings()`` call.
    """

    def __init__(self, filings: list[LobbyingFiling] | None = None) -> None:
        self._filings = filings or []

    async def get_filings(
        self,
        client_name: str,
        quarters: list[tuple[int, str]],
    ) -> list[LobbyingFiling]:
        return list(self._filings)

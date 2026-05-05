"""Unit tests for ContractPollerAgent.

Verifies that the agent:
  - Returns awards from the injected provider via run()
  - Writes ``contract_awards`` and ``last_poll_govtrade`` to state
  - Uses ``last_poll_govtrade`` from state as the lower time bound
  - Falls back to ``now - poll_interval`` when the key is absent
  - Handles provider exceptions gracefully (empty awards + error key)

No real HTTP, no real Redis, no real database.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from gov_trade.agents.contract_poller import ContractPollerAgent
from news_trade.config import GovTradeSettings, Settings
from news_trade.models.contracts import ContractAwardEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**kwargs: object) -> Settings:
    return Settings()  # type: ignore[call-arg]


def _make_gov_settings(**kwargs: object) -> GovTradeSettings:
    defaults: dict[str, object] = dict(usaspending_poll_interval_minutes=60)
    return GovTradeSettings(**(defaults | kwargs))  # type: ignore[arg-type]


def _make_award(**kwargs: object) -> ContractAwardEvent:
    defaults: dict[str, object] = dict(
        award_id="AWARD-001",
        recipient_name="LOCKHEED MARTIN CORPORATION",
        amount_usd=50_000_000.0,
        awarding_agency="DEPT OF DEFENSE",
        award_type="D",
        sign_date=datetime.utcnow().date(),
    )
    return ContractAwardEvent(**(defaults | kwargs))  # type: ignore[arg-type]


def _make_agent(
    *,
    gov_settings: GovTradeSettings | None = None,
    provider: object | None = None,
) -> ContractPollerAgent:
    mock_bus = MagicMock()
    mock_provider = provider or AsyncMock()
    if provider is None:
        mock_provider.name = "mock"
        mock_provider.fetch = AsyncMock(return_value=[])
    return ContractPollerAgent(
        settings=_make_settings(),
        event_bus=mock_bus,  # type: ignore[arg-type]
        gov_settings=gov_settings or _make_gov_settings(),
        provider=mock_provider,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# State key output
# ---------------------------------------------------------------------------


class TestStateOutput:
    async def test_run_returns_contract_awards_key(self) -> None:
        agent = _make_agent()
        result = await agent.run({})
        assert "contract_awards" in result

    async def test_run_returns_last_poll_govtrade_key(self) -> None:
        agent = _make_agent()
        result = await agent.run({})
        assert "last_poll_govtrade" in result
        assert isinstance(result["last_poll_govtrade"], datetime)

    async def test_empty_provider_returns_empty_awards(self) -> None:
        provider = AsyncMock()
        provider.name = "mock"
        provider.fetch = AsyncMock(return_value=[])
        agent = _make_agent(provider=provider)

        result = await agent.run({})

        assert result["contract_awards"] == []

    async def test_provider_awards_are_forwarded(self) -> None:
        award = _make_award()
        provider = AsyncMock()
        provider.name = "mock"
        provider.fetch = AsyncMock(return_value=[award])
        agent = _make_agent(provider=provider)

        result = await agent.run({})

        assert result["contract_awards"] == [award]

    async def test_multiple_awards_all_forwarded(self) -> None:
        awards = [_make_award(award_id=f"AWARD-{i:03d}") for i in range(5)]
        provider = AsyncMock()
        provider.name = "mock"
        provider.fetch = AsyncMock(return_value=awards)
        agent = _make_agent(provider=provider)

        result = await agent.run({})

        assert len(result["contract_awards"]) == 5


# ---------------------------------------------------------------------------
# Poll window
# ---------------------------------------------------------------------------


class TestPollWindow:
    async def test_last_poll_from_state_used_as_since(self) -> None:
        captured: dict[str, datetime] = {}
        known_since = datetime(2026, 1, 15, 12, 0, 0)

        async def _fetch(since: datetime, until: datetime | None = None) -> list:
            captured["since"] = since
            return []

        provider = AsyncMock()
        provider.name = "mock"
        provider.fetch = _fetch

        agent = _make_agent(provider=provider)
        await agent.run({"last_poll_govtrade": known_since})

        assert captured["since"] == known_since

    async def test_missing_last_poll_defaults_to_one_interval_ago(self) -> None:
        captured: dict[str, datetime] = {}
        interval_minutes = 30

        async def _fetch(since: datetime, until: datetime | None = None) -> list:
            captured["since"] = since
            return []

        provider = AsyncMock()
        provider.name = "mock"
        provider.fetch = _fetch

        gov = _make_gov_settings(usaspending_poll_interval_minutes=interval_minutes)
        agent = _make_agent(gov_settings=gov, provider=provider)

        before = datetime.utcnow()
        await agent.run({})

        expected_approx = before - timedelta(minutes=interval_minutes)
        assert abs((captured["since"] - expected_approx).total_seconds()) < 5

    async def test_last_poll_govtrade_updated_to_roughly_now(self) -> None:
        provider = AsyncMock()
        provider.name = "mock"
        provider.fetch = AsyncMock(return_value=[])

        agent = _make_agent(provider=provider)
        before = datetime.utcnow()
        result = await agent.run({})
        after = datetime.utcnow()

        ts: datetime = result["last_poll_govtrade"]
        assert before <= ts <= after


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    async def test_provider_exception_returns_empty_awards(self) -> None:
        provider = AsyncMock()
        provider.name = "mock"
        provider.fetch = AsyncMock(side_effect=RuntimeError("network timeout"))

        agent = _make_agent(provider=provider)
        result = await agent.run({})

        assert result["contract_awards"] == []

    async def test_provider_exception_populates_errors_key(self) -> None:
        provider = AsyncMock()
        provider.name = "mock"
        provider.fetch = AsyncMock(side_effect=RuntimeError("network timeout"))

        agent = _make_agent(provider=provider)
        result = await agent.run({})

        assert "errors" in result
        assert len(result["errors"]) == 1
        assert "ContractPoller" in result["errors"][0]

    async def test_provider_exception_still_updates_timestamp(self) -> None:
        provider = AsyncMock()
        provider.name = "mock"
        provider.fetch = AsyncMock(side_effect=ConnectionError("refused"))

        agent = _make_agent(provider=provider)
        before = datetime.utcnow()
        result = await agent.run({})
        after = datetime.utcnow()

        ts: datetime = result["last_poll_govtrade"]
        assert before <= ts <= after

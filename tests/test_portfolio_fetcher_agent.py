"""Tests for PortfolioFetcherAgent."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from news_trade.agents.portfolio_fetcher import (
    PortfolioFetcherAgent,
)
from news_trade.config import Settings
from news_trade.models.portfolio import PortfolioState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**kwargs) -> Settings:
    defaults: dict[str, object] = dict(
        anthropic_api_key="test",
        alpaca_api_key="test",
        alpaca_secret_key="test",
    )
    return Settings(**(defaults | kwargs))


def _make_agent(**kwargs) -> PortfolioFetcherAgent:
    settings = kwargs.pop("settings", _make_settings())
    return PortfolioFetcherAgent(settings, MagicMock(), **kwargs)


def _make_alpaca_account(**kwargs) -> MagicMock:
    """Alpaca Account mock; all numeric fields are Decimal strings in the real SDK."""
    defaults: dict[str, object] = dict(
        equity="100000.00",
        last_equity="99000.00",
        cash="50000.00",
        buying_power="100000.00",
    )
    mock = MagicMock()
    for k, v in (defaults | kwargs).items():
        setattr(mock, k, v)
    return mock


def _make_alpaca_position(**kwargs) -> MagicMock:
    """Alpaca Position mock."""
    defaults: dict[str, object] = dict(
        symbol="AAPL",
        qty="10",
        avg_entry_price="150.00",
        current_price="155.00",
        unrealized_pl="50.00",
        market_value="1550.00",
    )
    mock = MagicMock()
    for k, v in (defaults | kwargs).items():
        setattr(mock, k, v)
    return mock


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path() -> None:
    """All fields are populated from Alpaca data."""
    account = _make_alpaca_account()
    position = _make_alpaca_position()

    alpaca = MagicMock()
    alpaca.get_account.return_value = account
    alpaca.get_all_positions.return_value = [position]

    agent = _make_agent(alpaca_client=alpaca)
    result = await agent.run({})

    portfolio: PortfolioState = result["portfolio"]
    assert portfolio.equity == 100_000.0
    assert portfolio.cash == 50_000.0
    assert portfolio.buying_power == 100_000.0
    assert portfolio.daily_pnl == pytest.approx(1000.0)
    assert len(portfolio.positions) == 1
    assert "errors" not in result or result["errors"] == []


@pytest.mark.asyncio
async def test_drawdown_calculation() -> None:
    """Drawdown is (last_equity - equity) / last_equity when equity fell."""
    account = _make_alpaca_account(equity="97000.00", last_equity="100000.00")
    alpaca = MagicMock()
    alpaca.get_account.return_value = account
    alpaca.get_all_positions.return_value = []

    agent = _make_agent(alpaca_client=alpaca)
    result = await agent.run({})

    portfolio = result["portfolio"]
    assert portfolio.max_drawdown_pct == pytest.approx(0.03)
    assert portfolio.daily_pnl == pytest.approx(-3000.0)


@pytest.mark.asyncio
async def test_no_drawdown_when_gaining() -> None:
    """max_drawdown_pct is 0 when equity is above last_equity."""
    account = _make_alpaca_account(equity="103000.00", last_equity="100000.00")
    alpaca = MagicMock()
    alpaca.get_account.return_value = account
    alpaca.get_all_positions.return_value = []

    agent = _make_agent(alpaca_client=alpaca)
    result = await agent.run({})

    assert result["portfolio"].max_drawdown_pct == 0.0


@pytest.mark.asyncio
async def test_no_alpaca_client() -> None:
    """Returns empty PortfolioState when no client is configured."""
    agent = _make_agent(alpaca_client=None)
    result = await agent.run({})

    portfolio = result["portfolio"]
    assert portfolio.equity == 0.0
    assert portfolio.cash == 0.0
    assert portfolio.positions == []
    assert portfolio.max_drawdown_pct == 0.0
    assert "errors" not in result


@pytest.mark.asyncio
async def test_alpaca_unavailable() -> None:
    """Returns empty PortfolioState and records error when Alpaca raises."""
    alpaca = MagicMock()
    alpaca.get_account.side_effect = ConnectionError("broker offline")

    agent = _make_agent(alpaca_client=alpaca)
    result = await agent.run({})

    portfolio = result["portfolio"]
    assert portfolio.equity == 0.0
    assert portfolio.cash == 0.0
    errors: list[str] = result.get("errors", [])
    assert len(errors) == 1
    assert "broker offline" in errors[0]


@pytest.mark.asyncio
async def test_alpaca_unavailable_returns_new_error_only() -> None:
    """On Alpaca failure, only the new PortfolioFetcher error is returned.

    With operator.add reducers, agents return only NEW errors.
    The reducer accumulates errors across parallel nodes automatically.
    """
    alpaca = MagicMock()
    alpaca.get_account.side_effect = ConnectionError("broker offline")

    agent = _make_agent(alpaca_client=alpaca)
    result = await agent.run({"errors": ["prior error"]})

    errors = result["errors"]
    assert not any(e == "prior error" for e in errors)  # prior not preserved
    assert any("broker offline" in e for e in errors)


@pytest.mark.asyncio
async def test_empty_positions() -> None:
    """No positions → portfolio.positions is an empty list."""
    alpaca = MagicMock()
    alpaca.get_account.return_value = _make_alpaca_account()
    alpaca.get_all_positions.return_value = []

    agent = _make_agent(alpaca_client=alpaca)
    result = await agent.run({})

    assert result["portfolio"].positions == []


@pytest.mark.asyncio
async def test_position_mapping() -> None:
    """All Position fields are correctly mapped from the Alpaca model."""
    pos = _make_alpaca_position(
        symbol="TSLA",
        qty="-5",  # short position
        avg_entry_price="250.00",
        current_price="240.00",
        unrealized_pl="50.00",
        market_value="-1200.00",
    )
    alpaca = MagicMock()
    alpaca.get_account.return_value = _make_alpaca_account()
    alpaca.get_all_positions.return_value = [pos]

    agent = _make_agent(alpaca_client=alpaca)
    result = await agent.run({})

    mapped = result["portfolio"].positions[0]
    assert mapped.ticker == "TSLA"
    assert mapped.qty == -5
    assert mapped.avg_entry_price == pytest.approx(250.0)
    assert mapped.current_price == pytest.approx(240.0)
    assert mapped.unrealized_pnl == pytest.approx(50.0)
    assert mapped.market_value == pytest.approx(-1200.0)


@pytest.mark.asyncio
async def test_position_count_via_property() -> None:
    """portfolio.position_count reflects the number of live positions."""
    alpaca = MagicMock()
    alpaca.get_account.return_value = _make_alpaca_account()
    alpaca.get_all_positions.return_value = [
        _make_alpaca_position(symbol="AAPL"),
        _make_alpaca_position(symbol="MSFT"),
    ]

    agent = _make_agent(alpaca_client=alpaca)
    result = await agent.run({})

    assert result["portfolio"].position_count == 2


@pytest.mark.asyncio
async def test_zero_last_equity_no_division_error() -> None:
    """If last_equity is zero, drawdown is 0 (no ZeroDivisionError)."""
    alpaca = MagicMock()
    alpaca.get_account.return_value = _make_alpaca_account(
        equity="100000.00", last_equity="0.00"
    )
    alpaca.get_all_positions.return_value = []

    agent = _make_agent(alpaca_client=alpaca)
    result = await agent.run({})

    assert result["portfolio"].max_drawdown_pct == 0.0

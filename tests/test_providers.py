"""Unit tests for provider Protocol implementations and factory functions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from news_trade.config import (
    MarketDataProviderType,
    NewsProviderType,
    SentimentProviderType,
    Settings,
)
from news_trade.models.events import EventType, NewsEvent
from news_trade.models.sentiment import SentimentLabel
from news_trade.providers import (
    get_market_data_provider,
    get_news_provider,
    get_sentiment_provider,
)
from news_trade.providers.base import (
    MarketDataProvider,
    NewsProvider,
    SentimentProvider,
)

NOW = datetime(2026, 3, 2, 12, 0, 0, tzinfo=UTC)


def _make_settings(**kwargs) -> Settings:
    defaults = dict(
        anthropic_api_key="test",
        alpaca_api_key="test",
        alpaca_secret_key="test",
    )
    return Settings(**(defaults | kwargs))


def _make_event(event_id: str = "ev-1", ticker: str = "AAPL") -> NewsEvent:
    return NewsEvent(
        event_id=event_id,
        headline="Test headline",
        source="test",
        tickers=[ticker],
        published_at=NOW,
    )


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class TestProtocolCompliance:
    def test_rss_is_news_provider(self):
        from news_trade.providers.news.rss import RSSNewsProvider
        p = RSSNewsProvider()
        assert isinstance(p, NewsProvider)

    def test_benzinga_is_news_provider(self):
        from news_trade.providers.news.benzinga import BenzingaNewsProvider
        p = BenzingaNewsProvider(api_key="key")
        assert isinstance(p, NewsProvider)

    def test_yfinance_is_market_provider(self):
        from news_trade.providers.market.yfinance import YFinanceMarketProvider
        p = YFinanceMarketProvider()
        assert isinstance(p, MarketDataProvider)

    def test_polygon_free_is_market_provider(self):
        from news_trade.providers.market.polygon_free import PolygonFreeMarketProvider
        p = PolygonFreeMarketProvider(api_key="key")
        assert isinstance(p, MarketDataProvider)

    def test_polygon_paid_is_market_provider(self):
        from news_trade.providers.market.polygon_paid import PolygonPaidMarketProvider
        p = PolygonPaidMarketProvider(api_key="key")
        assert isinstance(p, MarketDataProvider)

    def test_keyword_is_sentiment_provider(self):
        from news_trade.providers.sentiment.keyword import KeywordSentimentProvider
        p = KeywordSentimentProvider()
        assert isinstance(p, SentimentProvider)


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------


class TestGetNewsProvider:
    def test_rss_returns_rss_provider(self):
        s = _make_settings(news_provider=NewsProviderType.RSS)
        p = get_news_provider(s)
        assert p.name == "rss"

    def test_benzinga_returns_benzinga_provider(self):
        s = _make_settings(news_provider=NewsProviderType.BENZINGA)
        p = get_news_provider(s)
        assert p.name == "benzinga"


class TestGetMarketDataProvider:
    def test_yfinance_returns_yfinance_provider(self):
        s = _make_settings(market_data_provider=MarketDataProviderType.YFINANCE)
        p = get_market_data_provider(s)
        assert p.name == "yfinance"

    def test_polygon_free_returns_polygon_free_provider(self):
        s = _make_settings(market_data_provider=MarketDataProviderType.POLYGON_FREE)
        p = get_market_data_provider(s)
        assert p.name == "polygon_free"

    def test_polygon_paid_returns_polygon_paid_provider(self):
        s = _make_settings(market_data_provider=MarketDataProviderType.POLYGON_PAID)
        p = get_market_data_provider(s)
        assert p.name == "polygon_paid"


class TestGetSentimentProvider:
    def test_keyword_returns_keyword_provider(self):
        s = _make_settings(sentiment_provider=SentimentProviderType.KEYWORD)
        p = get_sentiment_provider(s)
        assert p.name == "keyword"

    def test_claude_returns_claude_provider(self):
        s = _make_settings(sentiment_provider=SentimentProviderType.CLAUDE)
        p = get_sentiment_provider(s)
        assert p.name == "claude"


# ---------------------------------------------------------------------------
# KeywordSentimentProvider logic
# ---------------------------------------------------------------------------


class TestKeywordSentimentProvider:
    @pytest.fixture()
    def provider(self):
        from news_trade.providers.sentiment.keyword import KeywordSentimentProvider
        return KeywordSentimentProvider()

    async def test_bullish_headline(self, provider):
        event = NewsEvent(
            event_id="ev-1",
            headline="Apple beats earnings and surges",
            source="test",
            tickers=["AAPL"],
            published_at=NOW,
        )
        result = await provider.analyse(event)
        assert result.score > 0
        assert result.label in (SentimentLabel.BULLISH, SentimentLabel.VERY_BULLISH)

    async def test_bearish_headline(self, provider):
        event = NewsEvent(
            event_id="ev-2",
            headline="Company files for bankruptcy amid fraud investigation",
            source="test",
            tickers=["XYZ"],
            published_at=NOW,
        )
        result = await provider.analyse(event)
        assert result.score < 0
        assert result.label in (SentimentLabel.BEARISH, SentimentLabel.VERY_BEARISH)

    async def test_neutral_headline(self, provider):
        event = NewsEvent(
            event_id="ev-3",
            headline="Company announces meeting date",
            source="test",
            tickers=["ABC"],
            published_at=NOW,
        )
        result = await provider.analyse(event)
        assert result.label == SentimentLabel.NEUTRAL

    async def test_batch_returns_one_per_event(self, provider):
        events = [_make_event(f"ev-{i}") for i in range(3)]
        results = await provider.analyse_batch(events)
        assert len(results) == 3

    async def test_confidence_is_fixed(self, provider):
        event = _make_event()
        result = await provider.analyse(event)
        assert result.confidence == 0.4


# ---------------------------------------------------------------------------
# Settings: provider enums and cost controls
# ---------------------------------------------------------------------------


class TestSettings:
    def test_default_news_provider_is_rss(self):
        s = _make_settings()
        assert s.news_provider == NewsProviderType.RSS

    def test_default_market_provider_is_yfinance(self):
        s = _make_settings()
        assert s.market_data_provider == MarketDataProviderType.YFINANCE

    def test_default_sentiment_provider_is_claude(self):
        s = _make_settings()
        assert s.sentiment_provider == SentimentProviderType.CLAUDE

    def test_default_daily_budget(self):
        s = _make_settings()
        assert s.claude_daily_budget_usd == 2.00

    def test_default_dry_run_false(self):
        s = _make_settings()
        assert s.sentiment_dry_run is False

    def test_default_keyword_prefilter_true(self):
        s = _make_settings()
        assert s.news_keyword_prefilter is True

    def test_enum_coercion_from_string(self):
        s = _make_settings(news_provider="benzinga")
        assert s.news_provider == NewsProviderType.BENZINGA


# ---------------------------------------------------------------------------
# ClaudeSentimentProvider — helper
# ---------------------------------------------------------------------------


def _make_provider(
    mock_llm_response: str = "[]",
    daily_budget: float = 10.0,
    quick_model_id: str = "claude-haiku-4-5-20251001",
    deep_model_id: str = "claude-sonnet-4-6",
):
    """Build a ClaudeSentimentProvider with separate quick/deep mock clients."""
    from news_trade.providers.sentiment.claude import ClaudeSentimentProvider
    from news_trade.services.llm_client import LLMResponse

    def _make_client(model_id: str) -> AsyncMock:
        response = LLMResponse(
            content=mock_llm_response,
            model_id=model_id,
            provider="anthropic",
            input_tokens=10,
            output_tokens=5,
        )
        client = AsyncMock()
        client.invoke = AsyncMock(return_value=response)
        client.model_id = model_id
        client.provider = "anthropic"
        return client

    mock_quick = _make_client(quick_model_id)
    mock_deep = _make_client(deep_model_id)

    mock_factory = MagicMock()
    mock_factory.quick = mock_quick
    mock_factory.deep = mock_deep

    provider = ClaudeSentimentProvider(llm=mock_factory, daily_budget=daily_budget)
    return provider, mock_quick, mock_deep


def _make_event_with_type(
    event_type: EventType, event_id: str = "ev-1", ticker: str = "AAPL"
) -> NewsEvent:
    return NewsEvent(
        event_id=event_id,
        headline="Test headline",
        summary="Test summary",
        source="test",
        tickers=[ticker],
        published_at=NOW,
        event_type=event_type,
    )


# ---------------------------------------------------------------------------
# ClaudeSentimentProvider — tier routing
# ---------------------------------------------------------------------------


class TestClaudeProviderTierRouting:
    """Verify correct LLM tier is selected per event type."""

    async def test_earn_pre_uses_deep(self):
        provider, mock_quick, mock_deep = _make_provider()
        event = _make_event_with_type(EventType.EARN_PRE)
        await provider.analyse(event)
        mock_deep.invoke.assert_called_once()
        mock_quick.invoke.assert_not_called()

    async def test_earn_beat_uses_deep(self):
        provider, mock_quick, mock_deep = _make_provider()
        event = _make_event_with_type(EventType.EARN_BEAT)
        await provider.analyse(event)
        mock_deep.invoke.assert_called_once()
        mock_quick.invoke.assert_not_called()

    async def test_earn_miss_uses_deep(self):
        provider, mock_quick, mock_deep = _make_provider()
        event = _make_event_with_type(EventType.EARN_MISS)
        await provider.analyse(event)
        mock_deep.invoke.assert_called_once()
        mock_quick.invoke.assert_not_called()

    async def test_ma_target_uses_quick(self):
        provider, mock_quick, mock_deep = _make_provider()
        event = _make_event_with_type(EventType.MA_TARGET)
        await provider.analyse(event)
        mock_quick.invoke.assert_called_once()
        mock_deep.invoke.assert_not_called()

    async def test_guidance_uses_quick(self):
        provider, mock_quick, mock_deep = _make_provider()
        event = _make_event_with_type(EventType.GUIDANCE)
        await provider.analyse(event)
        mock_quick.invoke.assert_called_once()
        mock_deep.invoke.assert_not_called()

    async def test_earn_mixed_uses_quick(self):
        provider, mock_quick, mock_deep = _make_provider()
        event = _make_event_with_type(EventType.EARN_MIXED)
        await provider.analyse(event)
        mock_quick.invoke.assert_called_once()
        mock_deep.invoke.assert_not_called()

    async def test_other_uses_quick(self):
        provider, mock_quick, mock_deep = _make_provider()
        event = _make_event_with_type(EventType.OTHER)
        await provider.analyse(event)
        mock_quick.invoke.assert_called_once()
        mock_deep.invoke.assert_not_called()


# ---------------------------------------------------------------------------
# ClaudeSentimentProvider — EARN_PRE system prompt
# ---------------------------------------------------------------------------


class TestEarnPrePrompt:
    """Verify EARN_PRE events use the specialised system prompt."""

    async def test_earn_pre_receives_earn_pre_system_prompt(self):
        from news_trade.providers.sentiment.claude import _EARN_PRE_SYSTEM_PROMPT

        provider, _mock_quick, mock_deep = _make_provider()
        event = _make_event_with_type(EventType.EARN_PRE)
        await provider.analyse(event)
        _, call_kwargs = mock_deep.invoke.call_args
        assert call_kwargs["system"] == _EARN_PRE_SYSTEM_PROMPT

    async def test_non_earn_pre_receives_standard_system_prompt(self):
        from news_trade.providers.sentiment.claude import _SYSTEM_PROMPT

        provider, mock_quick, _mock_deep = _make_provider()
        event = _make_event_with_type(EventType.MA_TARGET)
        await provider.analyse(event)
        _, call_kwargs = mock_quick.invoke.call_args
        assert call_kwargs["system"] == _SYSTEM_PROMPT

    async def test_earn_beat_receives_standard_system_prompt(self):
        from news_trade.providers.sentiment.claude import _SYSTEM_PROMPT

        provider, _mock_quick, mock_deep = _make_provider()
        event = _make_event_with_type(EventType.EARN_BEAT)
        await provider.analyse(event)
        _, call_kwargs = mock_deep.invoke.call_args
        assert call_kwargs["system"] == _SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# ClaudeSentimentProvider — model_id provenance
# ---------------------------------------------------------------------------


class TestClaudeProviderModelIdProvenance:
    """Verify SentimentResult.model_id reflects the actual model used."""

    async def test_model_id_reflects_quick_model_for_non_earn(self):
        valid_response = (
            '[{"ticker":"AAPL","label":"BULLISH","score":0.5,'
            '"confidence":0.8,"reasoning":"test"}]'
        )
        provider, _mock_quick, _mock_deep = _make_provider(
            mock_llm_response=valid_response,
            quick_model_id="claude-haiku-4-5-20251001",
            deep_model_id="claude-sonnet-4-6",
        )
        event = _make_event_with_type(EventType.MA_TARGET)
        result = await provider.analyse(event)
        assert result.model_id == "claude-haiku-4-5-20251001"

    async def test_model_id_reflects_deep_model_for_earn_pre(self):
        valid_response = (
            '[{"ticker":"AAPL","label":"BULLISH","score":0.5,'
            '"confidence":0.8,"reasoning":"test"}]'
        )
        provider, _mock_quick, _mock_deep = _make_provider(
            mock_llm_response=valid_response,
            quick_model_id="claude-haiku-4-5-20251001",
            deep_model_id="claude-sonnet-4-6",
        )
        event = _make_event_with_type(EventType.EARN_PRE)
        result = await provider.analyse(event)
        assert result.model_id == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# ClaudeSentimentProvider — Phase 2: estimates injection into EARN_PRE prompt
# ---------------------------------------------------------------------------


def _make_estimates(ticker: str = "AAPL"):
    from datetime import date

    from news_trade.models.surprise import EstimatesData

    return EstimatesData(
        ticker=ticker,
        fiscal_period="Q1 2026",
        report_date=date(2026, 4, 25),
        eps_estimate=2.50,
        eps_low=2.50,
        eps_high=2.50,
        revenue_estimate=0.0,
        revenue_low=0.0,
        revenue_high=0.0,
        num_analysts=0,
    )


class TestEstimatesInjectionPhase2:
    """Verify EstimatesRenderer output is injected into EARN_PRE prompts."""

    async def test_earn_pre_with_estimates_appends_renderer_block(self):
        """EARN_PRE + matching ticker → user message contains estimates block."""
        provider, _mock_quick, mock_deep = _make_provider()
        event = _make_event_with_type(EventType.EARN_PRE, ticker="AAPL")
        estimates = {"AAPL": _make_estimates("AAPL")}
        await provider.analyse_batch([event], estimates=estimates)
        call_args, _ = mock_deep.invoke.call_args
        user_message = call_args[0]
        assert "EARNINGS ESTIMATES" in user_message
        assert "2.50" in user_message  # eps_estimate rendered

    async def test_earn_pre_without_estimates_sends_headline_only(self):
        """EARN_PRE without estimates dict → headline-only (no regression)."""
        provider, _mock_quick, mock_deep = _make_provider()
        event = _make_event_with_type(EventType.EARN_PRE, ticker="AAPL")
        await provider.analyse_batch([event], estimates=None)
        call_args, _ = mock_deep.invoke.call_args
        user_message = call_args[0]
        assert "EARNINGS ESTIMATES" not in user_message

    async def test_earn_pre_ticker_not_in_estimates_sends_headline_only(self):
        """EARN_PRE with estimates dict that lacks the ticker → no injection."""
        provider, _mock_quick, mock_deep = _make_provider()
        event = _make_event_with_type(EventType.EARN_PRE, ticker="AAPL")
        estimates = {"MSFT": _make_estimates("MSFT")}  # different ticker
        await provider.analyse_batch([event], estimates=estimates)
        call_args, _ = mock_deep.invoke.call_args
        user_message = call_args[0]
        assert "EARNINGS ESTIMATES" not in user_message

    async def test_non_earn_pre_ignores_estimates(self):
        """Non-EARN_PRE event with estimates provided → no estimates block."""
        provider, mock_quick, _mock_deep = _make_provider()
        event = _make_event_with_type(EventType.MA_TARGET, ticker="AAPL")
        estimates = {"AAPL": _make_estimates("AAPL")}
        await provider.analyse_batch([event], estimates=estimates)
        call_args, _ = mock_quick.invoke.call_args
        user_message = call_args[0]
        assert "EARNINGS ESTIMATES" not in user_message

    async def test_earn_beat_ignores_estimates(self):
        """EARN_BEAT (post-announcement) + estimates → no estimates block injected."""
        provider, _mock_quick, mock_deep = _make_provider()
        event = _make_event_with_type(EventType.EARN_BEAT, ticker="AAPL")
        estimates = {"AAPL": _make_estimates("AAPL")}
        await provider.analyse_batch([event], estimates=estimates)
        call_args, _ = mock_deep.invoke.call_args
        user_message = call_args[0]
        assert "EARNINGS ESTIMATES" not in user_message


# ---------------------------------------------------------------------------
# ClaudeSentimentProvider — concurrent analyse_batch
# ---------------------------------------------------------------------------


class TestConcurrentAnalyseBatch:
    """Verify that analyse_batch dispatches LLM calls concurrently."""

    def _make_neutral_response(self, ticker: str = "AAPL") -> str:
        return (
            f'[{{"ticker":"{ticker}","label":"NEUTRAL","score":0.0,'
            f'"confidence":0.5,"reasoning":"test"}}]'
        )

    async def test_all_events_run_concurrently(self) -> None:
        """asyncio.gather dispatches all _call_claude coroutines before any return."""
        provider, _, _ = _make_provider(daily_budget=100.0)

        events = [_make_event(f"e{i}", ticker=f"T{i}") for i in range(3)]
        call_order: list[str] = []

        async def fake_call(event, estimates):
            call_order.append(f"start:{event.event_id}")
            await asyncio.sleep(0)  # yield so others can start
            call_order.append(f"end:{event.event_id}")
            from news_trade.models.sentiment import SentimentLabel, SentimentResult
            return [SentimentResult(
                event_id=event.event_id,
                ticker=event.tickers[0],
                label=SentimentLabel.NEUTRAL,
                score=0.0,
                confidence=0.5,
                reasoning="test",
                model_id="mock",
                provider="mock",
            )]

        with patch.object(provider, "_call_claude", side_effect=fake_call):
            results = await provider.analyse_batch(events)

        assert len(results) == 3
        starts = [x for x in call_order if x.startswith("start:")]
        ends = [x for x in call_order if x.startswith("end:")]
        assert len(starts) == 3
        assert len(ends) == 3
        # Concurrent: all starts appear before the first end (after each yields)
        assert call_order.index("start:e2") < call_order.index("end:e0")

    async def test_budget_exhausted_before_batch_returns_neutral(self) -> None:
        """Events queued when budget already exhausted get neutral results."""
        provider, _, _ = _make_provider(daily_budget=0.00)
        # Force budget to be exhausted
        provider._spent_today = 1.00
        events = [_make_event("ex", ticker="AAPL")]

        results = await provider.analyse_batch(events)

        assert len(results) == 1
        from news_trade.models.sentiment import SentimentLabel
        assert results[0].label == SentimentLabel.NEUTRAL

    async def test_failed_call_returns_neutral_not_raises(self) -> None:
        """A RuntimeError from _call_claude produces a neutral result."""
        provider, _, _ = _make_provider(daily_budget=100.0)
        events = [_make_event("bad", ticker="AAPL")]

        async def boom(event, estimates):
            raise RuntimeError("LLM unavailable")

        with patch.object(provider, "_call_claude", side_effect=boom):
            results = await provider.analyse_batch(events)

        assert len(results) == 1
        from news_trade.models.sentiment import SentimentLabel
        assert results[0].label == SentimentLabel.NEUTRAL

    async def test_partial_failure_does_not_affect_other_results(self) -> None:
        """One failing call returns neutral; other calls' results are still returned."""
        from news_trade.models.sentiment import SentimentLabel, SentimentResult

        provider, _, _ = _make_provider(daily_budget=100.0)
        events = [_make_event("ok", ticker="AAPL"), _make_event("bad", ticker="MSFT")]
        call_count = 0

        async def mixed_call(event, estimates):
            nonlocal call_count
            call_count += 1
            if event.event_id == "bad":
                raise RuntimeError("fail")
            return [SentimentResult(
                event_id=event.event_id,
                ticker=event.tickers[0],
                label=SentimentLabel.BULLISH,
                score=0.7,
                confidence=0.8,
                reasoning="test",
                model_id="mock",
                provider="mock",
            )]

        with patch.object(provider, "_call_claude", side_effect=mixed_call):
            results = await provider.analyse_batch(events)

        assert call_count == 2
        assert len(results) == 2
        labels = {r.ticker: r.label for r in results}
        assert labels["AAPL"] == SentimentLabel.BULLISH
        assert labels["MSFT"] == SentimentLabel.NEUTRAL

    async def test_empty_event_list_returns_empty(self) -> None:
        provider, _, _ = _make_provider()
        results = await provider.analyse_batch([])
        assert results == []

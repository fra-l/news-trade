"""ClaudeSentimentProvider — sentiment analysis via the Anthropic Claude API.

Phase 1 primary sentiment provider.  Requires an Anthropic API key.
Tracks token usage against a daily budget cap; falls back gracefully when
the budget is exhausted (returns NEUTRAL with score=0 and confidence=0).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime

from news_trade.models.events import NewsEvent
from news_trade.models.sentiment import SentimentLabel, SentimentResult
from news_trade.services.llm_client import LLMClient, LLMClientFactory

_logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a financial news sentiment analyst.  Analyse the provided news event
and return a JSON object for EACH ticker mentioned in the event.

Return a JSON array where each element has these exact keys:
  ticker        (string)  — the stock symbol
  label         (string)  — one of: VERY_BULLISH, BULLISH, NEUTRAL, BEARISH, VERY_BEARISH
  score         (float)   — sentiment score from -1.0 (very bearish) to +1.0 (very bullish)
  confidence    (float)   — confidence from 0.0 to 1.0
  reasoning     (string)  — one-sentence explanation

Return ONLY the JSON array with no surrounding text or markdown fences.
"""

_EARN_PRE_SYSTEM_PROMPT = """\
You are a financial news sentiment analyst specialising in pre-earnings analysis.
The event you are analysing is a PRE-ANNOUNCEMENT — a report date is upcoming but
earnings have not yet been released.

Assess sentiment based on:
- Analyst estimate revisions and consensus trend in the headline/summary
- Any forward guidance signals
- Historical beat/miss reputation implied by the text
- Market positioning language (e.g. "raised guidance", "cautious outlook")

Return a JSON array where each element has these exact keys:
  ticker        (string)  — the stock symbol
  label         (string)  — one of: VERY_BULLISH, BULLISH, NEUTRAL, BEARISH, VERY_BEARISH
  score         (float)   — sentiment score from -1.0 (very bearish) to +1.0 (very bullish)
  confidence    (float)   — confidence from 0.0 to 1.0
  reasoning     (string)  — one-sentence explanation citing the specific signal

Return ONLY the JSON array with no surrounding text or markdown fences.
"""

_EARN_DEEP_TYPES = frozenset({
    "earn_pre", "earn_beat", "earn_miss",
    # coarse fallback
    "earnings",
})


class ClaudeSentimentProvider:
    """Calls the Anthropic Claude API to score news sentiment.

    Tracks cumulative token usage per calendar day and short-circuits to a
    zero-cost neutral result once the daily budget (in USD) is reached.
    """

    # Rough cost estimate: claude-sonnet input ~$3/1M tokens, output ~$15/1M
    _INPUT_COST_PER_TOKEN = 3.0 / 1_000_000
    _OUTPUT_COST_PER_TOKEN = 15.0 / 1_000_000

    def __init__(
        self,
        llm: LLMClientFactory,
        daily_budget: float = 2.00,
    ) -> None:
        self._factory = llm
        # keep self._llm pointing to deep for budget tracking (conservative rates)
        self._llm = llm.deep
        self._daily_budget = daily_budget
        self._budget_date: date | None = None
        self._spent_today: float = 0.0

    @property
    def name(self) -> str:
        return "claude"

    def _reset_budget_if_new_day(self) -> None:
        today = datetime.now(UTC).date()
        if self._budget_date != today:
            self._budget_date = today
            self._spent_today = 0.0

    def _budget_exhausted(self) -> bool:
        self._reset_budget_if_new_day()
        return self._spent_today >= self._daily_budget

    def _record_usage(self, input_tokens: int, output_tokens: int) -> None:
        cost = (
            input_tokens * self._INPUT_COST_PER_TOKEN
            + output_tokens * self._OUTPUT_COST_PER_TOKEN
        )
        self._spent_today += cost

    def _select_client(self, event: NewsEvent) -> LLMClient:
        """Return deep for high-stakes earnings events, quick for everything else."""
        event_type_str = str(event.event_type).lower()
        if event_type_str in _EARN_DEEP_TYPES:
            return self._factory.deep
        return self._factory.quick

    async def analyse(self, event: NewsEvent) -> SentimentResult:
        """Score a single news event; returns the first ticker's result."""
        results = await self.analyse_batch([event])
        if results:
            return results[0]
        return _neutral_result(event, self._llm.model_id, self._llm.provider)

    async def analyse_batch(self, events: list[NewsEvent]) -> list[SentimentResult]:
        """Score multiple events, respecting the daily budget cap."""
        self._reset_budget_if_new_day()
        all_results: list[SentimentResult] = []
        for event in events:
            if self._budget_exhausted():
                _logger.warning(
                    "Daily Claude budget $%.2f exhausted — returning neutral for %s",
                    self._daily_budget,
                    event.event_id,
                )
                all_results.append(
                    _neutral_result(event, self._llm.model_id, self._llm.provider)
                )
                continue
            results = await self._call_claude(event)
            all_results.extend(results)
        return all_results

    async def _call_claude(self, event: NewsEvent) -> list[SentimentResult]:
        client = self._select_client(event)

        # Select system prompt based on event type
        event_type_str = str(event.event_type).lower()
        system_prompt = (
            _EARN_PRE_SYSTEM_PROMPT
            if event_type_str == "earn_pre"
            else _SYSTEM_PROMPT
        )

        tickers_str = ", ".join(event.tickers) if event.tickers else "unspecified"
        user_message = (
            f"Headline: {event.headline}\n"
            f"Summary: {event.summary}\n"
            f"Tickers: {tickers_str}\n"
            f"Event type: {event.event_type}"
        )

        try:
            response = await client.invoke(user_message, system=system_prompt)
        except Exception as exc:
            _logger.error("Claude API error for event %s: %s", event.event_id, exc)
            return [_neutral_result(event, self._llm.model_id, self._llm.provider)]

        self._record_usage(response.input_tokens, response.output_tokens)
        return _parse_response(
            response.content, event, response.model_id, response.provider
        )


def _parse_response(
    raw: str,
    event: NewsEvent,
    model_id: str,
    provider: str,
) -> list[SentimentResult]:
    """Parse the JSON array returned by Claude into SentimentResult objects."""
    try:
        items = json.loads(raw)
        if not isinstance(items, list):
            items = [items]
    except json.JSONDecodeError:
        _logger.warning("Claude returned invalid JSON for event %s", event.event_id)
        return [_neutral_result(event, model_id, provider)]

    results: list[SentimentResult] = []
    for item in items:
        try:
            results.append(
                SentimentResult(
                    event_id=event.event_id,
                    ticker=item["ticker"],
                    label=SentimentLabel(item["label"]),
                    score=float(item["score"]),
                    confidence=float(item["confidence"]),
                    reasoning=item.get("reasoning", ""),
                    model_id=model_id,
                    provider=provider,
                )
            )
        except (KeyError, ValueError) as exc:
            _logger.warning("Skipping malformed Claude sentiment item: %s", exc)

    return results or [_neutral_result(event, model_id, provider)]


def _neutral_result(event: NewsEvent, model_id: str, provider: str) -> SentimentResult:
    ticker = event.tickers[0] if event.tickers else "UNKNOWN"
    return SentimentResult(
        event_id=event.event_id,
        ticker=ticker,
        label=SentimentLabel.NEUTRAL,
        score=0.0,
        confidence=0.0,
        reasoning="Budget exhausted or API error — defaulting to neutral.",
        model_id=model_id,
        provider=provider,
    )

"""KeywordSentimentProvider — rule-based sentiment fallback.

Phase 1 free sentiment source.  No API key or network call required.
Uses a weighted keyword dictionary to assign a coarse sentiment score.
Useful as a pre-filter before Claude or when the daily budget is exhausted.
"""

from __future__ import annotations

from news_trade.models.events import NewsEvent
from news_trade.models.sentiment import SentimentLabel, SentimentResult

# (keyword, weight) pairs — weight > 0 is bullish, < 0 is bearish
_KEYWORD_WEIGHTS: list[tuple[str, float]] = [
    # Bullish signals
    ("beats", 0.6),
    ("beat", 0.5),
    ("surges", 0.6),
    ("rally", 0.5),
    ("upgrade", 0.6),
    ("overweight", 0.5),
    ("buy", 0.4),
    ("raises guidance", 0.7),
    ("record revenue", 0.7),
    ("approval", 0.6),
    ("fda approves", 0.8),
    ("merger", 0.3),
    ("acquisition", 0.3),
    ("dividend", 0.3),
    ("buyback", 0.4),
    # Bearish signals
    ("misses", -0.6),
    ("miss", -0.5),
    ("falls", -0.5),
    ("drops", -0.5),
    ("plunges", -0.7),
    ("downgrade", -0.6),
    ("underweight", -0.5),
    ("sell", -0.4),
    ("lowers guidance", -0.7),
    ("recall", -0.6),
    ("lawsuit", -0.5),
    ("investigation", -0.5),
    ("fine", -0.4),
    ("layoffs", -0.4),
    ("bankruptcy", -0.9),
    ("fraud", -0.8),
]


def _score_headline(text: str) -> float:
    lower = text.lower()
    total = 0.0
    hits = 0
    for keyword, weight in _KEYWORD_WEIGHTS:
        if keyword in lower:
            total += weight
            hits += 1
    if hits == 0:
        return 0.0
    # Average and clamp to [-1, 1]
    avg = total / hits
    return max(-1.0, min(1.0, avg))


def _label_from_score(score: float) -> SentimentLabel:
    if score >= 0.5:
        return SentimentLabel.VERY_BULLISH
    if score >= 0.15:
        return SentimentLabel.BULLISH
    if score <= -0.5:
        return SentimentLabel.VERY_BEARISH
    if score <= -0.15:
        return SentimentLabel.BEARISH
    return SentimentLabel.NEUTRAL


class KeywordSentimentProvider:
    """Assigns sentiment scores using a keyword lookup table.

    Confidence is fixed at 0.4 to signal lower reliability vs. Claude.
    """

    @property
    def name(self) -> str:
        return "keyword"

    async def analyse(self, event: NewsEvent) -> SentimentResult:
        return (await self.analyse_batch([event]))[0]

    async def analyse_batch(self, events: list[NewsEvent]) -> list[SentimentResult]:
        results: list[SentimentResult] = []
        for event in events:
            text = event.headline + " " + event.summary
            score = _score_headline(text)
            label = _label_from_score(score)
            ticker = event.tickers[0] if event.tickers else "UNKNOWN"
            results.append(
                SentimentResult(
                    event_id=event.event_id,
                    ticker=ticker,
                    label=label,
                    score=score,
                    confidence=0.4,
                    reasoning="Keyword-based heuristic score.",
                )
            )
        return results

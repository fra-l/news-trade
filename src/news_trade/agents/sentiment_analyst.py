"""SentimentAnalystAgent — classifies news sentiment using Claude."""

from __future__ import annotations

from news_trade.agents.base import BaseAgent
from news_trade.models import NewsEvent, SentimentResult


class SentimentAnalystAgent(BaseAgent):
    """Uses the Anthropic Claude API to analyse sentiment of news events.

    Responsibilities:
        - Format each NewsEvent into a prompt for Claude.
        - Call the Claude Messages API (claude-sonnet-4-6).
        - Parse the structured response into a SentimentResult.
    """

    async def run(self, state: dict) -> dict:
        """Analyse sentiment for all news events in state.

        Returns:
            ``{"sentiment_results": [SentimentResult, ...]}``
        """
        raise NotImplementedError

    async def _analyse_event(self, event: NewsEvent) -> list[SentimentResult]:
        """Call Claude to classify sentiment for a single news event.

        Returns one SentimentResult per ticker mentioned in the event.
        """
        raise NotImplementedError

    def _build_prompt(self, event: NewsEvent) -> str:
        """Construct the sentiment-analysis prompt for Claude.

        The prompt instructs the model to return structured JSON matching
        the SentimentResult schema.
        """
        raise NotImplementedError

    def _parse_response(
        self, raw: str, event: NewsEvent
    ) -> list[SentimentResult]:
        """Parse Claude's response text into validated SentimentResult models."""
        raise NotImplementedError

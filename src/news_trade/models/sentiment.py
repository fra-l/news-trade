"""Sentiment analysis result models."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class SentimentLabel(StrEnum):
    """Discrete sentiment classification."""

    VERY_BULLISH = "very_bullish"
    BULLISH = "bullish"
    NEUTRAL = "neutral"
    BEARISH = "bearish"
    VERY_BEARISH = "very_bearish"


class SentimentResult(BaseModel):
    """Output of the SentimentAnalystAgent for a single news event.

    Wraps the Claude API classification along with confidence and reasoning,
    providing a structured input for the SignalGeneratorAgent.
    """

    event_id: str = Field(description="References the originating NewsEvent")
    ticker: str = Field(description="Ticker this sentiment applies to")
    label: SentimentLabel
    score: float = Field(
        ge=-1.0,
        le=1.0,
        description="Continuous sentiment score from -1 (bearish) to +1 (bullish)",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Model confidence in the classification",
    )
    reasoning: str = Field(
        default="", description="Brief explanation from the LLM"
    )
    model_id: str = Field(
        default="claude-sonnet-4-6",
        description="Claude model used for analysis",
    )
    analyzed_at: datetime = Field(default_factory=datetime.utcnow)

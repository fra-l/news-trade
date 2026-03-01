"""News event models."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class EventType(StrEnum):
    """Classification of the news event."""

    EARNINGS = "earnings"
    FDA_APPROVAL = "fda_approval"
    MERGER_ACQUISITION = "merger_acquisition"
    MACRO = "macro"
    GUIDANCE = "guidance"
    ANALYST_RATING = "analyst_rating"
    SEC_FILING = "sec_filing"
    OTHER = "other"


class NewsEvent(BaseModel):
    """A single news event ingested from an external provider.

    This is the primary input to the analysis pipeline. Every downstream
    agent receives or derives its data from one or more NewsEvent instances.
    """

    event_id: str = Field(description="Unique identifier from the news provider")
    headline: str = Field(description="News headline text")
    summary: str = Field(default="", description="Article summary or first paragraph")
    source: str = Field(description="News provider name (e.g. 'benzinga', 'polygon')")
    url: str = Field(default="", description="Link to the full article")
    tickers: list[str] = Field(
        default_factory=list,
        description="Stock tickers mentioned in the article",
    )
    event_type: EventType = Field(default=EventType.OTHER)
    published_at: datetime = Field(description="Original publication timestamp")
    ingested_at: datetime = Field(default_factory=datetime.utcnow)

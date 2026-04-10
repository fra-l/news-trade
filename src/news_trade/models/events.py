"""News event models."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class EventType(StrEnum):
    """Classification of the news event.

    Coarse values (EARNINGS, GUIDANCE, etc.) are kept for backward compatibility
    and as fallbacks. Fine-grained values (EARN_PRE, EARN_BEAT, etc.) are used
    by SignalGeneratorAgent and ConfidenceScorer for per-event-type logic.
    """

    # --- Coarse legacy values (backward compat) ---
    EARNINGS = "earnings"
    FDA_APPROVAL = "fda_approval"
    MERGER_ACQUISITION = "merger_acquisition"
    MACRO = "macro"
    GUIDANCE = "guidance"
    ANALYST_RATING = "analyst_rating"
    SEC_FILING = "sec_filing"
    OTHER = "other"

    # --- Tier 1: Earnings & Guidance (fine-grained) ---
    EARN_PRE = "earn_pre"           # Pre-earnings positioning (2-5 days before report)
    EARN_BEAT = "earn_beat"         # EPS/rev above consensus + positive guidance
    EARN_MISS = "earn_miss"         # EPS/rev below consensus or guidance cut
    EARN_MIXED = "earn_mixed"       # Beat one metric, miss other, or flat guidance
    GUID_UP = "guid_up"             # Forward guidance raised above consensus
    GUID_DOWN = "guid_down"         # Forward guidance cut
    GUID_WARN = "guid_warn"         # Off-cycle negative pre-announcement

    # --- Tier 2: M&A ---
    MA_TARGET = "ma_target"         # Company confirmed as acquisition target
    MA_ACQUIRER = "ma_acquirer"     # Company announces acquisition
    MA_RUMOUR = "ma_rumour"         # Unconfirmed reports of deal talks
    MA_BREAK = "ma_break"           # Confirmed deal falls apart
    MA_COUNTER = "ma_counter"       # Second bidder / bidding war

    # --- Tier 3: Regulatory & Legal (non-FDA) ---
    REG_BLOCK = "reg_block"         # Antitrust/FTC blocks a deal
    REG_CLEAR = "reg_clear"         # Regulatory approval granted
    REG_ACTION = "reg_action"       # SEC/DOJ formal investigation
    REG_FINE = "reg_fine"           # Regulatory fine announced
    REG_LICENSE = "reg_license"     # Operating license granted or revoked

    # --- Tier 4: Sector Contagion ---
    SECTOR_BEAT_SPILL = "sector_beat_spill" # Major peer beats; others not yet reported
    SECTOR_MISS_SPILL = "sector_miss_spill"   # Major peer misses
    SUPPLY_CHAIN = "supply_chain"   # Supplier/customer earnings imply demand shift


class NewsEvent(BaseModel):
    """A single news event ingested from an external provider.

    This is the primary input to the analysis pipeline. Every downstream
    agent receives or derives its data from one or more NewsEvent instances.
    """

    event_id: str = Field(description="Unique identifier from the news provider")
    headline: str = Field(description="News headline text")
    summary: str = Field(default="", description="Article summary or first paragraph")
    source: str = Field(description="News provider name (e.g. 'benzinga', 'massive')")
    url: str = Field(default="", description="Link to the full article")
    tickers: list[str] = Field(
        default_factory=list,
        description="Stock tickers mentioned in the article",
    )
    event_type: EventType = Field(default=EventType.OTHER)
    published_at: datetime = Field(description="Original publication timestamp")
    ingested_at: datetime = Field(default_factory=datetime.utcnow)

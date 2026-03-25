"""EstimatesRenderer — formats EstimatesData into a structured narrative.

Pure Python, no LLM calls, no I/O.  The rendered narrative is consumed by
``ConfidenceScorer`` and future LLM prompts (e.g. Pattern A debate context)
instead of raw JSON, reducing token ambiguity and improving scoring consistency.
"""

from __future__ import annotations

from news_trade.models.surprise import EstimatesData


class EstimatesRenderer:
    """Converts raw ``EstimatesData`` into a structured narrative block.

    All methods are deterministic: the same input always produces the same output.
    Instantiate cheaply — the class is stateless and holds no constructor parameters.
    """

    def render(self, ticker: str, data: EstimatesData) -> str:
        """Format EstimatesData as a human-readable report block.

        Produces the canonical narrative format expected by downstream LLM prompts.
        Optional fields (``eps_trailing_mean``, ``historical_beat_rate``,
        ``mean_eps_surprise``) render as "N/A" when absent.

        Args:
            ticker: Stock ticker symbol (e.g. "AAPL").
            data:   Pre-announcement consensus estimates.

        Returns:
            A multi-line string with labeled sections and aligned columns.
        """
        trailing_mean = (
            f"${data.eps_trailing_mean:.2f}"
            if data.eps_trailing_mean is not None
            else "N/A (insufficient history)"
        )
        beat_rate = (
            f"{data.historical_beat_rate:.0%}"
            if data.historical_beat_rate is not None
            else "N/A"
        )
        mean_surprise = (
            f"{data.mean_eps_surprise:+.1%}"
            if data.mean_eps_surprise is not None
            else "N/A"
        )

        return (
            f"=== EARNINGS ESTIMATES: {ticker} ===\n"
            f"\n"
            f"Report date:         {data.report_date}\n"
            f"Fiscal period:       {data.fiscal_period}\n"
            f"\n"
            f"EPS consensus:       ${data.eps_estimate:.2f}\n"
            f"EPS analyst range:   ${data.eps_low:.2f} \u2014 ${data.eps_high:.2f}\n"
            f"Prior 4Q EPS mean:   {trailing_mean}\n"
            f"\n"
            f"Revenue consensus:   ${data.revenue_estimate / 1e6:.0f}M\n"
            f"Revenue range:       ${data.revenue_low / 1e6:.0f}M"
            f" \u2014 ${data.revenue_high / 1e6:.0f}M\n"
            f"\n"
            f"Historical beat rate (last 8Q): {beat_rate}\n"
            f"Mean EPS surprise (last 8Q):    {mean_surprise}\n"
            f"\n"
            f"Analyst coverage:    {data.num_analysts} analysts\n"
            f"Estimate dispersion: {data.estimate_dispersion:.3f}"
            f"  (lower = higher consensus)\n"
        )

    def compute_pre_surprise_delta(self, data: EstimatesData) -> float:
        """Compute a normalised pre-announcement surprise delta.

        Range: [-1.0, 1.0]. Positive = current estimate above trailing mean
        (bullish momentum); negative = below (bearish revision trend).

        Primary formula (when ``eps_trailing_mean`` is available and non-zero):
            clamp((eps_estimate - eps_trailing_mean) / |eps_trailing_mean|, -1, 1)

        Fallback (when ``eps_trailing_mean`` is None or zero):
            clamp(mean_eps_surprise, -1, 1)
            ``mean_eps_surprise`` is a fraction (0.05 = 5% historical beat).

        Returns 0.0 when neither historical data point is available.

        Args:
            data: Pre-announcement consensus estimates.

        Returns:
            Float in [-1.0, 1.0].
        """
        if data.eps_trailing_mean is not None and data.eps_trailing_mean != 0.0:
            raw = (data.eps_estimate - data.eps_trailing_mean) / abs(
                data.eps_trailing_mean
            )
        elif data.mean_eps_surprise is not None:
            raw = data.mean_eps_surprise
        else:
            return 0.0
        return max(-1.0, min(1.0, raw))

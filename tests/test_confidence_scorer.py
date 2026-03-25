"""Unit tests for ConfidenceScorer."""

from __future__ import annotations

from datetime import date

from news_trade.config import Settings
from news_trade.models.events import EventType
from news_trade.models.sentiment import SentimentLabel, SentimentResult
from news_trade.models.signals import SignalDirection, TradeSignal
from news_trade.models.surprise import (
    EarningsSurprise,
    EstimatesData,
    MetricSurprise,
)
from news_trade.services.confidence_scorer import ConfidenceScorer
from news_trade.services.estimates_renderer import EstimatesRenderer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**kwargs) -> Settings:
    defaults: dict = dict(earn_min_analyst_count=3)
    return Settings(**(defaults | kwargs))  # type: ignore[call-arg]


def _make_scorer(**kwargs) -> ConfidenceScorer:
    return ConfidenceScorer(settings=_make_settings(), **kwargs)


def _make_sentiment(score: float = 0.8, confidence: float = 0.9) -> SentimentResult:
    return SentimentResult(
        event_id="evt-1",
        ticker="AAPL",
        label=SentimentLabel.BULLISH,
        score=score,
        confidence=confidence,
    )


def _make_estimates(**kwargs) -> EstimatesData:
    defaults = dict(
        ticker="AAPL",
        fiscal_period="Q1 2026",
        report_date=date(2026, 1, 31),
        eps_estimate=2.00,
        eps_low=1.80,
        eps_high=2.20,
        eps_trailing_mean=1.90,
        revenue_estimate=1_500_000_000.0,
        revenue_low=1_400_000_000.0,
        revenue_high=1_600_000_000.0,
        num_analysts=10,
    )
    return EstimatesData(**(defaults | kwargs))


def _make_metric(actual: float = 2.10, consensus: float = 2.00) -> MetricSurprise:
    return MetricSurprise(
        actual=actual,
        consensus=consensus,
        estimate_high=2.20,
        estimate_low=1.80,
        analyst_count=10,
    )


def _make_earnings_surprise(
    eps_actual: float = 2.10,
    rev_actual: float = 1.05e9,
) -> EarningsSurprise:
    return EarningsSurprise(
        ticker="AAPL",
        report_date=date(2026, 1, 31),
        fiscal_quarter="Q1 2026",
        eps=_make_metric(actual=eps_actual, consensus=2.00),
        revenue=_make_metric(actual=rev_actual, consensus=1.00e9),
    )


def _make_signal(**kwargs) -> TradeSignal:
    defaults = dict(
        signal_id="sig-1",
        event_id="evt-1",
        ticker="AAPL",
        direction=SignalDirection.LONG,
        conviction=0.70,
        suggested_qty=10,
    )
    return TradeSignal(**(defaults | kwargs))


# ---------------------------------------------------------------------------
# TestScoreSurprise
# ---------------------------------------------------------------------------


class TestScoreSurprise:
    def setup_method(self):
        self.scorer = _make_scorer()

    def test_no_data_returns_zero(self):
        assert self.scorer._score_surprise(None, None) == 0.0

    def test_post_announcement_uses_max_sigma(self):
        # eps sigma = 2.0, rev sigma = 1.0 → max = 2.0 → 2.0/3.0
        es = EarningsSurprise(
            ticker="AAPL",
            report_date=date(2026, 1, 31),
            fiscal_quarter="Q1 2026",
            eps=MetricSurprise(
                actual=2.0 + 2.0 * 0.1,  # sigma=2.0 with std=0.1
                consensus=2.0,
                estimate_high=2.2,
                estimate_low=1.8,  # std = (2.2-1.8)/4 = 0.1
                analyst_count=5,
            ),
            revenue=MetricSurprise(
                actual=1.0e9 + 1.0 * 0.025e9,  # sigma=1.0 with std=0.025e9
                consensus=1.0e9,
                estimate_high=1.1e9,
                estimate_low=0.9e9,  # std = 0.2e9/4 = 0.05e9
                analyst_count=5,
            ),
        )
        # eps.sigma_surprise = (2.2 - 2.0) / 0.1 = 2.0
        # rev.sigma_surprise = (1.025e9 - 1.0e9) / 0.05e9 = 0.5
        score = self.scorer._score_surprise(None, es)
        expected = min(2.0 / 3.0, 1.0)
        assert abs(score - expected) < 1e-6

    def test_post_announcement_clamped_to_one(self):
        es = EarningsSurprise(
            ticker="AAPL",
            report_date=date(2026, 1, 31),
            fiscal_quarter="Q1 2026",
            eps=MetricSurprise(
                actual=100.0,  # extreme beat → sigma >> 3
                consensus=1.0,
                estimate_high=1.2,
                estimate_low=0.8,
                analyst_count=5,
            ),
            revenue=_make_metric(),
        )
        assert self.scorer._score_surprise(None, es) == 1.0

    def test_pre_announcement_uses_delta_magnitude(self):
        # eps_estimate=2.0, trailing_mean=1.5 → delta=(2-1.5)/1.5=0.333
        data = _make_estimates(eps_estimate=2.0, eps_trailing_mean=1.5)
        score = self.scorer._score_surprise(data, None)
        expected = min(abs((2.0 - 1.5) / 1.5), 1.0)
        assert abs(score - expected) < 1e-9

    def test_post_takes_precedence_over_estimates(self):
        data = _make_estimates(eps_estimate=2.0, eps_trailing_mean=1.0)
        es = _make_earnings_surprise(eps_actual=4.0)  # large surprise
        # Post-announcement path should dominate
        score_with_both = self.scorer._score_surprise(data, es)
        score_post_only = self.scorer._score_surprise(None, es)
        assert abs(score_with_both - score_post_only) < 1e-9


# ---------------------------------------------------------------------------
# TestScoreSentiment
# ---------------------------------------------------------------------------


class TestScoreSentiment:
    def setup_method(self):
        self.scorer = _make_scorer()

    def test_none_returns_zero(self):
        assert self.scorer._score_sentiment(None) == 0.0

    def test_formula_confidence_times_abs_score(self):
        sentiment = _make_sentiment(score=-0.8, confidence=0.9)
        expected = 0.9 * abs(-0.8)  # = 0.72
        assert abs(self.scorer._score_sentiment(sentiment) - expected) < 1e-9

    def test_positive_and_negative_score_same_result(self):
        pos = _make_sentiment(score=0.6, confidence=0.8)
        neg = _make_sentiment(score=-0.6, confidence=0.8)
        assert (
            abs(self.scorer._score_sentiment(pos) - self.scorer._score_sentiment(neg))
            < 1e-9
        )


# ---------------------------------------------------------------------------
# TestScoreCoverage
# ---------------------------------------------------------------------------


class TestScoreCoverage:
    def setup_method(self):
        self.scorer = _make_scorer()

    def test_none_returns_minimum_0_1(self):
        assert self.scorer._score_coverage(None) == 0.1

    def test_below_min_analyst_count_returns_minimum(self):
        # earn_min_analyst_count=3, so count=2 → floor
        assert self.scorer._score_coverage(2) == 0.1

    def test_exactly_at_min_analyst_count_returns_formula(self):
        # count=3 → 3/10 = 0.3 (not the floor)
        assert abs(self.scorer._score_coverage(3) - 0.3) < 1e-9

    def test_ten_returns_one(self):
        assert self.scorer._score_coverage(10) == 1.0

    def test_above_ten_clamped_to_one(self):
        assert self.scorer._score_coverage(20) == 1.0

    def test_formula_analyst_count_over_ten(self):
        assert abs(self.scorer._score_coverage(5) - 0.5) < 1e-9


# ---------------------------------------------------------------------------
# TestScoreSource
# ---------------------------------------------------------------------------


class TestScoreSource:
    def setup_method(self):
        self.scorer = _make_scorer()

    def test_sec_gov_tier(self):
        assert self.scorer._score_source("sec.gov") == 0.95

    def test_businesswire_tier(self):
        assert self.scorer._score_source("businesswire") == 0.95

    def test_benzinga_tier(self):
        assert self.scorer._score_source("benzinga") == 0.80

    def test_reuters_tier(self):
        assert self.scorer._score_source("reuters") == 0.80

    def test_twitter_low_tier(self):
        assert self.scorer._score_source("twitter") == 0.17

    def test_reddit_low_tier(self):
        assert self.scorer._score_source("reddit") == 0.17

    def test_unknown_returns_default(self):
        assert self.scorer._score_source("some_random_blog") == 0.30

    def test_case_insensitive(self):
        assert self.scorer._score_source("Reuters") == self.scorer._score_source(
            "reuters"
        )
        assert self.scorer._score_source("BLOOMBERG") == self.scorer._score_source(
            "bloomberg"
        )


# ---------------------------------------------------------------------------
# TestScore (composite)
# ---------------------------------------------------------------------------


class TestScore:
    def setup_method(self):
        self.scorer = _make_scorer()

    def test_score_in_valid_range(self):
        score = self.scorer.score(
            EventType.EARN_BEAT,
            sentiment=_make_sentiment(),
            analyst_count=10,
            source="reuters",
        )
        assert 0.0 <= score <= 1.0

    def test_earn_mixed_score_is_zero(self):
        # EARN_MIXED has all-zero weights → score must be 0.0
        score = self.scorer.score(
            EventType.EARN_MIXED,
            sentiment=_make_sentiment(score=1.0, confidence=1.0),
            analyst_count=20,
            source="sec.gov",
        )
        assert score == 0.0

    def test_higher_source_increases_score_for_ma(self):
        # MA has high source weight (0.70) — better source → higher score
        score_low = self.scorer.score(EventType.MA_TARGET, source="reddit")
        score_high = self.scorer.score(EventType.MA_TARGET, source="reuters")
        assert score_high > score_low

    def test_unknown_event_type_uses_default_weights(self):
        # EventType.OTHER has no dedicated weight row → _default used
        score = self.scorer.score(
            EventType.OTHER,
            sentiment=_make_sentiment(score=0.8, confidence=0.9),
            source="benzinga",
        )
        assert 0.0 <= score <= 1.0

    def test_no_inputs_returns_floor_from_source_and_coverage(self):
        # No sentiment, estimates or surprise → only coverage floor + source default
        score = self.scorer.score(EventType.OTHER, source="unknown")
        # coverage floor (0.1) and source default (0.30) still contribute
        assert score > 0.0


# ---------------------------------------------------------------------------
# TestApplyGate
# ---------------------------------------------------------------------------


class TestApplyGate:
    def setup_method(self):
        self.scorer = _make_scorer()

    def test_passes_above_gate(self):
        signal = _make_signal()
        result = self.scorer.apply_gate(
            signal, EventType.EARN_BEAT, confidence_score=0.70
        )
        assert result.passed_confidence_gate is True
        assert result.rejection_reason is None
        assert abs(result.confidence_score - 0.70) < 1e-9

    def test_fails_below_gate(self):
        signal = _make_signal()
        result = self.scorer.apply_gate(
            signal, EventType.EARN_BEAT, confidence_score=0.50
        )
        assert result.passed_confidence_gate is False
        assert result.rejection_reason is not None
        assert "0.55" in result.rejection_reason  # gate value in message

    def test_exactly_at_gate_passes(self):
        # EARN_PRE gate = 0.45
        signal = _make_signal()
        result = self.scorer.apply_gate(
            signal, EventType.EARN_PRE, confidence_score=0.45
        )
        assert result.passed_confidence_gate is True

    def test_earn_mixed_always_fails(self):
        # Gate = 1.01, mathematically impossible
        signal = _make_signal()
        result = self.scorer.apply_gate(
            signal, EventType.EARN_MIXED, confidence_score=0.999
        )
        assert result.passed_confidence_gate is False
        assert result.rejection_reason is not None

    def test_original_signal_unchanged(self):
        signal = _make_signal()
        original_id = signal.signal_id
        result = self.scorer.apply_gate(
            signal, EventType.EARN_BEAT, confidence_score=0.70
        )
        # model_copy() returns a new object; original unmodified
        assert signal.confidence_score is None
        assert signal.passed_confidence_gate is False
        assert result is not signal
        assert result.signal_id == original_id

    def test_unknown_event_type_uses_default_gate(self):
        # FDA_APPROVAL has no gate → _DEFAULT_GATE = 0.50
        signal = _make_signal()
        result_pass = self.scorer.apply_gate(
            signal, EventType.FDA_APPROVAL, confidence_score=0.55
        )
        result_fail = self.scorer.apply_gate(
            signal, EventType.FDA_APPROVAL, confidence_score=0.45
        )
        assert result_pass.passed_confidence_gate is True
        assert result_fail.passed_confidence_gate is False

    def test_rejection_reason_contains_event_type(self):
        signal = _make_signal()
        result = self.scorer.apply_gate(
            signal, EventType.MA_RUMOUR, confidence_score=0.50
        )
        assert result.passed_confidence_gate is False
        assert "ma_rumour" in result.rejection_reason  # type: ignore[operator]

    def test_confidence_score_stamped_on_result(self):
        signal = _make_signal()
        result = self.scorer.apply_gate(
            signal, EventType.EARN_BEAT, confidence_score=0.63
        )
        assert abs(result.confidence_score - 0.63) < 1e-9


# ---------------------------------------------------------------------------
# TestScorerWithInjectedRenderer
# ---------------------------------------------------------------------------


class TestScorerWithInjectedRenderer:
    def test_accepts_custom_renderer(self):
        renderer = EstimatesRenderer()
        scorer = ConfidenceScorer(settings=_make_settings(), renderer=renderer)
        data = _make_estimates(eps_estimate=2.0, eps_trailing_mean=1.5)
        score = scorer.score(EventType.EARN_PRE, estimates=data)
        assert 0.0 <= score <= 1.0

    def test_default_renderer_created_when_none(self):
        scorer = ConfidenceScorer(settings=_make_settings(), renderer=None)
        assert scorer._renderer is not None

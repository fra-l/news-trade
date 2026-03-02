"""Placeholder tests for RiskManagerAgent internal rule methods.

Each test is skipped until the methods are accessible for unit testing.
The TODOs describe what each test should verify once implemented.
"""

import pytest


class TestCheckPositionLimit:
    @pytest.mark.skip(reason="TODO: expose _check_position_limit for unit testing")
    def test_rejects_signal_when_position_value_exceeds_max_pct(self):
        # TODO: construct a PortfolioState with equity=100_000 and
        # Settings.max_position_pct=0.05, then call _check_position_limit
        # with a TradeSignal whose suggested_qty * entry_price > 5 000.
        # Assert the method returns False (rejected).
        pass

    @pytest.mark.skip(reason="TODO: expose _check_position_limit for unit testing")
    def test_accepts_signal_within_position_limit(self):
        # TODO: same setup but suggested_qty * entry_price <= 5 000.
        # Assert the method returns True (accepted).
        pass


class TestCheckMaxPositions:
    @pytest.mark.skip(reason="TODO: expose _check_max_positions for unit testing")
    def test_rejects_new_position_when_max_reached(self):
        # TODO: build a PortfolioState with len(positions) == Settings.max_total_positions.
        # Call _check_max_positions with a signal for a NEW ticker.
        # Assert the method returns False.
        pass

    @pytest.mark.skip(reason="TODO: expose _check_max_positions for unit testing")
    def test_accepts_new_position_below_max(self):
        # TODO: build a PortfolioState with fewer positions than the limit.
        # Assert the method returns True.
        pass

    @pytest.mark.skip(reason="TODO: expose _check_max_positions for unit testing")
    def test_accepts_signal_for_existing_position_even_at_max(self):
        # TODO: portfolio at max_total_positions but signal is for a ticker
        # already in positions (adding to existing, not opening new).
        # Assert the method returns True.
        pass


class TestCheckDrawdown:
    @pytest.mark.skip(reason="TODO: expose _check_drawdown for unit testing")
    def test_rejects_when_drawdown_exceeds_limit(self):
        # TODO: build a PortfolioState with max_drawdown_pct > Settings.max_drawdown_pct.
        # Assert _check_drawdown returns False.
        pass

    @pytest.mark.skip(reason="TODO: expose _check_drawdown for unit testing")
    def test_accepts_when_drawdown_within_limit(self):
        # TODO: build a PortfolioState with max_drawdown_pct <= Settings.max_drawdown_pct.
        # Assert _check_drawdown returns True.
        pass


class TestHasConflictingPosition:
    @pytest.mark.skip(reason="TODO: expose _has_conflicting_position for unit testing")
    def test_detects_conflict_long_vs_short(self):
        # TODO: portfolio has a LONG position in AAPL (qty > 0).
        # Signal direction is SHORT for AAPL.
        # Assert _has_conflicting_position returns True.
        pass

    @pytest.mark.skip(reason="TODO: expose _has_conflicting_position for unit testing")
    def test_no_conflict_same_direction(self):
        # TODO: portfolio has a LONG position in AAPL.
        # Signal direction is also LONG for AAPL.
        # Assert _has_conflicting_position returns False.
        pass

    @pytest.mark.skip(reason="TODO: expose _has_conflicting_position for unit testing")
    def test_no_conflict_different_ticker(self):
        # TODO: portfolio has a position in AAPL; signal is for MSFT.
        # Assert _has_conflicting_position returns False.
        pass

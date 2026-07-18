"""
Unit tests for GEXDetector — Phase 2.

All tests use fixture data from tests/fixtures/ (no network I/O).
Spot price is fixed at 192.0 (between the 190 and 195 strikes in every fixture).
"""

from decimal import Decimal

import pytest

from trader.gex.detector import GEXDetector
from trader.gex.schemas import GEXDetectorParams, GEXRegime
from trader.uw.validators import parse_spot_gex_by_strike

SPOT = Decimal("192")


@pytest.fixture
def detector() -> GEXDetector:
    return GEXDetector()


@pytest.fixture
def gex_positive(gex_positive_raw):
    return parse_spot_gex_by_strike(gex_positive_raw)


@pytest.fixture
def gex_negative(gex_negative_raw):
    return parse_spot_gex_by_strike(gex_negative_raw)


@pytest.fixture
def gex_mixed(gex_mixed_raw):
    return parse_spot_gex_by_strike(gex_mixed_raw)


# ---------------------------------------------------------------------------
# Positive GEX fixture
# ---------------------------------------------------------------------------


class TestPositiveRegime:
    def test_regime_is_positive(self, detector, gex_positive):
        setup = detector.detect("AAPL", gex_positive, SPOT)
        assert setup.regime == GEXRegime.POSITIVE

    def test_candidate_direction_is_call(self, detector, gex_positive):
        setup = detector.detect("AAPL", gex_positive, SPOT)
        assert setup.candidate_direction == "call"

    def test_setup_type_is_pin(self, detector, gex_positive):
        setup = detector.detect("AAPL", gex_positive, SPOT)
        assert setup.setup_type == "pin"

    def test_call_wall_identified(self, detector, gex_positive):
        setup = detector.detect("AAPL", gex_positive, SPOT)
        assert setup.nearest_call_wall is not None
        assert setup.nearest_call_wall.strike > SPOT
        assert setup.nearest_call_wall.side == "call_wall"

    def test_call_wall_is_largest_above_spot(self, detector, gex_positive):
        # Strike 200 has the highest net GEX above spot (2750M)
        setup = detector.detect("AAPL", gex_positive, SPOT)
        assert setup.nearest_call_wall.strike == Decimal("200")

    def test_no_put_wall_in_all_positive_gex(self, detector, gex_positive):
        # All net GEX values are positive so no put wall exists
        setup = detector.detect("AAPL", gex_positive, SPOT)
        assert setup.nearest_put_wall is None

    def test_target_level_is_call_wall(self, detector, gex_positive):
        setup = detector.detect("AAPL", gex_positive, SPOT)
        assert setup.target_level == setup.nearest_call_wall.strike

    def test_high_structure_confidence(self, detector, gex_positive):
        setup = detector.detect("AAPL", gex_positive, SPOT)
        assert setup.structure_confidence >= 0.5

    def test_no_flip_point_when_all_same_sign(self, detector, gex_positive):
        setup = detector.detect("AAPL", gex_positive, SPOT)
        assert setup.flip_point is None

    def test_ticker_and_spot_preserved(self, detector, gex_positive):
        setup = detector.detect("AAPL", gex_positive, SPOT)
        assert setup.ticker == "AAPL"
        assert setup.spot_price == SPOT


# ---------------------------------------------------------------------------
# Negative GEX fixture
# ---------------------------------------------------------------------------


class TestNegativeRegime:
    def test_regime_is_negative(self, detector, gex_negative):
        setup = detector.detect("AAPL", gex_negative, SPOT)
        assert setup.regime == GEXRegime.NEGATIVE

    def test_setup_type_is_momentum(self, detector, gex_negative):
        setup = detector.detect("AAPL", gex_negative, SPOT)
        assert setup.setup_type == "momentum"

    def test_flip_point_found(self, detector, gex_negative):
        # Net GEX crosses from negative to positive between strikes 200 and 205
        setup = detector.detect("AAPL", gex_negative, SPOT)
        assert setup.flip_point is not None
        assert Decimal("200") < setup.flip_point < Decimal("205")

    def test_flip_point_within_one_strike_width(self, detector, gex_negative):
        setup = detector.detect("AAPL", gex_negative, SPOT)
        # Strike spacing is 5, so flip must be within ±5 of crossing bracket
        assert Decimal("200") <= setup.flip_point <= Decimal("205")

    def test_put_wall_identified_below_spot(self, detector, gex_negative):
        setup = detector.detect("AAPL", gex_negative, SPOT)
        assert setup.nearest_put_wall is not None
        assert setup.nearest_put_wall.strike < SPOT
        assert setup.nearest_put_wall.side == "put_wall"

    def test_put_wall_is_most_negative_below_spot(self, detector, gex_negative):
        # Strike 190 has net GEX = 200M - 3500M = -3300M (most negative below 192)
        setup = detector.detect("AAPL", gex_negative, SPOT)
        assert setup.nearest_put_wall.strike == Decimal("190")

    def test_spot_below_flip_gives_put_direction(self, detector, gex_negative):
        # SPOT=192 < flip~201 → bearish momentum → "put"
        setup = detector.detect("AAPL", gex_negative, SPOT)
        assert setup.candidate_direction == "put"

    def test_spot_above_flip_gives_call_direction(self, detector, gex_negative):
        # If spot is above the flip point, expect bullish squeeze direction
        setup = detector.detect("AAPL", gex_negative, Decimal("210"))
        assert setup.candidate_direction == "call"

    def test_target_level_is_put_wall_when_bearish(self, detector, gex_negative):
        setup = detector.detect("AAPL", gex_negative, SPOT)
        assert setup.target_level == setup.nearest_put_wall.strike

    def test_high_structure_confidence(self, detector, gex_negative):
        setup = detector.detect("AAPL", gex_negative, SPOT)
        assert setup.structure_confidence >= 0.5


# ---------------------------------------------------------------------------
# Mixed GEX fixture
# ---------------------------------------------------------------------------


class TestMixedRegime:
    def test_regime_is_mixed(self, detector, gex_mixed):
        setup = detector.detect("AAPL", gex_mixed, SPOT)
        assert setup.regime == GEXRegime.MIXED

    def test_direction_is_none(self, detector, gex_mixed):
        setup = detector.detect("AAPL", gex_mixed, SPOT)
        assert setup.candidate_direction == "none"

    def test_setup_type_is_none(self, detector, gex_mixed):
        setup = detector.detect("AAPL", gex_mixed, SPOT)
        assert setup.setup_type == "none"

    def test_target_level_is_none(self, detector, gex_mixed):
        setup = detector.detect("AAPL", gex_mixed, SPOT)
        assert setup.target_level is None

    def test_low_structure_confidence(self, detector, gex_mixed):
        setup = detector.detect("AAPL", gex_mixed, SPOT)
        assert setup.structure_confidence < 0.30


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_gex_data_returns_mixed(self, detector):
        setup = detector.detect("AAPL", [], SPOT)
        assert setup.regime == GEXRegime.MIXED
        assert setup.candidate_direction == "none"
        assert setup.structure_confidence == 0.0

    def test_zero_total_abs_gex_returns_mixed(self, detector):
        from trader.uw.schemas import SpotGEXByStrike
        zero_strike = SpotGEXByStrike(
            price=Decimal("200"),
            call_gamma_oi=Decimal("0"),
            put_gamma_oi=Decimal("0"),
        )
        setup = detector.detect("AAPL", [zero_strike], SPOT)
        assert setup.regime == GEXRegime.MIXED

    def test_custom_params_raises_threshold(self, gex_positive):
        # Inflate threshold so even a clear positive fixture fails
        strict = GEXDetector(GEXDetectorParams(min_confidence_threshold=0.99))
        setup = strict.detect("AAPL", gex_positive, SPOT)
        assert setup.regime == GEXRegime.MIXED

    def test_call_wall_distance_pct_is_correct(self, detector, gex_positive):
        setup = detector.detect("AAPL", gex_positive, SPOT)
        wall = setup.nearest_call_wall
        expected = abs(wall.strike - SPOT) / SPOT
        assert abs(wall.distance_pct - expected) < Decimal("0.0001")

    def test_raw_gex_preserved_in_setup(self, detector, gex_positive):
        setup = detector.detect("AAPL", gex_positive, SPOT)
        assert len(setup.raw_gex_by_strike) == 7

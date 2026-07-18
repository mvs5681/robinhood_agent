"""
Unit tests for Phase 3: BlendScorer, features, and validators for IV/technicals.

All tests are synchronous and fixture-driven — no network I/O.
"""

from decimal import Decimal

import pytest

from trader.gex.schemas import GEXRegime
from trader.scoring.features import (
    darkpool_score,
    flow_pressure_score,
    iv_cost_score,
    market_tide_score,
    technicals_score,
)
from trader.scoring.scorer import BlendScorer, DEFAULT_WEIGHTS
from trader.uw.validators import (
    parse_darkpool_prints,
    parse_flow_alerts,
    parse_interpolated_iv,
    parse_market_tide,
    parse_net_prem_ticks,
    parse_technical_indicator,
)


# ---------------------------------------------------------------------------
# Validator tests for new schemas
# ---------------------------------------------------------------------------


class TestParseInterpolatedIV:
    def test_parses_six_entries(self, interpolated_iv_raw):
        entries = parse_interpolated_iv(interpolated_iv_raw)
        assert len(entries) == 6

    def test_30_day_entry(self, interpolated_iv_raw):
        entries = parse_interpolated_iv(interpolated_iv_raw)
        entry_30 = next(e for e in entries if e.days == 30)
        assert entry_30.percentile == Decimal("35.2")
        assert entry_30.volatility == Decimal("0.299")

    def test_fields_are_decimal(self, interpolated_iv_raw):
        entries = parse_interpolated_iv(interpolated_iv_raw)
        assert isinstance(entries[0].percentile, Decimal)
        assert isinstance(entries[0].volatility, Decimal)


class TestParseTechnicalIndicator:
    def test_parses_rsi(self, technical_rsi_raw):
        points = parse_technical_indicator(technical_rsi_raw, "RSI")
        assert len(points) == 6
        assert points[-1].value == Decimal("46.75")
        assert points[-1].macd is None

    def test_parses_macd(self, technical_macd_raw):
        points = parse_technical_indicator(technical_macd_raw, "MACD")
        assert len(points) == 6
        last = points[-1]
        assert last.macd == Decimal("0.52")
        assert last.signal == Decimal("0.21")
        assert last.histogram == Decimal("0.31")
        assert last.value is None

    def test_rsi_timestamp_preserved(self, technical_rsi_raw):
        points = parse_technical_indicator(technical_rsi_raw, "RSI")
        assert points[0].timestamp == "2026-06-24"


# ---------------------------------------------------------------------------
# Feature function tests — market tide
# ---------------------------------------------------------------------------


class TestMarketTideScore:
    def test_bullish_tide_scores_high_for_call(self, market_tide_raw):
        ticks = parse_market_tide(market_tide_raw)
        score = market_tide_score(ticks, "call")
        assert score > 0.6

    def test_bullish_tide_scores_low_for_put(self, market_tide_raw):
        ticks = parse_market_tide(market_tide_raw)
        score = market_tide_score(ticks, "put")
        assert score < 0.4

    def test_empty_returns_neutral(self):
        assert market_tide_score([], "call") == 0.5

    def test_score_in_unit_range(self, market_tide_raw):
        ticks = parse_market_tide(market_tide_raw)
        for direction in ("call", "put"):
            s = market_tide_score(ticks, direction)
            assert 0.0 <= s <= 1.0

    def test_call_and_put_sum_to_one_on_symmetric_flow(self):
        from trader.uw.schemas import MarketTide
        from datetime import datetime, timezone
        tick = MarketTide(
            timestamp=datetime.now(timezone.utc),
            net_call_premium=Decimal("500000"),
            net_put_premium=Decimal("-500000"),
            net_volume=1000,
        )
        call_s = market_tide_score([tick], "call")
        put_s = market_tide_score([tick], "put")
        assert abs(call_s - 0.5) < 0.01
        assert abs(put_s - 0.5) < 0.01


# ---------------------------------------------------------------------------
# Feature — darkpool
# ---------------------------------------------------------------------------


class TestDarkpoolScore:
    def test_high_premium_scores_near_one(self, darkpool_raw):
        prints = parse_darkpool_prints(darkpool_raw)
        # Total premium ~4.5M, cap 5M → ~0.9
        score = darkpool_score(prints, premium_cap=Decimal("5_000_000"))
        assert score > 0.8

    def test_zero_premium_scores_zero(self):
        score = darkpool_score([])
        assert score == 0.0

    def test_caps_at_one(self, darkpool_raw):
        prints = parse_darkpool_prints(darkpool_raw)
        score = darkpool_score(prints, premium_cap=Decimal("1"))
        assert score == 1.0

    def test_canceled_prints_excluded(self, darkpool_raw):
        from trader.uw.schemas import DarkpoolPrint
        from datetime import datetime, timezone
        canceled = DarkpoolPrint(
            ticker="AAPL", price=Decimal("195"), size=1000,
            premium=Decimal("1_000_000"), executed_at=datetime.now(timezone.utc),
            market_center="L", canceled=True,
        )
        score_without = darkpool_score([canceled])
        assert score_without == 0.0


# ---------------------------------------------------------------------------
# Feature — flow pressure
# ---------------------------------------------------------------------------


class TestFlowPressureScore:
    def test_all_matching_alerts_scores_high(self, flow_alerts_raw, market_tide_raw):
        alerts = parse_flow_alerts(flow_alerts_raw)
        ticks = parse_market_tide(market_tide_raw)
        # AAPL has 1 call alert → alert_pct = 1.0, tick data is market-wide (no ticker net_prem)
        score = flow_pressure_score(alerts, [], "AAPL", "call")
        assert score >= 0.5

    def test_no_matching_direction_scores_low(self, flow_alerts_raw):
        alerts = parse_flow_alerts(flow_alerts_raw)
        # AAPL only has call alert, asking for put → alert_pct = 0
        score = flow_pressure_score(alerts, [], "AAPL", "put")
        assert score < 0.5

    def test_unknown_ticker_returns_neutral(self, flow_alerts_raw):
        alerts = parse_flow_alerts(flow_alerts_raw)
        score = flow_pressure_score(alerts, [], "UNKNOWN", "call")
        assert score == pytest.approx(0.5)

    def test_score_in_unit_range(self, flow_alerts_raw):
        alerts = parse_flow_alerts(flow_alerts_raw)
        for direction in ("call", "put"):
            s = flow_pressure_score(alerts, [], "AAPL", direction)
            assert 0.0 <= s <= 1.0


# ---------------------------------------------------------------------------
# Feature — IV cost
# ---------------------------------------------------------------------------


class TestIVCostScore:
    def test_low_percentile_scores_high(self, interpolated_iv_raw):
        entries = parse_interpolated_iv(interpolated_iv_raw)
        # 30-day entry has percentile=35.2 → iv_cost = 1 - 0.352 = 0.648
        score = iv_cost_score(entries)
        assert abs(score - (1.0 - 35.2 / 100)) < 0.01

    def test_empty_returns_neutral(self):
        assert iv_cost_score([]) == 0.5

    def test_percentile_100_scores_zero(self):
        from trader.uw.schemas import InterpolatedIVEntry
        entry = InterpolatedIVEntry(days=30, volatility=Decimal("0.5"), percentile=Decimal("100"))
        assert iv_cost_score([entry]) == 0.0

    def test_percentile_0_scores_one(self):
        from trader.uw.schemas import InterpolatedIVEntry
        entry = InterpolatedIVEntry(days=30, volatility=Decimal("0.2"), percentile=Decimal("0"))
        assert iv_cost_score([entry]) == 1.0

    def test_picks_closest_to_30_days(self, interpolated_iv_raw):
        entries = parse_interpolated_iv(interpolated_iv_raw)
        # 30-day entry should be used (exact match)
        score = iv_cost_score(entries)
        entry_30 = next(e for e in entries if e.days == 30)
        assert abs(score - (1 - float(entry_30.percentile) / 100)) < 0.001


# ---------------------------------------------------------------------------
# Feature — technicals
# ---------------------------------------------------------------------------


class TestTechnicalsScore:
    def test_bullish_rsi_macd_scores_high_for_call(
        self, technical_rsi_raw, technical_macd_raw
    ):
        rsi = parse_technical_indicator(technical_rsi_raw, "RSI")
        macd = parse_technical_indicator(technical_macd_raw, "MACD")
        # Latest RSI=46.75 (in 30-50 zone → 0.9), MACD=0.52>signal=0.21 (bullish→0.8)
        score = technicals_score(rsi, macd, "call")
        assert score > 0.7

    def test_bullish_rsi_macd_scores_low_for_put(
        self, technical_rsi_raw, technical_macd_raw
    ):
        rsi = parse_technical_indicator(technical_rsi_raw, "RSI")
        macd = parse_technical_indicator(technical_macd_raw, "MACD")
        score = technicals_score(rsi, macd, "put")
        assert score < 0.5

    def test_empty_data_returns_neutral(self):
        assert technicals_score([], [], "call") == 0.5

    def test_rsi_only_still_scores(self, technical_rsi_raw):
        rsi = parse_technical_indicator(technical_rsi_raw, "RSI")
        score = technicals_score(rsi, [], "call")
        assert 0.0 < score <= 1.0

    def test_score_in_unit_range(self, technical_rsi_raw, technical_macd_raw):
        rsi = parse_technical_indicator(technical_rsi_raw, "RSI")
        macd = parse_technical_indicator(technical_macd_raw, "MACD")
        for direction in ("call", "put"):
            s = technicals_score(rsi, macd, direction)
            assert 0.0 <= s <= 1.0


# ---------------------------------------------------------------------------
# BlendScorer
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed_data(
    market_tide_raw, darkpool_raw, flow_alerts_raw,
    interpolated_iv_raw, technical_rsi_raw, technical_macd_raw
):
    return {
        "market_tide": parse_market_tide(market_tide_raw),
        "darkpool": parse_darkpool_prints(darkpool_raw),
        "flow_alerts": parse_flow_alerts(flow_alerts_raw),
        "net_prem_ticks": [],
        "iv_entries": parse_interpolated_iv(interpolated_iv_raw),
        "rsi_data": parse_technical_indicator(technical_rsi_raw, "RSI"),
        "macd_data": parse_technical_indicator(technical_macd_raw, "MACD"),
    }


@pytest.fixture
def positive_setup(gex_positive_raw):
    from decimal import Decimal
    from trader.gex.detector import GEXDetector
    from trader.uw.validators import parse_spot_gex_by_strike
    gex = parse_spot_gex_by_strike(gex_positive_raw)
    return GEXDetector().detect("AAPL", gex, Decimal("192"))


@pytest.fixture
def mixed_setup(gex_mixed_raw):
    from decimal import Decimal
    from trader.gex.detector import GEXDetector
    from trader.uw.validators import parse_spot_gex_by_strike
    gex = parse_spot_gex_by_strike(gex_mixed_raw)
    return GEXDetector().detect("AAPL", gex, Decimal("192"))


class TestBlendScorer:
    def test_weights_must_sum_to_one(self):
        bad = {k: 0.1 for k in DEFAULT_WEIGHTS}  # sums to 0.5
        with pytest.raises(ValueError, match="sum to 1.0"):
            BlendScorer(bad)

    def test_missing_weight_key_raises(self):
        w = {k: v for k, v in DEFAULT_WEIGHTS.items() if k != "darkpool"}
        with pytest.raises(ValueError, match="Missing weight keys"):
            BlendScorer(w)

    def test_unknown_weight_key_raises(self):
        w = {**DEFAULT_WEIGHTS, "extra": 0.0}
        w["market_tide"] -= 0.0  # keep sum == 1
        with pytest.raises(ValueError, match="Unknown weight keys"):
            BlendScorer(w)

    def test_mixed_setup_returns_skipped(self, mixed_setup, parsed_data):
        scorer = BlendScorer()
        candidate = scorer.score(mixed_setup, **parsed_data)
        assert candidate.execution_status == "skipped_no_structure"
        assert candidate.blend_scores.composite == 0.0

    def test_positive_setup_returns_proposed(self, positive_setup, parsed_data):
        scorer = BlendScorer()
        candidate = scorer.score(positive_setup, **parsed_data)
        assert candidate.execution_status == "proposed"

    def test_composite_in_unit_range(self, positive_setup, parsed_data):
        scorer = BlendScorer()
        candidate = scorer.score(positive_setup, **parsed_data)
        assert 0.0 <= candidate.blend_scores.composite <= 1.0

    def test_composite_reflects_components(self, positive_setup, parsed_data):
        scorer = BlendScorer()
        c = scorer.score(positive_setup, **parsed_data)
        bs = c.blend_scores
        expected = (
            0.2 * bs.market_tide + 0.2 * bs.darkpool +
            0.2 * bs.flow_pressure + 0.2 * bs.iv_cost + 0.2 * bs.technicals
        )
        assert abs(bs.composite - expected) < 1e-6

    def test_rank_sorts_descending(self, positive_setup, mixed_setup, parsed_data):
        scorer = BlendScorer()
        c1 = scorer.score(positive_setup, **parsed_data)
        c2 = scorer.score(mixed_setup, **parsed_data)
        ranked = scorer.rank([c2, c1])
        # proposed candidates come first, sorted by composite desc
        proposed = [c for c in ranked if c.execution_status == "proposed"]
        assert proposed[0].rank == 1
        for i in range(len(proposed) - 1):
            assert proposed[i].blend_scores.composite >= proposed[i + 1].blend_scores.composite

    def test_skipped_candidates_get_rank_zero(self, mixed_setup, parsed_data):
        scorer = BlendScorer()
        c = scorer.score(mixed_setup, **parsed_data)
        ranked = scorer.rank([c])
        assert ranked[0].rank == 0

    def test_custom_weights_change_composite(self, positive_setup, parsed_data):
        equal = BlendScorer()
        heavy_tech = BlendScorer({
            "market_tide": 0.05, "darkpool": 0.05,
            "flow_pressure": 0.05, "iv_cost": 0.05,
            "technicals": 0.80,
        })
        c_equal = equal.score(positive_setup, **parsed_data)
        c_heavy = heavy_tech.score(positive_setup, **parsed_data)
        # Scores should differ because technicals weight is dominant
        assert c_equal.blend_scores.composite != c_heavy.blend_scores.composite

    def test_high_composite_with_all_bullish_signals(self, positive_setup):
        """Monotonicity: all-max inputs → composite close to 1."""
        from trader.uw.schemas import MarketTide, DarkpoolPrint, InterpolatedIVEntry, TechnicalPoint
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

        max_tide = [MarketTide(timestamp=now, net_call_premium=Decimal("10000000"),
                               net_put_premium=Decimal("0"), net_volume=99999)]
        max_dp = [DarkpoolPrint(ticker="AAPL", price=Decimal("200"), size=1,
                                premium=Decimal("10_000_000"), executed_at=now,
                                market_center="L")]
        max_iv = [InterpolatedIVEntry(days=30, volatility=Decimal("0.2"), percentile=Decimal("0"))]
        max_rsi = [TechnicalPoint(timestamp="2026-06-30", value=Decimal("45"))]
        max_macd = [TechnicalPoint(timestamp="2026-06-30", macd=Decimal("1"), signal=Decimal("0"))]

        scorer = BlendScorer()
        c = scorer.score(
            positive_setup,
            market_tide=max_tide,
            darkpool=max_dp,
            flow_alerts=[],
            net_prem_ticks=[],
            iv_entries=max_iv,
            rsi_data=max_rsi,
            macd_data=max_macd,
        )
        assert c.blend_scores.composite > 0.75

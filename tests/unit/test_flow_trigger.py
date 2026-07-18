"""
Unit tests for Phase 4: FlowTrigger confirmation gate.

All tests are synchronous and fixture-driven — no network I/O.
as_of is always passed explicitly so tests don't depend on wall-clock time.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trader.flow.trigger import FlowTrigger
from trader.scoring.schemas import CandidateSignal
from trader.uw.schemas import FlowAlert
from trader.uw.validators import parse_flow_alerts

# Fixture alerts are timestamped 2026-06-30T14:30:00Z (AAPL call $250k)
# and 2026-06-30T13:45:00Z (SPY put $180k).
# Use as_of=16:00Z → both within 4-hour window.
AS_OF = datetime(2026, 6, 30, 16, 0, 0, tzinfo=timezone.utc)
AS_OF_TOO_LATE = datetime(2026, 6, 30, 18, 31, 0, tzinfo=timezone.utc)  # 4h01m after 14:30

DEFAULT_TRIGGER = FlowTrigger(min_premium=Decimal("100_000"), lookback_hours=4)


# ---------------------------------------------------------------------------
# Fixtures — reusable candidate helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def aapl_call_candidate(gex_positive_raw):
    from trader.gex.detector import GEXDetector
    from trader.scoring.scorer import BlendScorer
    from trader.uw.validators import parse_spot_gex_by_strike

    gex = parse_spot_gex_by_strike(gex_positive_raw)
    setup = GEXDetector().detect("AAPL", gex, Decimal("192"))
    return BlendScorer().score(
        setup,
        market_tide=[],
        darkpool=[],
        flow_alerts=[],
        net_prem_ticks=[],
        iv_entries=[],
        rsi_data=[],
        macd_data=[],
    )


@pytest.fixture
def alerts(flow_alerts_raw) -> list[FlowAlert]:
    return parse_flow_alerts(flow_alerts_raw)


def _make_alert(
    ticker: str = "AAPL",
    direction: str = "call",
    premium: str = "250000",
    created_at: datetime | None = None,
) -> FlowAlert:
    return FlowAlert(
        ticker=ticker,
        expiry="2026-07-18",
        strike=Decimal("200"),
        type=direction,
        total_premium=Decimal(premium),
        total_size=100,
        volume=1000,
        open_interest=5000,
        alert_rule="RepeatedHits",
        trade_count=10,
        created_at=created_at or AS_OF - timedelta(hours=1),
    )


# ---------------------------------------------------------------------------
# Skipped candidates pass through unchanged
# ---------------------------------------------------------------------------


class TestPassThrough:
    def test_skipped_no_structure_passes_through(self, gex_mixed_raw, alerts):
        from trader.gex.detector import GEXDetector
        from trader.scoring.scorer import BlendScorer
        from trader.uw.validators import parse_spot_gex_by_strike

        gex = parse_spot_gex_by_strike(gex_mixed_raw)
        setup = GEXDetector().detect("AAPL", gex, Decimal("192"))
        already_skipped = BlendScorer().score(setup, market_tide=[], darkpool=[],
                                              flow_alerts=[], net_prem_ticks=[],
                                              iv_entries=[], rsi_data=[], macd_data=[])
        assert already_skipped.execution_status == "skipped_no_structure"

        result = DEFAULT_TRIGGER.check(already_skipped, alerts, as_of=AS_OF)
        assert result.execution_status == "skipped_no_structure"
        assert result is already_skipped  # pass-through returns same object, no copy

    def test_pass_through_preserves_skip_reason(self, gex_mixed_raw, alerts):
        from trader.gex.detector import GEXDetector
        from trader.scoring.scorer import BlendScorer
        from trader.uw.validators import parse_spot_gex_by_strike

        gex = parse_spot_gex_by_strike(gex_mixed_raw)
        setup = GEXDetector().detect("AAPL", gex, Decimal("192"))
        skipped = BlendScorer().score(setup, market_tide=[], darkpool=[], flow_alerts=[],
                                      net_prem_ticks=[], iv_entries=[], rsi_data=[], macd_data=[])
        original_reason = skipped.skip_reason

        result = DEFAULT_TRIGGER.check(skipped, alerts, as_of=AS_OF)
        assert result.skip_reason == original_reason


# ---------------------------------------------------------------------------
# "none" direction is always skipped
# ---------------------------------------------------------------------------


class TestNoneDirection:
    def test_none_direction_becomes_skipped_no_flow(self, aapl_call_candidate):
        # Manufacture a proposed candidate with direction="none" by patching the setup
        from trader.gex.schemas import GEXSetup, GEXRegime
        import datetime as dt

        patched_setup = aapl_call_candidate.gex_setup.model_copy(update={
            "candidate_direction": "none",
            "setup_type": "none",
        })
        candidate = aapl_call_candidate.model_copy(update={"gex_setup": patched_setup})

        result = DEFAULT_TRIGGER.check(candidate, [], as_of=AS_OF)
        assert result.execution_status == "skipped_no_flow"
        assert result.flow_confirmed is False


# ---------------------------------------------------------------------------
# Successful confirmation
# ---------------------------------------------------------------------------


class TestConfirmation:
    def test_matching_alert_confirms_candidate(self, aapl_call_candidate, alerts):
        result = DEFAULT_TRIGGER.check(aapl_call_candidate, alerts, as_of=AS_OF)
        assert result.flow_confirmed is True
        assert result.execution_status == "proposed"
        assert result.flow_trigger is not None
        assert result.flow_trigger.ticker == "AAPL"
        assert result.flow_trigger.type == "call"

    def test_confirmed_trigger_is_highest_premium(self, aapl_call_candidate):
        low = _make_alert(premium="150000", created_at=AS_OF - timedelta(hours=1))
        high = _make_alert(premium="500000", created_at=AS_OF - timedelta(hours=2))
        medium = _make_alert(premium="300000", created_at=AS_OF - timedelta(minutes=30))

        result = DEFAULT_TRIGGER.check(aapl_call_candidate, [low, medium, high], as_of=AS_OF)
        assert result.flow_trigger.total_premium == Decimal("500000")

    def test_confirmed_preserves_blend_scores(self, aapl_call_candidate, alerts):
        original_composite = aapl_call_candidate.blend_scores.composite
        result = DEFAULT_TRIGGER.check(aapl_call_candidate, alerts, as_of=AS_OF)
        assert result.blend_scores.composite == original_composite

    def test_confirmed_preserves_rank(self, aapl_call_candidate, alerts):
        candidate_with_rank = aapl_call_candidate.model_copy(update={"rank": 1})
        result = DEFAULT_TRIGGER.check(candidate_with_rank, alerts, as_of=AS_OF)
        assert result.rank == 1


# ---------------------------------------------------------------------------
# Rejection conditions
# ---------------------------------------------------------------------------


class TestRejection:
    def test_no_alerts_returns_skipped(self, aapl_call_candidate):
        result = DEFAULT_TRIGGER.check(aapl_call_candidate, [], as_of=AS_OF)
        assert result.execution_status == "skipped_no_flow"
        assert result.flow_confirmed is False
        assert result.flow_trigger is None

    def test_wrong_direction_returns_skipped(self, aapl_call_candidate):
        put_alert = _make_alert(ticker="AAPL", direction="put")
        result = DEFAULT_TRIGGER.check(aapl_call_candidate, [put_alert], as_of=AS_OF)
        assert result.execution_status == "skipped_no_flow"

    def test_wrong_ticker_returns_skipped(self, aapl_call_candidate):
        spy_alert = _make_alert(ticker="SPY", direction="call")
        result = DEFAULT_TRIGGER.check(aapl_call_candidate, [spy_alert], as_of=AS_OF)
        assert result.execution_status == "skipped_no_flow"

    def test_alert_outside_lookback_returns_skipped(self, aapl_call_candidate):
        # AAPL call at 14:30; as_of 18:31 → 4h01m gap → outside 4h window
        result = DEFAULT_TRIGGER.check(aapl_call_candidate, [
            _make_alert(created_at=datetime(2026, 6, 30, 14, 30, 0, tzinfo=timezone.utc))
        ], as_of=AS_OF_TOO_LATE)
        assert result.execution_status == "skipped_no_flow"

    def test_alert_exactly_at_boundary_is_included(self, aapl_call_candidate):
        # Exactly 4h before as_of → should be included (cutoff = as_of - 4h, alert >= cutoff)
        exactly_at_cutoff = AS_OF - timedelta(hours=4)
        result = DEFAULT_TRIGGER.check(aapl_call_candidate, [
            _make_alert(created_at=exactly_at_cutoff)
        ], as_of=AS_OF)
        assert result.flow_confirmed is True

    def test_alert_below_min_premium_returns_skipped(self, aapl_call_candidate):
        cheap = _make_alert(premium="99999")
        result = DEFAULT_TRIGGER.check(aapl_call_candidate, [cheap], as_of=AS_OF)
        assert result.execution_status == "skipped_no_flow"

    def test_alert_at_min_premium_exactly_is_accepted(self, aapl_call_candidate):
        exact = _make_alert(premium="100000")
        result = DEFAULT_TRIGGER.check(aapl_call_candidate, [exact], as_of=AS_OF)
        assert result.flow_confirmed is True

    def test_alert_with_null_created_at_excluded(self, aapl_call_candidate):
        no_ts = FlowAlert(
            ticker="AAPL", expiry="2026-07-18", strike=Decimal("200"),
            type="call", total_premium=Decimal("500000"), total_size=100,
            volume=1000, open_interest=5000, alert_rule="RepeatedHits",
            trade_count=10, created_at=None,
        )
        result = DEFAULT_TRIGGER.check(aapl_call_candidate, [no_ts], as_of=AS_OF)
        assert result.execution_status == "skipped_no_flow"

    def test_skip_reason_contains_ticker_and_direction(self, aapl_call_candidate):
        result = DEFAULT_TRIGGER.check(aapl_call_candidate, [], as_of=AS_OF)
        assert "AAPL" in (result.skip_reason or "")
        assert "call" in (result.skip_reason or "")


# ---------------------------------------------------------------------------
# Custom FlowTrigger params
# ---------------------------------------------------------------------------


class TestCustomParams:
    def test_higher_min_premium_rejects_otherwise_passing_alert(self, aapl_call_candidate):
        strict = FlowTrigger(min_premium=Decimal("300_000"), lookback_hours=4)
        alert_250k = _make_alert(premium="250000")
        result = strict.check(aapl_call_candidate, [alert_250k], as_of=AS_OF)
        assert result.execution_status == "skipped_no_flow"

    def test_shorter_lookback_rejects_older_alert(self, aapl_call_candidate):
        tight = FlowTrigger(min_premium=Decimal("100_000"), lookback_hours=1)
        two_hours_ago = AS_OF - timedelta(hours=2)
        result = tight.check(aapl_call_candidate, [
            _make_alert(created_at=two_hours_ago)
        ], as_of=AS_OF)
        assert result.execution_status == "skipped_no_flow"

    def test_longer_lookback_accepts_older_alert(self, aapl_call_candidate):
        relaxed = FlowTrigger(min_premium=Decimal("100_000"), lookback_hours=8)
        five_hours_ago = AS_OF - timedelta(hours=5)
        result = relaxed.check(aapl_call_candidate, [
            _make_alert(created_at=five_hours_ago)
        ], as_of=AS_OF)
        assert result.flow_confirmed is True

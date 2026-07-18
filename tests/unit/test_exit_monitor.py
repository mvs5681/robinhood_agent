"""Unit tests for Phase 6b: ExitMonitor."""

from datetime import datetime, timezone, date
from decimal import Decimal

import pytest

from trader.exits.monitor import ExitMonitor
from trader.exits.schemas import ExitReason, Position
from trader.uw.schemas import OptionContract


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

AS_OF = datetime(2026, 6, 30, 15, 0, 0, tzinfo=timezone.utc)

DEFAULT_MONITOR = ExitMonitor(stop_loss_pct=0.35, dte_floor=7)


def _contract() -> OptionContract:
    return OptionContract(
        ticker="AAPL", expiry=date(2026, 7, 25), strike=Decimal("200"),
        type="call", bid=Decimal("2.90"), ask=Decimal("3.10"),
        open_interest=9000, volume=4500, delta=Decimal("0.35"),
    )


def _position(
    target_level: str = "200",
    entry_premium: str = "3.00",
) -> Position:
    return Position(
        position_id="pos-001",
        ticker="AAPL",
        contract=_contract(),
        entry_premium=Decimal(entry_premium),
        target_level=Decimal(target_level),
        opened_at=datetime(2026, 6, 28, 10, 0, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# No exit — all clear
# ---------------------------------------------------------------------------


class TestNoExit:
    def test_no_signal_when_all_clear(self):
        pos = _position()
        result = DEFAULT_MONITOR.evaluate(
            pos,
            current_price=Decimal("195"),   # below target 200
            current_premium=Decimal("3.50"),  # +16.7% (not stopped)
            dte=14,
            as_of=AS_OF,
        )
        assert result is None

    def test_no_signal_at_dte_just_above_floor(self):
        pos = _position()
        result = DEFAULT_MONITOR.evaluate(
            pos,
            current_price=Decimal("195"),
            current_premium=Decimal("3.00"),
            dte=8,
            as_of=AS_OF,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Profit target
# ---------------------------------------------------------------------------


class TestProfitTarget:
    def test_fires_when_price_equals_target(self):
        pos = _position(target_level="200")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("200"), current_premium=Decimal("5.00"), dte=14, as_of=AS_OF
        )
        assert result is not None
        assert result.reason == ExitReason.PROFIT_TARGET

    def test_fires_when_price_above_target(self):
        pos = _position(target_level="200")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("201"), current_premium=Decimal("5.20"), dte=14, as_of=AS_OF
        )
        assert result.reason == ExitReason.PROFIT_TARGET

    def test_does_not_fire_below_target(self):
        pos = _position(target_level="200")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("199.99"), current_premium=Decimal("4.80"), dte=14, as_of=AS_OF
        )
        assert result is None

    def test_signal_contains_correct_pnl_pct(self):
        pos = _position(entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("200"), current_premium=Decimal("4.50"), dte=14, as_of=AS_OF
        )
        assert result.pnl_pct == pytest.approx(0.50, rel=1e-4)  # +50%

    def test_signal_carries_position_id(self):
        pos = _position()
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("200"), current_premium=Decimal("5.00"), dte=14, as_of=AS_OF
        )
        assert result.position_id == "pos-001"

    def test_signal_as_of_uses_provided_timestamp(self):
        pos = _position()
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("200"), current_premium=Decimal("5.00"), dte=14, as_of=AS_OF
        )
        assert result.as_of == AS_OF


# ---------------------------------------------------------------------------
# Stop loss
# ---------------------------------------------------------------------------


class TestStopLoss:
    def test_fires_at_35_pct_loss_exactly(self):
        pos = _position(entry_premium="3.00")
        # current = 3.00 × (1 - 0.35) = 1.95
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("190"), current_premium=Decimal("1.95"), dte=14, as_of=AS_OF
        )
        assert result.reason == ExitReason.STOP_LOSS

    def test_fires_when_loss_exceeds_threshold(self):
        pos = _position(entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("188"), current_premium=Decimal("1.50"), dte=14, as_of=AS_OF
        )
        assert result.reason == ExitReason.STOP_LOSS

    def test_does_not_fire_below_threshold(self):
        pos = _position(entry_premium="3.00")
        # -34% → above the -35% threshold, should NOT stop
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("191"), current_premium=Decimal("1.98"), dte=14, as_of=AS_OF
        )
        assert result is None

    def test_stop_signal_pnl_pct_is_negative(self):
        pos = _position(entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("188"), current_premium=Decimal("1.50"), dte=14, as_of=AS_OF
        )
        assert result.pnl_pct < 0

    def test_custom_stop_loss_pct(self):
        monitor = ExitMonitor(stop_loss_pct=0.50)
        pos = _position(entry_premium="3.00")
        # -35% should NOT fire with 50% threshold
        result = monitor.evaluate(
            pos, current_price=Decimal("190"), current_premium=Decimal("1.95"), dte=14, as_of=AS_OF
        )
        assert result is None
        # -50% exactly should fire
        result = monitor.evaluate(
            pos, current_price=Decimal("185"), current_premium=Decimal("1.50"), dte=14, as_of=AS_OF
        )
        assert result.reason == ExitReason.STOP_LOSS


# ---------------------------------------------------------------------------
# DTE stop
# ---------------------------------------------------------------------------


class TestDTEStop:
    def test_fires_at_dte_floor(self):
        pos = _position()
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("3.00"), dte=7, as_of=AS_OF
        )
        assert result.reason == ExitReason.DTE_STOP

    def test_fires_below_dte_floor(self):
        pos = _position()
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("2.50"), dte=3, as_of=AS_OF
        )
        assert result.reason == ExitReason.DTE_STOP

    def test_does_not_fire_above_dte_floor(self):
        pos = _position()
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("3.00"), dte=8, as_of=AS_OF
        )
        assert result is None

    def test_custom_dte_floor(self):
        monitor = ExitMonitor(dte_floor=14)
        pos = _position()
        result = monitor.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("3.00"), dte=14, as_of=AS_OF
        )
        assert result.reason == ExitReason.DTE_STOP
        result = monitor.evaluate(
            pos, current_price=Decimal("195"), current_premium=Decimal("3.00"), dte=15, as_of=AS_OF
        )
        assert result is None


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------


class TestPriority:
    def test_profit_target_over_stop_loss(self):
        # Both triggered: price at wall + premium cratered (somehow — edge case)
        pos = _position(target_level="200", entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos,
            current_price=Decimal("200"),   # at target → profit_target
            current_premium=Decimal("1.50"),  # -50% → stop_loss would also fire
            dte=14,
            as_of=AS_OF,
        )
        assert result.reason == ExitReason.PROFIT_TARGET

    def test_profit_target_over_dte_stop(self):
        pos = _position(target_level="200")
        result = DEFAULT_MONITOR.evaluate(
            pos,
            current_price=Decimal("200"),  # at target
            current_premium=Decimal("5.00"),
            dte=3,                          # also below floor
            as_of=AS_OF,
        )
        assert result.reason == ExitReason.PROFIT_TARGET

    def test_stop_loss_over_dte_stop(self):
        pos = _position(entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos,
            current_price=Decimal("190"),   # below target, no profit_target
            current_premium=Decimal("1.50"),  # -50% → stop_loss
            dte=5,                           # also below floor
            as_of=AS_OF,
        )
        assert result.reason == ExitReason.STOP_LOSS

    def test_only_first_reason_returned(self):
        pos = _position(target_level="200", entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos,
            current_price=Decimal("200"),
            current_premium=Decimal("1.50"),
            dte=3,
            as_of=AS_OF,
        )
        # All three could fire; only PROFIT_TARGET returned
        assert result.reason == ExitReason.PROFIT_TARGET


# ---------------------------------------------------------------------------
# ExitSignal fields
# ---------------------------------------------------------------------------


class TestExitSignalFields:
    def test_entry_and_current_premium_preserved(self):
        pos = _position(entry_premium="3.00")
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("200"), current_premium=Decimal("5.00"), dte=14, as_of=AS_OF
        )
        assert result.entry_premium == Decimal("3.00")
        assert result.current_premium == Decimal("5.00")

    def test_dte_remaining_preserved(self):
        pos = _position()
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("200"), current_premium=Decimal("5.00"), dte=12, as_of=AS_OF
        )
        assert result.dte_remaining == 12

    def test_ticker_preserved(self):
        pos = _position()
        result = DEFAULT_MONITOR.evaluate(
            pos, current_price=Decimal("200"), current_premium=Decimal("5.00"), dte=14, as_of=AS_OF
        )
        assert result.ticker == "AAPL"

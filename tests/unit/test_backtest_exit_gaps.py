"""Regression tests for two backtest exit gaps fixed alongside the dynamic
exit work:

  - THESIS_INVALIDATED was structurally unreachable because should_exit()
    never passed current_setup= into ExitMonitor.evaluate().
  - TRAILING_STOP was structurally unreachable because BacktestPosition had
    no peak_premium field, so as_exit_position() always produced a Position
    with peak_premium=None.

Both are now exercised end-to-end through StandardPolicy.should_exit(), the
exact method BacktestHarness.run() calls each simulated day.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from trader.backtest.data_store import BacktestDataSlice
from trader.backtest.policy import StandardPolicy
from trader.backtest.schemas import BacktestPosition
from trader.exits.schemas import ExitReason
from trader.uw.schemas import OptionContract

TICKER = "AAPL"
FAR_EXPIRY = date(2027, 6, 18)  # always far beyond dte_floor from any trade_date used here


def _contract(type_: str = "call", strike: Decimal = Decimal("100")) -> OptionContract:
    return OptionContract(
        ticker=TICKER, expiry=FAR_EXPIRY, strike=strike, type=type_,
        bid=Decimal("2.95"), ask=Decimal("3.05"),
        open_interest=1000, volume=500,
    )


def _position(contract: OptionContract, entry_premium: Decimal = Decimal("3.00"),
              target_level: Decimal = Decimal("500"), peak_premium: Decimal | None = None
              ) -> BacktestPosition:
    return BacktestPosition(
        position_id="bt-pos-1", ticker=TICKER, contract=contract,
        entry_premium=entry_premium, target_level=target_level,
        opened_on=date(2026, 1, 2), peak_premium=peak_premium,
    )


def _darkpool_raw(spot: Decimal, when: str) -> dict:
    return {"data": [{
        "ticker": TICKER, "price": str(spot), "size": 100, "premium": "10000",
        "executed_at": when, "market_center": "X",
    }]}


def _option_contracts_raw(contract: OptionContract, mid: Decimal) -> dict:
    half_spread = Decimal("0.05")
    return {"data": [{
        "ticker": contract.ticker, "expiry": contract.expiry.isoformat(),
        "strike": str(contract.strike), "type": contract.type,
        "bid": str(mid - half_spread), "ask": str(mid + half_spread),
        "open_interest": 1000, "volume": 500,
    }]}


def _spot_gex_raw_favoring_put(spot: Decimal) -> dict:
    """Strikes engineered so GEXDetector resolves regime=NEGATIVE,
    candidate_direction='put' with spot below the interpolated flip point —
    i.e. the opposite side of a held 'call' position."""
    return {"data": [
        {"price": "85", "call_gamma_oi": "0", "put_gamma_oi": "-150"},
        {"price": "90", "call_gamma_oi": "0", "put_gamma_oi": "-100"},
        {"price": "100", "call_gamma_oi": "0", "put_gamma_oi": "-50"},
        {"price": "110", "call_gamma_oi": "150", "put_gamma_oi": "0"},
    ]}


class TestThesisInvalidatedReachableInBacktest:
    def test_fires_when_live_setup_flips_against_held_direction(self):
        policy = StandardPolicy()
        contract = _contract(type_="call")
        # target_level far away so PROFIT_TARGET can't preempt the check;
        # entry == current premium so STOP_LOSS/TRAILING_STOP can't either.
        position = _position(contract, entry_premium=Decimal("3.00"),
                              target_level=Decimal("500"))
        data_slice = BacktestDataSlice(
            date=date(2026, 1, 5),
            tickers=[TICKER],
            spot_gex_raw={TICKER: _spot_gex_raw_favoring_put(Decimal("100"))},
            option_contracts_raw={TICKER: _option_contracts_raw(contract, Decimal("3.00"))},
            darkpool_raw={TICKER: _darkpool_raw(Decimal("100"), "2026-01-05T15:00:00Z")},
        )

        signal = policy.should_exit(position, data_slice)

        assert signal is not None
        assert signal.reason == ExitReason.THESIS_INVALIDATED

    def test_resolved_setup_direction_is_actually_put(self):
        # Sanity check on the fixture itself, independent of should_exit —
        # guards against the test accidentally passing for the wrong reason.
        from trader.gex.detector import GEXDetector
        from trader.uw.validators import parse_spot_gex_by_strike

        raw = _spot_gex_raw_favoring_put(Decimal("100"))
        spot_gex = parse_spot_gex_by_strike(raw)
        setup = GEXDetector().detect(TICKER, spot_gex, Decimal("100"))
        assert setup.candidate_direction == "put"


class TestTrailingStopReachableInBacktest:
    def test_fires_after_giveback_from_a_peak_tracked_across_days(self):
        policy = StandardPolicy()
        contract = _contract(type_="call")
        position = _position(contract, entry_premium=Decimal("3.00"),
                              target_level=Decimal("500"))

        # Day 1: premium runs up to 6.00 — activates the trailing stop
        # (default activation 30% over entry = 3.90) but doesn't itself
        # exit. The harness updates peak_premium on the position before
        # calling should_exit; replicate that here.
        day1 = BacktestDataSlice(
            date=date(2026, 1, 5), tickers=[TICKER],
            option_contracts_raw={TICKER: _option_contracts_raw(contract, Decimal("6.00"))},
            darkpool_raw={TICKER: _darkpool_raw(Decimal("100"), "2026-01-05T15:00:00Z")},
        )
        current_premium = day1.get_option_premium(contract)
        assert current_premium == Decimal("6.00")
        if position.peak_premium is None or current_premium > position.peak_premium:
            position.peak_premium = current_premium
        signal = policy.should_exit(position, day1)
        assert signal is None
        assert position.peak_premium == Decimal("6.00")

        # Day 2: gives back to 4.40, below the giveback floor
        # (3.00 + (6.00-3.00)*(1-0.50) = 4.50) — trailing stop should fire.
        day2 = BacktestDataSlice(
            date=date(2026, 1, 6), tickers=[TICKER],
            option_contracts_raw={TICKER: _option_contracts_raw(contract, Decimal("4.40"))},
            darkpool_raw={TICKER: _darkpool_raw(Decimal("100"), "2026-01-06T15:00:00Z")},
        )
        current_premium = day2.get_option_premium(contract)
        if position.peak_premium is None or current_premium > position.peak_premium:
            position.peak_premium = current_premium
        signal = policy.should_exit(position, day2)

        assert signal is not None
        assert signal.reason == ExitReason.TRAILING_STOP

    def test_peak_premium_survives_onto_exit_position_snapshot(self):
        contract = _contract(type_="call")
        position = _position(contract, peak_premium=Decimal("7.25"))
        assert position.as_exit_position().peak_premium == Decimal("7.25")

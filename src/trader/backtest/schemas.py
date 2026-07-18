from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from trader.exits.schemas import ExitSignal
    from trader.scoring.schemas import CandidateSignal
    from trader.uw.schemas import OptionContract


@dataclass
class BacktestPosition:
    """An open position tracked by the harness during replay."""

    position_id: str
    ticker: str
    contract: "OptionContract"
    entry_premium: Decimal
    target_level: Decimal
    opened_on: date
    contracts: int = 1          # number of option contracts entered
    sector: str | None = None

    @property
    def entry_cost(self) -> Decimal:
        """Total cash outlay: entry_premium × contracts × 100 shares/contract."""
        return self.entry_premium * self.contracts * 100

    def as_exit_position(self):
        """Return exits/schemas.Position for use with ExitMonitor.evaluate()."""
        from trader.exits.schemas import Position

        return Position(
            position_id=self.position_id,
            ticker=self.ticker,
            contract=self.contract,
            entry_premium=self.entry_premium,
            target_level=self.target_level,
            opened_at=datetime.combine(self.opened_on, time(9, 30), tzinfo=timezone.utc),
            sector=self.sector,
        )

    @classmethod
    def from_candidate(cls, candidate: "CandidateSignal", entry_date: date,
                       contracts: int = 1) -> BacktestPosition:
        contract = candidate.selected_contract
        assert contract is not None, "selected_contract must not be None at entry"
        target = candidate.gex_setup.target_level
        if target is None:
            target = contract.strike
        return cls(
            position_id=str(uuid.uuid4()),
            ticker=candidate.ticker,
            contract=contract,
            entry_premium=contract.mid,
            target_level=target,
            opened_on=entry_date,
            contracts=contracts,
        )


@dataclass
class BacktestTradeRecord:
    """Full lifecycle record for one backtest trade."""

    position: BacktestPosition
    candidate: "CandidateSignal"
    entry_date: date
    exit_date: date | None = None
    exit_signal: "ExitSignal | None" = None
    pnl_pct: float | None = None
    pnl_dollars: float | None = None   # actual dollar gain/loss for this trade
    status: Literal["open", "closed", "expired"] = "open"

    def close(self, signal: "ExitSignal", exit_date: date) -> None:
        self.exit_date = exit_date
        self.exit_signal = signal
        self.pnl_pct = signal.pnl_pct
        self.pnl_dollars = signal.pnl_pct * float(self.position.entry_cost)
        self.status = "closed"

    def expire(self, as_of_date: date) -> None:
        """Mark position as still open at end of backtest window."""
        self.exit_date = as_of_date
        self.status = "expired"

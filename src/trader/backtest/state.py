"""Resumable state for incremental day-by-day backtest replay.

BacktestHarness.run() is a one-shot, stateless replay: fresh positions,
fresh cash, walks the whole window every call. ReplayState instead persists
across separate step_forward() calls (e.g. one per night) so a cumulative
track record can be built up over time without re-walking history that's
already been processed — see BacktestHarness.step_forward().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from trader.exits.schemas import ExitSignal
from trader.scoring.schemas import CandidateSignal
from trader.uw.schemas import OptionContract

from .schemas import BacktestPosition, BacktestTradeRecord


def _position_to_dict(pos: BacktestPosition) -> dict[str, Any]:
    return {
        "position_id": pos.position_id,
        "ticker": pos.ticker,
        "contract": pos.contract.model_dump(mode="json"),
        "entry_premium": str(pos.entry_premium),
        "target_level": str(pos.target_level),
        "opened_on": pos.opened_on.isoformat(),
        "contracts": pos.contracts,
        "sector": pos.sector,
    }


def _position_from_dict(data: dict[str, Any]) -> BacktestPosition:
    return BacktestPosition(
        position_id=data["position_id"],
        ticker=data["ticker"],
        contract=OptionContract.model_validate(data["contract"]),
        entry_premium=Decimal(data["entry_premium"]),
        target_level=Decimal(data["target_level"]),
        opened_on=date.fromisoformat(data["opened_on"]),
        contracts=data["contracts"],
        sector=data.get("sector"),
    )


def _record_to_dict(r: BacktestTradeRecord) -> dict[str, Any]:
    return {
        "position": _position_to_dict(r.position),
        "candidate": r.candidate.model_dump(mode="json"),
        "entry_date": r.entry_date.isoformat(),
        "exit_date": r.exit_date.isoformat() if r.exit_date else None,
        "exit_signal": r.exit_signal.model_dump(mode="json") if r.exit_signal else None,
        "pnl_pct": r.pnl_pct,
        "pnl_dollars": r.pnl_dollars,
        "status": r.status,
    }


def _record_from_dict(data: dict[str, Any]) -> BacktestTradeRecord:
    return BacktestTradeRecord(
        position=_position_from_dict(data["position"]),
        candidate=CandidateSignal.model_validate(data["candidate"]),
        entry_date=date.fromisoformat(data["entry_date"]),
        exit_date=date.fromisoformat(data["exit_date"]) if data.get("exit_date") else None,
        exit_signal=ExitSignal.model_validate(data["exit_signal"]) if data.get("exit_signal") else None,
        pnl_pct=data.get("pnl_pct"),
        pnl_dollars=data.get("pnl_dollars"),
        status=data.get("status", "open"),
    )


@dataclass
class ReplayState:
    """In-progress replay state, resumable across step_forward() calls.

    open_positions is never serialized directly — it's a subset of the
    positions already present in `records` (every position that was ever
    opened gets a record; only the still-open ones are also referenced
    here), so round-tripping just needs the position ids.
    """

    open_positions: list[BacktestPosition] = field(default_factory=list)
    records: list[BacktestTradeRecord] = field(default_factory=list)
    cash: Decimal = Decimal("0")
    equity_curve: list[tuple[date, float]] = field(default_factory=list)
    processed_dates: list[date] = field(default_factory=list)

    @property
    def record_by_id(self) -> dict[str, BacktestTradeRecord]:
        return {r.position.position_id: r for r in self.records}

    def to_dict(self) -> dict[str, Any]:
        return {
            "open_position_ids": [p.position_id for p in self.open_positions],
            "records": [_record_to_dict(r) for r in self.records],
            "cash": str(self.cash),
            "equity_curve": [[d.isoformat(), v] for d, v in self.equity_curve],
            "processed_dates": [d.isoformat() for d in self.processed_dates],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReplayState":
        records = [_record_from_dict(r) for r in data.get("records", [])]
        by_id = {r.position.position_id: r for r in records}
        open_positions = [
            by_id[pid].position for pid in data.get("open_position_ids", []) if pid in by_id
        ]
        return cls(
            open_positions=open_positions,
            records=records,
            cash=Decimal(data.get("cash", "0")),
            equity_curve=[(date.fromisoformat(d), v) for d, v in data.get("equity_curve", [])],
            processed_dates=[date.fromisoformat(d) for d in data.get("processed_dates", [])],
        )

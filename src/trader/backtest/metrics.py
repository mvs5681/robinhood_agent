from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .schemas import BacktestTradeRecord


@dataclass
class TradeMetrics:
    """Aggregate statistics for a slice of backtest trades."""

    trade_count: int = 0
    closed_count: int = 0
    win_count: int = 0
    win_rate: float = 0.0
    avg_pnl_pct: float = 0.0
    max_drawdown: float = 0.0   # worst single-trade loss (negative or 0)
    total_pnl_pct: float = 0.0


@dataclass
class BacktestResult:
    """Full results from one backtest run."""

    records: list["BacktestTradeRecord"]
    overall: TradeMetrics
    by_regime: dict[str, TradeMetrics] = field(default_factory=dict)
    by_setup_type: dict[str, TradeMetrics] = field(default_factory=dict)
    by_regime_and_setup: dict[str, TradeMetrics] = field(default_factory=dict)


def _compute_metrics(records: list["BacktestTradeRecord"]) -> TradeMetrics:
    if not records:
        return TradeMetrics()

    closed = [r for r in records if r.status == "closed" and r.pnl_pct is not None]
    pnls = [r.pnl_pct for r in closed]  # type: ignore[misc]

    win_count = sum(1 for p in pnls if p > 0)
    total = sum(pnls)
    avg = total / len(pnls) if pnls else 0.0
    drawdown = min(pnls) if pnls else 0.0
    win_rate = win_count / len(closed) if closed else 0.0

    return TradeMetrics(
        trade_count=len(records),
        closed_count=len(closed),
        win_count=win_count,
        win_rate=win_rate,
        avg_pnl_pct=avg,
        max_drawdown=drawdown,
        total_pnl_pct=total,
    )


def compute_backtest_result(records: list["BacktestTradeRecord"]) -> BacktestResult:
    """Compute overall + sliced metrics from a list of trade records."""
    overall = _compute_metrics(records)

    by_regime: dict[str, list] = {}
    by_setup: dict[str, list] = {}
    by_combo: dict[str, list] = {}

    for r in records:
        regime = r.candidate.gex_setup.regime.value
        setup = r.candidate.gex_setup.setup_type
        combo = f"{regime}:{setup}"

        by_regime.setdefault(regime, []).append(r)
        by_setup.setdefault(setup, []).append(r)
        by_combo.setdefault(combo, []).append(r)

    return BacktestResult(
        records=records,
        overall=overall,
        by_regime={k: _compute_metrics(v) for k, v in by_regime.items()},
        by_setup_type={k: _compute_metrics(v) for k, v in by_setup.items()},
        by_regime_and_setup={k: _compute_metrics(v) for k, v in by_combo.items()},
    )

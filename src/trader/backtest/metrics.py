from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
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
class PortfolioMetrics:
    """Dollar-level simulation results for a fixed starting capital."""

    initial_capital: float
    final_value: float
    total_pnl_dollars: float
    total_return_pct: float           # (final - initial) / initial
    peak_value: float
    max_drawdown_dollars: float       # largest peak-to-trough drop (negative)
    max_drawdown_pct: float           # max_drawdown_dollars / peak_value
    equity_curve: list[tuple[date, float]] = field(default_factory=list)


@dataclass
class BacktestResult:
    """Full results from one backtest run."""

    records: list["BacktestTradeRecord"]
    overall: TradeMetrics
    by_regime: dict[str, TradeMetrics] = field(default_factory=dict)
    by_setup_type: dict[str, TradeMetrics] = field(default_factory=dict)
    by_regime_and_setup: dict[str, TradeMetrics] = field(default_factory=dict)
    portfolio: PortfolioMetrics | None = None


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


def compute_portfolio_metrics(
    equity_curve: list[tuple[date, float]],
    initial_capital: float,
) -> PortfolioMetrics:
    """Derive dollar-level metrics from a daily equity curve."""
    if not equity_curve:
        return PortfolioMetrics(
            initial_capital=initial_capital,
            final_value=initial_capital,
            total_pnl_dollars=0.0,
            total_return_pct=0.0,
            peak_value=initial_capital,
            max_drawdown_dollars=0.0,
            max_drawdown_pct=0.0,
            equity_curve=[],
        )

    final_value = equity_curve[-1][1]
    peak = initial_capital
    max_dd_dollars = 0.0

    for _, value in equity_curve:
        if value > peak:
            peak = value
        dd = value - peak
        if dd < max_dd_dollars:
            max_dd_dollars = dd

    max_dd_pct = max_dd_dollars / peak if peak else 0.0
    total_return = (final_value - initial_capital) / initial_capital if initial_capital else 0.0

    return PortfolioMetrics(
        initial_capital=initial_capital,
        final_value=final_value,
        total_pnl_dollars=final_value - initial_capital,
        total_return_pct=total_return,
        peak_value=peak,
        max_drawdown_dollars=max_dd_dollars,
        max_drawdown_pct=max_dd_pct,
        equity_curve=equity_curve,
    )


def compute_backtest_result(
    records: list["BacktestTradeRecord"],
    equity_curve: list[tuple[date, float]] | None = None,
    initial_capital: float | None = None,
) -> BacktestResult:
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

    portfolio: PortfolioMetrics | None = None
    if equity_curve is not None and initial_capital is not None:
        portfolio = compute_portfolio_metrics(equity_curve, initial_capital)

    return BacktestResult(
        records=records,
        overall=overall,
        by_regime={k: _compute_metrics(v) for k, v in by_regime.items()},
        by_setup_type={k: _compute_metrics(v) for k, v in by_setup.items()},
        by_regime_and_setup={k: _compute_metrics(v) for k, v in by_combo.items()},
        portfolio=portfolio,
    )

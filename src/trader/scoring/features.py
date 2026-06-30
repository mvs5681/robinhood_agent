"""
Five independently-testable signal feature functions.

Each returns a float in [0, 1].  Higher is always better for a long position
in the given direction — the caller (BlendScorer) weights and combines them.

No I/O — all inputs are pre-fetched Pydantic objects from state.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from trader.uw.schemas import (
    DarkpoolPrint,
    FlowAlert,
    InterpolatedIVEntry,
    MarketTide,
    NetPremTick,
    TechnicalPoint,
)

# Target DTE horizon used for IV percentile lookup (matches 21-45 DTE entry window)
_IV_TARGET_DAYS = 30

# Default darkpool premium normalisation cap (parameterisable via scorer)
_DEFAULT_DP_CAP = Decimal("5_000_000")


# ---------------------------------------------------------------------------
# 1. Market Tide
# ---------------------------------------------------------------------------


def market_tide_score(ticks: Sequence[MarketTide], direction: str) -> float:
    """
    Measures whether the broad market is flowing call-heavy (bullish) or put-heavy.

    Uses the last 30 ticks.  Net bias = (call_sum + put_sum) / abs_total maps to [-1, +1].
    Converts to [0, 1] for the given direction.
    """
    if not ticks:
        return 0.5

    recent = list(ticks)[-30:]
    call_sum = sum(float(t.net_call_premium) for t in recent)
    put_sum = sum(float(t.net_put_premium) for t in recent)   # already negative
    abs_total = abs(call_sum) + abs(put_sum)

    if abs_total == 0:
        return 0.5

    net_bias = (call_sum + put_sum) / abs_total  # [-1, +1]

    if direction == "call":
        return _clamp((net_bias + 1) / 2)
    else:
        return _clamp((1 - net_bias) / 2)


# ---------------------------------------------------------------------------
# 2. Darkpool
# ---------------------------------------------------------------------------


def darkpool_score(
    prints: Sequence[DarkpoolPrint],
    premium_cap: Decimal = _DEFAULT_DP_CAP,
) -> float:
    """
    Measures institutional darkpool conviction via total non-canceled premium.

    Direction-agnostic: heavy darkpool activity supports the thesis regardless
    of direction (institutions rarely telegraph side via DP prints).
    Score = min(total_premium / cap, 1).
    """
    active = [p for p in prints if not p.canceled]
    if not active:
        return 0.0

    total = sum(p.premium for p in active)
    return _clamp(float(total / premium_cap))


# ---------------------------------------------------------------------------
# 3. Flow Pressure
# ---------------------------------------------------------------------------


def flow_pressure_score(
    alerts: Sequence[FlowAlert],
    net_prem_ticks: Sequence[NetPremTick],
    ticker: str,
    direction: str,
) -> float:
    """
    Combines two sub-signals:
    - Alert directional fraction (60% weight): what % of this ticker's flow
      alerts match the candidate direction?
    - Net-premium tick momentum (40% weight): how many of the last 20 ticks
      show net call (or put) premium trending in the right direction?
    """
    ticker_alerts = [a for a in alerts if a.ticker == ticker]
    if ticker_alerts:
        matching = [a for a in ticker_alerts if a.type == direction]
        alert_pct = len(matching) / len(ticker_alerts)
    else:
        alert_pct = 0.5  # no data → neutral

    if net_prem_ticks:
        recent = list(net_prem_ticks)[-20:]
        if direction == "call":
            positive = sum(1 for t in recent if t.net_call_premium > 0)
        else:
            positive = sum(1 for t in recent if t.net_put_premium < 0)
        tick_momentum = positive / len(recent)
    else:
        tick_momentum = 0.5

    return _clamp(0.6 * alert_pct + 0.4 * tick_momentum)


# ---------------------------------------------------------------------------
# 4. IV Cost
# ---------------------------------------------------------------------------


def iv_cost_score(iv_entries: Sequence[InterpolatedIVEntry]) -> float:
    """
    Penalises entering when options are expensive relative to their 1-year history.

    Finds the DTE row closest to _IV_TARGET_DAYS (30) and reads its percentile.
    Score = 1 - percentile/100  →  1 = cheap, 0 = expensive.
    """
    if not iv_entries:
        return 0.5  # no data → neutral

    best = min(iv_entries, key=lambda e: abs(e.days - _IV_TARGET_DAYS))
    return _clamp(1.0 - float(best.percentile) / 100.0)


# ---------------------------------------------------------------------------
# 5. Technicals
# ---------------------------------------------------------------------------


def technicals_score(
    rsi_data: Sequence[TechnicalPoint],
    macd_data: Sequence[TechnicalPoint],
    direction: str,
) -> float:
    """
    Average of available sub-signals (RSI + MACD).  Returns 0.5 if neither available.
    """
    scores: list[float] = []

    if rsi_data:
        latest_rsi = rsi_data[-1]
        if latest_rsi.value is not None:
            scores.append(_rsi_score(float(latest_rsi.value), direction))

    if macd_data:
        latest_macd = macd_data[-1]
        if latest_macd.macd is not None and latest_macd.signal is not None:
            scores.append(_macd_score(float(latest_macd.macd), float(latest_macd.signal), direction))

    return _clamp(sum(scores) / len(scores)) if scores else 0.5


def _rsi_score(rsi: float, direction: str) -> float:
    """Map RSI value to a [0, 1] score for the given direction."""
    if direction == "call":
        if rsi < 30:   return 0.3   # extreme oversold — possible breakdown, not ideal
        if rsi < 50:   return 0.9   # oversold to neutral — best buy zone
        if rsi < 60:   return 0.7   # mild momentum — still good
        if rsi < 70:   return 0.4   # overbought approach — caution
        return 0.1                   # overbought — avoid
    else:  # "put"
        if rsi > 70:   return 0.9   # overbought — best short zone
        if rsi > 60:   return 0.7   # mild bearish momentum
        if rsi > 50:   return 0.4   # neutral
        if rsi > 30:   return 0.2   # oversold — weak put setup
        return 0.1                   # extreme oversold — avoid puts


def _macd_score(macd: float, signal: float, direction: str) -> float:
    bullish_cross = macd > signal
    if direction == "call":
        return 0.8 if bullish_cross else 0.2
    else:
        return 0.8 if not bullish_cross else 0.2


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))

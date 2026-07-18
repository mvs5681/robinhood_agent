"""Option price tick rounding.

Robinhood rejects limit prices that don't land on the contract's tick grid
("Price does not satisfy the min tick value"). Non-penny-pilot options tick
in $0.05 below a cutoff price and $0.10 above it; get_option_instruments
reports the rule per instrument as
    min_ticks: {"above_tick": "0.10", "below_tick": "0.05", "cutoff_price": "3.00"}

Prices are always rounded DOWN to the grid: for buys that keeps the limit at
or below the approved price; for sells it prices slightly more aggressively,
which is the safe direction for an exit.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

_PENNY = Decimal("0.01")


def round_price_to_tick(price: Decimal, min_ticks: dict | None) -> Decimal:
    """Floor `price` onto the instrument's tick grid (penny grid if unknown)."""
    tick = _PENNY
    if min_ticks:
        try:
            above = Decimal(str(min_ticks.get("above_tick", "0.01")))
            below = Decimal(str(min_ticks.get("below_tick", "0.01")))
            cutoff = Decimal(str(min_ticks.get("cutoff_price", "0")))
            tick = above if price >= cutoff else below
            if tick <= 0:
                tick = _PENNY
        except Exception:
            tick = _PENNY
    ticks = (price / tick).to_integral_value(rounding=ROUND_FLOOR)
    floored = ticks * tick
    return max(floored, tick)  # never round to zero

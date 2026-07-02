"""Market hours utilities (US Eastern time, NYSE schedule)."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Conservative window: skip first/last 15 min to avoid open/close volatility
MARKET_OPEN = time(9, 45)
MARKET_CLOSE = time(15, 30)


def now_et() -> datetime:
    return datetime.now(ET)


def is_market_hours() -> bool:
    """True during 09:45–15:30 ET on weekdays."""
    now = now_et()
    if now.weekday() >= 5:   # Saturday=5, Sunday=6
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def seconds_until_market_open() -> float:
    """Seconds until next market open (09:45 ET). Returns 0 if already open."""
    if is_market_hours():
        return 0.0
    now = now_et()
    # Try today first, then tomorrow (skip weekends)
    from datetime import timedelta
    candidate = now.replace(hour=9, minute=45, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return max(0.0, (candidate - now).total_seconds())

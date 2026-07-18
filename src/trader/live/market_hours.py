"""Market hours utilities (US Eastern time, NYSE schedule)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Conservative window: skip first/last 15 min to avoid open/close volatility
MARKET_OPEN = time(9, 45)
MARKET_CLOSE = time(15, 30)

# NYSE full-close holidays. Extend annually — a date missing from this set
# means the agent will run that day against stale/empty market data.
NYSE_HOLIDAYS: frozenset[date] = frozenset({
    # 2026
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # Martin Luther King Jr. Day
    date(2026, 2, 16),   # Washington's Birthday
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
    # 2027
    date(2027, 1, 1),    # New Year's Day
    date(2027, 1, 18),   # Martin Luther King Jr. Day
    date(2027, 2, 15),   # Washington's Birthday
    date(2027, 3, 26),   # Good Friday
    date(2027, 5, 31),   # Memorial Day
    date(2027, 6, 18),   # Juneteenth (observed)
    date(2027, 7, 5),    # Independence Day (observed)
    date(2027, 9, 6),    # Labor Day
    date(2027, 11, 25),  # Thanksgiving
    date(2027, 12, 24),  # Christmas (observed)
    date(2027, 12, 31),  # New Year's Day 2028 observed (Jan 1 falls on Saturday)
    # 2028
    date(2028, 1, 17),   # Martin Luther King Jr. Day
    date(2028, 2, 21),   # Washington's Birthday
    date(2028, 4, 14),   # Good Friday
    date(2028, 5, 29),   # Memorial Day
    date(2028, 6, 19),   # Juneteenth
    date(2028, 7, 4),    # Independence Day
    date(2028, 9, 4),    # Labor Day
    date(2028, 11, 23),  # Thanksgiving
    date(2028, 12, 25),  # Christmas
})


def now_et() -> datetime:
    return datetime.now(ET)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS


def is_market_hours() -> bool:
    """True during 09:45–15:30 ET on trading days (weekends and NYSE holidays excluded)."""
    now = now_et()
    if not is_trading_day(now.date()):
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def seconds_until_market_open() -> float:
    """Seconds until next market open (09:45 ET). Returns 0 if already open."""
    if is_market_hours():
        return 0.0
    now = now_et()
    candidate = now.replace(hour=9, minute=45, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    while not is_trading_day(candidate.date()):
        candidate += timedelta(days=1)
    return max(0.0, (candidate - now).total_seconds())

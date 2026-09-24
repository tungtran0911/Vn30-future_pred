"""Trading session phases for HNX derivatives.

Phase matters more here than in most markets: the 08:45-09:00 opening auction runs
before the cash index exists for the day, and the 14:30-14:45 closing auction sets
the price that expiry settles against. Bars that straddle a phase boundary are not
comparable to bars inside one, so every downstream feature gets a phase label rather
than being left to infer one from the clock.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from enum import Enum

from vn30f.config import (
    AFTERNOON_END,
    AFTERNOON_START,
    ATC_END,
    ATC_START,
    ATO_END,
    ATO_START,
    MORNING_END,
    MORNING_START,
    TZ,
)


class Phase(str, Enum):
    PRE_OPEN = "pre_open"
    ATO = "ato"
    MORNING = "morning"
    LUNCH = "lunch"
    AFTERNOON = "afternoon"
    ATC = "atc"
    CLOSED = "closed"


def now_vn() -> datetime:
    return datetime.now(TZ)


def phase_at(ts: datetime | time) -> Phase:
    t = ts.timetz().replace(tzinfo=None) if isinstance(ts, datetime) else ts
    if t < ATO_START:
        return Phase.PRE_OPEN
    if t < ATO_END:
        return Phase.ATO
    if t < MORNING_END:
        return Phase.MORNING
    if t < AFTERNOON_START:
        return Phase.LUNCH
    if t < AFTERNOON_END:
        return Phase.AFTERNOON
    if t < ATC_END:
        return Phase.ATC
    return Phase.CLOSED


def is_trading_phase(p: Phase) -> bool:
    """Phases where quotes and matches actually move."""
    return p in (Phase.ATO, Phase.MORNING, Phase.AFTERNOON, Phase.ATC)


def is_weekday(d: date) -> bool:
    return d.weekday() < 5


def is_session_open(ts: datetime | None = None) -> bool:
    ts = ts or now_vn()
    return is_weekday(ts.date()) and is_trading_phase(phase_at(ts))


def seconds_until_close(ts: datetime | None = None) -> float:
    ts = ts or now_vn()
    close = datetime.combine(ts.date(), ATC_END, tzinfo=TZ)
    return max(0.0, (close - ts).total_seconds())


def expiry_date(year: int, month: int) -> date:
    """Third Thursday of the contract month -- the last trading day."""
    d = date(year, month, 1)
    thursdays = [
        d + timedelta(days=i)
        for i in range(31)
        if (d + timedelta(days=i)).month == month
        and (d + timedelta(days=i)).weekday() == 3
    ]
    return thursdays[2]


def front_month_symbol(on: date | None = None) -> str:
    """The contract VN30F1M points at on a given date.

    Rolls the day after expiry, matching how the alias itself behaves, so a recorded
    VN30F1M series can be checked against this.
    """
    on = on or now_vn().date()
    y, m = on.year, on.month
    if on > expiry_date(y, m):
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return f"VN30F{y % 100:02d}{m:02d}"


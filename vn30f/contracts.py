"""Contract master and roll engine.

The problem this solves: `VN30F1M` is not an instrument. It is a label that points at
a different contract every month, and the provider splices those contracts together
without adjusting for the price gap between them. Differencing that series treats a
contract switch as a price move, which silently corrupts any return, volatility or
signal computed across a roll.

The roll gap is recoverable from data we actually hold. On expiry day the front and
second month both trade, so the gap between the outgoing and incoming contract is
just the calendar spread that day:

    gap = F2M(expiry) - F1M(expiry)

Verified on the 2026-07-16 expiry: spread was -1.0, and the following day's F1M move
is the incoming contract's own return rather than an artifact. Back-adjustment shifts
each historical segment by the sum of gaps that come after it, leaving the most recent
segment untouched:

    adjusted(t) = raw(t) + sum(gap_r for rolls r occurring after t)

Both series are kept. Raw is what actually traded and is what any execution or
price-level claim must use; adjusted is the only one whose returns are continuous.
Using the wrong one is a silent error in either direction, so neither is the default.

Known weakness: the expiring contract's last close is the thinnest price in the
series. On 2020-05-21 it printed at its +7% band limit (864.0 against 786.0 for the
next contract), a -78 point "gap" that is almost certainly not carry. Measured one
trading day before expiry, the 95 observed gaps sum to -160 points instead of -311.
Rolling a day early would avoid the expiry print; it is not applied because no
reported result depends on the multi-year adjusted series.
"""

from __future__ import annotations

import pandas as pd

from vn30f.calendar_vn import expiry_date, front_month_symbol


def roll_dates(start: str | pd.Timestamp, end: str | pd.Timestamp) -> list[pd.Timestamp]:
    """Expiry dates in [start, end]. The last trading day of each front month."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    out = []
    y, m = start.year, start.month
    while pd.Timestamp(y, m, 1) <= end:
        d = pd.Timestamp(expiry_date(y, m))
        if start <= d <= end:
            out.append(d)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def contract_master(dates: pd.DatetimeIndex) -> pd.DataFrame:
    """Which contract F1M and F2M denote on each date, and distance to expiry.

    Days-to-expiry matters beyond bookkeeping: liquidity, basis and expiry-day
    microstructure all move with it, so it is a feature, not just an index.
    """
    rows = []
    for ts in dates:
        d = ts.date()
        front = front_month_symbol(d)
        fy, fm = 2000 + int(front[5:7]), int(front[7:9])
        sy, sm = (fy, fm + 1) if fm < 12 else (fy + 1, 1)
        exp = pd.Timestamp(expiry_date(fy, fm))
        rows.append({
            "date": ts,
            "front": front,
            "second": f"VN30F{sy % 100:02d}{sm:02d}",
            "front_expiry": exp,
            "days_to_expiry": (exp - ts).days,
            "is_expiry": ts == exp,
        })
    return pd.DataFrame(rows)


def build_continuous(bars: pd.DataFrame, price_col: str = "close") -> pd.DataFrame:
    """Splice F1M into a continuous series, raw and back-adjusted.

    `bars` needs columns time, symbol, <price_col> and must contain both VN30F1M and
    VN30F2M from the SAME source: the gap is a difference between two prices, so
    mixing sources would put a cross-provider discrepancy into the adjustment.

    Rolls with no observed spread on expiry day (a holiday, or missing data) get a
    zero gap and are flagged rather than dropped or interpolated, because a wrong gap
    is worse than a known-missing one.
    """
    need = {"time", "symbol", price_col}
    missing = need - set(bars.columns)
    if missing:
        raise ValueError(f"bars is missing {sorted(missing)}")

    wide = (bars.pivot_table(index="time", columns="symbol", values=price_col,
                             aggfunc="last")
                .sort_index())
    for sym in ("VN30F1M", "VN30F2M"):
        if sym not in wide.columns:
            raise ValueError(f"bars must contain {sym}; got {list(wide.columns)}")

    rolls = [d for d in roll_dates(wide.index.min(), wide.index.max())]
    gaps: list[dict] = []
    for r in rolls:
        # Bars may be timestamped intraday; match on calendar date.
        same_day = wide[wide.index.normalize() == r.normalize()]
        if same_day.empty or same_day[["VN30F1M", "VN30F2M"]].isna().all(axis=None):
            gaps.append({"roll": r, "gap": 0.0, "observed": False})
            continue
        last = same_day.iloc[-1]
        if pd.isna(last["VN30F1M"]) or pd.isna(last["VN30F2M"]):
            gaps.append({"roll": r, "gap": 0.0, "observed": False})
            continue
        gaps.append({"roll": r, "gap": float(last["VN30F2M"] - last["VN30F1M"]),
                     "observed": True})

    out = pd.DataFrame({"time": wide.index, "raw": wide["VN30F1M"].to_numpy()})
    # A bar is shifted by the gaps of every roll AT OR AFTER its own date.
    #
    # The boundary is easy to get wrong by one bar: on expiry day the front month is
    # STILL the outgoing contract -- it trades until that day's close -- so the expiry
    # bar belongs to the old segment and must be shifted with it. Using a strict
    # "after" comparison leaves the expiry bar unshifted, which does not remove the
    # roll jump but merely moves it one bar earlier.
    days = out["time"].dt.normalize()
    adjustment = [sum(g["gap"] for g in gaps if g["roll"].normalize() >= d)
                  for d in days]
    out["cum_adjustment"] = adjustment
    out["adjusted"] = out["raw"] + out["cum_adjustment"]
    roll_days = {g["roll"].normalize() for g in gaps}
    out["is_roll"] = out["time"].dt.normalize().isin(roll_days)
    return out, pd.DataFrame(gaps)


def load_daily_bars(path: str, source: str = "vci") -> pd.DataFrame:
    """Daily alias bars from the backfill file, one source only."""
    d = pd.read_parquet(path)
    d["time"] = pd.to_datetime(d["time"])
    return d[(d["source"] == source)
             & (d["symbol"].isin(["VN30F1M", "VN30F2M"]))].copy()

"""The cost floor against the size of VN30F price moves; the roll; the basis.

Descriptive facts from bar data, which has a year of 1-minute and eight years of
daily history -- far more than the recorded tick sessions.

  1. Move size by horizon against the round-trip cost. Moves are measured inside
     one continuous block (09:00-11:30 or 13:00-14:30) on 1-minute closes of the
     front month, so none spans lunch, an auction or a night. Closes carry
     bid-ask bounce, which inflates move size -- the comparison flatters
     intraday trading, not the reverse.
  2. The roll: size of the splice gaps the continuous series has to remove.
  3. The basis (futures - index): level, and convergence toward expiry.

    python -m vn30f.studies.cost_horizon
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from vn30f.config import (AFTERNOON_END, AFTERNOON_START, MORNING_END,
                          MORNING_START, RAW, TICK_SIZE)
from vn30f.contracts import build_continuous, contract_master, load_daily_bars
from vn30f.execution.cost_model import CostModel
from vn30f.features.basis import basis_1m

HORIZONS_MIN = (1, 5, 15, 30)
SPREAD_TICKS = 2.0    # median quoted front-month spread in the recorded book


def _bars_1m() -> pd.DataFrame:
    b = pd.read_parquet(RAW / "bars_1m" / "bars.parquet")
    b["time"] = pd.to_datetime(b["time"])
    return b[b["source"] == "vci"]


def move_table() -> None:
    b = _bars_1m()
    b = b[b["symbol"] == "VN30F1M"].sort_values("time")
    t = b["time"].dt.time
    b["block"] = np.select(
        [(t >= MORNING_START) & (t < MORNING_END),
         (t >= AFTERNOON_START) & (t < AFTERNOON_END)], ["AM", "PM"], "")
    b = b[b["block"] != ""][["time", "close", "block"]]
    cost = CostModel()
    b["cost"] = b["close"].map(cost.breakeven_ticks)

    first, last = b["time"].min().date(), b["time"].max().date()
    print(f"## Move size vs cost, VN30F1M 1-minute closes, {first} to {last}\n")
    print(f"round-trip fees + tax: median {b['cost'].median():.2f} ticks "
          f"(range {b['cost'].min():.2f}-{b['cost'].max():.2f}, it scales with the "
          f"index level); taker adds ~{SPREAD_TICKS:.0f} ticks of spread\n")
    print("| horizon | median abs move | mean abs move | P(move >= fees+tax) "
          "| P(move >= fees+tax+spread) | n |")
    print("|---|---|---|---|---|---|")
    later = b.rename(columns={"time": "t2", "close": "c2", "block": "b2"})[
        ["t2", "c2", "b2"]]
    for h in HORIZONS_MIN:
        m = (b.assign(t2=b["time"] + pd.Timedelta(minutes=h))
              .merge(later, on="t2", how="inner"))
        m = m[m["block"] == m["b2"]]
        mv = (m["c2"] - m["close"]).abs() / TICK_SIZE
        print(f"| {h} min | {mv.median():.1f} | {mv.mean():.1f} "
              f"| {(mv >= m['cost']).mean():.1%} "
              f"| {(mv >= m['cost'] + SPREAD_TICKS).mean():.1%} | {len(m):,} |")


def roll_facts() -> None:
    bars = load_daily_bars(str(RAW / "bars_1D" / "bars.parquet"), source="vci")
    cont, gaps = build_continuous(bars)
    obs = gaps[gaps["observed"]]
    day = cont["time"].dt.normalize()
    crossing = day.shift(1).isin(set(gaps["roll"].dt.normalize())).to_numpy(bool)
    raw, adj = cont["raw"].diff().abs(), cont["adjusted"].diff().abs()
    print(f"\n## Roll, daily bars {cont['time'].min().date()} to "
          f"{cont['time'].max().date()}\n")
    print(f"- {len(gaps)} rolls; {len(obs)} with an observed expiry-day spread, "
          f"{len(gaps) - len(obs)} without (zero gap, flagged)")
    print(f"- gap mean {obs['gap'].mean():+.2f} pts, sd {obs['gap'].std():.2f}, "
          f"range {obs['gap'].min():+.1f} to {obs['gap'].max():+.1f}")
    print(f"- cumulative adjustment on the oldest bar: "
          f"{cont['cum_adjustment'].iloc[0]:+.1f} pts")
    print(f"- median abs daily move on roll-crossing days: raw "
          f"{raw[crossing].median():.2f} pts, adjusted {adj[crossing].median():.2f}; "
          f"ordinary days {adj[~crossing].median():.2f}")

    # The expiring contract's last close is the weakest price in the series: it
    # converges on a settlement average, trades thinly, and can print at the band
    # limit. The same spread one trading day earlier measures how much of the gap
    # distribution is expiry-day noise rather than genuine carry.
    wide = bars.pivot_table(index="time", columns="symbol", values="close").sort_index()
    spread = (wide["VN30F2M"] - wide["VN30F1M"]).dropna()
    day_before = pd.Series([spread[spread.index < r].iloc[-1]
                            for r in obs["roll"] if (spread.index < r).any()])
    print(f"- same spread one trading day before expiry: mean {day_before.mean():+.2f}, "
          f"sd {day_before.std():.2f}, range {day_before.min():+.1f} to "
          f"{day_before.max():+.1f}, sum {day_before.sum():+.1f}")


def basis_facts() -> None:
    bs = basis_1m(_bars_1m(), "VN30F1M", "VN30", source="vci")
    daily = bs.groupby(bs["time"].dt.normalize()).agg(
        basis=("basis", "mean"), spot=("S", "mean"))
    dte = contract_master(pd.DatetimeIndex(daily.index)).set_index("date")
    daily["dte"] = dte["days_to_expiry"]
    daily["bucket"] = pd.cut(daily["dte"], [-1, 3, 10, 20, 45],
                             labels=["0-3", "4-10", "11-20", "21+"])
    # Basis as an annualised rate, only where enough time is left for the ratio
    # to mean something.
    far = daily[daily["dte"] >= 5]
    implied = (far["basis"] / far["spot"] * 365 / far["dte"]).median()
    print(f"\n## Basis (VN30F1M - VN30), 1-minute closes, "
          f"{bs['time'].min().date()} to {bs['time'].max().date()}\n")
    print(f"mean {bs['basis'].mean():+.2f} pts, median {bs['basis'].median():+.2f}, "
          f"sd {bs['basis'].std():.2f}, over {len(bs):,} minutes / {len(daily)} days; "
          f"median implied annual rate (days to expiry >= 5): {implied:+.1%}\n")
    print("| days to expiry | days | mean basis (pts) | median (pts) |")
    print("|---|---|---|---|")
    for k, g in daily.groupby("bucket", observed=True):
        print(f"| {k} | {len(g)} | {g['basis'].mean():+.2f} "
              f"| {g['basis'].median():+.2f} |")


def run() -> int:
    move_table()
    roll_facts()
    basis_facts()
    return 0


if __name__ == "__main__":
    sys.exit(run())

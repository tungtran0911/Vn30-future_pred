"""Data quality gate.

Hard criterion before any analysis: on the median day, fewer than 2% of expected
in-session 1-minute bars missing. Also measures the things that would otherwise be
discovered much later and much more expensively:

  coverage       expected session minutes vs bars actually present, per day
  source drift   KBS and VCI both serve the same bars; where they disagree, the
                 disagreement is data about the sources, not noise to average away
  roll integrity rolls with no observed spread, which become zero-gap adjustments
  tick sessions  recorded trade sessions and whether each reconciles exactly

Run it before trusting anything downstream:

    python -m vn30f.quality
"""

from __future__ import annotations

import sys

import pandas as pd

from vn30f.config import (
    AFTERNOON_END,
    AFTERNOON_START,
    CURATED,
    MORNING_END,
    MORNING_START,
    RAW,
)
from vn30f.contracts import build_continuous, load_daily_bars

# Continuous-session minutes only. The ATO and ATC auctions produce a single print
# each, not a minute-by-minute series, so counting them as expected bars would
# manufacture a permanent ~16-minute shortfall every day.
EXPECTED_MINUTES = (
    (pd.Timestamp(MORNING_END.isoformat()) - pd.Timestamp(MORNING_START.isoformat()))
    + (pd.Timestamp(AFTERNOON_END.isoformat())
       - pd.Timestamp(AFTERNOON_START.isoformat()))
).total_seconds() / 60
MAX_MISSING_PCT = 2.0


def _in_session(ts: pd.Series) -> pd.Series:
    t = ts.dt.time
    return (((t >= MORNING_START) & (t < MORNING_END))
            | ((t >= AFTERNOON_START) & (t < AFTERNOON_END)))


def bar_coverage(symbol: str = "VN30F1M", source: str = "vci") -> pd.DataFrame:
    """Per-day count of in-session 1-minute bars against the expected total."""
    bars = pd.read_parquet(RAW / "bars_1m" / "bars.parquet")
    bars["time"] = pd.to_datetime(bars["time"])
    b = bars[(bars["symbol"] == symbol) & (bars["source"] == source)].copy()
    if b.empty:
        return pd.DataFrame()
    b = b[_in_session(b["time"])]
    per_day = (b.groupby(b["time"].dt.date)
                .size()
                .rename("bars")
                .to_frame())
    per_day["expected"] = EXPECTED_MINUTES
    per_day["missing_pct"] = (1 - per_day["bars"] / EXPECTED_MINUTES) * 100
    return per_day


def source_drift() -> pd.DataFrame:
    """Where KBS and VCI disagree on the same bar.

    Both are unofficial reads of the same exchange. Agreement is evidence; a
    systematic gap would mean at least one is doing something to the data.
    """
    bars = pd.read_parquet(RAW / "bars_1m" / "bars.parquet")
    bars["time"] = pd.to_datetime(bars["time"])
    out = []
    for sym in bars["symbol"].unique():
        sub = bars[bars["symbol"] == sym]
        wide = sub.pivot_table(index="time", columns="source", values="close",
                               aggfunc="last")
        if not {"kbs", "vci"}.issubset(wide.columns):
            continue
        both = wide[["kbs", "vci"]].dropna()
        if both.empty:
            continue
        diff = (both["kbs"] - both["vci"]).abs()
        out.append({
            "symbol": sym,
            "overlapping_bars": len(both),
            "identical": int((diff == 0).sum()),
            "identical_pct": float((diff == 0).mean() * 100),
            "max_abs_diff_pts": float(diff.max()),
            "median_abs_diff_pts": float(diff.median()),
        })
    return pd.DataFrame(out)


def tick_sessions() -> pd.DataFrame:
    """Recorded trade sessions, and whether each reconciles against the exchange.

    The volume identity from the recorder: summed trade volume must equal the largest
    accumulated volume. Non-zero means trades are missing.
    """
    d = CURATED / "ticks"
    rows = []
    for f in sorted(d.glob("ticks_*.parquet")) if d.exists() else []:
        t = pd.read_parquet(f)
        for label, part in t.groupby("label"):
            rows.append({
                "session": f.stem.replace("ticks_", ""),
                "contract": label,
                "trades": len(part),
                "volume": int(part["volume"].sum()),
                "volume_gap": int(part["accumulated_volume"].max()
                                  - part["volume"].sum()),
                "first": str(part["ts"].min().time()),
                "last": str(part["ts"].max().time()),
            })
    return pd.DataFrame(rows)


def report() -> int:
    """Print the report. Returns non-zero if the gate fails."""
    failures = []

    print("=" * 78)
    print("VN30F DATA QUALITY")
    print("=" * 78)

    cov = bar_coverage()
    if cov.empty:
        print("\n[coverage] no 1-minute bars found")
        failures.append("no bars")
    else:
        bad = cov[cov["missing_pct"] > MAX_MISSING_PCT]
        print(f"\n[coverage] VN30F1M 1m bars, {len(cov)} trading days "
              f"({cov.index.min()} .. {cov.index.max()})")
        print(f"  expected {EXPECTED_MINUTES:.0f} in-session minutes/day")
        print(f"  median missing {cov['missing_pct'].median():.2f}% | "
              f"mean {cov['missing_pct'].mean():.2f}%")
        print(f"  days over the {MAX_MISSING_PCT}% threshold: {len(bad)} / {len(cov)} "
              f"({len(bad) / len(cov) * 100:.1f}%)")
        if len(bad):
            print("  worst offenders:")
            print(bad.sort_values("missing_pct", ascending=False)
                     .head(5).to_string())
        # The gate is on the typical day, not on every day: a half-session holiday
        # legitimately has fewer bars and should not fail the project.
        if cov["missing_pct"].median() > MAX_MISSING_PCT:
            failures.append(
                f"median missing {cov['missing_pct'].median():.2f}% "
                f"> {MAX_MISSING_PCT}%")

    drift = source_drift()
    print("\n[source drift] KBS vs VCI on overlapping 1m bars")
    if drift.empty:
        print("  no symbol has both sources")
    else:
        print(drift.to_string(index=False))

    try:
        bars = load_daily_bars(str(RAW / "bars_1D" / "bars.parquet"), source="vci")
        cont, gaps = build_continuous(bars)
        unobserved = (~gaps["observed"]).sum()
        print(f"\n[roll integrity] {len(gaps)} rolls "
              f"{cont['time'].min().date()} .. {cont['time'].max().date()}")
        print(f"  rolls with an observed expiry-day spread: {gaps['observed'].sum()}")
        print(f"  rolls with NO spread (zero-gap, flagged): {unobserved}")
        print(f"  mean gap {gaps.loc[gaps['observed'], 'gap'].mean():+.2f} pts | "
              f"total back-adjustment {cont['cum_adjustment'].iloc[0]:+.1f} pts")
        print(f"  raw bars with no F1M price: {int(cont['raw'].isna().sum())}")
    except Exception as exc:
        print(f"\n[roll integrity] FAILED: {type(exc).__name__}: {exc}")
        failures.append("roll engine")

    ticks = tick_sessions()
    print("\n[recorded tick sessions]")
    if ticks.empty:
        print("  none recorded yet")
    else:
        print(ticks.to_string(index=False))
        # A past session that does not reconcile cannot be repaired -- the trade
        # endpoint only serves the current day -- so it is excluded from analysis
        # (vn30f.studies.order_flow audits every session) rather than failing the
        # gate forever. Today's session can still be swept, so it gets the advice.
        unreconciled = ticks[ticks["volume_gap"] != 0]
        if len(unreconciled):
            print(f"  {len(unreconciled)} contract-session(s) do not reconcile and "
                  f"are excluded from the order-flow study; for today's session, "
                  f"re-run --final-sweep before the day rolls")

    print("\n" + "=" * 78)
    if failures:
        print("QUALITY GATE: FAIL -- " + "; ".join(failures))
        return 1
    print("QUALITY GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(report())

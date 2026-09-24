"""Does trade-flow imbalance predict the VN30F mid, and does it pay after costs?

Sample
------
Every recorded session is audited. A session enters only if (a) the front-month
tick record passes the volume identity -- summed trade volume equals the
exchange's own accumulated-volume counter to within 0.01% -- and (b) board
snapshots exist during the continuous session. Excluded sessions are printed
with the reason, so the sample is never silently filtered.

Variables, on the board-snapshot grid (one row per distinct book state)
----------------------------------------------------------------------
  x        signed volume (exchange aggressor flag) over the trailing 30 seconds.
           Trades carry whole-second stamps, so only trades stamped at or before
           t - 1s are counted: those are certain to precede the snapshot. The
           trades stamped in the snapshot's own second are dropped rather than
           risk counting flow that happened after it.
  past     mid(t) - mid(t - 30s), ticks. Contemporaneous impact, a data check.
  fwd_h    mid(t + h) - mid(t), ticks, for h in 10s..300s. Mid-to-mid, so a
           signal is never scored against the bid-ask bounce it helped cause.
           No window crosses the lunch break or an auction.

Tests
-----
  1. Correlations with 95% intervals from a block bootstrap over 5-minute
     blocks (returns overlap and are autocorrelated; a naive standard error
     would overstate the evidence), plus the sign in each session separately.
  2. A trading rule, evaluated out of sample. Take sign(x) when |x| is above the
     80th percentile of |x| in the OTHER sessions (leave-one-session-out, so the
     threshold never sees the session it trades). Enter and exit as a taker,
     paying half the quoted spread each side, hold h seconds, no overlapping
     positions. Net = gross - spread paid - exchange fees - turnover tax.

    python -m vn30f.studies.order_flow
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from vn30f.calendar_vn import phase_at
from vn30f.config import CURATED, TICK_SIZE
from vn30f.execution.cost_model import CostModel
from vn30f.features.microstructure import book_features, tfi_at

LOOKBACK_S = 30
HORIZONS_S = (10, 30, 60, 120, 300)
MAX_GAP_FRAC = 1e-4
FWD_TOL_S = 5
SIGNAL_PCTL = 0.80
N_BOOT = 2000
CONTINUOUS = ("morning", "afternoon")
SEC = 1_000_000_000   # nanoseconds


def _ns(ts: pd.Series) -> np.ndarray:
    """tz-aware timestamps -> int64 nanoseconds (UTC), one resolution everywhere."""
    return (ts.dt.tz_convert("UTC").dt.tz_localize(None)
              .astype("datetime64[ns]").to_numpy().astype("int64"))


def load_sessions() -> tuple[list[tuple[str, pd.DataFrame, pd.DataFrame]], pd.DataFrame]:
    audit, sessions = [], []
    for path in sorted((CURATED / "ticks").glob("ticks_*.parquet")):
        date = path.stem.removeprefix("ticks_")
        ticks = pd.read_parquet(path)
        front = ticks.groupby("label")["volume"].sum().idxmax()
        t = ticks[ticks["label"] == front]
        volume = int(t["volume"].sum())
        gap = int(t["accumulated_volume"].max() - volume)

        book = None
        bpath = CURATED / "book" / f"book_{date}.parquet"
        if bpath.exists():
            b = pd.read_parquet(bpath)
            book = book_features(b[b["label"] == front])
            book["phase"] = book["ts"].map(lambda ts: phase_at(ts).value)
            book = book[book["phase"].isin(CONTINUOUS) & (book["spread"] > 0)]
            book = book.dropna(subset=["mid"])

        if abs(gap) > MAX_GAP_FRAC * volume:
            reason = f"volume identity fails (gap {gap:+,})"
        elif book is None or len(book) < 100:
            reason = "no continuous-session book"
        else:
            reason = ""
        audit.append({"session": date, "contract": front, "trades": len(t),
                      "volume": volume, "gap": gap,
                      "book_states": 0 if book is None else len(book),
                      "used": "yes" if not reason else f"no: {reason}"})
        if not reason:
            sessions.append((date, t, book))
    return sessions, pd.DataFrame(audit)


def build_panel(date: str, ticks: pd.DataFrame, book: pd.DataFrame) -> pd.DataFrame:
    bins = tfi_at(ticks, "1s").sort_values("ts")
    bin_ts = _ns(bins["ts"])
    cum = bins["signed_vol"].cumsum().to_numpy(dtype=float)

    def flow_before(q: np.ndarray) -> np.ndarray:
        # Signed volume of every trade stamped at or before q - 1s.
        i = np.searchsorted(bin_ts, q - SEC, side="right") - 1
        return np.where(i >= 0, cum[np.clip(i, 0, None)], 0.0)

    b = book.sort_values("ts").reset_index(drop=True)
    ts = _ns(b["ts"])
    mid = b["mid"].to_numpy(dtype=float)
    spread = b["spread"].to_numpy(dtype=float)
    phase = b["phase"].to_numpy()
    tol = FWD_TOL_S * SEC
    n = len(b)

    out = pd.DataFrame({"session": date, "ts": ts, "phase": phase,
                        "mid": mid, "spread": spread})
    out["x"] = flow_before(ts) - flow_before(ts - LOOKBACK_S * SEC)

    j = np.searchsorted(ts, ts - LOOKBACK_S * SEC, side="right") - 1
    jj = np.clip(j, 0, n - 1)
    ok = (j >= 0) & ((ts - LOOKBACK_S * SEC) - ts[jj] <= tol) & (phase[jj] == phase)
    out["past"] = np.where(ok, (mid - mid[jj]) / TICK_SIZE, np.nan)

    for h in HORIZONS_S:
        k = np.searchsorted(ts, ts + h * SEC, side="left")
        kk = np.clip(k, 0, n - 1)
        ok = (k < n) & (ts[kk] - (ts + h * SEC) <= tol) & (phase[kk] == phase)
        out[f"fwd_{h}"] = np.where(ok, (mid[kk] - mid) / TICK_SIZE, np.nan)
        out[f"mid_exit_{h}"] = np.where(ok, mid[kk], np.nan)
        out[f"spread_exit_{h}"] = np.where(ok, spread[kk], np.nan)

    out["block"] = date + "_" + (ts // (300 * SEC)).astype(str)
    return out


def corr_ci(p: pd.DataFrame, y: str, seed: int = 0) -> tuple[float, float, float, int]:
    d = p[["x", y, "block"]].dropna()
    r = float(np.corrcoef(d["x"], d[y])[0, 1])
    groups = [(g["x"].to_numpy(), g[y].to_numpy()) for _, g in d.groupby("block")]
    rng = np.random.default_rng(seed)
    reps = []
    for _ in range(N_BOOT):
        pick = rng.integers(0, len(groups), len(groups))
        xs = np.concatenate([groups[i][0] for i in pick])
        ys = np.concatenate([groups[i][1] for i in pick])
        reps.append(np.corrcoef(xs, ys)[0, 1])
    lo, hi = np.nanquantile(reps, [0.025, 0.975])
    return r, float(lo), float(hi), len(d)


def trade_rule(p: pd.DataFrame, h: int) -> pd.DataFrame:
    cost = CostModel()
    rows = []
    for date, s in p.groupby("session"):
        thr = p.loc[p["session"] != date, "x"].abs().quantile(SIGNAL_PCTL)
        free_at = -np.inf
        for r in s.sort_values("ts").itertuples(index=False):
            fwd = getattr(r, f"fwd_{h}")
            if r.ts < free_at or np.isnan(fwd) or r.x == 0 or abs(r.x) < thr:
                continue
            side = np.sign(r.x)
            exit_mid = getattr(r, f"mid_exit_{h}")
            spread_paid = (r.spread + getattr(r, f"spread_exit_{h}")) / 2 / TICK_SIZE
            fees_tax = cost.round_trip(r.mid, exit_mid).total_ticks
            gross = side * fwd
            rows.append({"session": date, "gross": gross, "spread_paid": spread_paid,
                         "fees_tax": fees_tax,
                         "net": gross - spread_paid - fees_tax})
            free_at = r.ts + h * SEC
    return pd.DataFrame(rows)


def _t(x: pd.Series) -> float:
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 1 else np.nan


def run() -> int:
    sessions, audit = load_sessions()
    print("## Sample\n")
    print("| session | contract | trades | volume | gap | book states | used |")
    print("|---|---|---|---|---|---|---|")
    for a in audit.itertuples(index=False):
        print(f"| {a.session} | {a.contract} | {a.trades:,} | {a.volume:,} "
              f"| {a.gap:+,} | {a.book_states:,} | {a.used} |")
    if not sessions:
        print("\nno usable sessions")
        return 1
    p = pd.concat([build_panel(*s) for s in sessions], ignore_index=True)
    hours = sum((g["ts"].max() - g["ts"].min()) / SEC / 3600
                for _, g in p.groupby(["session", "phase"]))
    print(f"\n{len(sessions)} sessions, {len(p):,} book states, "
          f"{hours:.1f} hours of continuous-session book\n")

    d = p[["x", "past"]].dropna()
    beta = np.cov(d["x"], d["past"])[0, 1] / d["x"].var()
    r, lo, hi, n = corr_ci(p, "past")
    print("## Contemporaneous impact (data check)\n")
    print(f"corr(x, past) = {r:+.3f} [{lo:+.3f}, {hi:+.3f}], n={n:,}; "
          f"slope {beta * 100:.2f} ticks per 100 contracts of net flow\n")

    print("## Predictive power\n")
    print("| horizon | corr(x, fwd) | 95% CI | sessions > 0 | median abs move (ticks) | n |")
    print("|---|---|---|---|---|---|")
    for h in HORIZONS_S:
        y = f"fwd_{h}"
        r, lo, hi, n = corr_ci(p, y)
        per = [np.corrcoef(g["x"], g[y])[0, 1]
               for _, g in p[["session", "x", y]].dropna().groupby("session")]
        pos = sum(c > 0 for c in per)
        print(f"| {h}s | {r:+.3f} | [{lo:+.3f}, {hi:+.3f}] | {pos}/{len(per)} "
              f"| {p[y].abs().median():.1f} | {n:,} |")

    print("\n## Trading rule, out of sample (ticks per trade)\n")
    print("| horizon | trades | hit rate | gross | t(gross) | spread paid "
          "| fees + tax | net | t(net) |")
    print("|---|---|---|---|---|---|---|---|---|")
    for h in HORIZONS_S:
        tr = trade_rule(p, h)
        if tr.empty:
            continue
        print(f"| {h}s | {len(tr)} | {(tr['gross'] > 0).mean():.0%} "
              f"| {tr['gross'].mean():+.2f} | {_t(tr['gross']):+.2f} "
              f"| {tr['spread_paid'].mean():.2f} | {tr['fees_tax'].mean():.2f} "
              f"| {tr['net'].mean():+.2f} | {_t(tr['net']):+.2f} |")
    return 0


if __name__ == "__main__":
    sys.exit(run())

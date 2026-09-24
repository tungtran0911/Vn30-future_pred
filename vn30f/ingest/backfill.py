"""Pull the bar history that exists today and will not exist later.

The 1-minute window is a ROLLING one -- the providers retain a couple of months
(KBS) to six months (VCI) and drop a day off the back for every day that passes.
Snapshotting it locally buys history that cannot be reconstructed later. Daily bars
reach back to contract launch and are not urgent, but they anchor the contract
calendar and cost nothing, so they come along.

Three series types, which are NOT interchangeable:

  contract   VN30F2608 -- one real contract with a birth and a death. What roll and
             term-structure work needs. Only KBS answers for these.

  alias      VN30F1M/VN30F2M -- the provider splices contracts behind the alias and
             does not back-adjust, so the series jumps at every roll. Usable within
             a day, wrong across one. Kept because it reaches back furthest.

  index      VN30 cash. The other leg of the basis, and the only reason the basis is
             computable at all: the futures leg alone says nothing.

Two sources on purpose. VCI reaches further back on 1-minute bars; KBS is the only
one that resolves explicit contract codes. Where both answer, both are stored under
a `source` column so a disagreement is visible rather than silently averaged.

VCI is reached through vnstock, which caps an unregistered caller at 20 requests per
minute and exits the process on breach, so that pass is deliberately throttled and
kept to a handful of symbols. KBS is reached directly and needs no such care.

    python -m vn30f.ingest.backfill                # everything
    python -m vn30f.ingest.backfill --skip-vci     # direct-HTTP only, no throttle
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, timedelta

import pandas as pd

from vn30f.calendar_vn import now_vn
from vn30f.config import LOGS, RAW
from vn30f.ingest.kbs_api import KbsClient, to_krx_code

LOG = logging.getLogger("backfill")

VN30F_LAUNCH = date(2017, 8, 10)
VCI_THROTTLE_S = 4.0          # keeps the vnstock pass under 20 requests/minute

ALIASES = ["VN30F1M", "VN30F2M"]
INDEX = "VN30"


def _setup_logging() -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.FileHandler(LOGS / f"backfill_{now_vn():%Y%m%d}.log",
                                      encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
        force=True)
    for noisy in ("vnstock", "vnai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def recent_contracts(back_months: int = 10) -> list[str]:
    """Contract codes plausibly listed over the recent past.

    The exchange lists the current month, the next month and the two nearest
    quarters. Walking back month by month covers all of them without a hardcoded
    table; codes that were never listed simply return nothing.
    """
    today = now_vn().date()
    out: list[str] = []
    for k in range(-3, back_months + 1):
        y, m = today.year, today.month - k
        while m <= 0:
            m += 12
            y -= 1
        while m > 12:
            m -= 12
            y += 1
        code = f"VN30F{y % 100:02d}{m:02d}"
        if code not in out:
            out.append(code)
    return out


def _merge_write(new: pd.DataFrame, interval: str) -> int:
    d = RAW / f"bars_{interval}"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "bars.parquet"
    combined = (pd.concat([pd.read_parquet(path), new], ignore_index=True)
                if os.path.exists(path) else new)
    before = len(combined)
    combined = (combined
                .drop_duplicates(subset=["symbol", "source", "time"], keep="last")
                .sort_values(["symbol", "source", "time"])
                .reset_index(drop=True))
    combined.to_parquet(path, compression="zstd", index=False)
    LOG.info("wrote %s: %d rows total (%d merged duplicates)",
             path.name, len(combined), before - len(combined))
    return len(combined)


def _frame(rows: list[dict], symbol: str, source: str, interval: str,
           series: str) -> pd.DataFrame | None:
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    for c in ("open", "high", "low", "close", "volume"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["time"]).sort_values("time")
    df["symbol"], df["source"] = symbol, source
    df["interval"], df["series"] = interval, series
    LOG.info("%-10s %-4s %-3s rows=%-7d %s .. %s", symbol, source, interval,
             len(df), df["time"].min(), df["time"].max())
    return df


def pull_kbs(intervals: list[str]) -> list[pd.DataFrame]:
    client = KbsClient()
    edate = now_vn().date().strftime("%d-%m-%Y")
    out: list[pd.DataFrame] = []

    targets: list[tuple[str, str, bool, str]] = [(INDEX, INDEX, True, "index")]
    for code in recent_contracts():
        try:
            targets.append((code, to_krx_code(code), False, "contract"))
        except ValueError as exc:
            LOG.warning("skipping %s: %s", code, exc)

    for interval in intervals:
        sdate = (VN30F_LAUNCH if interval == "1D"
                 else date.today() - timedelta(days=400)).strftime("%d-%m-%Y")
        for symbol, code, is_index, series in targets:
            try:
                rows = client.ohlc(code, interval, sdate, edate, is_index=is_index)
            except Exception as exc:
                LOG.warning("%-10s kbs  %-3s FAIL %s: %s", symbol, interval,
                            type(exc).__name__, str(exc)[:100])
                continue
            df = _frame(rows, symbol, "kbs", interval, series)
            if df is not None:
                out.append(df)
    return out


def pull_vci(intervals: list[str]) -> list[pd.DataFrame]:
    """The deeper 1-minute window, throttled to respect vnstock's guest cap."""
    try:
        from vnstock import Quote
    except Exception as exc:
        LOG.warning("vnstock unavailable, skipping VCI pass: %s", exc)
        return []

    out: list[pd.DataFrame] = []
    edate = now_vn().date().isoformat()
    for interval in intervals:
        sdate = (VN30F_LAUNCH if interval == "1D"
                 else date.today() - timedelta(days=400)).isoformat()
        for symbol in ALIASES + [INDEX]:
            series = "index" if symbol == INDEX else "alias"
            try:
                time.sleep(VCI_THROTTLE_S)
                df = Quote(symbol=symbol, source="vci").history(
                    start=sdate, end=edate, interval=interval)
            except SystemExit:
                LOG.error("vnstock terminated the process on its rate limit; "
                          "stopping the VCI pass and keeping what we have")
                return out
            except Exception as exc:
                LOG.warning("%-10s vci  %-3s FAIL %s: %s", symbol, interval,
                            type(exc).__name__, str(exc)[:100])
                continue
            if df is None or df.empty:
                continue
            frame = _frame(df.to_dict("records"), symbol, "vci", interval, series)
            if frame is not None:
                out.append(frame)
    return out


def run(intervals: list[str], skip_vci: bool) -> int:
    frames = pull_kbs(intervals)
    if not skip_vci:
        frames += pull_vci(intervals)
    if not frames:
        LOG.error("nothing fetched")
        return 1
    for interval in intervals:
        part = [f for f in frames if f["interval"].iloc[0] == interval]
        if part:
            _merge_write(pd.concat(part, ignore_index=True), interval)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--interval", choices=["1m", "1D", "both"], default="both")
    ap.add_argument("--skip-vci", action="store_true")
    args = ap.parse_args()
    _setup_logging()
    intervals = ["1m", "1D"] if args.interval == "both" else [args.interval]
    return run(intervals, args.skip_vci)


if __name__ == "__main__":
    raise SystemExit(main())

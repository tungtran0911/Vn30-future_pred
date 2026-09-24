"""Turn raw recorder parts into typed, deduplicated daily files.

The recorder writes everything as strings and never rewrites a file, because its
only job is to not lose data. All interpretation happens here, where a mistake is
repairable by rerunning against parts that are still on disk.

What this step decides, and why each is a decision rather than a detail:

  side       B/S is the AGGRESSOR flag, not the resting side. Signing volume by it
             gives trade-flow imbalance directly, with no need for the Lee-Ready or
             tick-rule classifiers that most microstructure work has to fall back
             on. That is a genuine advantage of this feed and worth stating plainly.

  mid        (bid_1 + ask_1) / 2 from the snapshot. Every forward return in the
             study is measured mid-to-mid, never trade-to-trade, so that a signal
             cannot be scored against the bid-ask bounce it partly caused.

  ts_raw     "2026-08-17 13:31:16:38" -- the fourth component is undocumented. It is
             used only as a within-second ordering key, never as a duration, and
             the QC report prints its observed range so the ambiguity stays visible.

    python -m vn30f.ingest.curate                # every recorded date
    python -m vn30f.ingest.curate --date 2026-08-17
"""

from __future__ import annotations

import argparse
import logging
import sys

import pandas as pd

from vn30f.config import CURATED, LOGS, RAW, TZ

LOG = logging.getLogger("curate")

TICK_NUMERIC = ["price", "volume", "price_change",
                "accumulated_volume", "accumulated_value"]
BOOK_NUMERIC = [
    "close_price", "open_price", "high_price", "low_price", "average_price",
    "reference_price", "ceiling_price", "floor_price", "price_change",
    "percent_change", "volume_accumulated", "total_value", "current_vol",
    "bid_price_1", "bid_vol_1", "bid_price_2", "bid_vol_2",
    "bid_price_3", "bid_vol_3",
    "ask_price_1", "ask_vol_1", "ask_price_2", "ask_vol_2",
    "ask_price_3", "ask_vol_3",
    "total_buy_vol", "total_offer_vol",
    "foreign_buy_volume", "foreign_sell_volume", "open_interest",
]


def _setup_logging() -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.FileHandler(LOGS / "curate.log", encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
        force=True)


def _read_parts(stream: str, session: str) -> pd.DataFrame | None:
    d = RAW / stream / f"date={session}"
    if not d.exists():
        return None
    frames = []
    for f in sorted(d.glob("*.parquet")):
        try:
            frames.append(pd.read_parquet(f))
        except Exception as exc:
            LOG.warning("unreadable part %s: %s", f.name, exc)
    if not frames:
        return None
    return pd.concat(frames, ignore_index=True)


def _to_num(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def curate_ticks(session: str) -> pd.DataFrame | None:
    df = _read_parts("ticks", session)
    if df is None or df.empty:
        return None
    n_raw = len(df)
    df = df.drop_duplicates(subset=["uid"], keep="first").copy()
    df = _to_num(df, TICK_NUMERIC)

    # "2026-08-17 13:31:16:38" -> whole-second timestamp. The tail component is
    # NOT used for ordering, because it is not a property of the trade: the same
    # trade comes back stamped :28 on one fetch and :26 on the next, and it repeats
    # for roughly a fifth of the rows within a single page. It is retained only as
    # `subsec_unstable` so that nobody rediscovers this by trusting it.
    parts = df["ts_raw"].str.rsplit(":", n=1, expand=True)
    df["ts"] = pd.to_datetime(parts[0], format="%Y-%m-%d %H:%M:%S",
                              errors="coerce").dt.tz_localize(TZ)
    df["subsec_unstable"] = pd.to_numeric(parts[1], errors="coerce")

    df["side_sign"] = df["side"].map({"B": 1, "S": -1}).astype("Int64")
    df["signed_volume"] = df["side_sign"] * df["volume"]
    df["notional_vnd"] = df["price"] * df["volume"] * 100_000

    # Accumulated volume is the real sequence number: one value per trade, strictly
    # increasing through the session. Within a second it is the only field that
    # orders trades correctly, so it is the sort key and the dedup key both.
    df["seq"] = df["accumulated_volume"]
    df = df.sort_values(["label", "seq"]).reset_index(drop=True)

    vol = df.groupby("label")["volume"].sum().to_dict()
    LOG.info("ticks %s: %d raw -> %d unique | %s .. %s | volume by contract %s",
             session, n_raw, len(df), df["ts"].min(), df["ts"].max(), vol)
    return df


def curate_book(session: str) -> pd.DataFrame | None:
    df = _read_parts("book", session)
    if df is None or df.empty:
        return None
    n_raw = len(df)
    df = _to_num(df, BOOK_NUMERIC)
    df["captured_at"] = pd.to_datetime(df["captured_at"], errors="coerce", utc=True) \
                          .dt.tz_convert(TZ)
    # The exchange stamps each board row; captured_at is when we read it. Keeping
    # both makes staleness measurable instead of assumed.
    df["exchange_ts"] = pd.to_datetime(pd.to_numeric(df["time"], errors="coerce"),
                                       unit="ms", utc=True).dt.tz_convert(TZ)
    df["staleness_s"] = (df["captured_at"] - df["exchange_ts"]).dt.total_seconds()

    # One row per (contract, exchange timestamp): consecutive polls return the same
    # board until something changes, and those repeats are not observations.
    df = df.drop_duplicates(subset=["label", "time"], keep="first").copy()

    df["mid"] = (df["bid_price_1"] + df["ask_price_1"]) / 2
    df["spread"] = df["ask_price_1"] - df["bid_price_1"]
    df["spread_ticks"] = (df["spread"] / 0.1).round(1)
    depth_b = df[["bid_vol_1", "bid_vol_2", "bid_vol_3"]].sum(axis=1)
    depth_a = df[["ask_vol_1", "ask_vol_2", "ask_vol_3"]].sum(axis=1)
    df["depth_imbalance"] = (depth_b - depth_a) / (depth_b + depth_a).replace(0, pd.NA)
    df["queue_imbalance_l1"] = (
        (df["bid_vol_1"] - df["ask_vol_1"])
        / (df["bid_vol_1"] + df["ask_vol_1"]).replace(0, pd.NA))

    df = df.sort_values(["label", "exchange_ts"]).reset_index(drop=True)
    LOG.info("book  %s: %d polls -> %d distinct states | median staleness %.1fs | "
             "median spread %.1f ticks",
             session, n_raw, len(df), df["staleness_s"].median(),
             df["spread_ticks"].median())
    return df


def write(df: pd.DataFrame, stream: str, session: str) -> None:
    d = CURATED / stream
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{stream}_{session}.parquet"
    df.to_parquet(path, compression="zstd", index=False)
    LOG.info("wrote %s (%d rows)", path.name, len(df))


def sessions_on_disk() -> list[str]:
    out: set[str] = set()
    for stream in ("ticks", "book"):
        d = RAW / stream
        if d.exists():
            out.update(p.name.split("=", 1)[1] for p in d.glob("date=*"))
    return sorted(out)


def run(dates: list[str]) -> int:
    for session in dates:
        t = curate_ticks(session)
        if t is not None:
            write(t, "ticks", session)
        b = curate_book(session)
        if b is not None:
            write(b, "book", session)
        if t is None and b is None:
            LOG.warning("nothing recorded for %s", session)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="YYYY-MM-DD; default every recorded session")
    args = ap.parse_args()
    _setup_logging()
    return run([args.date] if args.date else sessions_on_disk())


if __name__ == "__main__":
    raise SystemExit(main())

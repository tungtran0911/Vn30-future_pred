"""Trade-flow and book-imbalance features.

Two signals of different quality, kept apart because conflating them would misstate
what the study can claim.

  TFI  Trade-flow imbalance = summed signed volume, where the sign is the aggressor
       flag the exchange itself labels (B/S). Exact at trade resolution -- no
       Lee-Ready, no tick-rule classifier. This is the load-bearing feature.

  QI   Queue imbalance = (bid_vol_1 - ask_vol_1) / (bid_vol_1 + ask_vol_1) from the
       recorded board snapshots. A discrete-sample proxy for the continuous L1
       imbalance: at ~2 second cadence the board moves between reads.

Cont-Kukanov-Stoikov OFI is deliberately absent. It is event-driven -- it counts
every book update -- and event-level book data is not available here.
Approximating it from 2-second snapshots would give an OFI-shaped number that is
not OFI. TFI is a weaker signal but a genuine one.
"""

from __future__ import annotations

import pandas as pd


def tfi_at(ticks: pd.DataFrame, freq: str = "1s") -> pd.DataFrame:
    """Signed volume and trade count per contract, resampled to `freq`.

    `ticks` needs `ts`, `label`, `volume`, `signed_volume`, as emitted by the
    curator. `freq` is any pandas offset alias ("1s", "5s", "1min").
    """
    need = {"ts", "label", "signed_volume", "volume"}
    missing = need - set(ticks.columns)
    if missing:
        raise ValueError(f"ticks is missing {sorted(missing)}")
    if ticks.empty:
        return pd.DataFrame(columns=["ts", "label", "signed_vol", "abs_vol", "n_trades"])

    tf = (ticks.set_index("ts")
                .groupby("label")[["signed_volume", "volume"]]
                .resample(freq)
                .agg(signed_vol=("signed_volume", "sum"),
                     abs_vol=("volume", "sum"),
                     n_trades=("volume", "size"))
                .reset_index())
    # Only bins where at least one trade printed: a zero-trade second is not the
    # same thing as a second of balanced flow.
    return tf[tf["n_trades"] > 0].reset_index(drop=True)


def book_features(book: pd.DataFrame) -> pd.DataFrame:
    """Per-snapshot mid, spread and imbalance from a curated book table.

    Only selects and renames, so the "book feature" contract is stated in one
    place: if the curator drops or renames a column, this fails loudly.
    """
    need = {"exchange_ts", "label", "mid", "spread",
            "queue_imbalance_l1", "depth_imbalance"}
    missing = need - set(book.columns)
    if missing:
        raise ValueError(f"book is missing {sorted(missing)}")
    return (book[list(need)]
            .rename(columns={"exchange_ts": "ts", "queue_imbalance_l1": "qi_l1",
                             "depth_imbalance": "qi_l3"})
            .sort_values(["label", "ts"])
            .reset_index(drop=True))

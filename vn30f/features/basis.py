"""Basis (futures - spot) on aligned 1-minute bars.

Basis matters more in Vietnam than in most markets: equity short selling is banned,
so the arbitrage that pulls a cheap future back to fair value -- sell the basket,
buy the future -- is unavailable. Only the other side of the arbitrage can operate.

Alignment is per-minute and single-source: mixing sources on the two legs would put
a cross-provider discrepancy into the basis and it would look like a signal.
"""

from __future__ import annotations

import pandas as pd


def basis_1m(bars_1m: pd.DataFrame,
             fut_symbol: str = "VN30F1M",
             spot_symbol: str = "VN30",
             source: str = "vci",
             price_col: str = "close") -> pd.DataFrame:
    """F - S on aligned 1-minute bars from one source.

    KBS and VCI agree on 99.4% of overlapping bars but not exactly, and a 0.5-point
    cross-source residual is the same size as the basis being measured.
    """
    b = bars_1m[(bars_1m["source"] == source)
                & (bars_1m["symbol"].isin([fut_symbol, spot_symbol]))].copy()
    b["time"] = pd.to_datetime(b["time"])
    wide = (b.pivot_table(index="time", columns="symbol", values=price_col,
                          aggfunc="last")
             .dropna(subset=[fut_symbol, spot_symbol]))
    out = wide[[fut_symbol, spot_symbol]].rename(
        columns={fut_symbol: "F", spot_symbol: "S"})
    out["basis"] = out["F"] - out["S"]
    return out.reset_index()

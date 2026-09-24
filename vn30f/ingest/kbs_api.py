"""Direct client for the public KBS market-data endpoints.

Written against the raw HTTP API rather than through vnstock, for three reasons:

1. vnstock ships a client-side limiter that caps an unregistered caller at 20
   requests/minute and calls sys.exit when the cap is hit. A recorder polling a
   snapshot endpoint every two seconds needs 30/minute, so the wrapper terminates
   the recorder mid-session. That is not a limit imposed by KBS.
2. vnstock is a third-party layer over the same public endpoints and has broken on
   upstream changes before. The recorder is the one component whose failure loses
   data permanently, so it holds the fewest dependencies of anything here.
3. The wrapper drops fields. The derivative board returns open interest, foreign
   flow and three depth levels; going direct keeps whatever else appears without
   waiting for a wrapper release.

Politeness is enforced here instead: a minimum gap between requests, honest
identification, retry with backoff, and no concurrency. Steady-state load is one
request every two seconds against one endpoint, comparable to a browser sitting on
a price board.

Field name maps are transcribed from vnstock's KBS constants (MIT licensed) because
the API returns two-letter keys with no schema endpoint.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import requests

LOG = logging.getLogger("kbs_api")

BASE = "https://kbbuddywts.kbsec.com.vn/iis-server/investment"
DERIVATIVE_BOARD_URL = f"{BASE}/derivative/iss"
TRADE_HISTORY_URL = f"{BASE}/trade/history"

MIN_REQUEST_GAP_S = 0.35
DEFAULT_TIMEOUT = 20
MAX_RETRIES = 3

# Two-letter response keys -> readable names.
BOARD_FIELDS = {
    "SB": "symbol", "t": "time", "EX": "exchange", "MS": "market_status",
    "CL": "ceiling_price", "FL": "floor_price", "RE": "reference_price",
    "OP": "open_price", "HI": "high_price", "LO": "low_price", "CP": "close_price",
    "AP": "average_price", "CH": "price_change", "CHP": "percent_change",
    "PMP": "previous_match_price", "PMQ": "previous_match_qty",
    "TT": "volume_accumulated", "TV": "total_value", "CV": "current_vol",
    "B1": "bid_price_1", "V1": "bid_vol_1",
    "B2": "bid_price_2", "V2": "bid_vol_2",
    "B3": "bid_price_3", "V3": "bid_vol_3",
    "S1": "ask_price_1", "U1": "ask_vol_1",
    "S2": "ask_price_2", "U2": "ask_vol_2",
    "S3": "ask_price_3", "U3": "ask_vol_3",
    "TB": "total_buy_vol", "TO": "total_offer_vol",
    "FB": "foreign_buy_volume", "FS": "foreign_sell_volume", "FR": "foreign_room",
    "OI": "open_interest",
}

# The trade feed uses a different key set from the board. `t` carries a fourth
# time component ("2026-08-17 13:31:16:38") that the vnstock wrapper truncates to
# whole seconds -- it is kept raw here because sub-second ordering is exactly what
# a microstructure study needs. Whether that field is centiseconds or a within-second
# sequence number is not documented; `curate.py` treats it as an ordering key only.
TRADE_FIELDS = {
    "t": "ts_raw", "TD": "trading_date", "SB": "symbol",
    "FT": "match_time",          # HH:MM:SS
    "LC": "side",                # B = buyer-initiated, S = seller-initiated
    "FMP": "price",
    "FV": "volume",
    "FCV": "price_change",
    "AVO": "accumulated_volume",
    "AVA": "accumulated_value",
}

OHLC_FIELDS = {"t": "time", "o": "open", "h": "high", "l": "low",
               "c": "close", "v": "volume"}

INTERVAL_SUFFIX = {
    "1m": "1P", "5m": "5P", "15m": "15P", "30m": "30P", "1H": "60P",
    "1D": "day", "1W": "week", "1M": "month",
}

_MONTH_CODE = "123456789ABC"   # KRX: month 10-12 become A-C
_YEAR_BASE = 2020              # 2026 -> 'G', verified against vnstock's converter


def to_krx_code(symbol: str) -> str:
    """VN30F2608 -> 41I1G8000.

    The exchange's KRX-era instrument code. Aliases (VN30F1M/VN30F2M) must be
    resolved to a concrete contract before calling this -- see calendar_vn.
    """
    s = symbol.upper().strip()
    if not s.startswith("VN30F") or len(s) != 9 or not s[5:].isdigit():
        raise ValueError(
            f"expected an explicit contract like VN30F2608, got {symbol!r}")
    year = 2000 + int(s[5:7])
    month = int(s[7:9])
    if not 1 <= month <= 12:
        raise ValueError(f"bad contract month in {symbol!r}")
    year_letter = chr(ord("A") + (year - _YEAR_BASE))
    return f"41I1{year_letter}{_MONTH_CODE[month - 1]}000"


class KbsClient:
    """Thin, polite, synchronous HTTP client."""

    def __init__(self, min_gap_s: float = MIN_REQUEST_GAP_S,
                 timeout: int = DEFAULT_TIMEOUT):
        self.min_gap_s = min_gap_s
        self.timeout = timeout
        self._last_request = 0.0
        self._lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
            "Connection": "keep-alive",
            "Origin": "https://kbbuddywts.kbsec.com.vn",
            "Referer": "https://kbbuddywts.kbsec.com.vn/",
        })

    def _throttle(self) -> None:
        with self._lock:
            gap = time.monotonic() - self._last_request
            if gap < self.min_gap_s:
                time.sleep(self.min_gap_s - gap)
            self._last_request = time.monotonic()

    def _request(self, method: str, url: str, **kw) -> Any:
        last: Exception | None = None
        for attempt in range(MAX_RETRIES):
            self._throttle()
            try:
                r = self.session.request(method, url, timeout=self.timeout, **kw)
                if r.status_code in (200, 201):
                    return r.json()
                if r.status_code in (429, 503):
                    wait = 2 ** attempt
                    LOG.warning("HTTP %d from %s, backing off %ds",
                                r.status_code, url, wait)
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
            except (requests.RequestException, ValueError) as exc:
                last = exc
                time.sleep(2 ** attempt * 0.5)
        raise RuntimeError(f"{method} {url} failed after {MAX_RETRIES} attempts: {last}")

    # -- endpoints ---------------------------------------------------------
    def derivative_board(self, krx_codes: list[str]) -> list[dict]:
        """Snapshot: 3 depth levels, open interest, foreign flow. Not replayable."""
        payload = {"code": ",".join(krx_codes)}
        data = self._request(
            "POST", DERIVATIVE_BOARD_URL,
            headers={"Content-Type": "application/json", "x-lang": "vi"},
            data=json.dumps(payload),
        )
        rows = data if isinstance(data, list) else data.get("data", [])
        return [self._rename(r, BOARD_FIELDS) for r in rows]

    def trade_history(self, krx_code: str, page: int = 1,
                      limit: int = 1000) -> list[dict]:
        """One page of the current session's trades, newest first.

        Accepts no date parameter: only today is reachable, which is why the
        recorder exists.
        """
        data = self._request("GET", f"{TRADE_HISTORY_URL}/{krx_code}",
                             params={"page": page, "limit": limit})
        rows = data.get("data", []) if isinstance(data, dict) else (data or [])
        return [self._rename(r, TRADE_FIELDS) for r in rows]

    def ohlc(self, krx_code: str, interval: str, sdate: str, edate: str,
             is_index: bool = False) -> list[dict]:
        """Historical bars, newest first.

        `sdate`/`edate` are DD-MM-YYYY. The server clamps the window to whatever it
        retains -- roughly two months of 1-minute bars -- and clamping silently, so
        the caller must check what came back rather than trust what it asked for.
        """
        suffix = INTERVAL_SUFFIX.get(interval)
        if suffix is None:
            raise ValueError(f"unsupported interval {interval!r}; "
                             f"known: {sorted(INTERVAL_SUFFIX)}")
        root = "index" if is_index else "stocks"
        data = self._request("GET", f"{BASE}/{root}/{krx_code}/data_{suffix}",
                             params={"sdate": sdate, "edate": edate})
        rows = data.get(f"data_{suffix}", []) if isinstance(data, dict) else []
        return [self._rename(r, OHLC_FIELDS) for r in rows]

    @staticmethod
    def _rename(row: dict, mapping: dict[str, str]) -> dict:
        # Unmapped keys are kept under their raw name rather than dropped: the API
        # has added fields before and silently losing one is worse than an ugly
        # column.
        return {mapping.get(k, k): v for k, v in row.items()}

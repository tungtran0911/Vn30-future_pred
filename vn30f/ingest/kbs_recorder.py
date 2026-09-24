"""Record VN30 futures market data that cannot be bought back later.

Why this exists
---------------
The DNSE public-feed plan failed on contact: the broker at
datafeed-lts-krx.dnse.com.vn accepts a TLS/WebSocket connection from outside
Vietnam but rejects anonymous MQTT CONNECT ("Not authorized"). See `probe_dnse.py`.
KBS serves the same class of data over plain HTTP with no account, and more of it.

Two streams, with very different recovery properties:

  ticks  - /trade/history is cumulative for the CURRENT SESSION and paginates
           backwards to the open, so any single sweep before the close recovers the
           whole day. Miss an hour and the end-of-day sweep still gets it. It takes
           no date parameter, so once the session rolls the day is gone for good.

  book   - the derivative board is a SNAPSHOT. Depth, open interest and foreign flow
           exist only at the instant they are read, and nothing recovers one that
           was not taken. This is what justifies running every session rather than
           a single end-of-day job.

Output is append-only part files under data/vn30f/raw/{ticks,book}/date=.../, so a
crash costs at most one flush interval. `curate.py` merges and types them.

    python -m vn30f.ingest.kbs_recorder                 # run the session
    python -m vn30f.ingest.kbs_recorder --once          # single sample, for testing
    python -m vn30f.ingest.kbs_recorder --final-sweep   # recover today after a crash
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time as _time
from datetime import datetime
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from vn30f.calendar_vn import (
    Phase,
    front_month_symbol,
    is_session_open,
    now_vn,
    phase_at,
    seconds_until_close,
)
from vn30f.config import LOGS, RAW, TZ
from vn30f.ingest.kbs_api import KbsClient, to_krx_code

LOG = logging.getLogger("kbs_recorder")

BOOK_INTERVAL_S = 2.0      # the perishable stream
TICK_INTERVAL_S = 30.0     # recoverable, so it can be sampled lazily
FLUSH_INTERVAL_S = 60.0
TICK_PAGE_SIZE = 1000
MAX_TICK_PAGES = 200       # ~200k trades, far above a full VN30F session
PAGE_RETRIES = 4           # per page, before declaring the sweep incomplete


def trade_uid(label: str, row: dict) -> str:
    """Stable unique key for one trade.

    Uses ACCUMULATED VOLUME, namespaced by contract: it is the running per-trade
    total, so it is strictly increasing and unique within a session -- a natural
    sequence number.

    The obvious alternative, the timestamp, is unusable on this feed. Its
    sub-second component is not a property of the trade: fetching one page twice
    returns the same trade (identical accumulated volume, price and size) stamped
    :28 and then :26, and the field repeats for roughly a fifth of the rows within
    a single page. Keying on it admitted the same trade repeatedly and inflated a
    recorded session to about 1.5x the exchange's own volume figure before the sum
    of trade volumes was checked against the exchange's cumulative counter.
    """
    return f"{label}|{row.get('accumulated_volume')}"


class IncompleteSweep(RuntimeError):
    """A page could not be fetched, so the session on disk is not complete.

    Distinct from an exhausted endpoint, which returns an empty list. Conflating
    the two is how a whole morning went missing once.
    """


class _KeepAwake:
    """Stop Windows idle-sleeping while the recorder runs.

    A recording session is CPU-light -- it mostly waits between polls -- so Windows
    can decide the machine is idle and sleep it mid-session, which killed at least
    one afternoon this week. SetThreadExecutionState(ES_CONTINUOUS |
    ES_SYSTEM_REQUIRED) tells Windows the system is in use until we clear it. It does
    NOT keep the display on and does NOT stop a lid-close or a manual sleep; it only
    blocks the idle timer, which is the case that actually bit us.

    No-op off Windows, so the recorder still runs anywhere.
    """

    _ES_CONTINUOUS = 0x80000000
    _ES_SYSTEM_REQUIRED = 0x00000001

    def __enter__(self):
        self._set = None
        if sys.platform == "win32":
            try:
                import ctypes
                self._set = ctypes.windll.kernel32.SetThreadExecutionState
                self._set(self._ES_CONTINUOUS | self._ES_SYSTEM_REQUIRED)
                LOG.info("holding the system awake for the session")
            except Exception as exc:
                LOG.warning("could not assert keep-awake: %s", exc)
                self._set = None
        return self

    def __exit__(self, *exc):
        if self._set is not None:
            try:
                self._set(self._ES_CONTINUOUS)   # clear the request
            except Exception:
                pass


def _setup_logging(verbose: bool) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[
            logging.FileHandler(LOGS / f"recorder_{now_vn():%Y%m%d}.log",
                                encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )


def target_contracts() -> list[tuple[str, str]]:
    """[(label, krx_code)] for the front and second month.

    Resolved from the calendar rather than from a provider alias so the recorded
    file says which actual contract it holds. VN30F1M means different things on
    either side of an expiry, and a file that only says "1M" cannot be rolled later.
    """
    front = front_month_symbol()
    y, m = 2000 + int(front[5:7]), int(front[7:9])
    m2, y2 = (m + 1, y) if m < 12 else (1, y + 1)
    second = f"VN30F{y2 % 100:02d}{m2:02d}"
    return [(front, to_krx_code(front)), (second, to_krx_code(second))]


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
def _part_path(stream: str, session: str) -> Path:
    d = RAW / stream / f"date={session}"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"part-{now_vn():%H%M%S}-{os.getpid()}.parquet"


def _write_part(rows: list[dict], stream: str, session: str) -> int:
    if not rows:
        return 0
    # Raw fields arrive with inconsistent types (prices as strings, ints as ints).
    # They are stored as strings here and coerced in curation, so a type surprise
    # from the API can never abort a write and lose the buffer.
    df = pd.DataFrame(rows).astype(
        {c: "string" for c in pd.DataFrame(rows).columns
         if c not in ("captured_at", "phase", "session_date", "label", "uid")})
    path = _part_path(stream, session)
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), path,
                   compression="zstd")
    LOG.info("flushed %d %s rows -> %s", len(df), stream, path.name)
    return len(df)


def _known_tick_ids(session: str) -> set[str]:
    """Trade uids already on disk, so a restart does not duplicate them."""
    d = RAW / "ticks" / f"date={session}"
    if not d.exists():
        return set()
    ids: set[str] = set()
    for f in d.glob("*.parquet"):
        try:
            ids.update(pq.read_table(f, columns=["uid"])["uid"].to_pylist())
        except Exception as exc:  # a half-written part from a hard kill
            LOG.warning("skipping unreadable part %s: %s", f.name, exc)
    return ids


# --------------------------------------------------------------------------
# Recorder
# --------------------------------------------------------------------------
class Recorder:
    def __init__(self) -> None:
        self.client = KbsClient()
        self.contracts = target_contracts()
        self.session = now_vn().date().isoformat()
        self.known = _known_tick_ids(self.session)
        self.book_buf: list[dict] = []
        self.tick_buf: list[dict] = []
        self.n_book = 0
        self.n_tick = 0
        LOG.info("session %s | contracts %s | %d tick ids already on disk",
                 self.session,
                 ", ".join(f"{lbl}={code}" for lbl, code in self.contracts),
                 len(self.known))

    def snapshot_book(self) -> int:
        captured = now_vn()
        rows = self.client.derivative_board([c for _, c in self.contracts])
        by_code = {lbl: code for lbl, code in self.contracts}
        code_to_label = {v: k for k, v in by_code.items()}
        for r in rows:
            r["captured_at"] = captured.isoformat()
            r["phase"] = phase_at(captured).value
            r["session_date"] = self.session
            r["label"] = code_to_label.get(str(r.get("symbol")), "")
        self.book_buf.extend(rows)
        return len(rows)

    def sweep_ticks(self, label: str, code: str, full: bool) -> int:
        """Page backwards from the newest trade until everything is already known.

        Raises IncompleteSweep if a page cannot be fetched. An earlier version
        treated any exception as the end of pagination, which cost most of a
        session: the machine woke from sleep with the network not yet up, page 1
        threw, and the sweep reported "0 additional trades" -- silent data loss
        reported as success. An exhausted endpoint returns an EMPTY LIST, and that
        is the only thing allowed to end a sweep normally.
        """
        captured = now_vn().isoformat()
        new = 0
        for page in range(1, MAX_TICK_PAGES + 1):
            rows = None
            for attempt in range(PAGE_RETRIES):
                try:
                    rows = self.client.trade_history(code, page=page,
                                                     limit=TICK_PAGE_SIZE)
                    break
                except Exception as exc:
                    wait = 2 ** attempt
                    LOG.warning("%s page %d attempt %d/%d failed (%s: %s) -- "
                                "retrying in %ds", label, page, attempt + 1,
                                PAGE_RETRIES, type(exc).__name__,
                                str(exc)[:100], wait)
                    _time.sleep(wait)
            if rows is None:
                raise IncompleteSweep(
                    f"{label}: page {page} unreachable after {PAGE_RETRIES} "
                    f"attempts; {new} trades collected so far, session NOT complete")
            if not rows:
                break
            fresh = []
            for r in rows:
                uid = trade_uid(label, r)
                if uid in self.known:
                    continue
                self.known.add(uid)
                r["uid"] = uid
                r["label"] = label
                r["captured_at"] = captured
                r["session_date"] = self.session
                fresh.append(r)
            self.tick_buf.extend(fresh)
            new += len(fresh)
            # Stop only when a page is ENTIRELY known. Pagination is by offset from
            # the newest trade, so a live feed shifts the window between requests
            # and neighbouring pages overlap by a few rows as a matter of course.
            # Treating any single duplicate as "caught up" ends the sweep on page 2
            # and silently abandons the rest of the session.
            if not full and not fresh:
                break
        return new

    def flush(self) -> None:
        self.n_book += _write_part(self.book_buf, "book", self.session)
        self.n_tick += _write_part(self.tick_buf, "ticks", self.session)
        self.book_buf, self.tick_buf = [], []

    def volume_gap(self) -> dict[str, int]:
        """Contracts still missing trades, and by how many contracts of volume.

        Exploits an identity the exchange gives us for free: `accumulated_volume` is
        the running total including its own trade, so if we hold every trade of the
        session, the sum of our trade volumes equals the largest accumulated volume
        we have seen. Any shortfall is exactly the volume we are missing.

        This matters because a single full sweep does NOT guarantee completeness --
        offset-based pagination over a live dataset skips scattered rows, so repeated
        sweeps keep surfacing genuine trades that earlier passes stepped over. The
        gap turns "probably complete" into a number that can be driven to zero.
        """
        d = RAW / "ticks" / f"date={self.session}"
        files = sorted(d.glob("*.parquet")) if d.exists() else []
        if not files:
            return {label: -1 for label, _ in self.contracts}
        frames = []
        for f in files:
            try:
                frames.append(pq.read_table(
                    f, columns=["label", "volume", "accumulated_volume"]).to_pandas())
            except Exception:
                continue
        if not frames:
            return {label: -1 for label, _ in self.contracts}
        df = pd.concat(frames, ignore_index=True)
        for c in ("volume", "accumulated_volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        gaps: dict[str, int] = {}
        for label, _ in self.contracts:
            part = df[df["label"] == label]
            if part.empty:
                gaps[label] = -1
                continue
            gaps[label] = int(part["accumulated_volume"].max()
                              - part["volume"].sum())
        return gaps

    def sweep_until_complete(self, max_passes: int = 6) -> bool:
        """Sweep repeatedly until the volume identity balances for every contract."""
        for p in range(1, max_passes + 1):
            gaps = self.volume_gap()
            if all(g == 0 for g in gaps.values()):
                LOG.info("volume reconciled exactly for all contracts after %d "
                         "pass(es): %s", p - 1, gaps)
                return True
            LOG.info("pass %d/%d, outstanding volume gap %s", p, max_passes, gaps)
            for label, code in self.contracts:
                if gaps.get(label, -1) == 0:
                    continue
                try:
                    n = self.sweep_ticks(label, code, full=True)
                    LOG.info("  %s: +%d trades", label, n)
                except IncompleteSweep as exc:
                    LOG.error("  %s: %s", label, exc)
            self.flush()
        gaps = self.volume_gap()
        if all(g == 0 for g in gaps.values()):
            LOG.info("volume reconciled exactly: %s", gaps)
            return True
        LOG.warning("residual volume gap after %d passes: %s -- the session is "
                    "very nearly complete but not provably so", max_passes, gaps)
        return False

    def final_sweep(self, attempts: int = 3) -> bool:
        """Pull the whole session. Returns True only if every contract completed."""
        LOG.info("final tick sweep")
        ok = True
        for label, code in self.contracts:
            for attempt in range(attempts):
                try:
                    n = self.sweep_ticks(label, code, full=True)
                    LOG.info("  %s: %d additional trades", label, n)
                    break
                except IncompleteSweep as exc:
                    LOG.error("  %s incomplete (attempt %d/%d): %s", label,
                              attempt + 1, attempts, exc)
                    if attempt == attempts - 1:
                        ok = False
                    else:
                        _time.sleep(30)
                except Exception as exc:
                    LOG.error("  %s failed: %s: %s", label, type(exc).__name__, exc)
                    ok = False
                    break
        self.flush()
        reconciled = self.sweep_until_complete()
        if not self.verify_coverage() or not ok or not reconciled:
            LOG.error("SESSION INCOMPLETE for %s. The trade endpoint still serves "
                      "today until the trading day rolls, so re-run "
                      "`python -m vn30f.ingest.kbs_recorder --final-sweep` NOW. "
                      "After the roll this day is unrecoverable.", self.session)
            return False
        return True

    def verify_coverage(self) -> bool:
        """Check the recorded ticks actually span the session.

        The sweep can only report what it fetched; this asks the separate question
        of whether what landed on disk covers 09:00 to 14:45. Without it, a
        truncated day looks identical to a complete one until someone reads the
        data weeks later.
        """
        d = RAW / "ticks" / f"date={self.session}"
        files = sorted(d.glob("*.parquet")) if d.exists() else []
        if not files:
            LOG.error("coverage: no tick files for %s", self.session)
            return False
        frames = []
        for f in files:
            try:
                frames.append(
                    pq.read_table(f, columns=["label", "match_time", "trading_date"])
                    .to_pandas())
            except Exception:
                continue
        if not frames:
            return False
        df = pd.concat(frames, ignore_index=True)
        good = True

        # The partition is named from the local clock, but the trades carry the
        # exchange's own trading date. If they disagree we have filed one session
        # under another's date -- possible when a sweep runs after the local date has
        # rolled but before the exchange's has, or when a missed task fires a day late.
        if "trading_date" in df.columns:
            dates = set(df["trading_date"].dropna().unique())
            expected = pd.Timestamp(self.session).strftime("%d/%m/%Y")
            if dates and dates != {expected}:
                LOG.error("coverage: partition date=%s holds exchange trading_date(s) "
                          "%s -- this session is filed under the wrong date",
                          self.session, sorted(dates))
                good = False
        for label, _ in self.contracts:
            times = df.loc[df["label"] == label, "match_time"].dropna()
            if times.empty:
                LOG.error("coverage %s: NO trades recorded", label)
                good = False
                continue
            first, last = times.min(), times.max()
            # The front month trades continuously; a first print later than 09:05
            # or a last print before 14:30 means the sweep truncated.
            if first > "09:05:00" or last < "14:30:00":
                LOG.error("coverage %s: %s .. %s -- expected ~09:00 .. ~14:45",
                          label, first, last)
                good = False
            else:
                LOG.info("coverage %s: %s .. %s (%d trades) OK",
                         label, first, last, len(times))
        gaps = self.volume_gap()
        LOG.info("volume reconciliation (0 == provably complete): %s", gaps)
        return good


def wait_for_open(max_wait_h: float = 6.0) -> bool:
    """Block until the session opens. Returns False if it will not open today.

    A scheduled task fires on LOCAL time, and this machine sits in a timezone that
    observes daylight saving while Vietnam does not, so the gap between local wall
    clock and the Hanoi open moves by an hour twice a year. Rather than maintain two
    schedules, the task is set early and the wait happens here against the exchange
    clock, which cannot drift.
    """
    start = _time.monotonic()
    while _time.monotonic() - start < max_wait_h * 3600:
        ts = now_vn()
        if ts.weekday() >= 5:
            LOG.info("%s is a weekend in Hanoi -- exiting", ts.date())
            return False
        p = phase_at(ts)
        if p is Phase.CLOSED:
            LOG.info("session already closed at %s -- exiting", ts.strftime("%H:%M"))
            return False
        if is_session_open():
            return True
        LOG.info("VN %s, phase=%s -- waiting for the 08:45 open",
                 ts.strftime("%H:%M:%S"), p.value)
        _time.sleep(60)
    LOG.error("waited %.1fh without an open -- giving up", max_wait_h)
    return False


def _day_finished(ts: "datetime | None" = None) -> bool:
    """True once today's Hanoi session is over (weekend, or past the 14:45 close).

    Note LUNCH is deliberately NOT finished: the loop idles through it and resumes
    for the afternoon. Treating lunch as finished is the bug this guards against.
    """
    ts = ts or now_vn()
    return ts.weekday() >= 5 or phase_at(ts) is Phase.CLOSED


def _sweep_is_safe_today(ts: "datetime | None" = None) -> bool:
    """Whether a full sweep filed under today's date would actually be today's data.

    The trade endpoint serves whatever it considers the current session and takes no
    date parameter. Before the open (or on a weekend) it may still be serving the
    PREVIOUS trading day, so a sweep run then would file yesterday's -- or Friday's
    -- trades under today's partition. Refusing outside a live/just-closed weekday
    session keeps a mis-timed recovery from silently corrupting the archive; the
    cost is only that a fully-missed day stays missed, which it already was.
    """
    ts = ts or now_vn()
    if ts.weekday() >= 5:
        return False
    # From the open onward -- through lunch, the afternoon, and after the close but
    # before the day rolls -- the endpoint serves today. PRE_OPEN is the one phase
    # where it is still serving the prior session.
    return phase_at(ts) is not Phase.PRE_OPEN


def run(once: bool = False, final_sweep_only: bool = False,
        ignore_session: bool = False) -> int:
    rec = Recorder()

    if final_sweep_only:
        if not (ignore_session or _sweep_is_safe_today()):
            ts = now_vn()
            LOG.warning("refusing final sweep at VN %s (%s): the endpoint may be "
                        "serving a different trading day, and filing it under %s "
                        "would corrupt the archive. Nothing recorded.",
                        ts.strftime("%Y-%m-%d %H:%M:%S"), phase_at(ts).value,
                        rec.session)
            return 1
        return 0 if rec.final_sweep() else 2

    if once:
        n = rec.snapshot_book()
        t = sum(rec.sweep_ticks(l, c, full=False) for l, c in rec.contracts)
        LOG.info("single sample: %d book rows, %d ticks", n, t)
        rec.flush()
        return 0

    if not (ignore_session or is_session_open()):
        ts = now_vn()
        LOG.warning("market closed (%s, phase=%s) -- nothing to record",
                    ts.strftime("%Y-%m-%d %H:%M:%S"), phase_at(ts).value)
        return 1

    LOG.info("recording until 14:45 VN (%.0f min left)", seconds_until_close() / 60)
    next_book = next_tick = _time.monotonic()
    next_flush = _time.monotonic() + FLUSH_INTERVAL_S
    consecutive_errors = 0
    in_lunch = False

    # Hold the machine awake for the session: a poll loop is idle enough that Windows
    # will otherwise sleep the box mid-session, which is one of the ways this week's
    # data went missing. No-op off Windows.
    with _KeepAwake():
        try:
            # Run until the DAY is done (14:45 close), not until the market is
            # momentarily not trading. is_session_open() is False during the
            # 11:30-13:00 lunch break; an earlier version used it as the loop
            # condition, so a run that started before lunch recorded the morning,
            # exited at 11:30, and never came back for the afternoon. The loop now
            # idles through lunch and resumes.
            while ignore_session or not _day_finished():
                now = _time.monotonic()

                if not ignore_session and not is_session_open():
                    # Lunch break: the board is frozen, so polling it would only
                    # record duplicate pre-lunch states. Idle until the 13:00 reopen.
                    if not in_lunch:
                        LOG.info("lunch break -- pausing capture until 13:00 VN")
                        rec.flush()
                        in_lunch = True
                    _time.sleep(5)
                    continue
                if in_lunch:
                    LOG.info("afternoon session open -- resuming capture")
                    in_lunch = False
                    next_book = next_tick = now

                if now >= next_book:
                    try:
                        rec.snapshot_book()
                        consecutive_errors = 0
                    except Exception as exc:
                        consecutive_errors += 1
                        LOG.warning("book snapshot failed (%d consecutive): %s: %s",
                                    consecutive_errors, type(exc).__name__,
                                    str(exc)[:140])
                    next_book = now + BOOK_INTERVAL_S
                if now >= next_tick:
                    for label, code in rec.contracts:
                        try:
                            rec.sweep_ticks(label, code, full=False)
                        except Exception as exc:
                            LOG.warning("tick sweep %s failed: %s: %s", label,
                                        type(exc).__name__, str(exc)[:140])
                    next_tick = now + TICK_INTERVAL_S
                if now >= next_flush:
                    rec.flush()
                    next_flush = now + FLUSH_INTERVAL_S
                if consecutive_errors >= 20:
                    LOG.error("20 consecutive book failures -- sleeping 60s")
                    _time.sleep(60)
                    consecutive_errors = 0
                _time.sleep(0.2)
        except KeyboardInterrupt:
            LOG.info("interrupted")
        finally:
            rec.flush()

    # The session is over. One full sweep makes the tick record complete even if the
    # loop above missed hours of it.
    complete = rec.final_sweep()
    LOG.info("DONE %s | book rows=%d | ticks=%d | session complete: %s",
             rec.session, rec.n_book, rec.n_tick, complete)
    return 0 if complete else 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true", help="one sample then exit")
    ap.add_argument("--final-sweep", action="store_true",
                    help="pull the whole session's trades and exit")
    ap.add_argument("--ignore-session", action="store_true",
                    help="run the loop even outside market hours")
    ap.add_argument("--wait-for-open", action="store_true",
                    help="sleep until the Hanoi session opens, then record")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    _setup_logging(args.verbose)
    LOG.info("VN clock %s", datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z"))
    if args.wait_for_open and not wait_for_open():
        return 1
    return run(once=args.once, final_sweep_only=args.final_sweep,
               ignore_session=args.ignore_session)


if __name__ == "__main__":
    raise SystemExit(main())

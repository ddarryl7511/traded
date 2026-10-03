"""Idempotent daily-bar ingestion and data-quality checks.

Two series are stored per symbol and feed:
* adjustment='raw' - prices as traded. Used for order sizing / execution sanity checks.
  Downloaded incrementally (history never changes, barring provider corrections).
* adjustment='all' - split- and distribution-adjusted. Used ONLY for the research signal
  and the backtest. Because providers rescale the whole history when a new corporate
  action occurs, the trailing `adjusted_refresh_days` are re-downloaded and overwritten on
  every update so the signal window is always internally consistent (one snapshot).
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import calendar_utils as cal
from .db import iso, log_event, utcnow

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
ADJUSTMENTS = ("raw", "all")


def bar_session_date(ts: datetime) -> date:
    """Alpaca daily bars are stamped at the start of the session day in New York time
    (e.g. 04:00/05:00 UTC). Convert to the exchange-local date."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(NY).date()


def _quality(conn, now, symbol, feed, adj, check, severity, detail):
    conn.execute(
        "INSERT INTO data_quality (checked_at_utc, symbol, feed, adjustment, check_name, severity, detail)"
        " VALUES (?,?,?,?,?,?,?)", (iso(now), symbol, feed, adj, check, severity, detail))


def store_bars(conn, bars: list[dict], feed: str, adjustment: str, last_completed: date,
               provider: str, now: datetime | None = None) -> dict:
    """Upsert bars. Returns counts. Bars for non-sessions or not-yet-completed sessions are
    rejected; duplicates inside one response are rejected; changed raw values are logged."""
    now = now or utcnow()
    stats = Counter()
    keys = Counter((b["symbol"], bar_session_date(b["timestamp"])) for b in bars)
    for (sym, d), n in keys.items():
        if n > 1:
            _quality(conn, now, sym, feed, adjustment, "duplicate_in_response", "error",
                     f"{n} bars for {d}; all dropped")
    for b in bars:
        sym, d = b["symbol"], bar_session_date(b["timestamp"])
        if keys[(sym, d)] > 1:
            stats["duplicate_dropped"] += 1
            continue
        if d > last_completed:
            stats["incomplete_dropped"] += 1
            continue
        if not cal.is_session(d):
            _quality(conn, now, sym, feed, adjustment, "non_session_bar", "error", str(d))
            stats["non_session_dropped"] += 1
            continue
        o, h, l, c = (float(b[k]) for k in ("open", "high", "low", "close"))
        if min(o, h, l, c) <= 0 or h < max(o, c, l) or l > min(o, c, h):
            _quality(conn, now, sym, feed, adjustment, "inconsistent_ohlc", "error",
                     f"{d} o={o} h={h} l={l} c={c}")
            stats["inconsistent_dropped"] += 1
            continue
        prev = conn.execute(
            "SELECT open, high, low, close, volume FROM bars WHERE symbol=? AND session_date=?"
            " AND feed=? AND adjustment=?", (sym, d.isoformat(), feed, adjustment)).fetchone()
        new_vals = (o, h, l, c, float(b["volume"]))
        if prev is not None:
            if all(abs(a - x) <= 1e-9 * max(1.0, abs(a)) for a, x in zip(tuple(prev), new_vals)):
                stats["unchanged"] += 1
                continue
            stats["updated"] += 1
            if adjustment == "raw":
                _quality(conn, now, sym, feed, adjustment, "raw_revision", "warning",
                         f"{d} old={tuple(prev)} new={new_vals}")
        else:
            stats["inserted"] += 1
        ts = b["timestamp"] if b["timestamp"].tzinfo else b["timestamp"].replace(tzinfo=timezone.utc)
        conn.execute(
            """INSERT INTO bars (symbol, session_date, feed, adjustment, open, high, low, close, volume,
                   trade_count, vwap, bar_timestamp_utc, provider, retrieved_at_utc)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol, session_date, feed, adjustment) DO UPDATE SET
                   open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
                   volume=excluded.volume, trade_count=excluded.trade_count, vwap=excluded.vwap,
                   bar_timestamp_utc=excluded.bar_timestamp_utc, provider=excluded.provider,
                   retrieved_at_utc=excluded.retrieved_at_utc""",
            (sym, d.isoformat(), feed, adjustment, o, h, l, c, float(b["volume"]),
             b.get("trade_count"), b.get("vwap"), iso(ts), provider, iso(now)))
    conn.commit()
    return dict(stats)


def latest_stored_date(conn, symbol, feed, adjustment) -> date | None:
    r = conn.execute("SELECT MAX(session_date) FROM bars WHERE symbol=? AND feed=? AND adjustment=?",
                     (symbol, feed, adjustment)).fetchone()[0]
    return date.fromisoformat(r) if r else None


def update_bars(conn, cfg, market_data, now: datetime | None = None) -> dict:
    """Download history on first run, then incrementally. Safe to repeat."""
    now = now or utcnow()
    last_completed = cal.last_completed_session(now, cfg.completed_bar_delay_minutes)
    summary = {"last_completed_session": last_completed.isoformat()}
    for adj in ADJUSTMENTS:
        starts = {}
        for sym in cfg.symbols:
            latest = latest_stored_date(conn, sym, cfg.bars_feed, adj)
            if latest is None:
                starts[sym] = cfg.history_start
            elif adj == "raw":
                starts[sym] = latest + timedelta(days=1)
            else:
                starts[sym] = max(cfg.history_start,
                                  last_completed - timedelta(days=cfg.adjusted_refresh_days))
        start = min(starts.values())
        if start > last_completed:
            summary[adj] = {"skipped": "up to date"}
            continue
        try:
            bars = market_data.get_daily_bars(cfg.symbols, start, last_completed, adj, cfg.bars_feed)
        except Exception as exc:
            log_event(conn, "error", "data", f"bar download failed ({adj})", {"error": str(exc)})
            raise
        stats = store_bars(conn, bars, cfg.bars_feed, adj, last_completed,
                           getattr(market_data, "provider", "alpaca"), now)
        summary[adj] = stats
        log.info("bars %s %s..%s: %s", adj, start, last_completed, stats)
    issues = run_quality_checks(conn, cfg, last_completed, now)
    summary["quality_issues"] = len(issues)
    log_event(conn, "info", "data", "bars updated", summary, now)
    return summary


def missing_sessions(conn, symbol, feed, adjustment, start: date, end: date) -> list[date]:
    have = {r[0] for r in conn.execute(
        "SELECT session_date FROM bars WHERE symbol=? AND feed=? AND adjustment=? AND session_date"
        " BETWEEN ? AND ?", (symbol, feed, adjustment, start.isoformat(), end.isoformat()))}
    return [d for d in cal.sessions_in_range(start, end) if d.isoformat() not in have]


def run_quality_checks(conn, cfg, last_completed: date, now: datetime | None = None) -> list[str]:
    """Missing sessions, staleness and raw/adjusted coverage mismatch. Duplicate rows are
    impossible by primary key; duplicates inside API responses are caught in store_bars."""
    now = now or utcnow()
    issues = []
    for sym in cfg.symbols:
        for adj in ADJUSTMENTS:
            first = conn.execute(
                "SELECT MIN(session_date) FROM bars WHERE symbol=? AND feed=? AND adjustment=?",
                (sym, cfg.bars_feed, adj)).fetchone()[0]
            if first is None:
                issues.append(f"{sym}/{adj}: no data")
                _quality(conn, now, sym, cfg.bars_feed, adj, "no_data", "error", "")
                continue
            miss = missing_sessions(conn, sym, cfg.bars_feed, adj, date.fromisoformat(first), last_completed)
            if miss:
                issues.append(f"{sym}/{adj}: {len(miss)} missing sessions")
                _quality(conn, now, sym, cfg.bars_feed, adj, "missing_sessions", "warning",
                         f"{len(miss)} missing, last few: {[d.isoformat() for d in miss[-5:]]}")
            latest = latest_stored_date(conn, sym, cfg.bars_feed, adj)
            if latest != last_completed:
                issues.append(f"{sym}/{adj}: stale (latest {latest})")
                _quality(conn, now, sym, cfg.bars_feed, adj, "stale", "error",
                         f"latest {latest}, expected {last_completed}")
    conn.commit()
    return issues


def load_closes(conn, symbol, feed, adjustment, start: date, end: date) -> dict[date, float]:
    return {date.fromisoformat(r[0]): r[1] for r in conn.execute(
        "SELECT session_date, close FROM bars WHERE symbol=? AND feed=? AND adjustment=? AND"
        " session_date BETWEEN ? AND ? ORDER BY session_date",
        (symbol, feed, adjustment, start.isoformat(), end.isoformat()))}

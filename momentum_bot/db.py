"""SQLite storage. All timestamps are stored as ISO-8601 UTC strings; session dates
are stored as YYYY-MM-DD strings in the exchange's (America/New_York) calendar."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    session_date TEXT NOT NULL,
    feed TEXT NOT NULL,
    adjustment TEXT NOT NULL,          -- 'raw' (execution/sizing) or 'all' (research signal)
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume REAL NOT NULL, trade_count REAL, vwap REAL,
    bar_timestamp_utc TEXT NOT NULL,   -- provider bar timestamp, converted to UTC
    provider TEXT NOT NULL,
    retrieved_at_utc TEXT NOT NULL,
    PRIMARY KEY (symbol, session_date, feed, adjustment)
);
CREATE TABLE IF NOT EXISTS data_quality (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at_utc TEXT NOT NULL, symbol TEXT, feed TEXT, adjustment TEXT,
    check_name TEXT NOT NULL, severity TEXT NOT NULL, detail TEXT
);
CREATE TABLE IF NOT EXISTS experiments (
    name TEXT PRIMARY KEY, config_hash TEXT NOT NULL, frozen_params_json TEXT NOT NULL,
    full_config_json TEXT NOT NULL, strategy_version TEXT NOT NULL, created_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS universe_validations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, universe_hash TEXT NOT NULL, ok INTEGER NOT NULL,
    detail_json TEXT NOT NULL, validated_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signal_runs (
    experiment TEXT NOT NULL, month_key TEXT NOT NULL,       -- e.g. '2026-09'
    signal_date TEXT NOT NULL, execution_date TEXT NOT NULL,
    strategy_version TEXT NOT NULL, status TEXT NOT NULL,    -- 'ok' | 'blocked'
    reason TEXT, n_qualified INTEGER, created_at_utc TEXT NOT NULL,
    PRIMARY KEY (experiment, month_key)
);
CREATE TABLE IF NOT EXISTS signals (
    experiment TEXT NOT NULL, month_key TEXT NOT NULL, symbol TEXT NOT NULL,
    strategy_version TEXT NOT NULL, signal_date TEXT NOT NULL,
    lookback_start_date TEXT, lookback_end_date TEXT,
    start_adj_close REAL, end_adj_close REAL, trailing_return REAL,
    qualifies INTEGER NOT NULL, target_weight REAL NOT NULL, data_status TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY (experiment, month_key, symbol)
);
CREATE TABLE IF NOT EXISTS rebalances (
    rebalance_id TEXT PRIMARY KEY, experiment TEXT NOT NULL, month_key TEXT NOT NULL,
    mode TEXT NOT NULL,                       -- 'dry_run' | 'paper'
    execution_date TEXT NOT NULL,
    status TEXT NOT NULL,                     -- planned|selling|buying|completed|blocked|needs_attention|dry_run_recorded
    reason TEXT, created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY, rebalance_id TEXT NOT NULL, mode TEXT NOT NULL,
    symbol TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL, ref_price REAL,
    target_qty INTEGER, current_qty REAL,
    status TEXT NOT NULL,      -- proposed|pending_submit|<broker status>|not_found|blocked
    broker_order_id TEXT, filled_qty REAL DEFAULT 0, filled_avg_price REAL,
    submit_attempts INTEGER NOT NULL DEFAULT 0,
    created_at_utc TEXT NOT NULL, submitted_at_utc TEXT, updated_at_utc TEXT NOT NULL,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT, client_order_id TEXT NOT NULL, broker_order_id TEXT,
    symbol TEXT NOT NULL, side TEXT NOT NULL, cumulative_filled_qty REAL NOT NULL,
    filled_avg_price REAL, recorded_at_utc TEXT NOT NULL,
    UNIQUE (client_order_id, cumulative_filled_qty)
);
CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, taken_at_utc TEXT NOT NULL, context TEXT,
    equity REAL, cash REAL, buying_power REAL, non_marginable_buying_power REAL,
    long_market_value REAL, exposure_pct REAL, pending_buy_notional REAL, pending_sell_notional REAL
);
CREATE TABLE IF NOT EXISTS position_snapshots (
    snapshot_id INTEGER NOT NULL, symbol TEXT NOT NULL, qty REAL NOT NULL, market_value REAL,
    avg_entry_price REAL, side TEXT
);
CREATE TABLE IF NOT EXISTS open_order_snapshots (
    snapshot_id INTEGER NOT NULL, client_order_id TEXT, broker_order_id TEXT, symbol TEXT,
    side TEXT, qty REAL, filled_qty REAL, status TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL, level TEXT NOT NULL,
    category TEXT NOT NULL, message TEXT NOT NULL, detail_json TEXT
);
CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at_utc TEXT NOT NULL, period TEXT NOT NULL,
    start_date TEXT NOT NULL, end_date TEXT NOT NULL, experiment TEXT NOT NULL,
    config_hash TEXT NOT NULL, assumptions_json TEXT NOT NULL, metrics_json TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # safer on SD cards with concurrent readers
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def log_event(conn, level: str, category: str, message: str, detail=None, now=None) -> None:
    conn.execute(
        "INSERT INTO events (ts_utc, level, category, message, detail_json) VALUES (?,?,?,?,?)",
        (iso(now or utcnow()), level, category, message,
         json.dumps(detail, default=str) if detail is not None else None),
    )
    conn.commit()

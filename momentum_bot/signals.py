"""Experiment freezing and persisted monthly signals for live/paper operation."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from . import calendar_utils as cal
from .config import STRATEGY_VERSION
from .data import load_closes
from .db import iso, log_event, utcnow
from .strategy import compute_month_signal

log = logging.getLogger(__name__)


class ExperimentMismatch(RuntimeError):
    pass


def register_experiment(conn, cfg, now=None) -> None:
    """First use stores the frozen parameters; later runs must match exactly."""
    row = conn.execute("SELECT config_hash FROM experiments WHERE name=?", (cfg.experiment_name,)).fetchone()
    h = cfg.experiment_hash()
    if row is None:
        conn.execute(
            "INSERT INTO experiments (name, config_hash, frozen_params_json, full_config_json,"
            " strategy_version, created_at_utc) VALUES (?,?,?,?,?,?)",
            (cfg.experiment_name, h, json.dumps(cfg.frozen_params(), sort_keys=True), cfg.to_json(),
             STRATEGY_VERSION, iso(now or utcnow())))
        conn.commit()
        log_event(conn, "info", "experiment", f"registered experiment {cfg.experiment_name}", {"hash": h})
    elif row["config_hash"] != h:
        raise ExperimentMismatch(
            f"experiment '{cfg.experiment_name}' was frozen with different parameters. "
            "Revert the change or set a new [experiment] name.")


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def generate_signal(conn, cfg, now: datetime | None = None) -> dict | None:
    """If the last completed session is a month-end, compute and persist its signal once.
    Returns the signal_runs row (as dict) for that month, or None if not a month-end."""
    now = now or utcnow()
    register_experiment(conn, cfg, now)
    last = cal.last_completed_session(now, cfg.completed_bar_delay_minutes)
    if not cal.is_last_session_of_month(last):
        return None
    return compute_and_store_signal(conn, cfg, last, now)


def compute_and_store_signal(conn, cfg, signal_date: date, now: datetime | None = None) -> dict:
    now = now or utcnow()
    mk = month_key(signal_date)
    existing = conn.execute("SELECT * FROM signal_runs WHERE experiment=? AND month_key=?",
                            (cfg.experiment_name, mk)).fetchone()
    exec_date = cal.next_session(signal_date)
    if existing is not None:
        # An accepted signal is never recomputed. A blocked one may be retried (e.g. after a
        # data fix) as long as its execution session has not started.
        if existing["status"] == "ok" or now >= cal.session_open_utc(exec_date):
            return dict(existing)
        conn.execute("DELETE FROM signals WHERE experiment=? AND month_key=?", (cfg.experiment_name, mk))
        conn.execute("DELETE FROM signal_runs WHERE experiment=? AND month_key=?", (cfg.experiment_name, mk))

    start = cal.month_end_session_n_months_before(signal_date, cfg.lookback_months) - timedelta(days=7)
    closes = {s: load_closes(conn, s, cfg.bars_feed, "all", start, signal_date) for s in cfg.symbols}
    sig = compute_month_signal(closes, signal_date, cfg.symbols, cfg.lookback_months,
                               cfg.max_weight_per_symbol, cfg.max_total_exposure,
                               cfg.max_missing_sessions_in_lookback, cfg.max_abs_daily_return)
    ts = iso(now)
    for r in sig.symbols:
        conn.execute(
            "INSERT INTO signals (experiment, month_key, symbol, strategy_version, signal_date,"
            " lookback_start_date, lookback_end_date, start_adj_close, end_adj_close, trailing_return,"
            " qualifies, target_weight, data_status, created_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cfg.experiment_name, mk, r.symbol, STRATEGY_VERSION, signal_date.isoformat(),
             r.lookback_start.isoformat() if r.lookback_start else None, r.lookback_end.isoformat(),
             r.start_close, r.end_close, r.trailing_return, int(r.qualifies), r.target_weight,
             r.data_status, ts))
    n_q = sum(r.qualifies for r in sig.symbols)
    conn.execute(
        "INSERT INTO signal_runs (experiment, month_key, signal_date, execution_date, strategy_version,"
        " status, reason, n_qualified, created_at_utc) VALUES (?,?,?,?,?,?,?,?,?)",
        (cfg.experiment_name, mk, signal_date.isoformat(), exec_date.isoformat(), STRATEGY_VERSION,
         sig.status, sig.reason, n_q, ts))
    conn.commit()
    log_event(conn, "warning" if sig.status != "ok" else "info", "signal",
              f"signal {mk}: {sig.status} ({sig.reason})", sig.weights, now)
    return dict(conn.execute("SELECT * FROM signal_runs WHERE experiment=? AND month_key=?",
                             (cfg.experiment_name, mk)).fetchone())


def load_target_weights(conn, cfg, mk: str) -> dict[str, float]:
    return {r["symbol"]: r["target_weight"] for r in conn.execute(
        "SELECT symbol, target_weight FROM signals WHERE experiment=? AND month_key=?",
        (cfg.experiment_name, mk))}


def pending_signal(conn, cfg, today: date) -> dict | None:
    """The accepted signal whose execution window contains `today`, if any."""
    for r in conn.execute("SELECT * FROM signal_runs WHERE experiment=? AND status='ok'"
                          " ORDER BY month_key DESC LIMIT 3", (cfg.experiment_name,)):
        exec_date = date.fromisoformat(r["execution_date"])
        if exec_date <= today and cal.sessions_between(exec_date, today) <= cfg.max_execution_delay_sessions:
            return dict(r)
    return None

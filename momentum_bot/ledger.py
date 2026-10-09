"""Hypothesis ledger: what was tested, the prediction written BEFORE testing, and what happened.

Rules enforced here (not in prompts):
* A backtest needs a pre-registered prediction for the current experiment (`predict`).
* The prediction can never be edited or deleted (SQLite triggers in db.py).
* At most MAX_REVISIONS experiments per idea; more attempts just fit noise.
* Every distinct config ever backtested counts as a trial, which raises the bar the
  Deflated Sharpe Ratio has to clear.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
import tomllib
from pathlib import Path
from statistics import NormalDist

from .db import iso, utcnow

MAX_REVISIONS = 5
STATUSES = ("untested", "failed", "passed_backtest", "passed_eval", "passed_paper", "paused", "retired")
REQUIRED = ("idea_key", "idea", "source", "market", "expected_annual_return", "expected_max_drawdown",
            "fail_if_sharpe_below", "fail_if_drawdown_worse_than")
DSR_THRESHOLD = 0.95
EULER_GAMMA = 0.5772156649


class LedgerError(RuntimeError):
    pass


def register_prediction(conn, cfg, path: str | Path) -> dict:
    raw = Path(path).read_bytes()
    pred = tomllib.loads(raw.decode())
    missing = [k for k in REQUIRED if k not in pred]
    if missing:
        raise LedgerError(f"prediction file is missing {missing}")
    if get(conn, cfg.experiment_name):
        raise LedgerError(f"experiment {cfg.experiment_name!r} already has a prediction; it cannot be changed. "
                          "Change [experiment] name to test a revision.")
    n = conn.execute("SELECT COUNT(*) FROM hypotheses WHERE idea_key=?", (pred["idea_key"],)).fetchone()[0]
    if n >= MAX_REVISIONS:
        raise LedgerError(f"idea {pred['idea_key']!r} already has {n} variations (cap {MAX_REVISIONS}). "
                          "An idea that needs this many rewrites has answered the question.")
    late = conn.execute("SELECT COUNT(*) FROM backtest_runs WHERE experiment=?",
                        (cfg.experiment_name,)).fetchone()[0]
    pred["registered_after_backtests"] = late       # honest record if results were already seen
    pred["config_hash"] = cfg.experiment_hash()
    blob = json.dumps(pred, sort_keys=True, default=str)
    conn.execute("INSERT INTO hypotheses (experiment, idea_key, idea, source, market, prediction_json,"
                 " prediction_sha256, created_at_utc, updated_at_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                 (cfg.experiment_name, pred["idea_key"], pred["idea"], pred["source"], pred["market"], blob,
                  hashlib.sha256(blob.encode()).hexdigest(), iso(utcnow()), iso(utcnow())))
    conn.commit()
    return get(conn, cfg.experiment_name)


def get(conn, experiment: str) -> dict | None:
    r = conn.execute("SELECT * FROM hypotheses WHERE experiment=?", (experiment,)).fetchone()
    return dict(r) if r else None


def require(conn, cfg) -> dict:
    h = get(conn, cfg.experiment_name)
    if not h:
        raise LedgerError(f"no prediction registered for {cfg.experiment_name!r}. Write one first "
                          "(see research/README.md), then `python -m momentum_bot predict <file>`.")
    return h


def update(conn, experiment: str, **fields) -> None:
    if "status" in fields and fields["status"] not in STATUSES:
        raise LedgerError(f"status must be one of {STATUSES}")
    fields = {k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v) for k, v in fields.items()}
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE hypotheses SET {sets}, updated_at_utc=? WHERE experiment=?",
                 (*fields.values(), iso(utcnow()), experiment))
    conn.commit()


def search(conn, status=None, idea_key=None, regime=None) -> list[dict]:
    q, args = "SELECT * FROM hypotheses WHERE 1=1", []
    for col, v in (("status", status), ("idea_key", idea_key), ("regime", regime)):
        if v:
            q += f" AND {col}=?"
            args.append(v)
    return [dict(r) for r in conn.execute(q + " ORDER BY created_at_utc", args)]


# ----------------------------------------------------------------- statistics
def trials(conn) -> tuple[int, list[float]]:
    """(distinct configs ever backtested, their per-session momentum Sharpes where recorded)."""
    srs = {}
    for h, m in conn.execute("SELECT config_hash, metrics_json FROM backtest_runs WHERE period='dev'"):
        row = next((r for r in json.loads(m) if r.get("strategy") == "momentum"), {})
        srs.setdefault(h, row.get("sharpe_daily"))
    return max(len(srs), 1), [v for v in srs.values() if v is not None]


def deflated_sharpe(sr: float, n_obs: int, skew: float, kurt: float, n_trials: int, trial_srs) -> float:
    """Bailey & Lopez de Prado (2014). All Sharpes per-period (not annualised). Probability that
    the true Sharpe is > 0 after adjusting for how many configurations were tried."""
    var = statistics.variance(trial_srs) if len(trial_srs) >= 2 else 1.0 / max(n_obs, 2)
    # ponytail: with < 2 recorded trials, use 1/T (the null sampling variance of a Sharpe estimate)
    z = NormalDist().inv_cdf
    sr0 = 0.0 if n_trials < 2 else math.sqrt(var) * (
        (1 - EULER_GAMMA) * z(1 - 1 / n_trials) + EULER_GAMMA * z(1 - 1 / (n_trials * math.e)))
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if n_obs < 2 or denom <= 0:
        return 0.0
    return NormalDist().cdf((sr - sr0) * math.sqrt(n_obs - 1) / math.sqrt(denom))


def regime(bench: dict) -> str:
    # ponytail: coarse label from the equal-weight benchmark; enough to group ledger rows
    vol, ann = bench.get("annualized_volatility") or 0, bench.get("annualized_return")
    ann = ann if ann is not None else bench.get("net_return", 0)
    if vol > 0.20:
        return "high_volatility"
    if ann > 0.05:
        return "trending_up"
    if ann < -0.05:
        return "trending_down"
    return "choppy"


def grade(conn, cfg, period: str, rows: list[dict]) -> dict:
    """Compare a finished backtest against the pre-registered prediction and update the ledger."""
    h = require(conn, cfg)
    pred = json.loads(h["prediction_json"])
    m = rows[0]
    n, trial_srs = trials(conn)
    dsr = deflated_sharpe(m["sharpe_daily"], m["n_sessions"], m["skew"], m["kurtosis"], n, trial_srs)
    fails = []
    if m["sharpe"] < pred["fail_if_sharpe_below"]:
        fails.append(f"Sharpe {m['sharpe']:.2f} < predicted floor {pred['fail_if_sharpe_below']}")
    if m["max_drawdown"] < pred["fail_if_drawdown_worse_than"]:
        fails.append(f"max drawdown {m['max_drawdown']:.1%} worse than {pred['fail_if_drawdown_worse_than']:.1%}")
    if dsr < DSR_THRESHOLD:
        fails.append(f"Deflated Sharpe {dsr:.2f} < {DSR_THRESHOLD} ({n} configs tried)")
    if (m.get("stress_sharpe") or 0) <= 0:
        fails.append("does not survive 2x costs + 1-session-late fills")
    verdict = {
        "period": period, "passed": not fails, "fail_reasons": fails, "deflated_sharpe": dsr,
        "n_trials": n, "sharpe": m["sharpe"], "annualized_return": m["annualized_return"],
        "max_drawdown": m["max_drawdown"], "stress_sharpe": m.get("stress_sharpe"),
        "predicted": {"annual_return": pred["expected_annual_return"],
                      "max_drawdown": pred["expected_max_drawdown"]},
    }
    if period == "dev":
        update(conn, cfg.experiment_name, backtest_result=verdict, regime=regime(rows[1]),
               status="passed_backtest" if not fails else "failed")
    else:
        update(conn, cfg.experiment_name, eval_result=verdict, status="passed_eval" if not fails else "failed")
    return verdict

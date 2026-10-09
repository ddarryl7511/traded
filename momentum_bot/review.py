"""Post-mortem: paper results vs a backtest of the SAME window vs the pre-registered prediction.
A strategy that backtests well and lags live was overfit or is paying more in costs than assumed."""
from __future__ import annotations

import json
import statistics
from datetime import date, timedelta
from pathlib import Path

from . import calendar_utils as cal
from . import ledger
from .backtest import load_bars, metrics, momentum_weight_fn, simulate
from .db import utcnow
from .execution import halted

GAP_ALERT = -0.02          # live trails the same-window backtest by more than 2 points


def shadow_backtest(conn, cfg, signal_date: date, end: date, capital: float) -> float | None:
    start = cal.month_end_session_n_months_before(signal_date, cfg.lookback_months + 1) - timedelta(days=7)
    opens, closes = load_bars(conn, list(cfg.symbols), cfg.bars_feed, start, end)
    sessions = cal.sessions_in_range(signal_date, end)
    if len(sessions) < 2:
        return None
    r = simulate("shadow", sessions, opens, closes, momentum_weight_fn(cfg, closes), capital,
                 commission_per_order=cfg.commission_per_order, commission_bps=cfg.commission_bps,
                 slippage_bps=cfg.slippage_bps, cash_rate_annual=cfg.cash_rate_annual)
    return metrics(r, capital).get("net_return")


def fill_slippage_bps(conn, cfg) -> list[float]:
    """Signed cost per filled paper order vs the reference price used for sizing (+ = worse)."""
    out = []
    for side, ref, px in conn.execute(
            "SELECT o.side, o.ref_price, o.filled_avg_price FROM orders o JOIN rebalances r USING (rebalance_id)"
            " WHERE o.mode='paper' AND r.experiment=? AND o.filled_avg_price > 0 AND o.ref_price > 0",
            (cfg.experiment_name,)):
        out.append(1e4 * ((px / ref - 1) if side == "buy" else (ref / px - 1)))
    return out


def build_review(conn, cfg, account: dict) -> tuple[str, dict]:
    today = utcnow().date()
    h = ledger.get(conn, cfg.experiment_name)
    lines = [f"# Review {today} - {cfg.experiment_name}", ""]
    flags = []
    if h:
        pred = json.loads(h["prediction_json"])
        lines += [f"**Idea:** {h['idea']} (`{h['idea_key']}`, status `{h['status']}`)",
                  f"**Predicted:** {pred['expected_annual_return']:+.1%}/yr, max drawdown "
                  f"{pred['expected_max_drawdown']:.1%}", ""]
    else:
        flags.append("no pre-registered prediction for this experiment")
    rebs = conn.execute("SELECT rebalance_id, month_key, execution_date, status FROM rebalances"
                        " WHERE experiment=? AND mode='paper' ORDER BY created_at_utc", (cfg.experiment_name,)).fetchall()
    summary = {"date": today.isoformat(), "paper_rebalances": len(rebs)}
    if not rebs:
        lines.append("No paper rebalances yet; nothing to compare. (Dry-run results are not evidence.)")
    else:
        first = rebs[0]
        start_eq = conn.execute("SELECT equity FROM account_snapshots WHERE context=? ORDER BY id LIMIT 1",
                                (f"{first['rebalance_id']}:start",)).fetchone()
        start_eq = start_eq[0] if start_eq else cfg.initial_capital
        eq = account.get("equity") or start_eq
        live = eq / start_eq - 1
        last = cal.last_completed_session(utcnow(), cfg.completed_bar_delay_minutes)
        shadow = shadow_backtest(conn, cfg, cal.previous_session(date.fromisoformat(first["execution_date"])),
                                 last, start_eq)
        slips = fill_slippage_bps(conn, cfg)
        avg_slip = statistics.fmean(slips) if slips else None
        summary.update(start_equity=start_eq, equity=eq, live_return=live, backtest_same_window=shadow,
                       gap=None if shadow is None else live - shadow, avg_fill_slippage_bps=avg_slip,
                       n_fills=len(slips))
        lines += ["| | value |", "|---|---|",
                  f"| Paper since {first['execution_date']} | {live:+.2%} (${start_eq:,.0f} -> ${eq:,.0f}) |",
                  f"| Backtest of same window | {'n/a' if shadow is None else f'{shadow:+.2%}'} |",
                  f"| Avg fill cost vs sizing price | {'n/a' if avg_slip is None else f'{avg_slip:+.1f} bps'}"
                  f" (backtest assumes {cfg.slippage_bps:g}) over {len(slips)} fills |", ""]
        if shadow is not None and live - shadow < GAP_ALERT:
            flags.append(f"live trails its own backtest by {live - shadow:+.2%}: overfit or execution drag")
        if avg_slip is not None and avg_slip > 2 * cfg.slippage_bps:
            flags.append(f"real fill costs ({avg_slip:.1f} bps) are over 2x the backtest assumption")
        bad = [r["month_key"] for r in rebs if r["status"] not in ("completed",)]
        if bad:
            flags.append(f"rebalances not cleanly completed: {bad}")
    why = halted(conn)
    if why:
        flags.append(f"KILL SWITCH latched: {why}")
    warns = conn.execute("SELECT category, message FROM events WHERE level IN ('warning','error')"
                         " AND ts_utc >= datetime('now','-7 days') ORDER BY id DESC LIMIT 10").fetchall()
    lines += ["## Flags", *([f"- {f}" for f in flags] or ["- none"]), "",
              "## Warnings/errors (7d)", *([f"- [{c}] {m}" for c, m in warns] or ["- none"])]
    summary["flags"] = flags
    if h:
        ledger.update(conn, cfg.experiment_name, paper_result=summary)
    return "\n".join(lines) + "\n", summary


def write_review(root: Path, text: str) -> Path:
    p = root / "journal" / f"review-{utcnow().date()}.md"
    p.parent.mkdir(exist_ok=True)
    p.write_text(text)
    return p

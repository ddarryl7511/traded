"""Command-line entry point: `python -m momentum_bot <command>`."""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from . import calendar_utils as cal
from .config import ConfigError, load_config, paper_orders_allowed, universe_hash
from .db import connect, log_event, utcnow

log = logging.getLogger("momentum_bot")
ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@contextmanager
def single_instance(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("another momentum_bot process is running; exiting")
        yield


def _apis(cfg, need_broker=True):
    from .alpaca_api import MarketDataAPI, PaperBroker
    md = MarketDataAPI(cfg)
    return md, (PaperBroker(cfg) if need_broker else None)


def cmd_init(cfg, args, conn):
    print(f"database ready: {cfg.database}")


def cmd_doctor(cfg, args, conn):
    allowed, why = paper_orders_allowed(cfg)
    print(f"experiment: {cfg.experiment_name}  hash={cfg.experiment_hash()[:12]}")
    print(f"universe: {', '.join(cfg.symbols)}  review_hash(current)={universe_hash(cfg.symbols)}")
    print(f"paper order submission: {'ENABLED' if allowed else 'disabled'} ({why})")
    print(f"bars feed={cfg.bars_feed} price feed={cfg.price_feed}")
    _, broker = _apis(cfg)
    acct = broker.get_account()
    print("paper account:", {k: acct[k] for k in ("status", "equity", "cash", "multiplier",
                                                   "shorting_enabled", "trading_blocked")})
    print("account configuration:", broker.get_account_configuration())
    print("clock:", broker.get_clock())
    print("NOTE: the bot never uses margin or shorting regardless of these account settings.")


def cmd_validate_universe(cfg, args, conn):
    from .universe import validate_universe
    _, broker = _apis(cfg)
    ok, problems = validate_universe(conn, cfg, broker)
    for p in problems:
        print("PROBLEM:", p)
    print(f"universe hash: {universe_hash(cfg.symbols)}")
    if ok:
        print("Automated checks passed. Review each fund's prospectus yourself, then set\n"
              f"  [universe] reviewed = true\n  review_hash = \"{universe_hash(cfg.symbols)}\"")
    return 0 if ok else 2


def cmd_download(cfg, args, conn):
    from .data import update_bars
    md, _ = _apis(cfg, need_broker=False)
    print(json.dumps(update_bars(conn, cfg, md), indent=2, default=str))


def cmd_quality(cfg, args, conn):
    from .data import run_quality_checks
    last = cal.last_completed_session(utcnow(), cfg.completed_bar_delay_minutes)
    issues = run_quality_checks(conn, cfg, last)
    print("\n".join(issues) if issues else f"no issues (last completed session {last})")


def cmd_signal(cfg, args, conn):
    from .signals import compute_and_store_signal, generate_signal, register_experiment
    if args.date:
        d = date.fromisoformat(args.date)
        last = cal.last_completed_session(utcnow(), cfg.completed_bar_delay_minutes)
        if d > last:
            raise SystemExit(f"{d} is not a completed session yet")
        register_experiment(conn, cfg)
        row = compute_and_store_signal(conn, cfg, d)
    else:
        row = generate_signal(conn, cfg)
    if row is None:
        print("last completed session is not a month-end; no signal")
        return
    print(json.dumps(row, indent=2))
    for r in conn.execute("SELECT symbol, trailing_return, qualifies, target_weight, data_status FROM signals"
                          " WHERE experiment=? AND month_key=?", (cfg.experiment_name, row["month_key"])):
        tr = "n/a" if r[1] is None else f"{r[1]:+.2%}"
        print(f"  {r[0]:6s} 12m={tr:>8s} qualifies={bool(r[2])!s:5s} weight={r[3]:.2%} data={r[4]}")


def cmd_backtest(cfg, args, conn):
    from . import ledger
    from .backtest import run_backtest
    h = ledger.require(conn, cfg)                      # prediction must exist before any test
    if args.period in ("eval", "full"):
        if h["status"] != "passed_backtest":
            raise SystemExit(f"evaluation period is sealed: ledger status is {h['status']!r}, needs 'passed_backtest'")
        used = conn.execute("SELECT COUNT(*) FROM backtest_runs WHERE period IN ('eval','full') AND config_hash=?",
                            (cfg.experiment_hash(),)).fetchone()[0]
        if used:
            raise SystemExit("this experiment already used its one look at the evaluation period")
    if args.period in ("eval", "full") and not args.confirm_evaluation_period:
        raise SystemExit("The evaluation period is meant to stay untouched until your rules are final.\n"
                         "Re-run with --confirm-evaluation-period if you really want to look at it.")
    if args.period in ("eval", "full"):
        n = conn.execute("SELECT COUNT(*) FROM backtest_runs WHERE period IN ('eval','full')").fetchone()[0]
        print(f"WARNING: evaluation period has been viewed {n} time(s) before. Do not tune on it.")
        log_event(conn, "warning", "backtest", "evaluation period viewed", {"period": args.period})
    rows = run_backtest(conn, cfg, args.period, cfg.reports_dir)
    print("SIMULATED HISTORICAL RESULTS (not paper-account results). Past performance does not "
          "predict future results.")
    for r in rows:
        if "error" in r:
            print(r)
            continue
        ann = "n/a" if r["annualized_return"] is None else f"{r['annualized_return']:+.2%}"
        vol = "n/a" if r["annualized_volatility"] is None else f"{r['annualized_volatility']:.2%}"
        print(f"{r['strategy']:28s} net={r['net_return']:+.2%} ann={ann} vol={vol} "
              f"mdd={r['max_drawdown']:.2%} expo={r['average_exposure']:.1%} "
              f"turnover={r['turnover_total']:.2f} orders={r['order_count']} costs=${r['total_costs']:.2f}")
    m = rows[0]
    print(f"stress (2x costs, fills 1 session late): net={m['stress_net_return']:+.2%} "
          f"sharpe={m['stress_sharpe']:.2f} mdd={m['stress_max_drawdown']:.2%}")
    print("worst benchmark months:", ", ".join(f"{k} bench={v['benchmark']:+.1%} strat={v['strategy']:+.1%}"
                                               for k, v in m["worst_months"].items()))
    print(f"walk-forward: positive in {m['years_positive']}/{m['years_total']} years:",
          ", ".join(f"{y} {v['strategy']:+.1%} (bench {v['benchmark']:+.1%})" for y, v in m["yearly"].items()))
    v = ledger.grade(conn, cfg, "dev" if args.period == "dev" else "eval", rows)
    print(f"Deflated Sharpe {v['deflated_sharpe']:.2f} over {v['n_trials']} configs tried | predicted "
          f"{v['predicted']['annual_return']:+.1%}/yr mdd {v['predicted']['max_drawdown']:.1%}")
    print("VERDICT:", "PASS" if v["passed"] else "FAIL - " + "; ".join(v["fail_reasons"]))
    print(f"CSV reports written to {cfg.reports_dir}")


def _executor(cfg, conn):
    from .execution import Executor
    from .notify import make_notifier
    md, broker = _apis(cfg)
    return Executor(conn, cfg, broker, md, notify=make_notifier())


def cmd_dry_run(cfg, args, conn):
    print(_executor(cfg, conn).run(preview=True))


def cmd_trade(cfg, args, conn):
    allowed, why = paper_orders_allowed(cfg)
    print(f"mode: {'PAPER ORDERS' if allowed else 'DRY RUN'} ({why})")
    print(_executor(cfg, conn).run())


def cmd_run(cfg, args, conn):
    """Daily job for the systemd timer: update data, form signal if month-end, execute if due."""
    from .notify import make_notifier
    notify = make_notifier()
    try:
        cmd_download(cfg, args, conn)
    except Exception as exc:  # noqa: BLE001
        log_event(conn, "error", "run", "data update failed", {"error": str(exc)})
        notify(f"[momentum-bot] data update failed: {type(exc).__name__}")
        raise
    cmd_signal(cfg, argparse.Namespace(date=None), conn)
    last = cal.last_completed_session(utcnow(), cfg.completed_bar_delay_minutes)
    blocked = conn.execute("SELECT month_key, reason FROM signal_runs WHERE experiment=? AND signal_date=?"
                           " AND status='blocked'", (cfg.experiment_name, last.isoformat())).fetchone()
    if blocked:
        notify(f"[momentum-bot] signal {blocked[0]} blocked, no rebalance: {blocked[1]}")
    cmd_trade(cfg, args, conn)


def cmd_status(cfg, args, conn):
    allowed, why = paper_orders_allowed(cfg)
    print(f"paper orders: {'ENABLED' if allowed else 'disabled'} ({why})")
    for title, sql in (
            ("latest bars", "SELECT symbol, adjustment, MAX(session_date) FROM bars GROUP BY symbol, adjustment"),
            ("signal runs", "SELECT month_key, signal_date, execution_date, status, n_qualified, reason"
                            " FROM signal_runs ORDER BY month_key DESC LIMIT 6"),
            ("rebalances", "SELECT rebalance_id, status, reason FROM rebalances ORDER BY created_at_utc DESC LIMIT 6"),
            ("recent events", "SELECT ts_utc, level, category, message FROM events ORDER BY id DESC LIMIT 10")):
        print(f"--- {title}")
        for r in conn.execute(sql):
            print("  ", tuple(r))


def build_brief(cfg, conn) -> str:
    """Short plain-English summary from the local database only (no API calls)."""
    from datetime import timedelta
    allowed, _ = paper_orders_allowed(cfg)
    last = cal.last_completed_session(utcnow(), cfg.completed_bar_delay_minutes)
    nxt = next(d for d in cal.month_end_sessions(last + timedelta(days=1), last + timedelta(days=45)) if d > last)
    lines = [f"Mode: {'PAPER ORDERS' if allowed else 'dry run (no orders sent)'} | experiment {cfg.experiment_name}"]
    bars = conn.execute("SELECT MIN(d) FROM (SELECT MAX(session_date) d FROM bars GROUP BY symbol, adjustment)").fetchone()[0]
    lines.append(f"Data: through {bars or 'none'} (last completed session {last})")
    acct = conn.execute("SELECT taken_at_utc, equity, cash, exposure_pct FROM account_snapshots"
                        " ORDER BY id DESC LIMIT 1").fetchone()
    if acct:
        lines.append(f"Account ({acct[0][:10]}): equity ${acct[1]:,.2f}, cash ${acct[2]:,.2f}, "
                     f"invested {(acct[3] or 0):.0%}")
        snap = conn.execute("SELECT MAX(snapshot_id) FROM position_snapshots").fetchone()[0]
        pos = conn.execute("SELECT symbol, qty, market_value FROM position_snapshots WHERE snapshot_id=?"
                           " ORDER BY symbol", (snap,)).fetchall() if snap is not None else []
        lines.append("Holdings: " + (", ".join(f"{s} {q:g} (${(mv or 0):,.0f})" for s, q, mv in pos) or "none"))
    sig = conn.execute("SELECT month_key, status, n_qualified, reason FROM signal_runs WHERE experiment=?"
                       " ORDER BY month_key DESC LIMIT 1", (cfg.experiment_name,)).fetchone()
    if sig:
        picks = [r[0] for r in conn.execute("SELECT symbol FROM signals WHERE experiment=? AND month_key=?"
                                            " AND qualifies=1 ORDER BY symbol", (cfg.experiment_name, sig[0]))]
        detail = ", ".join(picks) if sig[1] == "ok" else (sig[3] or "")
        lines.append(f"Last signal {sig[0]}: {sig[1]}, {sig[2] or 0} qualify ({detail or 'none'})")
    reb = conn.execute("SELECT month_key, mode, status, reason FROM rebalances WHERE experiment=?"
                       " ORDER BY created_at_utc DESC LIMIT 1", (cfg.experiment_name,)).fetchone()
    if reb:
        lines.append(f"Last rebalance {reb[0]} ({reb[1]}): {reb[2]}" + (f" - {reb[3]}" if reb[3] else ""))
    probs = conn.execute("SELECT COUNT(*) FROM events WHERE level IN ('warning','error')"
                         " AND ts_utc >= datetime('now', '-7 days')").fetchone()[0]
    lines.append(f"Warnings/errors (7d): {probs}")
    lines.append(f"Next signal: {nxt} close -> trades {cal.next_session(nxt)}")
    return "\n".join(lines)


def cmd_brief(cfg, args, conn):
    text = build_brief(cfg, conn)
    print(text)
    if args.send:
        from .notify import make_notifier
        make_notifier()("brief\n" + text)


def cmd_predict(cfg, args, conn):
    from . import ledger
    h = ledger.register_prediction(conn, cfg, args.file)
    print(f"registered (immutable) prediction for {h['experiment']}: sha256 {h['prediction_sha256'][:16]}")
    if json.loads(h["prediction_json"])["registered_after_backtests"]:
        print("NOTE: backtests of this experiment already existed; the ledger records that.")


def cmd_ledger(cfg, args, conn):
    from . import ledger
    if args.set_status or args.lessons:
        if not ledger.get(conn, args.experiment):
            raise SystemExit(f"no ledger row for {args.experiment!r}")
        ledger.update(conn, args.experiment, **{k: v for k, v in
                                                (("status", args.set_status), ("lessons", args.lessons)) if v})
    rows = ledger.search(conn, args.status, args.idea_key, args.regime)
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    for r in rows:
        print(f"{r['experiment']:24s} {r['status']:16s} {r['regime'] or '-':16s} {r['idea_key']}: {r['idea']}")
        if r["lessons"]:
            print(f"{'':24s} lessons: {r['lessons']}")
    if not rows:
        print("ledger is empty")


def cmd_halt(cfg, args, conn):
    log_event(conn, "error", "halt", f"manual halt: {args.reason}")
    print("trading HALTED; `resume` clears it")


def cmd_resume(cfg, args, conn):
    from .execution import halted
    print(f"clearing halt: {halted(conn) or 'was not halted'}")
    log_event(conn, "warning", "resume", "kill switch cleared by operator")


def cmd_review(cfg, args, conn):
    from .review import build_review, write_review
    _, broker = _apis(cfg)
    text, summary = build_review(conn, cfg, broker.get_account())
    print(text)
    print("written to", write_review(ROOT, text))
    if args.send:
        from .notify import make_notifier
        flags = summary["flags"]
        make_notifier()(f"weekly review: {summary.get('live_return', 0):+.2%} live vs "
                        f"{summary.get('backtest_same_window') or 0:+.2%} backtest | "
                        + ("FLAGS: " + "; ".join(flags) if flags else "no flags"))


def cmd_export(cfg, args, conn):
    from .reports import export_all
    for p in export_all(conn, cfg.reports_dir):
        print(p)


COMMANDS = {
    "init": (cmd_init, "create the SQLite database"),
    "doctor": (cmd_doctor, "show config and read-only paper account status"),
    "validate-universe": (cmd_validate_universe, "check universe assets via Alpaca (read-only)"),
    "download": (cmd_download, "download/update daily bars (idempotent)"),
    "quality": (cmd_quality, "run data-quality checks"),
    "signal": (cmd_signal, "compute the month-end signal if due"),
    "backtest": (cmd_backtest, "run the simulated historical backtest"),
    "dry-run": (cmd_dry_run, "record proposed orders for the latest signal now (never submits)"),
    "trade": (cmd_trade, "execute today's scheduled rebalance (dry-run unless enabled)"),
    "run": (cmd_run, "daily job: download + signal + trade"),
    "status": (cmd_status, "summarise stored state"),
    "brief": (cmd_brief, "short summary of what the bot is up to (--send posts it to the webhook)"),
    "export": (cmd_export, "export all tables to CSV"),
    "predict": (cmd_predict, "pre-register the prediction for the current experiment (immutable)"),
    "ledger": (cmd_ledger, "search the hypothesis ledger / record status and lessons"),
    "halt": (cmd_halt, "kill switch: block all orders until `resume`"),
    "resume": (cmd_resume, "clear the kill switch"),
    "review": (cmd_review, "post-mortem: paper vs same-window backtest vs prediction (--send)"),
}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="momentum_bot", description="Paper-only monthly momentum research bot")
    p.add_argument("--config", default=str(ROOT / "config" / "config.toml"))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    for name, (_, help_) in COMMANDS.items():
        sp = sub.add_parser(name, help=help_)
        if name == "signal":
            sp.add_argument("--date", help="month-end session date YYYY-MM-DD (default: latest completed)")
        if name == "backtest":
            sp.add_argument("--period", choices=["dev", "eval", "full"], default="dev")
            sp.add_argument("--confirm-evaluation-period", action="store_true")
        if name == "predict":
            sp.add_argument("file", help="prediction TOML, see research/README.md")
        if name == "ledger":
            sp.add_argument("--status")
            sp.add_argument("--idea-key")
            sp.add_argument("--regime")
            sp.add_argument("--json", action="store_true")
            sp.add_argument("--experiment", default=None, help="row to update (default: current)")
            sp.add_argument("--set-status")
            sp.add_argument("--lessons")
        if name == "halt":
            sp.add_argument("reason")
        if name == "review":
            sp.add_argument("--send", action="store_true")
        if name == "brief":
            sp.add_argument("--send", action="store_true", help="also post the brief to NOTIFY_WEBHOOK_URL")
    args = p.parse_args(argv)

    load_dotenv(ROOT / ".env")
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    from .logutil import setup_logging
    setup_logging(cfg.log_file, args.verbose)
    if getattr(args, "experiment", "unset") is None:
        args.experiment = cfg.experiment_name
    fn = COMMANDS[args.command][0]
    with single_instance(cfg.database.parent / ".momentum_bot.lock"):
        conn = connect(cfg.database)
        try:
            rc = fn(cfg, args, conn)
        except Exception as exc:
            log.exception("command %s failed", args.command)
            try:
                log_event(conn, "error", "cli", f"{args.command} failed: {exc}")
            except Exception:  # noqa: BLE001
                pass
            return 1
        finally:
            conn.close()
    return rc or 0

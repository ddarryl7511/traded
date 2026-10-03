"""Clearly sequenced daily backtest. SIMULATED HISTORICAL RESULTS - not paper-account results.

Daily loop, for each NYSE session d in the test period:
  1. If a rebalance is scheduled for d (signal formed at the PREVIOUS session's close), execute
     at d's open: sells first, then buys limited by available cash. Fill price = open +/- slippage.
  2. Accrue interest on idle cash (cash_rate_annual / 252 per session).
  3. Mark the portfolio to d's close.
  4. If d is the last session of its month, form a signal using closes <= d only and schedule
     execution for the next session.

Prices: the backtest uses the split+distribution adjusted ('all') series for signals AND for
fills/valuation. Distributions are therefore embedded in the price path (as if reinvested) and are
NOT also credited as cash - this is what prevents double counting. Consequence: whole-share rounding
is applied to adjusted prices, which differ from the prices that were actually quoted at the time
(documented limitation). The paper-trading path, by contrast, sizes orders with raw prices.
"""
from __future__ import annotations

import csv
import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from . import calendar_utils as cal
from .db import iso, utcnow
from .strategy import compute_month_signal


@dataclass
class SimResult:
    name: str
    dates: list[date] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)
    exposure: list[float] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    commissions: float = 0.0
    slippage_cost: float = 0.0
    traded_notional: float = 0.0
    rebalances: int = 0
    skipped: list[str] = field(default_factory=list)


def load_bars(conn, symbols, feed, start: date, end: date):
    opens, closes = {s: {} for s in symbols}, {s: {} for s in symbols}
    q = ("SELECT symbol, session_date, open, close FROM bars WHERE adjustment='all' AND feed=? AND"
         " session_date BETWEEN ? AND ? AND symbol IN (%s)" % ",".join("?" * len(symbols)))
    for sym, d, o, c in conn.execute(q, (feed, start.isoformat(), end.isoformat(), *symbols)):
        dd = date.fromisoformat(d)
        opens[sym][dd], closes[sym][dd] = o, c
    return opens, closes


def simulate(name, sessions, opens, closes, weight_fn, capital, commission_per_order=0.0,
             commission_bps=0.0, slippage_bps=0.0, cash_rate_annual=0.0) -> SimResult:
    res = SimResult(name)
    cash, shares = capital, {}
    last_close: dict[str, float] = {}
    pending: tuple[date, dict] | None = None
    slip, cbps = slippage_bps / 1e4, commission_bps / 1e4
    daily_rate = (1 + cash_rate_annual) ** (1 / 252) - 1

    for d in sessions:
        # 1. execute at today's open what was decided at an earlier close
        if pending and pending[0] == d:
            weights = pending[1]
            pending = None
            involved = set(shares) | {s for s, w in weights.items() if w > 0}
            missing = [s for s in involved if d not in opens.get(s, {})]
            if missing:
                res.skipped.append(f"{d}: missing open for {missing}")
            else:
                eq_open = cash + sum(q * opens[s][d] for s, q in shares.items())
                target = {s: int(math.floor(w * eq_open / opens[s][d])) for s, w in weights.items() if w > 0}
                orders = []
                for s in sorted(involved):
                    delta = target.get(s, 0) - shares.get(s, 0)
                    if delta:
                        orders.append((s, delta))
                for s, delta in [o for o in orders if o[1] < 0]:            # sells first
                    q = -delta
                    px = opens[s][d] * (1 - slip)
                    fee = commission_per_order + cbps * q * px
                    cash += q * px - fee
                    shares[s] -= q
                    if shares[s] == 0:
                        del shares[s]
                    res.trades.append({"strategy": name, "date": d, "symbol": s, "side": "sell", "qty": q,
                                       "price": px, "commission": fee})
                    res.commissions += fee
                    res.slippage_cost += q * opens[s][d] * slip
                    res.traded_notional += q * px
                for s, q in [o for o in orders if o[1] > 0]:               # then buys, cash-limited
                    px = opens[s][d] * (1 + slip)
                    while q > 0 and q * px + commission_per_order + cbps * q * px > cash:
                        q -= 1
                    if q <= 0:
                        continue
                    fee = commission_per_order + cbps * q * px
                    cash -= q * px + fee
                    shares[s] = shares.get(s, 0) + q
                    res.trades.append({"strategy": name, "date": d, "symbol": s, "side": "buy", "qty": q,
                                       "price": px, "commission": fee})
                    res.commissions += fee
                    res.slippage_cost += q * opens[s][d] * slip
                    res.traded_notional += q * px
                res.rebalances += 1
        elif pending and pending[0] < d:
            pending = None
        # 2. cash interest
        cash *= 1 + daily_rate
        # 3. mark to close (carry last known close if a bar is missing)
        for s in shares:
            if d in closes.get(s, {}):
                last_close[s] = closes[s][d]
        mv = sum(q * last_close[s] for s, q in shares.items())
        res.dates.append(d)
        res.equity.append(cash + mv)
        res.exposure.append(mv / (cash + mv) if cash + mv > 0 else 0.0)
        # 4. signal at month-end close -> execute next session
        if cal.is_last_session_of_month(d):
            w = weight_fn(d)
            if w is None:
                res.skipped.append(f"{d}: signal blocked (data)")
            else:
                pending = (cal.next_session(d), w)
    return res


def metrics(r: SimResult, capital: float) -> dict:
    eq = r.equity
    if len(eq) < 2:
        return {"strategy": r.name, "error": "not enough data"}
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
    years = (r.dates[-1] - r.dates[0]).days / 365.25
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    avg_eq = statistics.fmean(eq)
    net = eq[-1] / capital - 1
    return {
        "strategy": r.name, "start": r.dates[0].isoformat(), "end": r.dates[-1].isoformat(),
        "final_equity": round(eq[-1], 2), "net_return": net,
        "annualized_return": (eq[-1] / capital) ** (1 / years) - 1 if years >= 1 else None,
        "annualized_volatility": statistics.stdev(rets) * math.sqrt(252) if len(rets) > 2 else None,
        "max_drawdown": mdd,
        "turnover_total": r.traded_notional / avg_eq,
        "turnover_annualized": (r.traded_notional / avg_eq / years) if years > 0 else None,
        "average_exposure": statistics.fmean(r.exposure),
        "order_count": len(r.trades), "rebalances_executed": r.rebalances,
        "rebalances_skipped": len(r.skipped),
        "commissions": round(r.commissions, 2), "slippage_cost": round(r.slippage_cost, 2),
        "total_costs": round(r.commissions + r.slippage_cost, 2),
    }


def period_bounds(cfg, period: str, conn) -> tuple[date, date]:
    latest = conn.execute("SELECT MAX(session_date) FROM bars WHERE adjustment='all' AND feed=?",
                          (cfg.bars_feed,)).fetchone()[0]
    if latest is None:
        raise RuntimeError("no adjusted bars stored; run `download` first")
    latest = date.fromisoformat(latest)
    if period == "dev":
        return cfg.dev_start, min(cfg.dev_end, latest)
    if period == "eval":
        return cfg.eval_start, min(cfg.eval_end or latest, latest)
    if period == "full":
        return cfg.dev_start, min(cfg.eval_end or latest, latest)
    raise ValueError(period)


def run_backtest(conn, cfg, period: str, out_dir: Path | None = None) -> list[dict]:
    start, end = period_bounds(cfg, period, conn)
    if end <= start:
        raise RuntimeError(f"empty period {start}..{end}")
    lookback_start = cal.month_end_session_n_months_before(start, cfg.lookback_months + 1) - timedelta(days=7)
    syms = list(cfg.symbols)
    opens, closes = load_bars(conn, syms, cfg.bars_feed, lookback_start, end)
    sessions = cal.sessions_in_range(start, end)
    kw = dict(capital=cfg.initial_capital, commission_per_order=cfg.commission_per_order,
              commission_bps=cfg.commission_bps, slippage_bps=cfg.slippage_bps,
              cash_rate_annual=cfg.cash_rate_annual)

    def strategy_w(d):
        sig = compute_month_signal(closes, d, syms, cfg.lookback_months, cfg.max_weight_per_symbol,
                                   cfg.max_total_exposure, cfg.max_missing_sessions_in_lookback,
                                   cfg.max_abs_daily_return)
        return sig.weights if sig.status == "ok" else None

    def static_w(scale):
        def fn(d):
            if any(d not in closes[s] for s in syms):
                return None
            return {s: scale / len(syms) for s in syms}
        return fn

    strat = simulate("momentum", sessions, opens, closes, strategy_w, **kw)
    avg_exp = statistics.fmean(strat.exposure) if strat.exposure else 0.0
    results = [
        strat,
        simulate("equal_weight_100pct", sessions, opens, closes, static_w(1.0), **kw),
        simulate(f"equal_weight_{avg_exp:.0%}_exposure", sessions, opens, closes, static_w(avg_exp), **kw),
        simulate("cash", sessions, opens, closes, lambda d: {}, **kw),
    ]
    rows = [metrics(r, cfg.initial_capital) for r in results]
    assumptions = {
        "label": "SIMULATED HISTORICAL BACKTEST (not paper-account results)",
        "period": period, "start": start.isoformat(), "end": end.isoformat(),
        "price_series": f"alpaca {cfg.bars_feed} daily bars, adjustment=all (splits+distributions)",
        "execution": "next session open after month-end signal; sells before buys; whole shares",
        "slippage_bps": cfg.slippage_bps, "commission_per_order": cfg.commission_per_order,
        "commission_bps": cfg.commission_bps, "cash_rate_annual": cfg.cash_rate_annual,
        "comparable_exposure_benchmark": f"static equal weight scaled to the strategy's realised "
                                         f"average exposure ({avg_exp:.1%}); uses hindsight by design",
        "skipped": {r.name: r.skipped for r in results if r.skipped},
    }
    conn.execute(
        "INSERT INTO backtest_runs (created_at_utc, period, start_date, end_date, experiment, config_hash,"
        " assumptions_json, metrics_json) VALUES (?,?,?,?,?,?,?,?)",
        (iso(utcnow()), period, start.isoformat(), end.isoformat(), cfg.experiment_name,
         cfg.experiment_hash(), json.dumps(assumptions, default=str), json.dumps(rows, default=str)))
    conn.commit()
    if out_dir:
        write_backtest_csvs(Path(out_dir), period, results, rows, assumptions)
    return rows


def write_backtest_csvs(out_dir: Path, period, results, rows, assumptions):
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / f"backtest_{period}_summary.csv").open("w", newline="") as fh:
        fh.write(f"# {assumptions['label']}; {assumptions['price_series']}; "
                 f"cash_rate_annual={assumptions['cash_rate_annual']}\n")
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    with (out_dir / f"backtest_{period}_equity.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date"] + [f"{r.name}_equity" for r in results] + [f"{r.name}_exposure" for r in results])
        for i, d in enumerate(results[0].dates):
            w.writerow([d.isoformat()] + [round(r.equity[i], 2) for r in results]
                       + [round(r.exposure[i], 4) for r in results])
    with (out_dir / f"backtest_{period}_trades.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["strategy", "date", "symbol", "side", "qty", "price", "commission"])
        w.writeheader()
        for r in results:
            w.writerows(r.trades)

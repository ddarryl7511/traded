"""Long-only monthly 12-month momentum: pure functions shared by live trading and backtest.

Rule (frozen per experiment):
* Signal date S = last NYSE session of a month, using the completed close of S.
* Lookback start B = last session of the month `lookback_months` earlier.
* r = adj_close(S) / adj_close(B) - 1, using the split+distribution adjusted series.
* Symbol qualifies iff r > 0.
* N qualifiers -> each gets min(max_weight_per_symbol, max_total_exposure / N); rest is cash.
* Any missing / stale / inconsistent data for ANY universe symbol blocks the whole month
  (no trades at all), because acting on a partial universe could force unintended exits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from . import calendar_utils as cal


@dataclass
class SymbolSignal:
    symbol: str
    lookback_start: date | None
    lookback_end: date
    start_close: float | None
    end_close: float | None
    trailing_return: float | None
    qualifies: bool
    target_weight: float
    data_status: str  # 'ok' or a reason


@dataclass
class MonthSignal:
    signal_date: date
    status: str               # 'ok' | 'blocked'
    reason: str
    symbols: list[SymbolSignal] = field(default_factory=list)

    @property
    def weights(self) -> dict[str, float]:
        return {s.symbol: s.target_weight for s in self.symbols}


def target_weights(qualifiers: list[str], max_weight: float, max_total: float) -> dict[str, float]:
    if not qualifiers:
        return {}
    w = min(max_weight, max_total / len(qualifiers))
    return {s: w for s in qualifiers}


def _check_symbol(closes: dict[date, float], start: date, end: date,
                  max_missing: int, max_abs_ret: float) -> str:
    if start not in closes:
        return f"missing lookback-start close {start}"
    if end not in closes:
        return f"missing signal-date close {end} (stale data)"
    window = cal.sessions_in_range(start, end)
    missing = [d for d in window if d not in closes]
    if len(missing) > max_missing:
        return f"{len(missing)} missing sessions in lookback (max {max_missing})"
    prev = None
    for d in window:
        if d not in closes:
            continue
        px = closes[d]
        if px is None or px <= 0:
            return f"non-positive close on {d}"
        if prev is not None and abs(px / prev - 1) > max_abs_ret:
            return f"implausible daily move {px / prev - 1:+.1%} on {d} (possible bad adjustment)"
        prev = px
    return "ok"


def compute_month_signal(adj_closes: dict[str, dict[date, float]], signal_date: date, symbols,
                         lookback_months: int, max_weight: float, max_total: float,
                         max_missing: int, max_abs_ret: float) -> MonthSignal:
    """`adj_closes` must contain ONLY data known at the close of signal_date; anything dated
    after signal_date is ignored defensively (look-ahead guard)."""
    if not cal.is_last_session_of_month(signal_date):
        return MonthSignal(signal_date, "blocked", f"{signal_date} is not a month-end session")
    start = cal.month_end_session_n_months_before(signal_date, lookback_months)
    rows: list[SymbolSignal] = []
    problems = []
    for sym in symbols:
        closes = {d: p for d, p in adj_closes.get(sym, {}).items() if d <= signal_date}
        status = _check_symbol(closes, start, signal_date, max_missing, max_abs_ret)
        s_px, e_px = closes.get(start), closes.get(signal_date)
        ret = (e_px / s_px - 1) if status == "ok" else None
        rows.append(SymbolSignal(sym, start, signal_date, s_px, e_px, ret,
                                 bool(ret is not None and ret > 0), 0.0, status))
        if status != "ok":
            problems.append(f"{sym}: {status}")
    if problems:
        for r in rows:
            r.qualifies = False
        return MonthSignal(signal_date, "blocked", "; ".join(problems), rows)
    weights = target_weights([r.symbol for r in rows if r.qualifies], max_weight, max_total)
    for r in rows:
        r.target_weight = weights.get(r.symbol, 0.0)
    return MonthSignal(signal_date, "ok", f"{len(weights)} qualify", rows)

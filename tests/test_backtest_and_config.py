from datetime import date

import pytest

from momentum_bot import calendar_utils as cal
from momentum_bot.alpaca_api import PAPER_BASE_URL, PaperBroker
from momentum_bot.backtest import metrics, run_backtest, simulate
from momentum_bot.config import ConfigError


def test_execution_at_next_open_not_signal_close():
    sessions = cal.sessions_in_range(date(2026, 7, 27), date(2026, 8, 7))
    opens = {"X": {d: 100.0 + i for i, d in enumerate(sessions)}}
    closes = {"X": {d: 100.5 + i for i, d in enumerate(sessions)}}
    seen = []

    def wfn(d):
        seen.append(d)
        return {"X": 0.10}

    r = simulate("t", sessions, opens, closes, wfn, 10_000, slippage_bps=0)
    assert seen == [date(2026, 7, 31)]
    assert len(r.trades) == 1
    t = r.trades[0]
    assert t["date"] == date(2026, 8, 3)                       # next session
    assert t["price"] == opens["X"][date(2026, 8, 3)]          # its open, not 7/31 close
    assert t["qty"] == int(0.10 * 10_000 / t["price"])


def test_costs_and_slippage_applied():
    sessions = cal.sessions_in_range(date(2026, 7, 27), date(2026, 8, 7))
    px = {"X": {d: 100.0 for d in sessions}}
    r = simulate("t", sessions, px, px, lambda d: {"X": 0.5}, 10_000, commission_per_order=1.0, slippage_bps=10)
    assert r.commissions == 1.0
    assert r.trades[0]["price"] == pytest.approx(100.1)
    assert r.equity[-1] < 10_000


def test_cash_baseline_rate():
    sessions = cal.sessions_in_range(date(2025, 1, 2), date(2025, 12, 31))
    flat = simulate("c", sessions, {}, {}, lambda d: {}, 1000, cash_rate_annual=0.0)
    assert flat.equity[-1] == 1000
    rate = simulate("c", sessions, {}, {}, lambda d: {}, 1000, cash_rate_annual=0.04)
    assert rate.equity[-1] == pytest.approx(1000 * 1.04 ** (len(sessions) / 252))


def test_missing_open_skips_rebalance():
    sessions = cal.sessions_in_range(date(2026, 7, 27), date(2026, 8, 7))
    closes = {"X": {d: 100.0 for d in sessions}}
    opens = {"X": {d: 100.0 for d in sessions if d != date(2026, 8, 3)}}
    r = simulate("t", sessions, opens, closes, lambda d: {"X": 0.1}, 10_000)
    assert r.trades == [] and r.skipped


def test_full_backtest_runs_and_writes_csv(seeded, tmp_path):
    cfg, conn = seeded()
    rows = run_backtest(conn, cfg, "eval", tmp_path)
    names = [r["strategy"] for r in rows]
    assert names[0] == "momentum" and names[1] == "equal_weight_100pct" and names[-1] == "cash"
    mom = rows[0]
    assert mom["average_exposure"] <= 0.5 + 1e-9
    assert mom["order_count"] > 0
    assert (tmp_path / "backtest_eval_summary.csv").exists()
    assert metrics(simulate("x", [], {}, {}, lambda d: {}, 1), 1)["error"]


def test_config_rejects_leverage_and_caps(make_cfg):
    with pytest.raises(ConfigError):
        make_cfg(symbols=["SPY", "TQQQ", "EFA", "EEM", "AGG"])
    with pytest.raises(ConfigError):
        make_cfg(max_weight_per_symbol="0.2")
    with pytest.raises(ConfigError):
        make_cfg(max_total_exposure="0.9")
    with pytest.raises(ConfigError):
        make_cfg(symbols=["A", "B", "C"])


def test_paper_broker_is_hardwired_to_paper(monkeypatch, make_cfg):
    monkeypatch.setenv("APCA_PAPER_API_KEY", "PKTESTKEY")
    monkeypatch.setenv("APCA_PAPER_SECRET_KEY", "secretsecret")
    b = PaperBroker(make_cfg())               # constructing the client makes no network call
    assert b.client._base_url.rstrip("/") == PAPER_BASE_URL
    with pytest.raises(Exception):
        b.submit_market_order("SPY", 1.5, "buy", "x")   # fractional qty refused before any request

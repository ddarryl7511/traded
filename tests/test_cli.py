"""End-to-end CLI flow with fake APIs: download -> signal -> dry-run/trade. Never submits."""
from datetime import date, timedelta

import pytest

from momentum_bot import calendar_utils as cal
from momentum_bot import cli
from momentum_bot.db import connect, utcnow
from tests.conftest import FakeBroker, FakeMarketData, make_bars


@pytest.fixture
def wired(make_cfg, monkeypatch):
    cfg = make_cfg()
    last = cal.last_completed_session(utcnow(), cfg.completed_bar_delay_minutes)
    md = FakeMarketData(bars=make_bars(cfg.symbols, date(2025, 1, 2), last), now=utcnow())
    broker = FakeBroker(now=utcnow())
    broker.prices = {s: [b for b in md.bars if b["symbol"] == s][-1]["close"] for s in cfg.symbols}
    md.prices = dict(broker.prices)
    monkeypatch.setattr(cli, "_apis", lambda c, need_broker=True: (md, broker if need_broker else None))
    cfg_path = str(cfg.database.parent / "config.toml")
    return cfg, cfg_path, broker, last


def test_cli_flow_never_submits_in_default_mode(wired, capsys):
    cfg, path, broker, last = wired
    assert cli.main(["--config", path, "download"]) == 0
    assert cli.main(["--config", path, "download"]) == 0              # idempotent
    month_end = last if cal.is_last_session_of_month(last) else cal.previous_session(date(last.year, last.month, 1))
    assert cli.main(["--config", path, "signal", "--date", month_end.isoformat()]) == 0
    assert cli.main(["--config", path, "dry-run"]) == 0
    assert cli.main(["--config", path, "run"]) == 0
    assert cli.main(["--config", path, "export"]) == 0
    assert broker.submit_calls == 0
    conn = connect(cfg.database)
    assert conn.execute("SELECT COUNT(*) FROM orders WHERE mode='paper'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM orders WHERE status='proposed'").fetchone()[0] > 0
    assert (cfg.reports_dir / "dry_run_orders.csv").exists()


def test_backtest_eval_requires_confirmation(wired):
    cfg, path, broker, last = wired
    cli.main(["--config", path, "download"])
    with pytest.raises(SystemExit):
        cli.main(["--config", path, "backtest", "--period", "eval"])
    assert cli.main(["--config", path, "backtest", "--period", "dev"]) == 0


def test_brief_summarises_state(wired, capsys):
    cfg, path, broker, last = wired
    assert cli.main(["--config", path, "brief"]) == 0                 # works on an empty database
    month_end = last if cal.is_last_session_of_month(last) else cal.previous_session(date(last.year, last.month, 1))
    cli.main(["--config", path, "download"])
    cli.main(["--config", path, "signal", "--date", month_end.isoformat()])
    cli.main(["--config", path, "dry-run"])
    capsys.readouterr()
    assert cli.main(["--config", path, "brief"]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and f"Last signal {month_end:%Y-%m}" in out and "Next signal:" in out
    assert broker.submit_calls == 0

"""Ledger rules, Deflated Sharpe, kill switch and review."""
import sqlite3
from datetime import timedelta

import pytest

from momentum_bot import ledger
from momentum_bot.db import log_event
from momentum_bot.review import build_review
from tests.test_cli import PREDICTION
from tests.test_execution import setup


def _predict(conn, cfg, tmp_path, text=PREDICTION):
    f = tmp_path / "pred.toml"
    f.write_text(text)
    return ledger.register_prediction(conn, cfg, f)


def test_prediction_is_immutable_and_rows_never_deleted(seeded, tmp_path):
    cfg, conn = seeded()
    _predict(conn, cfg, tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE hypotheses SET prediction_json='{}'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM hypotheses")
    ledger.update(conn, cfg.experiment_name, status="failed", lessons="noise")   # outcomes are editable
    assert ledger.get(conn, cfg.experiment_name)["lessons"] == "noise"
    with pytest.raises(ledger.LedgerError):
        _predict(conn, cfg, tmp_path)


def test_revision_cap(seeded, make_cfg, tmp_path):
    cfg, conn = seeded()
    for i in range(ledger.MAX_REVISIONS):
        _predict(conn, make_cfg(**{"name": f'"v{i}"'}), tmp_path)
    with pytest.raises(ledger.LedgerError, match="variations"):
        _predict(conn, make_cfg(**{"name": '"v-too-many"'}), tmp_path)


def test_more_trials_lower_deflated_sharpe():
    one = ledger.deflated_sharpe(0.08, 1000, 0.0, 3.0, 1, [])
    many = ledger.deflated_sharpe(0.08, 1000, 0.0, 3.0, 100, [0.0, 0.03, -0.02, 0.05])
    assert one > 0.95 > many


def test_drawdown_trips_and_latches_kill_switch(seeded):
    cfg, conn, broker, ex = setup(seeded)
    conn.execute("INSERT INTO account_snapshots (taken_at_utc, equity) VALUES ('2026-08-01T00:00:00+00:00', 200000)")
    assert "KILL SWITCH" in ex.run()
    conn.execute("DELETE FROM account_snapshots")          # recovery alone does not re-arm it
    assert "KILL SWITCH latched" in ex.run()
    assert broker.submit_calls == 0
    log_event(conn, "warning", "resume", "operator")
    assert ex.run().startswith("completed")
    assert broker.submit_calls > 0


def test_daily_loss_and_manual_halt_block_orders(seeded):
    cfg, conn, broker, ex = setup(seeded)
    real = broker.get_account
    broker.get_account = lambda: {**real(), "last_equity": 110_000}
    assert "daily loss" in ex.run()
    broker.get_account = real
    log_event(conn, "warning", "resume", "operator")
    log_event(conn, "error", "halt", "manual halt: testing")
    assert "manual halt" in ex.run()
    assert broker.submit_calls == 0


def test_review_records_paper_result(seeded, tmp_path):
    cfg, conn, broker, ex = setup(seeded)
    text, summary = build_review(conn, cfg, broker.get_account())
    assert "No paper rebalances yet" in text and "no pre-registered prediction" in text
    _predict(conn, cfg, tmp_path)
    assert ex.run().startswith("completed")
    text, summary = build_review(conn, cfg, broker.get_account())
    assert "Paper since" in text and summary["n_fills"] > 0
    assert ledger.get(conn, cfg.experiment_name)["paper_result"]

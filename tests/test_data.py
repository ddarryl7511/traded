from datetime import date, datetime, timezone

from momentum_bot.data import bar_session_date, run_quality_checks, store_bars, update_bars
from momentum_bot.db import connect
from tests.conftest import EXEC_NOW, SIGNAL_DATE, FakeMarketData, make_bars


def count(conn, adj=None):
    q = "SELECT COUNT(*) FROM bars" + (" WHERE adjustment=?" if adj else "")
    return conn.execute(q, (adj,) if adj else ()).fetchone()[0]


def test_session_date_conversion():
    assert bar_session_date(datetime(2026, 8, 31, 4, tzinfo=timezone.utc)) == date(2026, 8, 31)
    assert bar_session_date(datetime(2026, 1, 5, 5, tzinfo=timezone.utc)) == date(2026, 1, 5)


def test_ingestion_is_idempotent(make_cfg):
    cfg = make_cfg()
    conn = connect(cfg.database)
    md = FakeMarketData(bars=make_bars(cfg.symbols, date(2025, 1, 2), SIGNAL_DATE))
    update_bars(conn, cfg, md, now=EXEC_NOW)
    n = count(conn)
    assert n > 0 and count(conn, "raw") == count(conn, "all")
    s2 = update_bars(conn, cfg, md, now=EXEC_NOW)
    assert count(conn) == n
    assert s2["raw"] == {"skipped": "up to date"}       # raw is incremental
    assert s2["all"].get("inserted", 0) == 0          # adjusted window refreshed, no new rows


def test_incomplete_and_duplicate_bars_rejected(make_cfg):
    cfg = make_cfg()
    conn = connect(cfg.database)
    bars = make_bars(["AAA"], date(2026, 8, 24), date(2026, 9, 1))   # includes 9/1 (in progress)
    bars.append(dict(bars[0]))                                        # duplicate in response
    stats = store_bars(conn, bars, "sip", "raw", SIGNAL_DATE, "test")
    assert stats["incomplete_dropped"] == 1
    assert stats["duplicate_dropped"] == 2
    assert conn.execute("SELECT MAX(session_date) FROM bars").fetchone()[0] == "2026-08-31"
    assert conn.execute("SELECT COUNT(*) FROM data_quality WHERE check_name='duplicate_in_response'").fetchone()[0] == 1


def test_inconsistent_ohlc_rejected(make_cfg):
    cfg = make_cfg()
    conn = connect(cfg.database)
    bars = make_bars(["AAA"], date(2026, 8, 24), SIGNAL_DATE)
    bars[0]["high"] = bars[0]["low"] / 2
    stats = store_bars(conn, bars, "sip", "raw", SIGNAL_DATE, "test")
    assert stats["inconsistent_dropped"] == 1


def test_missing_and_stale_detected(make_cfg):
    cfg = make_cfg()
    conn = connect(cfg.database)
    bars = make_bars(cfg.symbols, date(2026, 8, 3), date(2026, 8, 28), skip={("BBB", date(2026, 8, 12))})
    for adj in ("raw", "all"):
        store_bars(conn, bars, "sip", adj, SIGNAL_DATE, "test")
    issues = run_quality_checks(conn, cfg, SIGNAL_DATE)
    # BBB misses 8/12 plus 8/31 (stale); the others miss only 8/31
    assert "BBB/raw: 2 missing sessions" in issues and "AAA/raw: 1 missing sessions" in issues
    assert any("stale" in i for i in issues)


def test_api_failure_during_download_is_logged(make_cfg):
    cfg = make_cfg()
    conn = connect(cfg.database)

    class Boom(FakeMarketData):
        def get_daily_bars(self, *a, **k):
            raise RuntimeError("503")
    import pytest
    with pytest.raises(RuntimeError):
        update_bars(conn, cfg, Boom(), now=EXEC_NOW)
    assert conn.execute("SELECT COUNT(*) FROM events WHERE level='error'").fetchone()[0] == 1
    assert count(conn) == 0

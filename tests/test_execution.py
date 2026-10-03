from datetime import datetime, timedelta, timezone

import pytest

from momentum_bot.alpaca_api import TransientAPIError
from momentum_bot.config import paper_orders_allowed
from momentum_bot.execution import Executor, client_order_id
from momentum_bot.signals import ExperimentMismatch, compute_and_store_signal, register_experiment
from tests.conftest import EXEC_NOW, SIGNAL_DATE, FakeBroker, FakeMarketData, price_path


def setup(seeded, submit=True, broker=None, now=EXEC_NOW, **kw):
    cfg, conn = seeded(submit=submit, **kw)
    register_experiment(conn, cfg, now)
    compute_and_store_signal(conn, cfg, SIGNAL_DATE, now - timedelta(hours=12))
    broker = broker or FakeBroker(now=now)
    clock = {"now": now}
    ex = Executor(conn, cfg, broker, FakeMarketData(now=now), now_fn=lambda: clock["now"],
                  sleep_fn=lambda s: clock.__setitem__("now", clock["now"] + timedelta(seconds=s)))
    return cfg, conn, broker, ex


def orders(conn, mode="paper"):
    return {(r["symbol"], r["side"]): dict(r) for r in conn.execute("SELECT * FROM orders WHERE mode=?", (mode,))}


def rstatus(conn):
    return [tuple(r) for r in conn.execute("SELECT mode, status FROM rebalances")]


# ---------------------------------------------------------------- safety gates
def test_default_is_dry_run_and_never_submits(seeded):
    cfg, conn, broker, ex = setup(seeded, submit=False)
    assert paper_orders_allowed(cfg)[0] is False
    out = ex.run()
    assert out.startswith("dry_run_recorded")
    assert broker.submit_calls == 0
    prop = orders(conn, "dry_run")
    assert {k for k in prop} == {("AAA", "buy"), ("BBB", "buy"), ("CCC", "buy")}
    assert all(o["status"] == "proposed" for o in prop.values())
    assert ex.run().startswith("rebalance")     # recorded once per month


def test_paper_requires_review_hash(seeded, make_cfg):
    cfg = make_cfg(submit=True, reviewed=False)
    assert paper_orders_allowed(cfg) == (False, "universe.reviewed is false")
    cfg2 = make_cfg(submit=True, reviewed=True, review_hash='"stale"')
    assert paper_orders_allowed(cfg2)[0] is False


def test_paper_blocked_without_universe_validation(seeded):
    cfg, conn, broker, ex = setup(seeded, submit=True, validated=False)
    assert ex.run().startswith("blocked: current universe has not passed")
    assert broker.submit_calls == 0


def test_market_closed_blocks(seeded):
    cfg, conn, broker, ex = setup(seeded)
    broker.is_open = False
    assert "market is closed" in ex.run()
    assert broker.submit_calls == 0


def test_no_trade_on_signal_date_close(seeded):
    """Orders are never placed on the session whose close formed the signal."""
    now = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    cfg, conn, broker, ex = setup(seeded, now=now)
    assert ex.run() == "no rebalance scheduled today"
    assert broker.submit_calls == 0


# ---------------------------------------------------------------- happy path + restart
def test_paper_rebalance_sells_before_buys_and_caps(seeded):
    broker = FakeBroker(cash=70_000, positions={"DDD": 100, "AAA": 200})
    cfg, conn, broker, ex = setup(seeded, broker=broker)
    out = ex.run()
    assert out == "completed", out
    submits = [c for c in broker.calls if c[0] == "submit"]
    sides = [c[1] for c in submits]
    assert sides == sorted(sides, key=lambda s: s != "sell")    # all sells first
    assert ("submit", "sell", "DDD", 100, client_order_id(cfg, "2026-08", "DDD", "sell")) in submits
    acct = broker.get_account()
    for s in ("AAA", "BBB", "CCC"):
        assert broker.positions[s] * broker.prices[s] <= 0.10 * acct["equity"] + 1e-6
        assert broker.positions[s] == int(0.10 * acct["equity"] / broker.prices[s]) or s == "AAA"
    assert broker.positions.get("DDD", 0) == 0
    assert sum(q * broker.prices[s] for s, q in broker.positions.items()) <= 0.5 * acct["equity"]
    assert broker.cash > 0                                            # no margin


def test_restart_does_not_repeat_completed_rebalance(seeded):
    cfg, conn, broker, ex = setup(seeded)
    assert ex.run() == "completed"
    n = broker.submit_calls
    ex2 = Executor(conn, cfg, broker, FakeMarketData(), now_fn=lambda: EXEC_NOW + timedelta(hours=1))
    assert "already completed" in ex2.run()
    assert broker.submit_calls == n


def test_missed_window_never_trades_late(seeded):
    cfg, conn, broker, ex = setup(seeded)
    later = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)
    ex2 = Executor(conn, cfg, FakeBroker(now=later), FakeMarketData(now=later), now_fn=lambda: later)
    assert ex2.run() == "no rebalance scheduled today"
    assert ("paper", "missed") in rstatus(conn)
    assert ex2.broker.submit_calls == 0


# ---------------------------------------------------------------- duplicate prevention / API failures
def test_timeout_after_accept_queries_instead_of_resubmitting(seeded):
    cfg, conn, broker, ex = setup(seeded)
    broker.behavior["AAA"] = "timeout_after_accept"
    assert ex.run() == "completed"
    aaa = [c for c in broker.calls if c[0] == "submit" and c[2] == "AAA"]
    assert len(aaa) == 1                         # looked up, not resubmitted
    assert orders(conn)[("AAA", "buy")]["status"] == "filled"


def test_timeout_before_accept_resubmits_same_client_id_once(seeded):
    cfg, conn, broker, ex = setup(seeded)
    broker.behavior["BBB"] = "timeout_before_accept"
    assert ex.run() == "completed"
    bbb = [c for c in broker.calls if c[0] == "submit" and c[2] == "BBB"]
    assert len(bbb) == 2 and bbb[0][4] == bbb[1][4]
    assert len([o for o in broker.orders.values() if o["symbol"] == "BBB"]) == 1


def test_lookup_failure_blocks_further_orders(seeded):
    cfg, conn, broker, ex = setup(seeded)
    broker.behavior["AAA"] = "timeout_before_accept"
    broker.fail_lookup = True
    out = ex.run()
    assert out.startswith("blocked: cannot determine state")
    assert [c[2] for c in broker.calls if c[0] == "submit"] == ["AAA"]   # nothing after the uncertainty


def test_account_api_failure_places_no_orders(seeded):
    cfg, conn, broker, ex = setup(seeded)
    broker.fail_account = True
    with pytest.raises(TransientAPIError):
        ex.run()
    assert broker.submit_calls == 0


def test_rejected_sell_stops_buys(seeded):
    broker = FakeBroker(cash=90_000, positions={"DDD": 100})
    cfg, conn, broker, ex = setup(seeded, broker=broker)
    broker.behavior["DDD"] = "reject"
    assert ex.run().startswith("needs_attention")
    assert not [c for c in broker.calls if c[0] == "submit" and c[1] == "buy"]


# ---------------------------------------------------------------- pending orders, partial fills, crash recovery
def test_pending_orders_survive_restart_without_duplicates(seeded):
    cfg, conn, broker, ex = setup(seeded)
    for s in ("AAA", "BBB", "CCC"):
        broker.behavior[s] = "accept"            # orders stay open
    assert "still open" in ex.run()
    first = broker.submit_calls
    # process restarts (new Executor); orders are still open at the broker
    broker.now = EXEC_NOW + timedelta(minutes=5)
    ex2 = Executor(conn, cfg, broker, FakeMarketData(), now_fn=lambda: EXEC_NOW + timedelta(minutes=5),
                   sleep_fn=lambda s: broker.fill_open())
    assert ex2.run() == "completed"
    assert broker.submit_calls == first
    assert len(broker.orders) == 3


def test_pending_buy_counts_toward_exposure(seeded):
    cfg, conn, broker, ex = setup(seeded)
    snap_orders = [{"symbol": "AAA", "side": "buy", "qty": 50, "filled_qty": 10}]
    eff = Executor.effective_qty({"AAA": {"qty": 10.0}}, snap_orders)
    assert eff["AAA"] == 50
    from momentum_bot.execution import Snapshot
    acct = broker.get_account()
    snap = Snapshot(acct, {}, [{"symbol": "AAA", "side": "buy", "qty": 99.0, "filled_qty": 0.0}])
    prices = {s: price_path(s, SIGNAL_DATE) for s in cfg.symbols}
    buys = {b["symbol"]: b for b in ex.plan_buys({"AAA": 0.10, "BBB": 0.10}, snap, prices, {})}
    target = int(0.10 * acct["equity"] / prices["AAA"])
    assert buys.get("AAA", {"qty": 0})["qty"] == max(0, target - 99)


def test_partial_fill_recorded(seeded):
    cfg, conn, broker, ex = setup(seeded)
    broker.behavior["CCC"] = "partial"
    assert ex.run() == "completed_with_issues"
    o = orders(conn)[("CCC", "buy")]
    assert o["status"] == "canceled" and 0 < o["filled_qty"] < o["qty"]
    assert conn.execute("SELECT COUNT(*) FROM fills WHERE symbol='CCC'").fetchone()[0] == 1


def test_crash_between_intent_and_response_recovers(seeded):
    """Order row is 'pending_submit' (process died mid-request) but broker has it."""
    cfg, conn, broker, ex = setup(seeded)
    ex._get_or_create_rebalance("test-exp:2026-08:paper", {"month_key": "2026-08",
                                "execution_date": "2026-09-01"}, "paper")
    ex._set_status("test-exp:2026-08:paper", "buying")
    cid = client_order_id(cfg, "2026-08", "AAA", "buy")
    broker.submit_market_order("AAA", 10, "buy", cid)
    broker.submit_calls = 0
    conn.execute("INSERT INTO orders (client_order_id, rebalance_id, mode, symbol, side, qty, status,"
                 " submit_attempts, created_at_utc, updated_at_utc) VALUES (?,?,?,?,?,?,?,1,'x','x')",
                 (cid, "test-exp:2026-08:paper", "paper", "AAA", "buy", 10, "pending_submit"))
    conn.commit()
    assert ex.run() == "completed"
    assert broker.submit_calls == 0            # buy rows existed: nothing new, nothing duplicated
    assert orders(conn)[("AAA", "buy")]["status"] == "filled"


def test_clock_skew_blocks(seeded):
    cfg, conn, broker, ex = setup(seeded)
    broker.now = EXEC_NOW + timedelta(minutes=10)
    assert "local clock differs" in ex.run()
    assert broker.submit_calls == 0


def test_foreign_open_order_blocks(seeded):
    cfg, conn, broker, ex = setup(seeded)
    broker.behavior["AAA"] = "accept"
    broker.submit_market_order("AAA", 1, "buy", "manual-order-1")
    broker.submit_calls = 0
    assert "not created by this bot" in ex.run()
    assert broker.submit_calls == 0


def test_short_or_foreign_position_blocks(seeded):
    cfg, conn, broker, ex = setup(seeded, broker=FakeBroker(positions={"ZZZ": 5}))
    broker.prices["ZZZ"] = 10.0
    assert "non-universe symbol" in ex.run()


def test_price_deviation_blocks_symbol(seeded):
    cfg, conn, broker, ex = setup(seeded)
    prices = {s: price_path(s, SIGNAL_DATE) for s in cfg.symbols}
    prices["AAA"] *= 1.5
    ex.md = FakeMarketData(prices=prices)
    assert ex.run() == "completed_with_issues"
    assert ("AAA", "buy") not in orders(conn)


def test_experiment_parameters_frozen(seeded, make_cfg):
    cfg, conn, broker, ex = setup(seeded)
    changed = make_cfg(submit=True, max_total_exposure="0.40")
    with pytest.raises(ExperimentMismatch):
        register_experiment(conn, changed)

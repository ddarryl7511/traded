"""Rebalance execution with reconciliation and restart recovery.

State machine per (experiment, month, mode), persisted in `rebalances`:

    planned -> selling -> buying -> completed | completed_with_issues
    any     -> needs_attention   (manual review required; never auto-resumed)
    dry-run: planned -> dry_run_recorded
    no valid window: missed

Every step re-reads broker state first, so killing the process at any point and starting
again continues where it left off. Client order IDs are deterministic per rebalance,
symbol and side, so a resubmission after an unknown outcome can never create a second
live order (Alpaca rejects duplicate client_order_id values).
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import calendar_utils as cal
from .alpaca_api import PermanentAPIError, TransientAPIError
from .config import paper_orders_allowed, universe_hash
from .db import iso, log_event, utcnow
from .signals import load_target_weights, pending_signal, register_experiment

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")

TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced",
            "not_found_final", "rejected_by_api", "blocked", "proposed"}
FINAL_REBALANCE = {"completed", "completed_with_issues", "needs_attention", "dry_run_recorded", "missed"}
MAX_SUBMIT_ATTEMPTS = 2


class Blocked(RuntimeError):
    """State is uncertain or unsafe: do not place new orders now."""


@dataclass
class Snapshot:
    account: dict
    positions: dict[str, dict]
    open_orders: list[dict]


def halted(conn) -> str | None:
    """Latched kill switch: the latest 'halt' event wins until a later 'resume' event."""
    r = conn.execute("SELECT category, message FROM events WHERE category IN ('halt','resume')"
                     " ORDER BY id DESC LIMIT 1").fetchone()
    return r[1] if r and r[0] == "halt" else None


def kill_switch(conn, cfg, account: dict) -> None:
    """Runs in code before any order. Trips (and latches) on drawdown or daily loss; only
    `python -m momentum_bot resume` clears it. No prompt or config flip can talk past it."""
    why = halted(conn)
    if why:
        raise Blocked(f"KILL SWITCH latched: {why} (run `resume` after reviewing)")
    eq = account.get("equity") or 0
    # ponytail: peak from rebalance-time snapshots only; reset events table if the paper account is reset
    peak = conn.execute("SELECT MAX(equity) FROM account_snapshots").fetchone()[0] or eq
    last = account.get("last_equity")
    if peak and eq <= peak * (1 - cfg.max_drawdown):
        why = f"drawdown {eq / peak - 1:.1%} from peak ${peak:,.0f} hit limit {cfg.max_drawdown:.0%}"
    elif last and eq <= last * (1 - cfg.max_daily_loss):
        why = f"daily loss {eq / last - 1:.1%} hit limit {cfg.max_daily_loss:.0%}"
    if why:
        log_event(conn, "error", "halt", why)     # run() notifies via its Blocked handler
        raise Blocked(f"KILL SWITCH: {why}")


def client_order_id(cfg, mk: str, symbol: str, side: str, suffix: str = "") -> str:
    return f"mb-{cfg.experiment_hash()[:8]}-{mk.replace('-', '')}-{symbol}-{side[0].upper()}{suffix}"


def rebalance_id(cfg, mk: str, mode: str) -> str:
    return f"{cfg.experiment_name}:{mk}:{mode}"


class Executor:
    def __init__(self, conn, cfg, broker, market_data, now_fn=utcnow, sleep_fn=time.sleep, notify=None):
        self.conn, self.cfg, self.broker, self.md = conn, cfg, broker, market_data
        self.now_fn, self.sleep = now_fn, sleep_fn
        self.notify = notify or (lambda msg: None)

    # ------------------------------------------------------------------ entry
    def run(self, preview: bool = False) -> str:
        cfg, now = self.cfg, self.now_fn()
        register_experiment(self.conn, cfg, now)
        today = now.astimezone(NY).date()
        allowed, why = paper_orders_allowed(cfg)
        mode = "paper" if allowed and not preview else "dry_run"

        if preview:
            sig = self.conn.execute("SELECT * FROM signal_runs WHERE experiment=? AND status='ok'"
                                    " ORDER BY month_key DESC LIMIT 1", (cfg.experiment_name,)).fetchone()
            if sig is None:
                return "no accepted signal to preview"
            sig = dict(sig)
            rid = rebalance_id(cfg, sig["month_key"], "preview") + ":" + now.strftime("%Y%m%dT%H%M%S")
        else:
            self._mark_missed(today)
            self._expire_stale(today)
            sig = pending_signal(self.conn, cfg, today)
            if sig is None:
                return "no rebalance scheduled today"
            rid = rebalance_id(cfg, sig["month_key"], mode)
        if mode == "dry_run" and not preview:
            log.info("dry-run mode: %s", why)

        reb = self._get_or_create_rebalance(rid, sig, mode)
        if reb["status"] in FINAL_REBALANCE:
            return f"rebalance {rid} already {reb['status']}"
        try:
            if mode == "paper":
                return self._run_paper(rid, sig)
            return self._run_dry(rid, sig, check_window=not preview)
        except Blocked as exc:
            self.conn.execute("UPDATE rebalances SET reason=?, updated_at_utc=? WHERE rebalance_id=?",
                              (f"blocked: {exc}", iso(self.now_fn()), rid))
            self.conn.commit()
            log_event(self.conn, "error", "execution", f"blocked: {exc}", {"rebalance": rid})
            self.notify(f"[momentum-bot] rebalance {rid} blocked: {exc}")
            return f"blocked: {exc}"

    # ------------------------------------------------------------ bookkeeping
    def _get_or_create_rebalance(self, rid, sig, mode):
        row = self.conn.execute("SELECT * FROM rebalances WHERE rebalance_id=?", (rid,)).fetchone()
        if row is None:
            ts = iso(self.now_fn())
            self.conn.execute(
                "INSERT INTO rebalances (rebalance_id, experiment, month_key, mode, execution_date, status,"
                " reason, created_at_utc, updated_at_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                (rid, self.cfg.experiment_name, sig["month_key"], mode, sig["execution_date"], "planned",
                 None, ts, ts))
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM rebalances WHERE rebalance_id=?", (rid,)).fetchone()
        return dict(row)

    def _set_status(self, rid, status, reason=None):
        self.conn.execute("UPDATE rebalances SET status=?, reason=?, updated_at_utc=? WHERE rebalance_id=?",
                          (status, reason, iso(self.now_fn()), rid))
        self.conn.commit()

    def _status(self, rid):
        return self.conn.execute("SELECT status FROM rebalances WHERE rebalance_id=?", (rid,)).fetchone()[0]

    def _mark_missed(self, today: date):
        """Accepted signals whose execution window passed without a paper/dry-run attempt."""
        for r in self.conn.execute(
                "SELECT * FROM signal_runs s WHERE experiment=? AND status='ok' AND NOT EXISTS ("
                " SELECT 1 FROM rebalances b WHERE b.experiment=s.experiment AND b.month_key=s.month_key"
                " AND b.mode IN ('paper','dry_run'))", (self.cfg.experiment_name,)).fetchall():
            exec_date = date.fromisoformat(r["execution_date"])
            if exec_date < today and cal.sessions_between(exec_date, today) > self.cfg.max_execution_delay_sessions:
                mode = "paper" if paper_orders_allowed(self.cfg)[0] else "dry_run"
                rid = rebalance_id(self.cfg, r["month_key"], mode)
                self._get_or_create_rebalance(rid, dict(r), mode)
                self._set_status(rid, "missed", f"execution window {exec_date} passed; no late trading")
                log_event(self.conn, "warning", "execution", f"missed rebalance {rid}")
                self.notify(f"[momentum-bot] missed rebalance window for {r['month_key']}")

    def _expire_stale(self, today: date):
        """In-progress rebalances whose window has passed need a human, never a late resume."""
        for r in self.conn.execute(
                "SELECT rebalance_id, execution_date FROM rebalances WHERE experiment=? AND status IN"
                " ('planned','selling','buying')", (self.cfg.experiment_name,)).fetchall():
            exec_date = date.fromisoformat(r["execution_date"])
            if exec_date < today and cal.sessions_between(exec_date, today) > self.cfg.max_execution_delay_sessions:
                self._set_status(r["rebalance_id"], "needs_attention",
                                 "execution window ended before the rebalance finished")
                log_event(self.conn, "error", "execution", f"{r['rebalance_id']} unfinished after window")
                self.notify(f"[momentum-bot] {r['rebalance_id']} did not finish in its window; review needed")

    # ------------------------------------------------------------ broker state
    def snapshot(self, context: str) -> Snapshot:
        acct = self.broker.get_account()
        positions = {p["symbol"]: p for p in self.broker.get_positions()}
        open_orders = self.broker.get_open_orders()
        pend_buy = sum((o["qty"] - o["filled_qty"]) * (self._last_raw_close(o["symbol"]) or 0)
                       for o in open_orders if o["side"] == "buy")
        pend_sell = sum((o["qty"] - o["filled_qty"]) * (self._last_raw_close(o["symbol"]) or 0)
                        for o in open_orders if o["side"] == "sell")
        lmv = sum((p.get("market_value") or 0) for p in positions.values())
        eq = acct.get("equity") or 0
        cur = self.conn.execute(
            "INSERT INTO account_snapshots (taken_at_utc, context, equity, cash, buying_power,"
            " non_marginable_buying_power, long_market_value, exposure_pct, pending_buy_notional,"
            " pending_sell_notional) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (iso(self.now_fn()), context, eq, acct.get("cash"), acct.get("buying_power"),
             acct.get("non_marginable_buying_power"), lmv, (lmv / eq) if eq else None, pend_buy, pend_sell))
        sid = cur.lastrowid
        for p in positions.values():
            self.conn.execute("INSERT INTO position_snapshots VALUES (?,?,?,?,?,?)",
                              (sid, p["symbol"], p["qty"], p.get("market_value"), p.get("avg_entry_price"),
                               p.get("side")))
        for o in open_orders:
            self.conn.execute("INSERT INTO open_order_snapshots VALUES (?,?,?,?,?,?,?,?)",
                              (sid, o["client_order_id"], o["id"], o["symbol"], o["side"], o["qty"],
                               o["filled_qty"], o["status"]))
        self.conn.commit()
        return Snapshot(acct, positions, open_orders)

    def _last_raw_close(self, symbol) -> float | None:
        r = self.conn.execute("SELECT close FROM bars WHERE symbol=? AND feed=? AND adjustment='raw'"
                              " ORDER BY session_date DESC LIMIT 1", (symbol, self.cfg.bars_feed)).fetchone()
        return r[0] if r else None

    def preflight(self, snap: Snapshot, rid: str, strict: bool) -> None:
        a, cfg = snap.account, self.cfg
        if str(a.get("status", "")).upper() != "ACTIVE":
            raise Blocked(f"account status {a.get('status')}")
        if a.get("trading_blocked") or a.get("account_blocked") or a.get("trade_suspended_by_user"):
            raise Blocked("account trading is blocked/suspended")
        if not a.get("equity") or a["equity"] <= 0:
            raise Blocked("account equity unavailable or non-positive")
        kill_switch(self.conn, cfg, a)
        if a.get("cash") is None or a["cash"] < 0:
            raise Blocked("negative or unknown cash (margin borrowing?)")
        if (a.get("short_market_value") or 0) != 0:
            raise Blocked("account has short market value")
        for sym, p in snap.positions.items():
            if p["qty"] < 0 or str(p.get("side", "long")).lower() == "short":
                raise Blocked(f"short position in {sym}")
            if sym not in cfg.symbols and strict:
                raise Blocked(f"position in non-universe symbol {sym}; account state not owned by bot")
        ours = {r[0]: r[1] for r in self.conn.execute("SELECT client_order_id, rebalance_id FROM orders")}
        for o in snap.open_orders:
            if o["client_order_id"] not in ours:
                raise Blocked(f"open order {o['client_order_id']} ({o['symbol']}) not created by this bot")
            if ours[o["client_order_id"]] != rid:
                raise Blocked(f"open order {o['client_order_id']} belongs to another rebalance")
        if strict:
            ok = self.conn.execute(
                "SELECT ok FROM universe_validations WHERE universe_hash=? ORDER BY id DESC LIMIT 1",
                (universe_hash(cfg.symbols),)).fetchone()
            if not ok or not ok[0]:
                raise Blocked("current universe has not passed validate-universe")
        last = cal.last_completed_session(self.now_fn(), cfg.completed_bar_delay_minutes)
        for sym in cfg.symbols:
            r = self.conn.execute("SELECT MAX(session_date) FROM bars WHERE symbol=? AND feed=? AND"
                                  " adjustment='raw'", (sym, cfg.bars_feed)).fetchone()[0]
            if r != last.isoformat():
                raise Blocked(f"raw data for {sym} is stale (latest {r}, expected {last})")

    def check_window(self) -> None:
        now = self.now_fn()
        clock = self.broker.get_clock()
        if not clock["is_open"]:
            raise Blocked("market is closed (broker clock)")
        ts = clock.get("timestamp")
        if ts is not None and abs((ts - now).total_seconds()) > 120:
            raise Blocked(f"local clock differs from broker clock by {(ts - now).total_seconds():.0f}s")
        today = now.astimezone(NY).date()
        if not cal.is_session(today):
            raise Blocked("today is not an exchange session")
        start = cal.session_open_utc(today) + timedelta(minutes=self.cfg.start_delay_minutes)
        end = cal.session_close_utc(today) - timedelta(minutes=self.cfg.stop_before_close_minutes)
        if not start <= now <= end:
            raise Blocked(f"outside trading window {start:%H:%M}-{end:%H:%M} UTC")

    def prices(self, check_staleness: bool) -> tuple[dict[str, float], dict[str, str]]:
        """Latest trade per symbol for sizing; returns (prices, blocked_reasons)."""
        latest = self.md.get_latest_trade_prices(self.cfg.symbols, self.cfg.price_feed)
        now, out, blocked = self.now_fn(), {}, {}
        for sym in self.cfg.symbols:
            t, ref = latest.get(sym), self._last_raw_close(sym)
            if t is None or not t.get("price") or t["price"] <= 0:
                blocked[sym] = "no latest trade"
                continue
            if ref and abs(t["price"] / ref - 1) > self.cfg.max_price_deviation:
                blocked[sym] = f"latest {t['price']} deviates from last close {ref}"
                continue
            if check_staleness and t.get("timestamp") and now - t["timestamp"] > timedelta(minutes=30):
                blocked[sym] = f"latest trade is stale ({t['timestamp']})"
                continue
            out[sym] = float(t["price"])
        return out, blocked

    # ------------------------------------------------------------ planning
    @staticmethod
    def effective_qty(positions, open_orders) -> dict[str, float]:
        """Position quantity adjusted for the unfilled part of pending orders."""
        eff = {s: p["qty"] for s, p in positions.items()}
        for o in open_orders:
            rem = (o["qty"] or 0) - (o["filled_qty"] or 0)
            eff[o["symbol"]] = eff.get(o["symbol"], 0) + (rem if o["side"] == "buy" else -rem)
        return eff

    def target_qty(self, weights, equity, prices) -> dict[str, int]:
        return {s: int(math.floor(w * equity / prices[s])) if w > 0 else 0
                for s, w in weights.items() if s in prices}

    def plan_sells(self, weights, snap, prices, blocked):
        tq = self.target_qty(weights, snap.account["equity"], prices)
        eff = self.effective_qty(snap.positions, snap.open_orders)
        sells = []
        for sym, p in snap.positions.items():
            if sym in blocked or sym not in prices:
                continue
            excess = eff.get(sym, 0) - tq.get(sym, 0)
            qty = int(math.floor(min(excess, p["qty"])))  # never sell more than held -> no shorts
            if qty > 0:
                sells.append({"symbol": sym, "qty": qty, "ref_price": prices[sym],
                              "target_qty": tq.get(sym, 0), "current_qty": p["qty"]})
        return sells

    def plan_buys(self, weights, snap, prices, blocked, cash_override=None, positions_override=None):
        cfg = self.cfg
        equity = snap.account["equity"]
        positions = positions_override if positions_override is not None else snap.positions
        tq = self.target_qty(weights, equity, prices)
        eff = self.effective_qty(positions, snap.open_orders)
        pend_buy = sum((o["qty"] - o["filled_qty"]) * prices.get(o["symbol"], 0)
                       for o in snap.open_orders if o["side"] == "buy")
        cash = cash_override if cash_override is not None else min(
            snap.account["cash"], snap.account.get("non_marginable_buying_power") or snap.account["cash"])
        available = cash - pend_buy
        buys = []
        for sym, target in sorted(tq.items()):
            if sym in blocked:
                continue
            qty = int(target - math.ceil(eff.get(sym, 0)))
            if qty > 0:
                buys.append({"symbol": sym, "qty": qty, "ref_price": prices[sym], "target_qty": target,
                             "current_qty": positions.get(sym, {}).get("qty", 0)})
        # Cash constraint (no margin): shrink largest orders first until affordable.
        def cost(b):
            return b["qty"] * b["ref_price"] * (1 + cfg.cash_buffer_pct)
        while buys and sum(cost(b) for b in buys) > available:
            big = max(buys, key=cost)
            big["qty"] -= 1
            buys = [b for b in buys if b["qty"] > 0]
        # Exposure caps, counting existing positions and pending buys at current prices.
        long_mv = sum(eff.get(s, 0) * prices.get(s, positions[s].get("current_price") or 0)
                      for s in positions) + sum(
            (o["qty"] - o["filled_qty"]) * prices.get(o["symbol"], 0)
            for o in snap.open_orders if o["side"] == "buy" and o["symbol"] not in positions)
        ok = []
        for b in buys:
            add = b["qty"] * b["ref_price"]
            sym_val = eff.get(b["symbol"], 0) * b["ref_price"] + add
            if sym_val > cfg.max_weight_per_symbol * equity + 1e-6:
                log_event(self.conn, "warning", "risk", f"buy {b['symbol']} skipped: per-symbol cap")
                continue
            if long_mv + add > cfg.max_total_exposure * equity + 1e-6:
                log_event(self.conn, "warning", "risk", f"buy {b['symbol']} skipped: total exposure cap")
                continue
            long_mv += add
            ok.append(b)
        return ok

    # ------------------------------------------------------------ orders
    def _insert_order(self, rid, mk, mode, side, o, status):
        cid = client_order_id(self.cfg, mk, o["symbol"], side)
        ts = iso(self.now_fn())
        self.conn.execute(
            "INSERT OR IGNORE INTO orders (client_order_id, rebalance_id, mode, symbol, side, qty, ref_price,"
            " target_qty, current_qty, status, created_at_utc, updated_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, rid, mode, o["symbol"], side, int(o["qty"]), o["ref_price"], o.get("target_qty"),
             o.get("current_qty"), status, ts, ts))
        self.conn.commit()
        return cid

    def _orders(self, rid, side=None):
        q, args = "SELECT * FROM orders WHERE rebalance_id=?", [rid]
        if side:
            q += " AND side=?"
            args.append(side)
        return [dict(r) for r in self.conn.execute(q, args)]

    def _apply_broker_order(self, cid, o):
        ts = iso(self.now_fn())
        self.conn.execute(
            "UPDATE orders SET status=?, broker_order_id=?, filled_qty=?, filled_avg_price=?,"
            " updated_at_utc=? WHERE client_order_id=?",
            (o["status"], o["id"], o["filled_qty"], o["filled_avg_price"], ts, cid))
        if o["filled_qty"]:
            self.conn.execute(
                "INSERT OR IGNORE INTO fills (client_order_id, broker_order_id, symbol, side,"
                " cumulative_filled_qty, filled_avg_price, recorded_at_utc) VALUES (?,?,?,?,?,?,?)",
                (cid, o["id"], o["symbol"], o["side"], o["filled_qty"], o["filled_avg_price"], ts))
        self.conn.commit()

    def _submit(self, row):
        cid = row["client_order_id"]
        if row["submit_attempts"] >= MAX_SUBMIT_ATTEMPTS:
            self.conn.execute("UPDATE orders SET status='not_found_final' WHERE client_order_id=?", (cid,))
            self.conn.commit()
            return
        self.conn.execute("UPDATE orders SET status='pending_submit', submit_attempts=submit_attempts+1,"
                          " submitted_at_utc=?, updated_at_utc=? WHERE client_order_id=?",
                          (iso(self.now_fn()), iso(self.now_fn()), cid))
        self.conn.commit()  # intent is durable BEFORE the network call
        try:
            o = self.broker.submit_market_order(row["symbol"], int(row["qty"]), row["side"], cid)
            self._apply_broker_order(cid, o)
            log_event(self.conn, "info", "order", f"submitted {row['side']} {row['qty']} {row['symbol']}",
                      {"client_order_id": cid, "status": o["status"]})
        except PermanentAPIError as exc:
            found = self._lookup(cid)  # e.g. 422 because the id already exists
            if found:
                self._apply_broker_order(cid, found)
            else:
                self.conn.execute("UPDATE orders SET status='rejected_by_api', last_error=? WHERE"
                                  " client_order_id=?", (str(exc)[:500], cid))
                self.conn.commit()
                log_event(self.conn, "error", "order", f"order {cid} rejected: {exc}")
        except Exception as exc:  # timeout / network: outcome unknown -> query, never blind resubmit
            log_event(self.conn, "warning", "order", f"submit outcome unknown for {cid}: {exc}")
            found = self._lookup(cid)
            if found:
                self._apply_broker_order(cid, found)
            else:
                self.conn.execute("UPDATE orders SET status='not_found', last_error=? WHERE client_order_id=?",
                                  (str(exc)[:500], cid))
                self.conn.commit()

    def _lookup(self, cid):
        try:
            return self.broker.get_order_by_client_id(cid)
        except (TransientAPIError, PermanentAPIError) as exc:
            raise Blocked(f"cannot determine state of order {cid}: {exc}") from exc

    def sync_orders(self, rid):
        """Refresh every non-terminal order of this rebalance from the broker."""
        for row in self._orders(rid):
            if row["status"] in TERMINAL:
                continue
            found = self._lookup(row["client_order_id"])
            if found:
                self._apply_broker_order(row["client_order_id"], found)
            elif row["status"] == "new_local":
                continue  # recorded locally, never sent; submitted by the phase logic
            elif row["status"] in ("pending_submit", "not_found"):
                # Never acknowledged by the broker. Same client id -> resubmission is duplicate-safe.
                self.conn.execute("UPDATE orders SET status='not_found' WHERE client_order_id=?",
                                  (row["client_order_id"],))
                self.conn.commit()
            else:
                raise Blocked(f"order {row['client_order_id']} vanished at broker (status {row['status']})")

    def _wait_terminal(self, rid, side) -> bool:
        deadline = self.now_fn() + timedelta(minutes=self.cfg.phase_timeout_minutes)
        while True:
            for row in self._orders(rid, side):
                if row["status"] in ("not_found", "new_local"):
                    self._submit(row)
            self.sync_orders(rid)
            if all(r["status"] in TERMINAL for r in self._orders(rid, side)):
                return True
            if self.now_fn() >= deadline:
                return False
            self.sleep(self.cfg.poll_interval_seconds)

    # ------------------------------------------------------------ flows
    def _run_paper(self, rid, sig) -> str:
        mk = sig["month_key"]
        weights = load_target_weights(self.conn, self.cfg, mk)
        self.check_window()
        self.sync_orders(rid)
        snap = self.snapshot(f"{rid}:start")
        self.preflight(snap, rid, strict=True)
        prices, blocked = self.prices(check_staleness=True)
        for sym, reason in blocked.items():
            log_event(self.conn, "warning", "execution", f"{sym} blocked: {reason}", {"rebalance": rid})

        status = self._status(rid)
        if status == "planned":
            for s in self.plan_sells(weights, snap, prices, blocked):
                self._insert_order(rid, mk, "paper", "sell", s, "new_local")
            for row in self._orders(rid, "sell"):
                if row["status"] == "new_local":
                    self._submit(row)
            self._set_status(rid, "selling")
            status = "selling"

        if status == "selling":
            if not self._wait_terminal(rid, "sell"):
                return "sell orders still open; will resume on next run"
            bad = [r for r in self._orders(rid, "sell") if r["status"] in ("rejected", "rejected_by_api",
                                                                          "not_found_final")]
            if bad:
                self._set_status(rid, "needs_attention", f"sell orders failed: {[r['symbol'] for r in bad]}")
                self.notify(f"[momentum-bot] {rid}: sell orders failed, buys not placed")
                return "needs_attention: sell failures; buys not placed"
            self._set_status(rid, "buying")
            status = "buying"

        if status == "buying":
            if not self._orders(rid, "buy"):
                self.check_window()
                snap = self.snapshot(f"{rid}:pre-buy")   # re-check funds after sells
                self.preflight(snap, rid, strict=True)
                prices, blocked = self.prices(check_staleness=True)
                for b in self.plan_buys(weights, snap, prices, blocked):
                    self._insert_order(rid, mk, "paper", "buy", b, "new_local")
                for row in self._orders(rid, "buy"):
                    if row["status"] == "new_local":
                        self._submit(row)
            if not self._wait_terminal(rid, "buy"):
                return "buy orders still open; will resume on next run"
            self.snapshot(f"{rid}:end")
            issues = [r for r in self._orders(rid) if r["status"] != "filled"] or blocked
            final = "completed_with_issues" if issues else "completed"
            self._set_status(rid, final, None if not issues else "partial fills / rejections / blocked symbols")
            self.notify(f"[momentum-bot] {rid}: {final}")
            return final
        return status

    def _run_dry(self, rid, sig, check_window: bool) -> str:
        mk = sig["month_key"]
        weights = load_target_weights(self.conn, self.cfg, mk)
        if check_window:
            self.check_window()
        snap = self.snapshot(f"{rid}:dry-run")
        self.preflight(snap, rid, strict=False)
        prices, blocked = self.prices(check_staleness=check_window)
        sells = self.plan_sells(weights, snap, prices, blocked)
        # Assume sells fill at the reference price to project cash for the buy leg.
        cash = min(snap.account["cash"], snap.account.get("non_marginable_buying_power") or snap.account["cash"])
        positions = {s: dict(p) for s, p in snap.positions.items()}
        for s in sells:
            cash += s["qty"] * s["ref_price"]
            positions[s["symbol"]]["qty"] -= s["qty"]
        buys = self.plan_buys(weights, snap, prices, blocked, cash_override=cash, positions_override=positions)
        for side, lst in (("sell", sells), ("buy", buys)):
            for o in lst:
                suffix = "-DRY" if check_window else f"-PV{self.now_fn():%Y%m%d%H%M%S}"
                cid = client_order_id(self.cfg, mk, o["symbol"], side) + suffix
                ts = iso(self.now_fn())
                self.conn.execute(
                    "INSERT OR IGNORE INTO orders (client_order_id, rebalance_id, mode, symbol, side, qty,"
                    " ref_price, target_qty, current_qty, status, created_at_utc, updated_at_utc)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, rid, "dry_run", o["symbol"], side, o["qty"], o["ref_price"], o["target_qty"],
                     o["current_qty"], "proposed", ts, ts))
        self.conn.commit()
        self._set_status(rid, "dry_run_recorded", f"{len(sells)} sells, {len(buys)} buys proposed; blocked={blocked}")
        log_event(self.conn, "info", "execution", f"dry run recorded for {rid}",
                  {"sells": sells, "buys": buys, "blocked": blocked})
        return f"dry_run_recorded: {len(sells)} sells, {len(buys)} buys"

"""Shared fixtures. Network access is disabled for every test, so no test can reach Alpaca
or place an order; brokers and data providers are in-memory fakes."""
from __future__ import annotations

import socket
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from momentum_bot import calendar_utils as cal
from momentum_bot.alpaca_api import PermanentAPIError, TransientAPIError
from momentum_bot.config import load_config, universe_hash
from momentum_bot.data import store_bars
from momentum_bot.db import connect

NY = ZoneInfo("America/New_York")
SYMBOLS = ["AAA", "BBB", "CCC", "DDD", "EEE"]
SIGNAL_DATE = date(2026, 8, 31)          # last session of Aug 2026
EXEC_DATE = date(2026, 9, 1)             # next session
EXEC_NOW = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)   # 11:00 New York


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def guard(*a, **k):
        raise RuntimeError("network access attempted during tests")
    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard)


CONFIG_TEMPLATE = """
[experiment]
name = "test-exp"
[universe]
symbols = {symbols}
reviewed = {reviewed}
review_hash = "{review_hash}"
[strategy]
lookback_months = 12
max_weight_per_symbol = 0.10
max_total_exposure = 0.50
[data]
bars_feed = "sip"
price_feed = "iex"
history_start = "2025-01-01"
adjusted_refresh_days = 400
completed_bar_delay_minutes = 60
max_missing_sessions_in_lookback = 0
max_abs_daily_return = 0.40
[trading]
submit_paper_orders = {submit}
start_delay_minutes = 15
stop_before_close_minutes = 30
max_execution_delay_sessions = 0
cash_buffer_pct = 0.01
max_price_deviation = 0.15
poll_interval_seconds = 1
phase_timeout_minutes = 1
http_timeout_seconds = 5
min_seconds_between_calls = 0
max_retries = 1
[backtest]
dev_start = "2025-01-01"
dev_end = "2025-12-31"
eval_start = "2026-01-01"
eval_end = ""
initial_capital = 100000.0
commission_per_order = 1.0
commission_bps = 0.0
slippage_bps = 10.0
cash_rate_annual = 0.0
[paths]
database = "{db}"
reports_dir = "{tmp}/reports"
log_file = "{tmp}/bot.log"
"""


@pytest.fixture
def make_cfg(tmp_path):
    def _make(submit=False, reviewed=None, symbols=SYMBOLS, **overrides):
        reviewed = submit if reviewed is None else reviewed
        text = CONFIG_TEMPLATE.format(
            symbols="[" + ", ".join(f'"{s}"' for s in symbols) + "]",
            reviewed=str(reviewed).lower(), review_hash=universe_hash(symbols) if reviewed else "",
            submit=str(submit).lower(), db=tmp_path / "t.sqlite", tmp=tmp_path)
        for k, v in overrides.items():
            import re
            text = re.sub(rf"^{k} = .*$", f"{k} = {v}", text, flags=re.M)
        p = tmp_path / "config.toml"
        p.write_text(text)
        return load_config(p)
    return _make


def price_path(sym: str, d: date) -> float:
    """Deterministic synthetic prices: AAA/BBB/CCC trend up, DDD/EEE trend down."""
    t = (d - date(2025, 1, 1)).days
    drift = {"AAA": 0.0008, "BBB": 0.0005, "CCC": 0.0003, "DDD": -0.0004, "EEE": -0.0006}.get(sym, 0.0)
    base = {"AAA": 100.0, "BBB": 50.0, "CCC": 200.0, "DDD": 80.0, "EEE": 40.0}.get(sym, 100.0)
    return round(base * (1 + drift) ** t, 4)


def make_bars(symbols, start: date, end: date, skip=None, mutate=None):
    skip = skip or set()
    out = []
    for d in cal.sessions_in_range(start, end):
        for s in symbols:
            if (s, d) in skip:
                continue
            c = price_path(s, d)
            o = round(c * 0.999, 4)
            # Alpaca stamps daily bars at midnight New York time (04:00 or 05:00 UTC).
            ts = datetime(d.year, d.month, d.day, tzinfo=NY).astimezone(timezone.utc)
            bar = {"symbol": s, "timestamp": ts,
                   "open": o, "high": max(o, c) * 1.002, "low": min(o, c) * 0.998, "close": c,
                   "volume": 1e6, "trade_count": 1000, "vwap": c}
            if mutate:
                bar = mutate(bar, d) or bar
            out.append(bar)
    return out


@pytest.fixture
def seeded(make_cfg):
    """Factory: config + DB with raw and adjusted bars through SIGNAL_DATE."""
    def _seed(submit=False, validated=None, **kw):
        cfg = make_cfg(submit=submit, **kw)
        conn = connect(cfg.database)
        bars = make_bars(cfg.symbols, date(2025, 1, 2), SIGNAL_DATE)
        for adj in ("raw", "all"):
            store_bars(conn, bars, "sip", adj, SIGNAL_DATE, "test")
        if validated if validated is not None else submit:
            conn.execute("INSERT INTO universe_validations (universe_hash, ok, detail_json, validated_at_utc)"
                         " VALUES (?,1,'{}','2026-08-01T00:00:00+00:00')", (universe_hash(cfg.symbols),))
            conn.commit()
        return cfg, conn
    return _seed


class FakeMarketData:
    provider = "fake"

    def __init__(self, bars=None, prices=None, now=EXEC_NOW):
        self.bars = bars or []
        self.prices = prices
        self.now = now
        self.calls = []

    def get_daily_bars(self, symbols, start, end, adjustment, feed):
        self.calls.append((tuple(symbols), start, end, adjustment, feed))
        return [b for b in self.bars if b["symbol"] in symbols
                and start <= b["timestamp"].date() <= end]

    def get_latest_trade_prices(self, symbols, feed):
        prices = self.prices or {s: price_path(s, SIGNAL_DATE) for s in symbols}
        return {s: {"price": prices[s], "timestamp": self.now - timedelta(minutes=1)}
                for s in symbols if s in prices}


class FakeBroker:
    """In-memory paper broker. `behavior[symbol]` controls submit outcomes:
    'fill' (default), 'accept' (stays open), 'partial', 'reject',
    'timeout_after_accept', 'timeout_before_accept'."""

    def __init__(self, cash=100_000.0, positions=None, prices=None, now=EXEC_NOW):
        self.cash = cash
        self.positions = dict(positions or {})     # symbol -> qty
        self.prices = prices or {s: price_path(s, SIGNAL_DATE) for s in SYMBOLS}
        self.orders: dict[str, dict] = {}
        self.behavior: dict[str, str] = {}
        self.calls: list[tuple] = []
        self.submit_calls = 0
        self.fail_account = False
        self.fail_lookup = False
        self.is_open = True
        self.now = now

    # --- reads
    def get_account(self):
        self.calls.append(("get_account",))
        if self.fail_account:
            raise TransientAPIError("network: ConnectionError")
        lmv = sum(q * self.prices[s] for s, q in self.positions.items())
        return {"status": "ACTIVE", "equity": self.cash + lmv, "cash": self.cash, "buying_power": self.cash * 2,
                "non_marginable_buying_power": self.cash, "long_market_value": lmv, "short_market_value": 0.0,
                "trading_blocked": False, "account_blocked": False, "trade_suspended_by_user": False,
                "multiplier": 2.0, "shorting_enabled": True, "currency": "USD"}

    def get_positions(self):
        return [{"symbol": s, "qty": float(q), "side": "long", "market_value": q * self.prices[s],
                 "avg_entry_price": self.prices[s], "current_price": self.prices[s], "asset_class": "us_equity"}
                for s, q in self.positions.items() if q]

    def get_open_orders(self):
        return [dict(o) for o in self.orders.values() if o["status"] in ("new", "accepted", "partially_filled")]

    def get_order_by_client_id(self, cid):
        self.calls.append(("lookup", cid))
        if self.fail_lookup:
            raise TransientAPIError("network: Timeout")
        o = self.orders.get(cid)
        return dict(o) if o else None

    def get_clock(self):
        return {"timestamp": self.now, "is_open": self.is_open, "next_open": None, "next_close": None}

    def get_asset(self, symbol):
        return {"symbol": symbol, "name": f"{symbol} Index ETF", "status": "active", "tradable": True,
                "asset_class": "us_equity", "exchange": "ARCA", "fractionable": True, "attributes": []}

    # --- orders
    def _fill(self, o, qty):
        px = self.prices[o["symbol"]]
        sign = 1 if o["side"] == "buy" else -1
        self.positions[o["symbol"]] = self.positions.get(o["symbol"], 0) + sign * qty
        self.cash -= sign * qty * px
        o["filled_qty"] += qty
        o["filled_avg_price"] = px

    def submit_market_order(self, symbol, qty, side, client_order_id):
        self.submit_calls += 1
        self.calls.append(("submit", side, symbol, qty, client_order_id))
        if client_order_id in self.orders:
            raise PermanentAPIError("client_order_id must be unique", status_code=422)
        if side == "sell" and qty > self.positions.get(symbol, 0):
            raise PermanentAPIError("insufficient qty (would short)", status_code=403)
        beh = self.behavior.get(symbol, "fill")
        if beh == "timeout_before_accept":
            self.behavior[symbol] = "fill"      # next attempt succeeds
            raise TransientAPIError("network: Timeout")
        if beh == "reject":
            o = self._new(symbol, qty, side, client_order_id, "rejected")
            return dict(o)
        o = self._new(symbol, qty, side, client_order_id, "accepted")
        if beh in ("fill", "timeout_after_accept"):
            self._fill(o, qty)
            o["status"] = "filled"
        elif beh == "partial":
            self._fill(o, max(1, qty // 2))
            o["status"] = "canceled"            # remainder canceled (e.g. end of day)
        if beh == "timeout_after_accept":
            raise TransientAPIError("network: Timeout")
        return dict(o)

    def _new(self, symbol, qty, side, cid, status):
        o = {"id": str(uuid.uuid4()), "client_order_id": cid, "symbol": symbol, "side": side,
             "qty": float(qty), "filled_qty": 0.0, "filled_avg_price": None, "status": status,
             "type": "market", "submitted_at": self.now}
        self.orders[cid] = o
        return o

    def fill_open(self):
        for o in self.orders.values():
            if o["status"] == "accepted":
                self._fill(o, int(o["qty"]))
                o["status"] = "filled"

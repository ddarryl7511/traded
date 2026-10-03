"""Thin, safety-focused wrappers around alpaca-py (verified against alpaca-py 0.44.0).

The rest of the code only talks to `MarketDataAPI` and `PaperBroker`, which return plain
dicts. Tests replace them with fakes, so no test can reach the network or place orders.

Safety properties enforced here:
* TradingClient is always constructed with paper=True and the resulting base URL is
  asserted to be Alpaca's paper endpoint. There is no code path for live trading.
* Only whole-share, long-side, DAY market orders in the regular session can be built.
* Every HTTP request gets a timeout (alpaca-py does not set one by default).
* Read-only calls are retried a bounded number of times; order submission is NEVER
  retried blindly - callers must look the order up by client_order_id instead.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger(__name__)

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
KEY_ENV = "APCA_PAPER_API_KEY"
SECRET_ENV = "APCA_PAPER_SECRET_KEY"


class TransientAPIError(RuntimeError):
    """Network failure, timeout, 429 or 5xx: outcome of the request is unknown."""


class PermanentAPIError(RuntimeError):
    """4xx (other than 429): the request was understood and refused."""

    def __init__(self, msg, status_code=None):
        super().__init__(msg)
        self.status_code = status_code


class SafetyError(RuntimeError):
    pass


def load_credentials() -> tuple[str, str]:
    key, secret = os.environ.get(KEY_ENV, "").strip(), os.environ.get(SECRET_ENV, "").strip()
    if not key or not secret:
        raise SafetyError(f"{KEY_ENV} and {SECRET_ENV} must be set (paper keys, via environment/.env)")
    return key, secret


class _Pacer:
    """Process-wide minimum spacing between API calls (Alpaca default: 200 req/min)."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            delta = time.monotonic() - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.monotonic()


def _install_timeout(client, seconds: float) -> None:
    # alpaca-py's RESTClient calls self._session.request(...) without a timeout.
    # This touches a private attribute; re-check after upgrading alpaca-py.
    session = getattr(client, "_session", None)
    if session is None:
        log.warning("could not install HTTP timeout (alpaca-py internals changed?)")
        return
    original = session.request

    def request(method, url, **kw):
        kw.setdefault("timeout", seconds)
        return original(method, url, **kw)

    session.request = request


def _classify(exc: Exception) -> Exception:
    import requests
    from alpaca.common.exceptions import APIError

    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return TransientAPIError(f"network: {type(exc).__name__}")
    if isinstance(exc, APIError):
        code = exc.status_code
        if code is None or code == 429 or code >= 500:
            return TransientAPIError(f"api status {code}: {exc}")
        return PermanentAPIError(f"api status {code}: {exc}", status_code=code)
    return exc


class _Base:
    def __init__(self, cfg):
        self.cfg = cfg
        self.pacer = _Pacer(cfg.min_seconds_between_calls)

    def _call(self, fn, *args, retry: bool = True, **kw):
        attempts = 1 + (self.cfg.max_retries if retry else 0)
        for i in range(attempts):
            self.pacer.wait()
            try:
                return fn(*args, **kw)
            except Exception as exc:  # noqa: BLE001 - re-raised after classification
                err = _classify(exc)
                if isinstance(err, TransientAPIError) and i + 1 < attempts:
                    backoff = min(30.0, 2.0 * (2 ** i))
                    log.warning("transient API error (%s); retry %d/%d in %.0fs",
                                err, i + 1, attempts - 1, backoff)
                    time.sleep(backoff)
                    continue
                raise err from exc


def _f(v):
    return None if v is None or v == "" else float(v)


def _enum(v):
    return getattr(v, "value", v)


def order_to_dict(o) -> dict:
    return {
        "id": str(o.id), "client_order_id": o.client_order_id, "symbol": o.symbol,
        "side": _enum(o.side), "qty": _f(o.qty), "filled_qty": _f(o.filled_qty) or 0.0,
        "filled_avg_price": _f(o.filled_avg_price), "status": _enum(o.status),
        "type": _enum(o.type or o.order_type), "submitted_at": o.submitted_at,
    }


class MarketDataAPI(_Base):
    provider = "alpaca"

    def __init__(self, cfg):
        super().__init__(cfg)
        from alpaca.data.historical import StockHistoricalDataClient

        key, secret = load_credentials()
        self.client = StockHistoricalDataClient(key, secret)
        _install_timeout(self.client, cfg.http_timeout_seconds)

    def get_daily_bars(self, symbols, start: date, end: date, adjustment: str, feed: str) -> list[dict]:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        req = StockBarsRequest(
            symbol_or_symbols=list(symbols), timeframe=TimeFrame.Day,
            start=datetime(start.year, start.month, start.day, tzinfo=timezone.utc),
            # `end` is inclusive of the whole end date; callers only pass completed sessions.
            end=datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1),
            adjustment=Adjustment(adjustment), feed=DataFeed(feed),
        )
        barset = self._call(self.client.get_stock_bars, req)
        out = []
        for sym, bars in barset.data.items():
            for b in bars:
                out.append({"symbol": sym, "timestamp": b.timestamp, "open": b.open, "high": b.high,
                            "low": b.low, "close": b.close, "volume": b.volume,
                            "trade_count": b.trade_count, "vwap": b.vwap})
        return out

    def get_latest_trade_prices(self, symbols, feed: str) -> dict[str, dict]:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestTradeRequest

        res = self._call(self.client.get_stock_latest_trade,
                         StockLatestTradeRequest(symbol_or_symbols=list(symbols), feed=DataFeed(feed)))
        return {s: {"price": float(t.price), "timestamp": t.timestamp} for s, t in res.items()}


class PaperBroker(_Base):
    def __init__(self, cfg):
        super().__init__(cfg)
        from alpaca.trading.client import TradingClient

        key, secret = load_credentials()
        # paper=True is explicit and non-configurable.
        self.client = TradingClient(key, secret, paper=True)
        base = getattr(self.client, "_base_url", "")
        base = str(getattr(base, "value", base))  # alpaca-py stores a BaseURL enum
        if base.rstrip("/") != PAPER_BASE_URL:
            raise SafetyError(f"TradingClient base URL is {base!r}, expected the paper endpoint")
        _install_timeout(self.client, cfg.http_timeout_seconds)

    def get_account(self) -> dict:
        a = self._call(self.client.get_account)
        return {
            "status": _enum(a.status), "equity": _f(a.equity), "cash": _f(a.cash),
            "buying_power": _f(a.buying_power),
            "non_marginable_buying_power": _f(a.non_marginable_buying_power),
            "long_market_value": _f(a.long_market_value), "short_market_value": _f(a.short_market_value),
            "trading_blocked": bool(a.trading_blocked), "account_blocked": bool(a.account_blocked),
            "trade_suspended_by_user": bool(a.trade_suspended_by_user), "multiplier": _f(a.multiplier),
            "shorting_enabled": a.shorting_enabled, "currency": a.currency,
        }

    def get_account_configuration(self) -> dict:
        c = self._call(self.client.get_account_configurations)
        return {"no_shorting": c.no_shorting, "max_margin_multiplier": c.max_margin_multiplier,
                "fractional_trading": c.fractional_trading, "suspend_trade": c.suspend_trade}

    def get_positions(self) -> list[dict]:
        return [{"symbol": p.symbol, "qty": float(p.qty), "side": _enum(p.side),
                 "market_value": _f(p.market_value), "avg_entry_price": _f(p.avg_entry_price),
                 "current_price": _f(p.current_price), "asset_class": _enum(p.asset_class)}
                for p in self._call(self.client.get_all_positions)]

    def get_open_orders(self) -> list[dict]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = self._call(self.client.get_orders,
                            GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500, nested=False))
        return [order_to_dict(o) for o in orders]

    def get_order_by_client_id(self, client_order_id: str) -> dict | None:
        try:
            return order_to_dict(self._call(self.client.get_order_by_client_id, client_order_id))
        except PermanentAPIError as exc:
            if exc.status_code == 404:
                return None
            raise

    def submit_market_order(self, symbol: str, qty: int, side: str, client_order_id: str) -> dict:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        if not isinstance(qty, int) or qty <= 0:
            raise SafetyError(f"refusing non-positive / fractional qty {qty!r}")
        if side not in ("buy", "sell"):
            raise SafetyError(f"bad side {side!r}")
        req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide(side),
                                 time_in_force=TimeInForce.DAY, client_order_id=client_order_id,
                                 extended_hours=False)
        # retry=False: the outcome of a failed submit is unknown; caller reconciles by client id.
        # (alpaca-py itself retries 429/504 internally; a duplicate client_order_id is rejected
        # by Alpaca, which is why IDs are deterministic.)
        return order_to_dict(self._call(self.client.submit_order, req, retry=False))

    def get_clock(self) -> dict:
        c = self._call(self.client.get_clock)
        return {"timestamp": c.timestamp, "is_open": bool(c.is_open),
                "next_open": c.next_open, "next_close": c.next_close}

    def get_asset(self, symbol: str) -> dict:
        a = self._call(self.client.get_asset, symbol)
        return {"symbol": a.symbol, "name": a.name, "status": _enum(a.status),
                "tradable": a.tradable, "asset_class": _enum(a.asset_class),
                "exchange": _enum(a.exchange), "fractionable": a.fractionable,
                "attributes": a.attributes}

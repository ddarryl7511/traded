"""Exercise the real alpaca-py request/parse path with the HTTP layer mocked out.
Confirms request shapes (paper URL, whole-share DAY market orders, feeds, adjustment)
without touching the network."""
import json
from datetime import date

import pytest
import requests

from momentum_bot.alpaca_api import MarketDataAPI, PaperBroker, SafetyError, TransientAPIError

ORDER = {
    "id": "61e69015-8549-4bfd-b9c3-01e75843f47d", "client_order_id": "mb-x-202608-SPY-B",
    "created_at": "2026-09-01T14:00:00Z", "updated_at": "2026-09-01T14:00:00Z",
    "submitted_at": "2026-09-01T14:00:00Z", "filled_at": None, "expired_at": None, "canceled_at": None,
    "failed_at": None, "replaced_at": None, "replaced_by": None, "replaces": None,
    "asset_id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415", "symbol": "SPY", "asset_class": "us_equity",
    "notional": None, "qty": "3", "filled_qty": "0", "filled_avg_price": None, "order_class": "simple",
    "order_type": "market", "type": "market", "side": "buy", "time_in_force": "day", "limit_price": None,
    "stop_price": None, "status": "accepted", "extended_hours": False, "legs": None,
}


@pytest.fixture
def http(monkeypatch):
    log = []
    responses = []

    def fake_request(self, method, url, **kw):
        log.append((method, url, kw))
        status, body = responses.pop(0)
        if isinstance(body, Exception):
            raise body
        r = requests.models.Response()
        r.status_code = status
        r._content = json.dumps(body).encode()
        r.url = url
        return r

    monkeypatch.setattr(requests.Session, "request", fake_request)
    monkeypatch.setenv("APCA_PAPER_API_KEY", "PKTEST")
    monkeypatch.setenv("APCA_PAPER_SECRET_KEY", "SECRETTEST")
    return log, responses


def test_daily_bars_request_and_parse(http, make_cfg):
    log, responses = http
    responses.append((200, {"bars": {"SPY": [{"t": "2026-08-31T04:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5,
                                              "v": 100, "n": 10, "vw": 1.2}]}, "next_page_token": None}))
    bars = MarketDataAPI(make_cfg()).get_daily_bars(["SPY"], date(2026, 8, 31), date(2026, 8, 31), "all", "sip")
    method, url, kw = log[0]
    assert url.startswith("https://data.alpaca.markets/v2/stocks/bars")
    assert kw["params"]["adjustment"] == "all" and kw["params"]["feed"] == "sip"
    assert str(kw["params"]["timeframe"]) == "1Day"
    assert kw["timeout"] == 5                         # our timeout was installed
    assert bars[0]["symbol"] == "SPY" and bars[0]["close"] == 1.5


def test_submit_is_paper_whole_share_day_market(http, make_cfg):
    log, responses = http
    responses.append((200, ORDER))
    o = PaperBroker(make_cfg()).submit_market_order("SPY", 3, "buy", "mb-x-202608-SPY-B")
    method, url, kw = log[0]
    assert method == "POST" and url == "https://paper-api.alpaca.markets/v2/orders"
    body = kw["json"]
    assert body["qty"] == 3 and body["type"] == "market" and body["time_in_force"] == "day"
    assert body["client_order_id"] == "mb-x-202608-SPY-B" and body["extended_hours"] is False
    assert "notional" not in body
    assert o["status"] == "accepted" and o["qty"] == 3.0


def test_submit_network_error_is_not_retried(http, make_cfg):
    log, responses = http
    responses.append((0, requests.exceptions.ConnectionError("down")))
    with pytest.raises(TransientAPIError):
        PaperBroker(make_cfg()).submit_market_order("SPY", 3, "buy", "cid")
    assert len(log) == 1


def test_reads_are_retried_boundedly(http, make_cfg, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    log, responses = http
    responses.extend([(0, requests.exceptions.Timeout()), (0, requests.exceptions.Timeout())])
    with pytest.raises(TransientAPIError):
        PaperBroker(make_cfg()).get_clock()
    assert len(log) == 2                              # 1 + max_retries(1)


def test_order_lookup_404_returns_none(http, make_cfg):
    log, responses = http
    responses.append((404, {"code": 40410000, "message": "order not found"}))
    assert PaperBroker(make_cfg()).get_order_by_client_id("nope") is None


def test_refuses_bad_orders_before_any_request(http, make_cfg):
    log, _ = http
    b = PaperBroker(make_cfg())
    for qty, side in ((0, "buy"), (-1, "sell"), (2.5, "buy"), (1, "short")):
        with pytest.raises(SafetyError):
            b.submit_market_order("SPY", qty, side, "cid")
    assert log == []

from datetime import date

import pytest

from momentum_bot import calendar_utils as cal
from momentum_bot.strategy import compute_month_signal, target_weights
from tests.conftest import SIGNAL_DATE, SYMBOLS, price_path


def closes_for(symbols, start=date(2025, 1, 2), end=SIGNAL_DATE, skip=()):
    return {s: {d: price_path(s, d) for d in cal.sessions_in_range(start, end) if (s, d) not in skip}
            for s in symbols}


def signal(closes, d=SIGNAL_DATE):
    return compute_month_signal(closes, d, SYMBOLS, 12, 0.10, 0.50, 0, 0.40)


@pytest.mark.parametrize("n,expected", [(1, 0.10), (3, 0.10), (5, 0.10), (6, 0.50 / 6), (8, 0.0625), (10, 0.05)])
def test_allocation_caps(n, expected):
    w = target_weights([f"S{i}" for i in range(n)], 0.10, 0.50)
    assert all(v == pytest.approx(expected) for v in w.values())
    assert sum(w.values()) <= 0.50 + 1e-12
    assert max(w.values()) <= 0.10


def test_no_qualifiers_means_cash():
    assert target_weights([], 0.10, 0.50) == {}


def test_signal_qualifies_positive_returns_only():
    sig = signal(closes_for(SYMBOLS))
    assert sig.status == "ok"
    assert sig.weights == {"AAA": 0.10, "BBB": 0.10, "CCC": 0.10, "DDD": 0.0, "EEE": 0.0}
    row = next(r for r in sig.symbols if r.symbol == "AAA")
    assert row.lookback_start == date(2025, 8, 29)       # last session of Aug 2025
    assert row.trailing_return == pytest.approx(price_path("AAA", SIGNAL_DATE) / price_path("AAA", date(2025, 8, 29)) - 1)


def test_missing_bar_blocks_whole_month():
    sig = signal(closes_for(SYMBOLS, skip={("CCC", date(2026, 3, 10))}))
    assert sig.status == "blocked" and "CCC" in sig.reason
    assert all(r.target_weight == 0 for r in sig.symbols)


def test_stale_data_blocks():
    sig = signal(closes_for(SYMBOLS, end=date(2026, 8, 28)))
    assert sig.status == "blocked" and "stale" in sig.reason


def test_insufficient_history_blocks():
    sig = signal(closes_for(SYMBOLS, start=date(2025, 10, 1)))
    assert sig.status == "blocked" and "lookback-start" in sig.reason


def test_inconsistent_jump_blocks():
    c = closes_for(SYMBOLS)
    c["AAA"][date(2026, 5, 4)] *= 2          # looks like an unadjusted split
    assert signal(c).status == "blocked"


def test_not_month_end_rejected():
    assert signal(closes_for(SYMBOLS), d=date(2026, 8, 28)).status == "blocked"


def test_future_data_is_ignored():
    """Look-ahead guard: data after the signal date must not change the signal."""
    base = signal(closes_for(SYMBOLS))
    future = closes_for(SYMBOLS, end=date(2026, 9, 30))
    for s in SYMBOLS:
        for d in list(future[s]):
            if d > SIGNAL_DATE:
                future[s][d] = 1e-6 if s in ("AAA", "BBB", "CCC") else 1e6
    assert signal(future).weights == base.weights

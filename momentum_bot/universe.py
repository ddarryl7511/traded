"""Universe validation against Alpaca asset metadata (read-only)."""
from __future__ import annotations

import json

from .config import LEVERAGED_OR_INVERSE, NAME_RED_FLAGS, universe_hash
from .db import iso, utcnow


def validate_universe(conn, cfg, broker) -> tuple[bool, list[str]]:
    problems, details = [], {}
    for sym in cfg.symbols:
        if sym in LEVERAGED_OR_INVERSE:
            problems.append(f"{sym}: on leveraged/inverse denylist")
        try:
            a = broker.get_asset(sym)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{sym}: asset lookup failed ({exc})")
            continue
        details[sym] = a
        name = (a.get("name") or "").upper()
        if str(a.get("asset_class")) != "us_equity":
            problems.append(f"{sym}: asset class {a.get('asset_class')}")
        if str(a.get("status")) != "active" or not a.get("tradable"):
            problems.append(f"{sym}: not active/tradable")
        flags = [f for f in NAME_RED_FLAGS if f in name]
        if flags:
            problems.append(f"{sym}: name '{a.get('name')}' contains {flags} - leveraged/inverse/volatility?")
    ok = not problems
    conn.execute("INSERT INTO universe_validations (universe_hash, ok, detail_json, validated_at_utc)"
                 " VALUES (?,?,?,?)", (universe_hash(cfg.symbols), int(ok),
                                       json.dumps({"problems": problems, "assets": details}, default=str),
                                       iso(utcnow())))
    conn.commit()
    return ok, problems

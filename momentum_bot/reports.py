"""CSV exports of the SQLite tables. Paper-account tables and backtest tables are kept in
separate files so simulated and paper results are never mixed."""
from __future__ import annotations

import csv
from pathlib import Path

EXPORTS = {
    "bars_raw": "SELECT * FROM bars WHERE adjustment='raw' ORDER BY symbol, session_date",
    "bars_adjusted": "SELECT * FROM bars WHERE adjustment='all' ORDER BY symbol, session_date",
    "data_quality": "SELECT * FROM data_quality ORDER BY id",
    "experiments": "SELECT * FROM experiments",
    "signal_runs": "SELECT * FROM signal_runs ORDER BY month_key",
    "signals": "SELECT * FROM signals ORDER BY month_key, symbol",
    "rebalances": "SELECT * FROM rebalances ORDER BY created_at_utc",
    "paper_orders": "SELECT * FROM orders WHERE mode='paper' ORDER BY created_at_utc",
    "dry_run_orders": "SELECT * FROM orders WHERE mode='dry_run' ORDER BY created_at_utc",
    "paper_fills": "SELECT * FROM fills ORDER BY id",
    "account_snapshots": "SELECT * FROM account_snapshots ORDER BY id",
    "position_snapshots": "SELECT * FROM position_snapshots ORDER BY snapshot_id, symbol",
    "open_order_snapshots": "SELECT * FROM open_order_snapshots ORDER BY snapshot_id",
    "events": "SELECT * FROM events ORDER BY id",
    "backtest_runs": "SELECT * FROM backtest_runs ORDER BY id",
}


def export_all(conn, out_dir: Path, tables=None) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, sql in EXPORTS.items():
        if tables and name not in tables:
            continue
        cur = conn.execute(sql)
        path = out_dir / f"{name}.csv"
        with path.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow([c[0] for c in cur.description])
            for row in cur:  # streamed row by row: no large in-memory datasets
                w.writerow(list(row))
        written.append(path)
    return written

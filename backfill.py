"""
Backfill historical price data for all watched markets.

Uses clob.polymarket.com/prices-history (max interval) to pull full price
history from market creation, then computes hourly snapshots from the
trades_raw table for any period where we have trade data.

Run once before starting the live collector.
"""

import json
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from collector import init_db, discover_markets, enrich_market_tokens, ingest_trades, DB_PATH, BUCKET_SECS

ROOT = Path(__file__).parent


def _get(url: str, timeout: int = 15) -> dict | list:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "poly-potato/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def backfill_price_history(con: sqlite3.Connection, condition_id: str, label: str) -> int:
    """
    Pull full price history from CLOB prices-history endpoint and write to
    market_snapshots as best-effort (no trade-level features, just price).
    Returns number of rows written.
    """
    yes_token, _ = enrich_market_tokens(con, condition_id)
    if not yes_token:
        print(f"  {label[:50]}: no yes_token, skipping")
        return 0

    try:
        hist = _get(
            f"https://clob.polymarket.com/prices-history"
            f"?market={yes_token}&interval=max&fidelity=60"
        )
    except Exception as e:
        print(f"  {label[:50]}: price history error: {e}")
        return 0

    points = hist.get("history", [])
    if not points:
        return 0

    rows_written = 0
    for pt in points:
        ts    = pt["t"]
        price = pt["p"]
        bucket = (ts // BUCKET_SECS) * BUCKET_SECS

        # Only insert if no existing row (don't overwrite richer live data)
        existing = con.execute(
            "SELECT 1 FROM market_snapshots WHERE condition_id=? AND bucket_start=?",
            (condition_id, bucket)
        ).fetchone()
        if existing:
            continue

        con.execute("""
            INSERT OR IGNORE INTO market_snapshots
                (condition_id, bucket_start, price_yes)
            VALUES (?, ?, ?)
        """, (condition_id, bucket, price))
        rows_written += 1

    con.commit()

    if points:
        t0 = datetime.fromtimestamp(points[0]["t"],  tz=timezone.utc).strftime("%Y-%m-%d")
        t1 = datetime.fromtimestamp(points[-1]["t"], tz=timezone.utc).strftime("%Y-%m-%d")
        p_lo = min(p["p"] for p in points)
        p_hi = max(p["p"] for p in points)
        print(f"  {label[:50]:<52} {t0}→{t1}  p=[{p_lo:.2f},{p_hi:.2f}]  +{rows_written} pts")

    return rows_written


def main() -> None:
    print("poly_potato backfill starting")
    con     = init_db()
    active  = discover_markets(con)
    print(f"Markets to backfill: {len(active)}\n")

    total_pts    = 0
    total_trades = 0

    for cid, label in active.items():
        pts = backfill_price_history(con, cid, label)
        total_pts += pts

        # Also pull as many trades as the API will give us
        n = ingest_trades(con, cid)
        total_trades += n
        if n:
            print(f"    +{n} historical trades")

        time.sleep(0.4)

    print(f"\nDone. {total_pts} price points, {total_trades} trades written to {DB_PATH}")


if __name__ == "__main__":
    main()

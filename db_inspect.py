"""
Quick database inspection tool.

Usage:
  python inspect.py              — summary of all markets
  python inspect.py <cid>        — detail view for one condition_id (partial match ok)
  python inspect.py alerts       — show all alert-worthy snapshots
"""

import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from collector import DB_PATH, BUCKET_SECS

ALERT_PRICE_MAX  = 0.30
ALERT_DELTA_MIN  = 0.04
ALERT_OFI_MIN    = 0.65
ALERT_NEW_W_MIN  = 0.25


def ts(epoch: int | None) -> str:
    if not epoch:
        return "—"
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def summary(con: sqlite3.Connection) -> None:
    markets = con.execute("""
        SELECT m.condition_id, m.label, m.question,
               (SELECT COUNT(*) FROM market_snapshots s WHERE s.condition_id = m.condition_id) AS n_snaps,
               (SELECT MIN(bucket_start) FROM market_snapshots s WHERE s.condition_id = m.condition_id) AS first_snap,
               (SELECT MAX(bucket_start) FROM market_snapshots s WHERE s.condition_id = m.condition_id) AS last_snap,
               (SELECT COUNT(*) FROM trades_raw t WHERE t.condition_id = m.condition_id) AS n_trades
        FROM markets m
        ORDER BY n_snaps DESC
    """).fetchall()

    print(f"\n{'Label':<50} {'Snaps':>6} {'Trades':>7} {'Since':<12} {'Last':<12}")
    print("─" * 100)
    for cid, label, question, n_snaps, first, last, n_trades in markets:
        display = (label or question or cid[:16])[:50]
        print(f"{display:<50} {n_snaps:>6} {n_trades:>7} {ts(first):<12} {ts(last):<12}")

    total_snaps  = con.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
    total_trades = con.execute("SELECT COUNT(*) FROM trades_raw").fetchone()[0]
    print(f"\nTotal: {len(markets)} markets  |  {total_snaps:,} snapshots  |  {total_trades:,} trades")


def detail(con: sqlite3.Connection, partial_id: str) -> None:
    row = con.execute(
        "SELECT condition_id, label, question FROM markets WHERE condition_id LIKE ?",
        (f"%{partial_id}%",)
    ).fetchone()
    if not row:
        print(f"No market matching '{partial_id}'")
        return

    cid, label, question = row
    print(f"\n{question or label}")
    print(f"condition_id: {cid}")
    print()

    snaps = con.execute("""
        SELECT bucket_start, price_yes, delta_price_1h, ofi,
               n_wallets, new_wallet_pct, volume_usdc, trade_count
        FROM market_snapshots
        WHERE condition_id=?
        ORDER BY bucket_start DESC
        LIMIT 48
    """, (cid,)).fetchall()

    print(f"{'Time (UTC)':<18} {'Price':>6} {'Δ1h':>6} {'OFI':>6} {'Wlts':>5} {'New%':>6} {'Vol$':>8} {'Trd':>4}  Alert")
    print("─" * 85)
    for b, p, dp, ofi, nw, nwp, vol, tc in snaps:
        alert = ""
        if p and dp and ofi and nwp:
            if p < ALERT_PRICE_MAX and dp > ALERT_DELTA_MIN and ofi > ALERT_OFI_MIN and nwp > ALERT_NEW_W_MIN:
                alert = " *** ALERT ***"
        p_s   = f"{p:.3f}"   if p   is not None else "—"
        dp_s  = f"{dp:+.3f}" if dp  is not None else "—"
        ofi_s = f"{ofi:+.2f}" if ofi is not None else "—"
        nw_s  = str(nw)      if nw  is not None else "—"
        nwp_s = f"{nwp:.0%}" if nwp is not None else "—"
        vol_s = f"${vol:,.0f}" if vol is not None else "—"
        tc_s  = str(tc)      if tc  is not None else "—"
        print(f"{ts(b):<18} {p_s:>6} {dp_s:>6} {ofi_s:>6} {nw_s:>5} {nwp_s:>6} {vol_s:>8} {tc_s:>4}{alert}")


def show_alerts(con: sqlite3.Connection) -> None:
    rows = con.execute("""
        SELECT s.bucket_start, m.label, m.question,
               s.price_yes, s.delta_price_1h, s.ofi, s.new_wallet_pct, s.volume_usdc
        FROM market_snapshots s
        JOIN markets m ON m.condition_id = s.condition_id
        WHERE s.price_yes       < ?
          AND s.delta_price_1h  > ?
          AND s.ofi             > ?
          AND s.new_wallet_pct  > ?
        ORDER BY s.bucket_start DESC
    """, (ALERT_PRICE_MAX, ALERT_DELTA_MIN, ALERT_OFI_MIN, ALERT_NEW_W_MIN)).fetchall()

    if not rows:
        print("No alert-worthy snapshots in database yet.")
        return

    print(f"\n{'Time (UTC)':<18} {'Market':<50} {'Price':>6} {'Δ1h':>6} {'OFI':>6} {'New%':>5}")
    print("─" * 100)
    for b, label, question, p, dp, ofi, nwp, vol in rows:
        display = (label or question or "")[:50]
        print(f"{ts(b):<18} {display:<50} {p:.3f} {dp:+.3f} {ofi:+.2f} {nwp:.0%}")


def main() -> None:
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}. Run backfill.py first.")
        return

    con = sqlite3.connect(DB_PATH)
    args = sys.argv[1:]

    if not args:
        summary(con)
    elif args[0] == "alerts":
        show_alerts(con)
    else:
        detail(con, args[0])


if __name__ == "__main__":
    main()

"""
Polymarket data collector.

Every RUN_INTERVAL_SECONDS it:
  1. Discovers new political markets above MIN_LIQUIDITY threshold
  2. For each watched market, fetches recent trades and current order book
  3. Computes per-bucket features and appends to SQLite DB

Schema
------
  market_snapshots  — one row per (condition_id, bucket_start)
  trades_raw        — one row per individual trade (deduplicated by tx hash)
"""

import json
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from watchlist import WATCHLIST, DISCOVERY_KEYWORDS, MIN_LIQUIDITY

ROOT        = Path(__file__).parent
DB_PATH     = ROOT / "data" / "poly_potato.db"
BUCKET_SECS = 15 * 60   # 15-minute buckets
POLL_SECS   = 5 * 60    # poll every 5 minutes
TRADE_LIMIT = 500        # trades to fetch per market per poll


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _get(url: str, timeout: int = 12) -> dict | list:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "poly-potato/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ── Database ──────────────────────────────────────────────────────────────────

def init_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS markets (
            condition_id  TEXT PRIMARY KEY,
            question      TEXT,
            yes_token     TEXT,
            no_token      TEXT,
            label         TEXT,
            first_seen    INTEGER,
            last_seen     INTEGER
        );

        CREATE TABLE IF NOT EXISTS trades_raw (
            tx_hash       TEXT PRIMARY KEY,
            condition_id  TEXT,
            timestamp     INTEGER,
            wallet        TEXT,
            side          TEXT,
            outcome       TEXT,
            price         REAL,
            size          REAL
        );

        CREATE TABLE IF NOT EXISTS market_snapshots (
            condition_id        TEXT,
            bucket_start        INTEGER,
            price_yes           REAL,
            delta_price_1h      REAL,
            volume_usdc         REAL,
            ofi                 REAL,   -- order flow imbalance YES: (+1 all buys, -1 all sells)
            n_wallets           INTEGER,
            new_wallet_pct      REAL,   -- fraction of wallets not seen in prior 48h
            max_trade_size      REAL,
            spread              REAL,
            best_bid            REAL,
            best_ask            REAL,
            trade_count         INTEGER,
            PRIMARY KEY (condition_id, bucket_start)
        );

        CREATE INDEX IF NOT EXISTS idx_trades_cid_ts
            ON trades_raw(condition_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_snap_cid_ts
            ON market_snapshots(condition_id, bucket_start);
    """)
    con.commit()
    return con


# ── Market discovery ──────────────────────────────────────────────────────────

def discover_markets(con: sqlite3.Connection) -> dict[str, str]:
    """
    Return {condition_id: question} for all watched + newly discovered markets.
    """
    active = dict(WATCHLIST)

    try:
        markets = _get("https://gamma-api.polymarket.com/markets?active=true&limit=200&order=volume&ascending=false")
        for m in markets:
            q   = m.get("question", "").lower()
            liq = float(m.get("liquidity", 0))
            cid = m.get("conditionId", "")
            if not cid:
                continue
            if liq < MIN_LIQUIDITY:
                continue
            if any(kw in q for kw in DISCOVERY_KEYWORDS):
                active.setdefault(cid, m.get("question", ""))
    except Exception as e:
        print(f"  [discovery] warning: {e}")

    # Upsert into markets table
    now = int(time.time())
    for cid, label in active.items():
        con.execute("""
            INSERT INTO markets(condition_id, label, first_seen, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(condition_id) DO UPDATE SET last_seen=excluded.last_seen
        """, (cid, label, now, now))
    con.commit()

    return active


def enrich_market_tokens(con: sqlite3.Connection, condition_id: str) -> tuple[str | None, str | None]:
    """
    Fetch YES/NO token IDs from CLOB if not already stored.
    Returns (yes_token, no_token).
    """
    row = con.execute(
        "SELECT yes_token, no_token, question FROM markets WHERE condition_id=?",
        (condition_id,)
    ).fetchone()

    if row and row[0]:
        return row[0], row[1]

    try:
        m = _get(f"https://clob.polymarket.com/markets/{condition_id}")
        tokens  = m.get("tokens", [])
        yes_tok = next((t["token_id"] for t in tokens if t["outcome"] == "Yes"), None)
        no_tok  = next((t["token_id"] for t in tokens if t["outcome"] == "No"),  None)
        question = m.get("question", "")
        con.execute("""
            UPDATE markets SET yes_token=?, no_token=?, question=? WHERE condition_id=?
        """, (yes_tok, no_tok, question, condition_id))
        con.commit()
        return yes_tok, no_tok
    except Exception as e:
        print(f"  [tokens] {condition_id[:12]}... error: {e}")
        return None, None


# ── Trade ingestion ───────────────────────────────────────────────────────────

def ingest_trades(con: sqlite3.Connection, condition_id: str) -> int:
    """Fetch recent trades and insert new ones. Returns count inserted."""
    try:
        trades = _get(
            f"https://data-api.polymarket.com/trades?market={condition_id}&limit={TRADE_LIMIT}"
        )
    except Exception as e:
        print(f"  [trades] {condition_id[:12]}... error: {e}")
        return 0

    rows = []
    for t in trades:
        rows.append((
            t["transactionHash"],
            condition_id,
            t["timestamp"],
            t["proxyWallet"],
            t["side"],
            t["outcome"],
            t["price"],
            t["size"],
        ))

    if not rows:
        return 0

    con.executemany("""
        INSERT OR IGNORE INTO trades_raw
            (tx_hash, condition_id, timestamp, wallet, side, outcome, price, size)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    con.commit()
    return len(rows)


# ── Feature computation ───────────────────────────────────────────────────────

def compute_bucket_features(
    con: sqlite3.Connection,
    condition_id: str,
    bucket_start: int,
    yes_token: str | None,
) -> dict | None:
    """
    Compute features for a single 15-minute bucket ending at bucket_start + BUCKET_SECS.
    Returns None if no trades in bucket.
    """
    bucket_end = bucket_start + BUCKET_SECS

    trades = con.execute("""
        SELECT wallet, side, outcome, price, size, timestamp
        FROM trades_raw
        WHERE condition_id=? AND timestamp >= ? AND timestamp < ?
    """, (condition_id, bucket_start, bucket_end)).fetchall()

    if not trades:
        return None

    # Order flow imbalance: positive = net YES buying pressure
    yes_buy_vol  = sum(s for _, side, out, _, s, _ in trades if side == "BUY"  and out == "Yes")
    yes_sell_vol = sum(s for _, side, out, _, s, _ in trades if side == "SELL" and out == "Yes")
    total_vol    = sum(s for _, _, _, _, s, _ in trades)

    ofi = (yes_buy_vol - yes_sell_vol) / total_vol if total_vol > 0 else 0.0

    # Wallet novelty: fraction of wallets not seen in prior 48h
    bucket_wallets = set(w for w, *_ in trades)
    cutoff_48h     = bucket_start - 48 * 3600
    known_wallets  = set(
        row[0] for row in con.execute("""
            SELECT DISTINCT wallet FROM trades_raw
            WHERE condition_id=? AND timestamp >= ? AND timestamp < ?
        """, (condition_id, cutoff_48h, bucket_start)).fetchall()
    )
    new_wallet_pct = len(bucket_wallets - known_wallets) / len(bucket_wallets) if bucket_wallets else 0.0

    # Price stats
    yes_trades = [(p, s) for _, _, out, p, s, _ in trades if out == "Yes"]
    price_yes  = yes_trades[-1][0] if yes_trades else None  # most recent YES price

    # 1-hour price change: compare to snapshot 4 buckets ago
    prior_snap = con.execute("""
        SELECT price_yes FROM market_snapshots
        WHERE condition_id=? AND bucket_start <= ?
        ORDER BY bucket_start DESC LIMIT 1
    """, (condition_id, bucket_start - 3600)).fetchone()
    delta_1h = (price_yes - prior_snap[0]) if (price_yes is not None and prior_snap and prior_snap[0]) else None

    max_trade_size = max(s for _, _, _, _, s, _ in trades) if trades else 0.0

    # Live order book for spread
    spread = best_bid = best_ask = None
    if yes_token:
        try:
            ob = _get(f"https://clob.polymarket.com/order-book?token_id={yes_token}")
            bids = ob.get("bids", [])
            asks = ob.get("asks", [])
            if bids and asks:
                best_bid = float(bids[0]["price"])
                best_ask = float(asks[0]["price"])
                spread   = best_ask - best_bid
        except Exception:
            pass

    return {
        "condition_id":    condition_id,
        "bucket_start":    bucket_start,
        "price_yes":       price_yes,
        "delta_price_1h":  delta_1h,
        "volume_usdc":     total_vol,
        "ofi":             ofi,
        "n_wallets":       len(bucket_wallets),
        "new_wallet_pct":  new_wallet_pct,
        "max_trade_size":  max_trade_size,
        "spread":          spread,
        "best_bid":        best_bid,
        "best_ask":        best_ask,
        "trade_count":     len(trades),
    }


def write_snapshot(con: sqlite3.Connection, snap: dict) -> None:
    con.execute("""
        INSERT OR REPLACE INTO market_snapshots
            (condition_id, bucket_start, price_yes, delta_price_1h, volume_usdc,
             ofi, n_wallets, new_wallet_pct, max_trade_size, spread,
             best_bid, best_ask, trade_count)
        VALUES
            (:condition_id, :bucket_start, :price_yes, :delta_price_1h, :volume_usdc,
             :ofi, :n_wallets, :new_wallet_pct, :max_trade_size, :spread,
             :best_bid, :best_ask, :trade_count)
    """, snap)
    con.commit()


# ── Alert logic ───────────────────────────────────────────────────────────────

def check_alert(snap: dict, label: str) -> bool:
    """
    Rule-based alert: fires when the snapshot matches the known insider accumulation pattern.

      - Market still pricing event as unlikely  (price < 0.30)
      - Price has moved up meaningfully in 1h   (delta > 0.04)
      - Almost all flow is YES buying            (ofi > 0.65)
      - Fresh wallets appearing                  (new_wallet_pct > 0.25)
    """
    p     = snap.get("price_yes")
    dp    = snap.get("delta_price_1h")
    ofi   = snap.get("ofi", 0)
    nwp   = snap.get("new_wallet_pct", 0)

    if p is None or dp is None:
        return False

    triggered = (p < 0.30) and (dp > 0.04) and (ofi > 0.65) and (nwp > 0.25)

    if triggered:
        ts = datetime.fromtimestamp(snap["bucket_start"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        print(f"\n  *** ALERT *** {ts}")
        print(f"  Market : {label}")
        print(f"  Price  : {p:.3f}  (Δ1h={dp:+.3f})")
        print(f"  OFI    : {ofi:+.3f}  (new wallets={nwp:.0%})")
        print(f"  Volume : ${snap.get('volume_usdc',0):,.0f} USDC in this bucket\n")

    return triggered


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_once(con: sqlite3.Connection) -> None:
    now    = int(time.time())
    bucket = (now // BUCKET_SECS) * BUCKET_SECS

    print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] polling {len(WATCHLIST)} seed markets...")

    active = discover_markets(con)
    print(f"  watchlist size: {len(active)}")

    for cid, label in active.items():
        yes_token, _ = enrich_market_tokens(con, cid)
        n_new = ingest_trades(con, cid)
        snap  = compute_bucket_features(con, cid, bucket, yes_token)

        if snap:
            write_snapshot(con, snap)
            check_alert(snap, label)
            p = snap.get("price_yes")
            p_s = f"{p:.3f}" if p else "N/A"
            print(f"  {label[:50]:<50}  p={p_s}  ofi={snap['ofi']:+.2f}  trades={snap['trade_count']}  new_trades={n_new}")
        else:
            print(f"  {label[:50]:<50}  (no trades this bucket)")

        time.sleep(0.3)


def main() -> None:
    print("poly_potato collector starting")
    print(f"  DB: {DB_PATH}")
    print(f"  Bucket: {BUCKET_SECS//60}m  |  Poll interval: {POLL_SECS//60}m")

    con = init_db()

    while True:
        try:
            run_once(con)
        except KeyboardInterrupt:
            print("\nstopped.")
            break
        except Exception as e:
            print(f"[error] {e}")

        print(f"  sleeping {POLL_SECS//60}m...")
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()

"""
Single-pass collector + HMM scorer for cron execution.

Runs one poll cycle, scores all markets with the trained HMM,
sends Pushover alerts for accumulation signals, then exits.
"""

import json
import os
import pickle
import sqlite3
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

import numpy as np

from collector import init_db, run_once, DB_PATH, BUCKET_SECS as SNAP_SECS
from alert import send as push

ROOT       = Path(__file__).parent
MODEL_PATH = ROOT / "data" / "hmm_model.pkl"

# Scoring thresholds
P_ACCUM_ALERT     = 0.50   # P(accumulation) triggers normal alert
P_ACCUM_EMERGENCY = 0.85   # P(accumulation) triggers emergency (repeat until ack)
MIN_PRICE_FOR_ALERT = 0.03  # don't alert if market already resolved near zero
MAX_PRICE_FOR_ALERT = 0.20  # above 20 cents = market already pricing known news, no edge
MIN_HOURLY_BUCKETS  = 3     # need at least 3 hours of data to score

# GDELT news check — suppress alert if too many articles found in last 24h
GDELT_SUPPRESS_THRESHOLD = 5   # >= 5 articles = news-driven, suppress
GDELT_WARN_THRESHOLD     = 1   # 1-4 articles = flag but still alert

# Keywords in market name that indicate sports/entertainment — skip alerting
SPORTS_KEYWORDS = [
    "atp", "wta", "roland garros", "wimbledon", "us open", "australian open",
    "nba", "nfl", "nhl", "mlb", "nba finals", "world series", "super bowl",
    "fifa", "world cup", "champions league", "premier league", "la liga",
    "ufc", "boxing", "mma",
    "moneyline", "spread", "o/u", "over/under", "1h ", "map ",
    "esports", "esl", "iem", "lol:", "cs2", "dota",
    "oscars", "emmy", "grammy", "golden globe",
    "temperature", "weather", "celsius", "fahrenheit",
]

HOUR_SECS  = 3600
LOOKBACK   = 7 * 24 * HOUR_SECS  # use last 7 days of snapshots


# ── Feature extraction (matches train_hmm.py) ─────────────────────────────────

def extract(bucket: dict) -> np.ndarray:
    ofi   = float(np.clip(bucket["ofi"]   if bucket["ofi"]   is not None else 0.0, -1.0, 1.0))
    vol   = float(np.log1p(bucket["volume_usdc"] or 0.0))
    price = float(bucket["price_yes"]       if bucket["price_yes"]       is not None else 0.5)
    new_w = float(bucket["new_wallet_pct"]  if bucket["new_wallet_pct"]  is not None else 0.0)
    return np.array([ofi, vol, price, new_w])


def aggregate_to_hourly(snaps: list[tuple]) -> list[dict]:
    """
    Collapse 15-min snapshots into 1-hour buckets by averaging features.
    snaps: list of (bucket_start, price_yes, ofi, volume_usdc, new_wallet_pct)
    """
    hourly: dict[int, list] = defaultdict(list)
    for bkt, price, ofi, vol, new_w in snaps:
        hour_bkt = (bkt // HOUR_SECS) * HOUR_SECS
        hourly[hour_bkt].append({
            "price_yes":      price,
            "ofi":            ofi,
            "volume_usdc":    vol,
            "new_wallet_pct": new_w,
        })

    result = []
    for hour_bkt in sorted(hourly.keys()):
        rows = hourly[hour_bkt]
        def avg(key):
            vals = [r[key] for r in rows if r[key] is not None]
            return sum(vals) / len(vals) if vals else None
        result.append({
            "bucket_start":   hour_bkt,
            "price_yes":      avg("price_yes"),
            "ofi":            avg("ofi"),
            "volume_usdc":    avg("volume_usdc"),
            "new_wallet_pct": avg("new_wallet_pct"),
        })
    return result


def forward_filter(model, X: np.ndarray) -> np.ndarray:
    """Causal forward algorithm — P(state_t | obs_1..obs_t)."""
    from scipy.special import logsumexp
    T, K = len(X), model.n_components
    log_emit  = model._compute_log_likelihood(X)
    log_trans = np.log(model.transmat_ + 1e-300)

    log_alpha = np.full((T, K), -np.inf)
    log_alpha[0] = np.log(model.startprob_ + 1e-300) + log_emit[0]
    for t in range(1, T):
        for k in range(K):
            log_alpha[t, k] = (
                np.logaddexp.reduce(log_alpha[t - 1] + log_trans[:, k])
                + log_emit[t, k]
            )
    log_norm = np.logaddexp.reduce(log_alpha, axis=1, keepdims=True)
    return np.exp(log_alpha - log_norm)


# ── GDELT news check ─────────────────────────────────────────────────────────

def gdelt_article_count(market_name: str, hours: int = 24) -> tuple[int, str]:
    """
    Query GDELT for articles mentioning this market in the last N hours.
    Returns (article_count, top_headline).
    Fails open — returns (0, '') on any error so alerts aren't suppressed by GDELT outage.
    """
    query    = urllib.parse.quote(market_name[:80])
    timespan = hours * 60  # GDELT uses minutes
    url = (
        f"https://api.gdeltproject.org/api/v2/doc/doc"
        f"?query={query}&mode=artlist&maxrecords=10"
        f"&timespan={timespan}&format=json"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "poly-potato/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        articles = data.get("articles", [])
        headline = articles[0].get("title", "") if articles else ""
        return len(articles), headline
    except Exception:
        return 0, ""  # fail open


# ── Per-market alert deduplication ────────────────────────────────────────────
# Track last alert time per market so we don't spam on every 15-min run.

ALERT_COOLDOWN_SECS = 4 * HOUR_SECS   # re-alert at most once per 4 hours

_alert_state_path = ROOT / "data" / "alert_state.json"

def load_alert_state() -> dict:
    import json
    if _alert_state_path.exists():
        try:
            return json.loads(_alert_state_path.read_text())
        except Exception:
            pass
    return {}

def save_alert_state(state: dict) -> None:
    import json
    _alert_state_path.write_text(json.dumps(state, indent=2))


# ── Main ──────────────────────────────────────────────────────────────────────

def score_markets(con: sqlite3.Connection, model) -> None:
    now     = int(time.time())
    cutoff  = now - LOOKBACK
    markets = con.execute("SELECT condition_id, label, question FROM markets").fetchall()
    alert_state = load_alert_state()

    for cid, label, question in markets:
        snaps = con.execute("""
            SELECT bucket_start, price_yes, ofi, volume_usdc, new_wallet_pct
            FROM market_snapshots
            WHERE condition_id = ? AND bucket_start >= ?
            ORDER BY bucket_start ASC
        """, (cid, cutoff)).fetchall()

        if not snaps:
            continue

        hourly = aggregate_to_hourly(list(snaps))
        if len(hourly) < MIN_HOURLY_BUCKETS:
            continue

        X      = np.array([extract(b) for b in hourly])
        probs  = forward_filter(model, X)
        p_accum = float(probs[-1, 1])
        price   = hourly[-1]["price_yes"] or 0.0

        name = (label or question or cid[:16])[:50]
        print(f"  {name:<50} price={price:.3f}  P(accum)={p_accum:.3f}")

        if p_accum < P_ACCUM_ALERT:
            continue
        if price < MIN_PRICE_FOR_ALERT or price >= MAX_PRICE_FOR_ALERT:
            continue
        name_lower = name.lower()
        if any(kw in name_lower for kw in SPORTS_KEYWORDS):
            continue

        # Require price to have been below 0.08 in the last 48h —
        # distinguishes spike-from-floor (insider) from market sitting at
        # elevated price due to public news (no edge)
        cutoff_48h = now - 48 * 3600
        baseline = con.execute("""
            SELECT MIN(price_yes) FROM market_snapshots
            WHERE condition_id = ? AND bucket_start >= ? AND price_yes IS NOT NULL
        """, (cid, cutoff_48h)).fetchone()[0]
        if baseline is None or baseline >= 0.08:
            continue

        last_alerted = alert_state.get(cid, 0)
        if now - last_alerted < ALERT_COOLDOWN_SECS:
            print(f"    → cooldown active, skipping alert")
            continue

        # GDELT news check
        n_articles, headline = gdelt_article_count(name)
        print(f"    → GDELT: {n_articles} articles in last 24h")
        if n_articles >= GDELT_SUPPRESS_THRESHOLD:
            print(f"    → suppressed (news-driven): {headline[:80]}")
            continue

        news_query = urllib.parse.quote(name[:60])
        news_url   = f"https://news.google.com/search?q={news_query}&hl=en-US&gl=US&ceid=US:en"
        news_note  = f"⚠️ {n_articles} news article(s) — verify before acting" if n_articles >= GDELT_WARN_THRESHOLD else "No public news found ✓"

        priority = 2 if p_accum >= P_ACCUM_EMERGENCY else 1
        title    = f"{'STRONG ' if p_accum >= P_ACCUM_EMERGENCY else ''}Accumulation: {name[:30]}"
        body     = (
            f"P(accum)={p_accum:.0%}  price={price:.3f}\n"
            f"{news_note}\n"
            f"{len(hourly)} hours of data"
        )
        push(title, body, priority=priority, url=news_url, url_title="Check News")

        alert_state[cid] = now

    save_alert_state(alert_state)


def main() -> None:
    con = init_db()
    print(f"[{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}] collecting...")
    run_once(con)

    if not MODEL_PATH.exists():
        print("No HMM model found — skipping scoring. Run train_hmm.py first.")
        con.close()
        return

    with open(MODEL_PATH, "rb") as f:
        model = pickle.load(f)

    print("Scoring markets...")
    score_markets(con, model)
    con.close()


if __name__ == "__main__":
    main()

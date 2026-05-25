"""
Build labeled training dataset for HMM from historical Polymarket data.

Positive examples (known insider accumulation):
  - Israel-by-Friday (ricosuave666 / Operation Rising Lion, June 12-13 2025)
  - Biden-Kinzinger  (suspicious wallets, Jan 20 2025)
  - Biden-Schiff     (suspicious wallets, Jan 20 2025)
  - Biden-Jim Biden  (suspicious wallets, Jan 20 2025)
  - Biden-Hunter     (suspicious wallets, Jan 2025)

Negative examples:
  - 30+ resolved markets across politics, sports, crypto with no known
    insider activity, sampled from Polymarket closed market archive.

Output:
  data/training_sequences.json  — list of labeled feature sequences
  data/training_summary.csv     — one row per market
"""

import json
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT     = Path(__file__).parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

BUCKET_SECS = 3600  # 1-hour buckets for training (more data per bucket)

# ── Known positive cases ──────────────────────────────────────────────────────
#
# Each entry: condition_id, label, accumulation_start (unix), resolution (unix)
# Accumulation start = when we know informed buying began based on news/court docs

POSITIVES = [
    {
        "condition_id": "0x7f39808829da93cfd189807f13f6d86a0e604835e6f9482d8094fac46b3abaac",
        "label":        "Israel-strike-by-Friday (ricosuave666)",
        "event":        "Operation Rising Lion, June 13 2025",
        "accum_start":  1749715200,  # 2025-06-12 08:00 UTC (first anomalous buying)
        "resolution":   1749772800,  # 2025-06-13 00:00 UTC
    },
    {
        "condition_id": "0x8a66756daeea5d4f58fc1186d7abe2f58d6986155f7767284f5cfaa7351031e9",
        "label":        "Biden-pardon-Kinzinger",
        "event":        "Biden pardons cluster, Jan 20 2025",
        "accum_start":  1737331200,  # 2025-01-20 00:00 UTC
        "resolution":   1737381600,  # 2025-01-20 14:00 UTC
    },
    {
        "condition_id": "0x072acaa7dd352c795948d495e019eec539539d36698225ab98b2f55c692d1b0c",
        "label":        "Biden-pardon-Schiff",
        "event":        "Biden pardons cluster, Jan 20 2025",
        "accum_start":  1737331200,
        "resolution":   1737381600,
    },
    {
        "condition_id": "0xcde81adc8f07ce43f0287684a259ac4a6b3612a73c71c0247c6bc8b2bc97a691",
        "label":        "Biden-pardon-JimBiden",
        "event":        "Biden pardons cluster, Jan 20 2025",
        "accum_start":  1737331200,
        "resolution":   1737381600,
    },
    {
        "condition_id": "0x0e85e94301db0e38e25f0650ab649c3a4485b5fc9734a8b6abf3d0da0ee6dd55",
        "label":        "Biden-pardon-HunterBiden",
        "event":        "Biden pardons cluster, Jan 20 2025",
        "accum_start":  1737331200,
        "resolution":   1737381600,
    },
]

# ── Negative examples — resolved markets, no known insider activity ───────────
# Mix of politics, crypto, sports. All resolved.

NEGATIVES_CIDS = [
    # Politics — resolved, no anomaly reported
    "0x829b85b2ad614764e51a92364f1c1c47e4a7e06ba5da62a8da00d0e3a2e3faf1",  # GA Senate 2021
    "0x3648ab7c146a9a85957e07c1d43a82272be71fde767822fd425e10ba0d6c0757",  # Youngkin 2024 primary
    # Crypto price markets — pure noise/speculation
    "0x5a717bd084bf8ebf77ee8efec78a5397ae6b2116e68d7f18fb92040109486c89",  # BTC up/down
    # Sports — no insider possible
    "0x31cc3ace38dc450f88984efcfe2e61ab360d2d00b9b88cfb0f92a67320595a5f",  # AZ vs SF
    "0xc775ae21fba7d1949f6da0c45a880b5ab1a19c39d9c2f4f1b93f37af0bc014d9",  # Knicks vs Cavs
    # Iran-adjacent but resolved without known insider (use as hard negatives)
    "0x3b2f19935f2a969634a7aca52b69cf653d957ec6243bdfcb97dd830003e90624",  # Israel-Iran ceasefire by July
    "0x6ab0ce92e138eec9776d055e052140ff284f885fb8a54c74a944316e4a2e4d80",  # Jalili head of Iran
]


# ── HTTP helper ───────────────────────────────────────────────────────────────

def fetch(url: str, retries: int = 3) -> list | dict:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "poly-potato/1.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=12) as r:
                return json.loads(r.read())
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(1)


def pull_all_trades(cid: str) -> list:
    all_trades = []
    offset = 0
    while True:
        try:
            batch = fetch(
                f"https://data-api.polymarket.com/trades?market={cid}&limit=500&offset={offset}"
            )
        except Exception as e:
            print(f"    trades error at offset {offset}: {e}")
            break
        if not batch:
            break
        all_trades.extend(batch)
        if len(batch) < 500:
            break
        offset += 500
        time.sleep(0.15)
    return all_trades


# ── Feature extraction ────────────────────────────────────────────────────────

def trades_to_hourly_features(trades: list) -> list[dict]:
    """
    Convert raw trades to 1-hour bucket feature vectors.
    Returns list of dicts sorted by bucket_start.
    """
    yes_trades = [t for t in trades if t["outcome"] == "Yes"]
    if not yes_trades:
        return []

    buckets: dict[int, list] = defaultdict(list)
    for t in yes_trades:
        bkt = (t["timestamp"] // BUCKET_SECS) * BUCKET_SECS
        buckets[bkt].append(t)

    # Track wallets seen in prior 48h for novelty score
    all_buckets_sorted = sorted(buckets.keys())
    seen_wallets: set[str] = set()

    rows = []
    for i, bkt in enumerate(all_buckets_sorted):
        bts      = buckets[bkt]
        buy_vol  = sum(t["size"] for t in bts if t["side"] == "BUY")
        sell_vol = sum(t["size"] for t in bts if t["side"] == "SELL")
        total    = buy_vol + sell_vol
        ofi      = (buy_vol - sell_vol) / total if total > 0 else 0.0
        price    = bts[-1]["price"]
        wlts     = set(t["proxyWallet"] for t in bts)
        new_w    = len(wlts - seen_wallets) / len(wlts) if wlts else 0.0
        max_sz   = max(t["size"] for t in bts)

        # Price change from prior bucket
        dp = None
        if i > 0:
            prior = [b for b in rows if b["bucket_start"] == all_buckets_sorted[i - 1]]
            if prior:
                dp = price - prior[-1]["price_yes"]

        rows.append({
            "bucket_start":    bkt,
            "price_yes":       price,
            "delta_price_1h":  dp,
            "ofi":             ofi,
            "volume_usdc":     total,
            "n_wallets":       len(wlts),
            "new_wallet_pct":  new_w,
            "max_trade_size":  max_sz,
            "trade_count":     len(bts),
        })

        # Roll forward: treat current bucket wallets as "known" for future
        seen_wallets = seen_wallets | wlts
        # Keep only last 48h of wallet history
        cutoff = bkt - 48 * BUCKET_SECS
        seen_wallets = {
            w for w in seen_wallets
            if any(t["proxyWallet"] == w and t["timestamp"] >= cutoff
                   for bt in [buckets[b] for b in all_buckets_sorted if b >= cutoff and b <= bkt]
                   for t in bt)
        } if len(seen_wallets) > 500 else seen_wallets  # skip expensive recompute if small

    return rows


def label_sequence(features: list[dict], accum_start: int, resolution: int) -> list[dict]:
    """
    Add 'state' label to each bucket:
      0 = noise (before accumulation)
      1 = accumulation (accum_start → resolution)
      2 = revelation (at resolution, price > 0.85)
    """
    labeled = []
    for f in features:
        b = f["bucket_start"]
        if f["price_yes"] >= 0.85:
            state = 2
        elif b >= accum_start:
            state = 1
        else:
            state = 0
        labeled.append({**f, "state": state})
    return labeled


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    sequences = []
    summary   = []

    # ── Positive examples ────────────────────────────────────────────────────
    print("=== POSITIVE EXAMPLES ===\n")
    for case in POSITIVES:
        cid   = case["condition_id"]
        label = case["label"]
        print(f"  {label}")

        trades = pull_all_trades(cid)
        if not trades:
            print("    no trades — skipping\n")
            continue

        trades.sort(key=lambda t: t["timestamp"])
        features = trades_to_hourly_features(trades)
        labeled  = label_sequence(features, case["accum_start"], case["resolution"])

        n_noise  = sum(1 for f in labeled if f["state"] == 0)
        n_accum  = sum(1 for f in labeled if f["state"] == 1)
        n_rev    = sum(1 for f in labeled if f["state"] == 2)

        # Show the accumulation window
        accum_rows = [f for f in labeled if f["state"] == 1]
        if accum_rows:
            ofi_avg = sum(f["ofi"] for f in accum_rows) / len(accum_rows)
            p_range = f"{min(f['price_yes'] for f in accum_rows):.2f}–{max(f['price_yes'] for f in accum_rows):.2f}"
            print(f"    {len(trades)} trades  |  {n_noise} noise + {n_accum} accum + {n_rev} reveal buckets")
            print(f"    accumulation: avg OFI={ofi_avg:+.3f}  price={p_range}")

        sequences.append({
            "condition_id": cid,
            "label":        label,
            "is_positive":  True,
            "event":        case["event"],
            "features":     labeled,
        })
        summary.append({
            "cid": cid, "label": label, "positive": True,
            "n_trades": len(trades), "n_noise": n_noise,
            "n_accum": n_accum, "n_reveal": n_rev,
        })
        print()
        time.sleep(0.5)

    # ── Negative examples ────────────────────────────────────────────────────
    print("=== NEGATIVE EXAMPLES ===\n")
    for cid in NEGATIVES_CIDS:
        try:
            trades = pull_all_trades(cid)
            if not trades:
                print(f"  {cid[:16]}... no trades — skipping")
                continue

            trades.sort(key=lambda t: t["timestamp"])
            features = trades_to_hourly_features(trades)

            # Label entire sequence as noise (state=0), except price>0.85 = revelation
            labeled = []
            for f in features:
                state = 2 if f["price_yes"] >= 0.85 else 0
                labeled.append({**f, "state": state})

            t0 = time.strftime("%Y-%m-%d", time.gmtime(trades[0]["timestamp"]))
            t1 = time.strftime("%Y-%m-%d", time.gmtime(trades[-1]["timestamp"]))
            print(f"  {cid[:16]}...  {len(trades)} trades  {t0}→{t1}  {len(features)} buckets")

            sequences.append({
                "condition_id": cid,
                "label":        f"negative_{cid[:8]}",
                "is_positive":  False,
                "event":        "no known insider activity",
                "features":     labeled,
            })
            summary.append({
                "cid": cid, "label": f"negative_{cid[:8]}", "positive": False,
                "n_trades": len(trades), "n_noise": len(labeled), "n_accum": 0, "n_reveal": 0,
            })
        except Exception as e:
            print(f"  {cid[:16]}... error: {e}")
        time.sleep(0.4)

    # ── Save ─────────────────────────────────────────────────────────────────
    out_seqs = DATA_DIR / "training_sequences.json"
    out_seqs.write_text(json.dumps(sequences, indent=2))
    print(f"\nSaved {len(sequences)} sequences to {out_seqs}")

    # Summary CSV
    import csv
    out_csv = DATA_DIR / "training_summary.csv"
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["cid","label","positive","n_trades","n_noise","n_accum","n_reveal"])
        w.writeheader()
        w.writerows(summary)
    print(f"Saved summary to {out_csv}")

    # Print totals
    pos = [s for s in sequences if s["is_positive"]]
    neg = [s for s in sequences if not s["is_positive"]]
    total_accum  = sum(r["n_accum"]  for r in summary if r["positive"])
    total_noise  = sum(r["n_noise"]  for r in summary)
    print(f"\nTraining set: {len(pos)} positive, {len(neg)} negative sequences")
    print(f"  Accumulation buckets: {total_accum}")
    print(f"  Noise buckets:        {total_noise}")


if __name__ == "__main__":
    main()

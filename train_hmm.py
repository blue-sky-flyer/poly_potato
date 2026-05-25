"""
Train a 3-state Gaussian HMM on labeled Polymarket trade sequences.

States:
  0 = Noise        — normal market activity
  1 = Accumulation — informed buying before event revelation
  2 = Revelation   — price converged to outcome (>0.85)

Features (4-dim per bucket):
  [ofi, log1p(volume_usdc), price_yes, new_wallet_pct]

Usage:
  python train_hmm.py              — train + save model to data/hmm_model.pkl
  python train_hmm.py --validate   — leave-one-out on positive sequences
  python train_hmm.py --score <cid_prefix>  — score a market from live DB
"""

import argparse
import json
import pickle
import sqlite3
import sys
from pathlib import Path

import numpy as np
from hmmlearn.hmm import GaussianHMM
from scipy.special import logsumexp

ROOT       = Path(__file__).parent
DATA_DIR   = ROOT / "data"
MODEL_PATH = DATA_DIR / "hmm_model.pkl"
SEQ_PATH   = DATA_DIR / "training_sequences.json"

NOISE  = 0
ACCUM  = 1
REVEAL = 2
N_STATES = 3
FEATURE_NAMES = ["ofi", "log_volume", "price_yes", "new_wallet_pct"]


# ── Feature extraction ────────────────────────────────────────────────────────

def extract(bucket: dict) -> np.ndarray:
    ofi   = float(np.clip(bucket["ofi"]   if bucket["ofi"]   is not None else 0.0, -1.0, 1.0))
    vol   = float(np.log1p(bucket["volume_usdc"] or 0.0))
    price = float(bucket["price_yes"]       if bucket["price_yes"]       is not None else 0.5)
    new_w = float(bucket["new_wallet_pct"]  if bucket["new_wallet_pct"]  is not None else 0.0)
    return np.array([ofi, vol, price, new_w])


def seq_arrays(seq: dict) -> tuple[np.ndarray, np.ndarray]:
    X      = np.array([extract(f) for f in seq["features"]])
    states = np.array([f["state"] for f in seq["features"]], dtype=int)
    return X, states


# ── Supervised model fitting ──────────────────────────────────────────────────

def fit_supervised(sequences: list[dict]) -> GaussianHMM:
    """
    Compute emission parameters from labeled buckets, transition matrix
    from consecutive state pairs, then build a GaussianHMM without EM.
    """
    per_state: dict[int, list[np.ndarray]] = {0: [], 1: [], 2: []}
    for seq in sequences:
        X, states = seq_arrays(seq)
        for i, s in enumerate(states):
            if s in per_state:
                per_state[s].append(X[i])

    for s, name in [(0, "noise"), (1, "accum"), (2, "reveal")]:
        print(f"  state {s} ({name}): {len(per_state[s])} labeled buckets")

    means  = np.zeros((N_STATES, len(FEATURE_NAMES)))
    covars = np.zeros((N_STATES, len(FEATURE_NAMES)))
    for s in range(N_STATES):
        arr = np.array(per_state[s]) if per_state[s] else np.zeros((1, len(FEATURE_NAMES)))
        means[s]  = arr.mean(axis=0)
        covars[s] = np.maximum(arr.var(axis=0), 1e-4)

    # Estimate transitions from consecutive labeled pairs
    trans_counts = np.ones((N_STATES, N_STATES)) * 0.1  # Laplace smoothing
    for seq in sequences:
        _, states = seq_arrays(seq)
        for t in range(len(states) - 1):
            trans_counts[states[t], states[t + 1]] += 1.0
    trans_counts[REVEAL, ACCUM] = 0.01  # can't de-accumulate after revelation
    transmat = trans_counts / trans_counts.sum(axis=1, keepdims=True)

    model = GaussianHMM(
        n_components=N_STATES,
        covariance_type="diag",
        n_iter=0,
        init_params="",
        params="",
    )
    model.startprob_ = np.array([0.90, 0.05, 0.05])
    model.transmat_  = transmat
    model.means_     = means
    model.covars_    = covars
    model._check()
    return model


# ── Forward filter (no lookahead) ─────────────────────────────────────────────

def forward_filter(model: GaussianHMM, X: np.ndarray) -> np.ndarray:
    """
    P(state_t | obs_1..obs_t) for each t — causal, no future observations used.
    Returns (T, N_STATES) array of filtered probabilities.
    """
    T = len(X)
    K = model.n_components
    log_emit  = model._compute_log_likelihood(X)   # (T, K)
    log_trans = np.log(model.transmat_ + 1e-300)   # (K, K)

    log_alpha = np.full((T, K), -np.inf)
    log_alpha[0] = np.log(model.startprob_ + 1e-300) + log_emit[0]

    for t in range(1, T):
        for k in range(K):
            log_alpha[t, k] = logsumexp(log_alpha[t - 1] + log_trans[:, k]) + log_emit[t, k]

    log_norm = logsumexp(log_alpha, axis=1, keepdims=True)
    return np.exp(log_alpha - log_norm)


# ── Model summary ─────────────────────────────────────────────────────────────

def print_summary(model: GaussianHMM) -> None:
    names = ["noise", "accum", "reveal"]
    print("\n=== Emission means ===")
    header = "  ".join(f"{n:>14}" for n in FEATURE_NAMES)
    print(f"  {'':8} {header}")
    for s, name in enumerate(names):
        vals = "  ".join(f"{v:>14.4f}" for v in model.means_[s])
        print(f"  {name:8} {vals}")

    print("\n=== Emission std-devs ===")
    print(f"  {'':8} {header}")
    for s, name in enumerate(names):
        # hmmlearn 0.3.x stores diag covars as full (f,f) matrices internally
        diag = np.diag(model.covars_[s]) if model.covars_[s].ndim == 2 else model.covars_[s]
        stds = "  ".join(f"{float(v):>14.4f}" for v in np.sqrt(diag))
        print(f"  {name:8} {stds}")

    print("\n=== Transition matrix (row → col) ===")
    col_h = "  ".join(f"{n:>8}" for n in names)
    print(f"  {'':8} {col_h}")
    for s, name in enumerate(names):
        vals = "  ".join(f"{v:>8.4f}" for v in model.transmat_[s])
        print(f"  {name:8} {vals}")


# ── Leave-one-out validation ──────────────────────────────────────────────────

def validate_loo(sequences: list[dict]) -> None:
    positives = [s for s in sequences if s["is_positive"]]
    negatives = [s for s in sequences if not s["is_positive"]]
    print(f"\n=== LOO Validation ({len(positives)} positive sequences) ===\n")

    latencies: list[int] = []
    for i, held_out in enumerate(positives):
        train = [s for j, s in enumerate(positives) if j != i] + negatives
        model = fit_supervised(train)

        X, states = seq_arrays(held_out)
        if len(X) == 0:
            continue

        probs = forward_filter(model, X)
        accum_idx = np.where(states == ACCUM)[0]

        if len(accum_idx) == 0:
            peak = probs[:, ACCUM].max()
            print(f"  {held_out['label'][:45]:<45} no accum label  peak={peak:.3f}")
            continue

        true_start = accum_idx[0]
        p_after = probs[true_start:, ACCUM]
        hits = np.where(p_after > 0.50)[0]

        if len(hits) == 0:
            peak = p_after.max()
            print(f"  {held_out['label'][:45]:<45} NEVER DETECTED  peak={peak:.3f}")
        else:
            lat = hits[0]
            latencies.append(lat)
            p_at = probs[true_start + lat, ACCUM]
            print(f"  {held_out['label'][:45]:<45} +{lat:2d}h  P(accum)={p_at:.3f}")

    if latencies:
        print(f"\n  Median latency: {np.median(latencies):.0f}h")
        print(f"  Mean   latency: {np.mean(latencies):.1f}h")


# ── Live scoring from DB ──────────────────────────────────────────────────────

def score_market(model: GaussianHMM, partial_cid: str) -> None:
    db_path = DATA_DIR / "poly_potato.db"
    if not db_path.exists():
        print("Database not found.")
        return

    con = sqlite3.connect(db_path)
    row = con.execute(
        "SELECT condition_id, label, question FROM markets WHERE condition_id LIKE ?",
        (f"%{partial_cid}%",)
    ).fetchone()
    if not row:
        print(f"No market matching '{partial_cid}'")
        return

    cid, label, question = row
    print(f"\n{question or label}")

    snaps = con.execute("""
        SELECT bucket_start, price_yes, ofi, volume_usdc, new_wallet_pct
        FROM market_snapshots
        WHERE condition_id = ?
        ORDER BY bucket_start ASC
    """, (cid,)).fetchall()
    con.close()

    if not snaps:
        print("No snapshots.")
        return

    buckets = [
        {"bucket_start": r[0], "price_yes": r[1], "ofi": r[2],
         "volume_usdc": r[3], "new_wallet_pct": r[4]}
        for r in snaps
    ]
    X = np.array([extract(b) for b in buckets])
    probs = forward_filter(model, X)

    from datetime import datetime, timezone
    print(f"\n{'Time (UTC)':<18} {'price':>6} {'P(noise)':>9} {'P(accum)':>9} {'P(reveal)':>9}  Signal")
    print("─" * 70)
    for i, b in enumerate(buckets):
        t    = datetime.fromtimestamp(b["bucket_start"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        p    = b["price_yes"]
        pn, pa, pr = probs[i]
        sig  = " *** ACCUM ***" if pa > 0.50 else ""
        p_s  = f"{p:.3f}" if p is not None else "—"
        print(f"{t:<18} {p_s:>6} {pn:>9.3f} {pa:>9.3f} {pr:>9.3f}{sig}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate", action="store_true", help="Run LOO validation")
    parser.add_argument("--no-save",  action="store_true", help="Skip saving model")
    parser.add_argument("--score",    metavar="CID",       help="Score a live market by partial condition_id")
    args = parser.parse_args()

    if args.score:
        if not MODEL_PATH.exists():
            print("Model not found — run train_hmm.py first.")
            sys.exit(1)
        with open(MODEL_PATH, "rb") as f:
            model = pickle.load(f)
        score_market(model, args.score)
        return

    if not SEQ_PATH.exists():
        print(f"Training data not found at {SEQ_PATH}. Run build_training_data.py first.")
        sys.exit(1)

    sequences = [s for s in json.loads(SEQ_PATH.read_text()) if s["features"]]
    print(f"Loaded {len(sequences)} sequences  "
          f"({sum(s['is_positive'] for s in sequences)} positive, "
          f"{sum(not s['is_positive'] for s in sequences)} negative)")

    print("\nFitting supervised HMM...")
    model = fit_supervised(sequences)
    print_summary(model)

    if not args.no_save:
        with open(MODEL_PATH, "wb") as f:
            pickle.dump(model, f)
        print(f"\nModel saved → {MODEL_PATH}")

    if args.validate:
        validate_loo(sequences)


if __name__ == "__main__":
    main()

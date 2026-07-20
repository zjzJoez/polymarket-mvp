"""Strategy ζ shadow harness — smart-money copy collector + progress report.

Collect mode (run by smartmoney-shadow.timer every 2 minutes):
    .venv/bin/python scripts/run_smartmoney_shadow.py --mode=collect

Report mode (run manually any time to see accumulation + gate progress):
    .venv/bin/python scripts/run_smartmoney_shadow.py --mode=report

Shadow-only: never places orders. Pre-registered kill gates live in
services/smartmoney_signal.py — G1 latency median <5min, G2 copy EV >0.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List

from polymarket_mvp.common import load_repo_env
from polymarket_mvp.db import connect_db, init_db
from polymarket_mvp.services.smartmoney_signal import SIGNAL_NAME, collect_once

load_repo_env()


def run_collect() -> int:
    init_db()
    with connect_db() as conn:
        stats = collect_once(conn)
    print(
        f"[smartmoney-shadow] wallets={stats['wallets']} trades_seen={stats['trades_seen']} "
        f"signals={stats['signals']} dedup_skips={stats['dedup_skips']} errors={stats['errors']}"
    )
    return 0


def _percentile(sorted_vals: List[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    idx = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def run_report() -> int:
    init_db()
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT recommendation, model_p, market_p, edge, payload_json, generated_at
            FROM signal_events WHERE signal_name = ? ORDER BY generated_at
            """,
            (SIGNAL_NAME,),
        ).fetchall()

    n = len(rows)
    print(f"=== Strategy ζ shadow report — {n} signals ===")
    if not n:
        print("No signals yet. Collector may need more runtime (or top wallets are quiet).")
        return 0

    payloads: List[Dict[str, Any]] = []
    for r in rows:
        try:
            payloads.append(json.loads(r["payload_json"]))
        except Exception:
            payloads.append({})

    first, last = rows[0]["generated_at"], rows[-1]["generated_at"]
    print(f"window: {first} → {last}")

    bets = [r for r in rows if r["recommendation"] == "bet"]
    print(f"recommendation=bet: {len(bets)}   no_match: {n - len(bets)}")

    latencies = sorted(
        float(p.get("latency_seconds")) for p in payloads
        if p.get("latency_seconds") is not None
    )
    if latencies:
        med = _percentile(latencies, 0.5)
        p90 = _percentile(latencies, 0.9)
        gate = "PASS" if med < 300 else "FAIL"
        print(f"\n[G1 latency] median={med:.0f}s p90={p90:.0f}s n={len(latencies)}  → {gate} (gate: median <300s)")

    slips = sorted(
        float(r["edge"]) for r in rows
        if r["edge"] is not None and r["recommendation"] == "bet"
    )
    if slips:
        med_slip = _percentile(slips, 0.5)
        print(f"[slippage] trader_price - our_ask: median={med_slip:+.4f} "
              f"p10={_percentile(slips, 0.1):+.4f} p90={_percentile(slips, 0.9):+.4f}")
        print("           (negative = we pay more than the smart wallet did)")

    by_wallet: Dict[str, int] = {}
    for p in payloads:
        w = (p.get("wallet") or "?")[:10]
        by_wallet[w] = by_wallet.get(w, 0) + 1
    top = sorted(by_wallet.items(), key=lambda kv: -kv[1])[:10]
    print("\nsignals by wallet (top 10):")
    for w, c in top:
        print(f"  {w}  {c}")

    print("\n[G2 copy EV] needs resolved markets — run the Day-7/Day-21 evaluator "
          "(scripts/evaluate_smartmoney_shadow.py, built at first review).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["collect", "report"], default="collect")
    args = parser.parse_args()
    return run_collect() if args.mode == "collect" else run_report()


if __name__ == "__main__":
    sys.exit(main())

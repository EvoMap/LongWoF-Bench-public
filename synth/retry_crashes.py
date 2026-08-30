#!/usr/bin/env python3
"""Retry only verdict=crash entries from a solution-first summary JSON.

Patches matching records in place so prior pass/reject rows are preserved.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from run_solution_first import FAMILIES, load_seeds, make_client, run_one  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Retry crash rows in a summary_sf*.json")
    ap.add_argument("--family", required=True, choices=FAMILIES)
    ap.add_argument("--summary", required=True, help="Summary JSON to read and patch")
    ap.add_argument("--model", default="gemini-3.1-pro-preview")
    ap.add_argument("--min-chain-depth", type=int, default=3)
    ap.add_argument("--min-hidden-conventions", type=int, default=2)
    ap.add_argument("--min-deliverables", type=int, default=2)
    ap.add_argument("--min-ref-helpers", type=int, default=3)
    ap.add_argument("--max-retries", type=int, default=3)
    ap.add_argument("--fixture-timeout-s", type=int, default=120)
    args = ap.parse_args()

    summary_path = Path(args.summary)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    records = summary["results"][args.family]
    crashes = [r for r in records if r.get("verdict") == "crash"]
    if not crashes:
        print(f"[{args.family}] no crash rows in {summary_path}")
        return 0

    run_args = SimpleNamespace(
        model=args.model,
        min_chain_depth=args.min_chain_depth,
        min_hidden_conventions=args.min_hidden_conventions,
        min_deliverables=args.min_deliverables,
        min_ref_helpers=args.min_ref_helpers,
        max_retries=args.max_retries,
        fixture_timeout_s=args.fixture_timeout_s,
    )

    seeds = load_seeds(args.family)
    client = make_client()
    by_id = {r["candidate_id"]: i for i, r in enumerate(records)}

    print(f"[{args.family}] retrying {len(crashes)} crash rows from {summary_path}")
    for j, old in enumerate(crashes, start=1):
        idx = int(old["candidate_id"][1:5])
        domain, idea = seeds[idx - 1]
        print(f"  [{j}/{len(crashes)}] {old['candidate_id']} {domain!r} | {idea[:70]!r}")
        t0 = time.time()
        new = run_one(client, args.family, domain, idea, idx, run_args)
        new["elapsed_s"] = round(time.time() - t0, 1)
        records[by_id[old["candidate_id"]]] = new
        summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        print(f"      -> {new['verdict']} stages={len(new.get('stages_passed') or [])} "
              f"{new.get('gate') or ''} ({new['elapsed_s']}s)")

    passed = sum(1 for r in records if r.get("verdict") == "pass_pre_calibration")
    still_crash = sum(1 for r in records if r.get("verdict") == "crash")
    print(f"\n[{args.family}] done: {passed}/{len(records)} pass_pre_calibration, "
          f"{still_crash} still crash")
    print(f"Summary patched -> {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

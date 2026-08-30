#!/usr/bin/env python3
"""Retroactive structured audit for agent_env_synth host pytest.

Walks _sample_pilot/candidates/agent_env_synth/A*/ and for each candidate:
  1. runs reference_solution.py (cwd=cand_dir)
  2. runs `pytest test_script.py` (cwd=cand_dir)
  3. emits the same `stage2_host_pytest` record run_sample.py would have
     produced if it had been part of the run from the start
  4. appends the record to that candidate's `_trace.json` (idempotent —
     overwrites any existing stage2_host_pytest entry)
  5. updates summary.json's `results.agent_env_synth[*].stages` with the
     new stage record

Run after creating new env candidates without Docker available.

Usage:
    python audit_env_host_pytest.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run_sample import stage2_host_pytest_env  # type: ignore


def main() -> int:
    cand_root = HERE / "candidates" / "agent_env_synth"
    summary_path = HERE / "summary.json"

    if not cand_root.exists():
        print(f"ERROR: {cand_root} does not exist", file=sys.stderr)
        return 1
    if not summary_path.exists():
        print(f"ERROR: {summary_path} does not exist", file=sys.stderr)
        return 1

    summary = json.loads(summary_path.read_text())
    env_records = summary.get("results", {}).get("agent_env_synth", [])
    if not env_records:
        print("ERROR: no agent_env_synth records in summary.json", file=sys.stderr)
        return 1

    by_id = {r["scenario_id"]: r for r in env_records}

    print(f"{'id':<6}  {'ref_rc':>6}  {'pyt_rc':>6}  {'passed':>6}  {'failed':>6}  ok")
    print("-" * 50)
    n_ok = 0
    for cand_dir in sorted(cand_root.iterdir()):
        if not cand_dir.is_dir():
            continue
        cid = cand_dir.name
        rec = stage2_host_pytest_env(cand_dir)
        is_ok = bool(rec.get("ok"))
        if is_ok:
            n_ok += 1
        print(f"{cid:<6}  {str(rec.get('ref_rc')):>6}  {str(rec.get('pytest_rc')):>6}  "
              f"{str(rec.get('pytest_passed')):>6}  {str(rec.get('pytest_failed')):>6}  "
              f"{'PASS' if is_ok else 'FAIL'}")

        trace_path = cand_dir / "_trace.json"
        if trace_path.exists():
            try:
                trace = json.loads(trace_path.read_text())
            except json.JSONDecodeError:
                trace = {"scenario_id": cid, "stages": []}
        else:
            trace = {"scenario_id": cid, "stages": []}
        trace.setdefault("stages", [])
        trace["stages"] = [s for s in trace["stages"]
                           if s.get("stage") != "stage2_host_pytest"]
        trace["stages"].append(rec)
        trace_path.write_text(json.dumps(trace, indent=2, default=str))

        if cid in by_id:
            by_id[cid].setdefault("stages", [])
            by_id[cid]["stages"] = [s for s in by_id[cid]["stages"]
                                    if s.get("stage") != "stage2_host_pytest"]
            by_id[cid]["stages"].append(rec)
            by_id[cid]["verdict"] = "pass" if is_ok else "stage2_host_pytest_fail"

    summary.setdefault("audits", {})["agent_env_synth_host_pytest"] = {
        "ran_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
        "n_total": len(env_records),
        "n_ok": n_ok,
        "note": "in-process pytest (no Docker); safe for synth-time only, "
                "not eval-time of untrusted candidates",
    }
    summary_path.write_text(json.dumps(summary, indent=2, default=str))

    print(f"\n{n_ok}/{len(env_records)} agent_env_synth candidates pass host pytest.")
    print(f"Traces and summary.json updated.")
    return 0 if n_ok == len(env_records) else 1


if __name__ == "__main__":
    sys.exit(main())

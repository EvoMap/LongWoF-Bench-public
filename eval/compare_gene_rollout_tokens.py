#!/usr/bin/env python3
"""Compare evolve-stage rollout tokens against one-shot with_gene evaluation.

This script expects traces written by ``evolve_genes_v3.py`` under
``<genes_dir>/_traces``. It selects tasks solved by the Gene-generation evolve
stage within N rollouts, then compares those generation-side tokens with the
later official ``model::with_gene::<task_id>`` one-shot run.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
V3_ROOT = HERE.parent
POOL_ROOT = V3_ROOT / "tasks_final"
DEFAULT_TRACES_DIR = POOL_ROOT / "genes_evolved" / "_traces"
DEFAULT_OFFICIAL_RUN_DIR = V3_ROOT / "_runs" / "v3_final_common778"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _trial_key(row: dict[str, Any]) -> str:
    key = row.get("trial_key")
    if isinstance(key, str) and key:
        return key
    trial = row.get("trial")
    if isinstance(trial, dict) and isinstance(trial.get("trial_key"), str):
        return str(trial["trial_key"])
    return ""


def _tokens(row: dict[str, Any]) -> dict[str, int]:
    input_tokens = int(row.get("input_tokens") or row.get("input") or 0)
    output_tokens = int(row.get("output_tokens") or row.get("output") or 0)
    thoughts_tokens = int(row.get("thoughts_tokens") or row.get("thoughts") or 0)
    return {
        "input": input_tokens,
        "output": output_tokens,
        "thoughts": thoughts_tokens,
        "completion_plus_thoughts": output_tokens + thoughts_tokens,
        "total": input_tokens + output_tokens + thoughts_tokens,
    }


def _add_tokens(dst: dict[str, int], src: dict[str, int]) -> None:
    for key in ("input", "output", "thoughts", "completion_plus_thoughts", "total"):
        dst[key] += int(src.get(key) or 0)


def _blank_tokens() -> dict[str, int]:
    return {"input": 0, "output": 0, "thoughts": 0, "completion_plus_thoughts": 0, "total": 0}


def _rollout_number(row: dict[str, Any]) -> int:
    if row.get("rollout") is not None:
        return int(row.get("rollout") or 0)
    return int(row.get("step") or 0) + 1


def _trace_rollout_stats(trace: dict[str, Any], max_rollouts: int) -> dict[str, Any] | None:
    evolve = trace.get("evolve")
    if not isinstance(evolve, dict):
        return None
    solved = bool(evolve.get("solved"))
    n_iters = int(evolve.get("n_iters") or 0)
    if not solved or n_iters <= 0 or n_iters > max_rollouts:
        return None

    raw_rollouts = evolve.get("rollouts")
    if not isinstance(raw_rollouts, list):
        raw_rollouts = evolve.get("calls") if isinstance(evolve.get("calls"), list) else []

    totals = _blank_tokens()
    rollout_count = 0
    api_calls = 0
    seed_rollouts = 0
    for item in raw_rollouts:
        if not isinstance(item, dict):
            continue
        if _rollout_number(item) > n_iters:
            continue
        rollout_count += 1
        if item.get("api_call", True):
            api_calls += 1
            _add_tokens(totals, _tokens(item))
        else:
            seed_rollouts += 1

    return {
        "task_id": str(trace.get("task_id")),
        "family": str(trace.get("family") or "unknown"),
        "n_iters": n_iters,
        "rollouts": rollout_count,
        "api_calls": api_calls,
        "seed_rollouts": seed_rollouts,
        "tokens": totals,
    }


def _load_trace_stats(traces_dir: Path, max_rollouts: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(traces_dir.glob("*.json")):
        try:
            trace = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(trace, dict):
            continue
        stats = _trace_rollout_stats(trace, max_rollouts)
        if stats is not None:
            stats["trace_file"] = str(path)
            out.append(stats)
    return out


def _official_maps(
    budget_path: Path,
    results_path: Path,
    model: str,
    condition: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, bool]]:
    prefix = f"{model}::{condition}::"
    budget_by_task: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(budget_path):
        key = _trial_key(row)
        if key.startswith(prefix):
            budget_by_task[key[len(prefix):]] = row

    passed_by_task: dict[str, bool] = {}
    for row in _read_jsonl(results_path):
        key = _trial_key(row)
        if not key.startswith(prefix):
            continue
        ev = row.get("eval")
        passed_by_task[key[len(prefix):]] = bool(isinstance(ev, dict) and ev.get("passed"))
    return budget_by_task, passed_by_task


def _fmt(n: int | float) -> str:
    if isinstance(n, float):
        return f"{n:,.1f}"
    return f"{n:,}"


def _summary_line(
    label: str,
    tasks: int,
    calls: int,
    tokens: dict[str, int],
    passed: int | None = None,
) -> str:
    pass_text = "" if passed is None else f" | {_fmt(passed)}"
    return (
        f"| {label} | {_fmt(tasks)} | {_fmt(calls)} | {_fmt(tokens['input'])} | "
        f"{_fmt(tokens['output'])} | {_fmt(tokens['thoughts'])} | "
        f"{_fmt(tokens['completion_plus_thoughts'])} | {_fmt(tokens['total'])}{pass_text} |"
    )


def _render_report(
    *,
    selected: list[dict[str, Any]],
    budget_by_task: dict[str, dict[str, Any]],
    passed_by_task: dict[str, bool],
    model: str,
    condition: str,
    max_rollouts: int,
    traces_dir: Path,
    budget_path: Path,
    results_path: Path,
) -> str:
    gene_tokens = _blank_tokens()
    gene_calls = 0
    gene_rollouts = 0
    seed_rollouts = 0
    for row in selected:
        _add_tokens(gene_tokens, row["tokens"])
        gene_calls += int(row["api_calls"])
        gene_rollouts += int(row["rollouts"])
        seed_rollouts += int(row["seed_rollouts"])

    official_tokens = _blank_tokens()
    official_calls = 0
    official_passed = 0
    missing_budget: list[str] = []
    missing_result: list[str] = []
    both_correct: list[dict[str, Any]] = []
    for row in selected:
        task_id = row["task_id"]
        budget = budget_by_task.get(task_id)
        if budget is None:
            missing_budget.append(task_id)
        else:
            official_calls += 1
            _add_tokens(official_tokens, _tokens(budget))
        if task_id not in passed_by_task:
            missing_result.append(task_id)
        elif passed_by_task[task_id]:
            official_passed += 1
            both_correct.append(row)

    both_gene_tokens = _blank_tokens()
    both_official_tokens = _blank_tokens()
    both_official_calls = 0
    both_gene_calls = 0
    for row in both_correct:
        _add_tokens(both_gene_tokens, row["tokens"])
        both_gene_calls += int(row["api_calls"])
        budget = budget_by_task.get(row["task_id"])
        if budget is not None:
            both_official_calls += 1
            _add_tokens(both_official_tokens, _tokens(budget))

    family_counts = Counter(str(row["family"]) for row in selected)
    iter_counts = Counter(int(row["n_iters"]) for row in selected)

    lines = [
        f"# Gene Rollout Token Comparison ({model}, {condition})",
        "",
        f"- traces: `{traces_dir}`",
        f"- official budget: `{budget_path}`",
        f"- official results: `{results_path}`",
        f"- selected: tasks solved by Gene generation within `{max_rollouts}` rollout(s)",
        "",
        "## Main Comparison",
        "",
        "| Slice | Tasks | Calls | Input | Output | Thoughts | Completion+Thoughts | Total | Passed |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        _summary_line(
            f"Gene generation <= {max_rollouts} rollouts",
            len(selected),
            gene_calls,
            gene_tokens,
            passed=len(selected),
        ),
        _summary_line(
            f"{model} + generated Gene one rollout",
            official_calls,
            official_calls,
            official_tokens,
            passed=official_passed,
        ),
        "",
        "## Both-Correct Subset",
        "",
        "| Slice | Tasks | Calls | Input | Output | Thoughts | Completion+Thoughts | Total | Passed |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        _summary_line(
            f"Gene generation <= {max_rollouts} rollouts",
            len(both_correct),
            both_gene_calls,
            both_gene_tokens,
            passed=len(both_correct),
        ),
        _summary_line(
            f"{model} + generated Gene one rollout",
            both_official_calls,
            both_official_calls,
            both_official_tokens,
            passed=len(both_correct),
        ),
        "",
        "## Selection Details",
        "",
        f"- selected tasks: {_fmt(len(selected))}",
        f"- generation rollouts evaluated: {_fmt(gene_rollouts)}",
        f"- generation API calls counted: {_fmt(gene_calls)}",
        f"- seed rollouts without fresh API tokens: {_fmt(seed_rollouts)}",
        f"- missing official budget rows: {_fmt(len(missing_budget))}",
        f"- missing official result rows: {_fmt(len(missing_result))}",
        "",
        "### n_iters Distribution",
        "",
        "| n_iters | Tasks |",
        "| ---: | ---: |",
    ]
    for n_iters in sorted(iter_counts):
        lines.append(f"| {n_iters} | {_fmt(iter_counts[n_iters])} |")

    lines.extend(["", "### Family Distribution", "", "| Family | Tasks |", "| --- | ---: |"])
    for family, count in sorted(family_counts.items()):
        lines.append(f"| {family} | {_fmt(count)} |")

    if seed_rollouts:
        lines.extend([
            "",
            "Note: seed rollouts come from cached no-context responses and do not have",
            "fresh API token usage inside the trace, so generation-side totals exclude",
            "that cached seed-call cost unless the pipeline was rerun without cache seeds.",
        ])
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--traces-dir", default=str(DEFAULT_TRACES_DIR))
    p.add_argument("--official-run-dir", default=str(DEFAULT_OFFICIAL_RUN_DIR))
    p.add_argument("--budget", default="", help="defaults to <official-run-dir>/budget.jsonl")
    p.add_argument("--results", default="", help="defaults to <official-run-dir>/results.jsonl")
    p.add_argument("--model", default="gemini_pro")
    p.add_argument("--condition", default="with_gene")
    p.add_argument("--max-rollouts", type=int, default=3)
    p.add_argument("--out-md", default="", help="optional markdown report path")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    traces_dir = Path(args.traces_dir).resolve()
    official_run_dir = Path(args.official_run_dir).resolve()
    budget_path = Path(args.budget).resolve() if args.budget else official_run_dir / "budget.jsonl"
    results_path = Path(args.results).resolve() if args.results else official_run_dir / "results.jsonl"

    selected = _load_trace_stats(traces_dir, args.max_rollouts)
    if not selected:
        print(f"no solved <= {args.max_rollouts} rollout traces found under {traces_dir}", file=sys.stderr)
        return 1

    budget_by_task, passed_by_task = _official_maps(
        budget_path,
        results_path,
        model=args.model,
        condition=args.condition,
    )
    report = _render_report(
        selected=selected,
        budget_by_task=budget_by_task,
        passed_by_task=passed_by_task,
        model=args.model,
        condition=args.condition,
        max_rollouts=args.max_rollouts,
        traces_dir=traces_dir,
        budget_path=budget_path,
        results_path=results_path,
    )

    if args.out_md:
        out_path = Path(args.out_md).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        print(f"wrote {out_path}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

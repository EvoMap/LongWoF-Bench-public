#!/usr/bin/env python3
"""Merge a separately recorded supplement into a historical run evidence set.

The original archive remains immutable under ``source/``.  JSONL artifacts are
merged by trial key; a conflicting duplicate is rejected.  The resulting
``config.json`` records both source configurations and content digests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


JSONL_NAMES = ("results.jsonl", "cases.jsonl", "budget.jsonl")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"JSON object required: {path}")
    return payload


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"invalid JSONL {path}:{line_number}: {exc}") from exc
        if not isinstance(row, dict):
            raise SystemExit(f"JSON object required at {path}:{line_number}")
        rows.append(row)
    return rows


def trial_key(row: dict[str, Any]) -> str:
    trial = row.get("trial")
    value = trial.get("trial_key") if isinstance(trial, dict) else row.get("trial_key")
    if not isinstance(value, str) or not value:
        raise SystemExit("every merged JSONL row must have a trial_key")
    return value


def canonical(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def write_jsonl_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    temp = path.with_name(f".{path.name}.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def merge_jsonl(base_path: Path, supplement_path: Path) -> dict[str, Any]:
    base = read_jsonl(base_path)
    supplement = read_jsonl(supplement_path)
    by_key: dict[str, str] = {}
    for row in base:
        key = trial_key(row)
        if key in by_key:
            raise SystemExit(f"duplicate base trial key in {base_path}: {key}")
        by_key[key] = canonical(row)

    appended: list[dict[str, Any]] = []
    for row in supplement:
        key = trial_key(row)
        encoded = canonical(row)
        if key in by_key:
            if by_key[key] != encoded:
                raise SystemExit(f"conflicting trial key while merging {base_path}: {key}")
            continue
        by_key[key] = encoded
        appended.append(row)

    if appended:
        write_jsonl_atomic(base_path, [*base, *appended])
    return {
        "base_rows_before": len(base),
        "supplement_rows": len(supplement),
        "appended_rows": len(appended),
        "merged_rows": len(base) + len(appended),
    }


def archive_inventory(path: Path) -> dict[str, Any]:
    members: list[dict[str, Any]] = []
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            stream = archive.extractfile(member)
            if stream is None:
                raise SystemExit(f"cannot read archive member: {member.name}")
            data = stream.read()
            members.append(
                {"path": member.name, "size": len(data), "sha256": sha256_bytes(data)}
            )
    return {
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "members": members,
    }


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--supplement-dir", required=True, type=Path)
    parser.add_argument("--source-archive", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_dir = args.run_dir.resolve()
    supplement_dir = args.supplement_dir.resolve()
    source_archive = args.source_archive.resolve()
    source_dir = run_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)

    original_config = source_dir / "config.original-777.json"
    original_summary = source_dir / "summary.original-777.md"
    if not original_config.exists():
        shutil.copy2(run_dir / "config.json", original_config)
    if not original_summary.exists():
        shutil.copy2(run_dir / "summary.md", original_summary)

    original = read_json(original_config)
    supplement = read_json(supplement_dir / "config.json")
    prior_merged = run_dir / "config.json"
    prior = read_json(prior_merged) if prior_merged.exists() else {}
    merged_at = prior.get("merged_at") or datetime.now(timezone.utc).isoformat()

    reports = {
        name: merge_jsonl(run_dir / name, supplement_dir / name)
        for name in JSONL_NAMES
    }
    results = read_jsonl(run_dir / "results.jsonl")
    keys = [trial_key(row) for row in results]
    if len(keys) != len(set(keys)):
        raise SystemExit("merged results contain duplicate trial keys")

    trials = [row.get("trial") or {} for row in results]
    task_ids = sorted({str(row.get("task_id")) for row in trials})
    models = sorted({str(row.get("model")) for row in trials})
    conditions = sorted({str(row.get("condition")) for row in trials})
    supplement_results = read_jsonl(supplement_dir / "results.jsonl")
    supplement_keys = sorted(trial_key(row) for row in supplement_results)

    artifact_hashes = {
        name: {
            "size": (run_dir / name).stat().st_size,
            "sha256": sha256_file(run_dir / name),
        }
        for name in JSONL_NAMES
    }
    supplement_hashes = {
        path.name: {"size": path.stat().st_size, "sha256": sha256_file(path)}
        for path in sorted(supplement_dir.iterdir())
        if path.is_file()
    }

    payload = {
        "schema_version": "taskgenome.merged-run-evidence.v1",
        "run_id": run_dir.name,
        "prompt_protocol": "legacy-v1",
        "scoring_protocol": "legacy-v1",
        "runtime_protocol": "legacy-v1",
        "manifest_sha256": supplement.get("manifest_sha256"),
        "models": models,
        "model_registry": supplement.get("model_registry", {}),
        "conditions": conditions,
        "selected_task_ids": task_ids,
        "n_tasks_selected": len(task_ids),
        "n_trials_total": len(results),
        "n_trials_pending": 0,
        "default_skip_ids": original.get("default_skip_ids", []),
        "effective_skip_ids": [],
        "merged_at": merged_at,
        "aggregation_policy": (
            "Union original and supplement records by trial_key; reject conflicting "
            "duplicates; preserve source records byte-for-byte at the JSON value level."
        ),
        "original_run": {
            "run_id": (original.get("args") or {}).get("run_id"),
            "started_at": original.get("started_at"),
            "n_tasks_selected": original.get("n_tasks_selected"),
            "n_trials_total": original.get("n_trials_total"),
            "effective_skip_ids": original.get("effective_skip_ids", []),
            "config_path": str(original_config.relative_to(run_dir)),
            "config_sha256": sha256_file(original_config),
            "summary_path": str(original_summary.relative_to(run_dir)),
            "summary_sha256": sha256_file(original_summary),
            "archive": archive_inventory(source_archive),
        },
        "supplement": {
            "run_id": (supplement.get("args") or {}).get("run_id"),
            "started_at": supplement.get("started_at"),
            "path": str(supplement_dir.relative_to(run_dir)),
            "task_ids": sorted(
                {str((row.get("trial") or {}).get("task_id")) for row in supplement_results}
            ),
            "trial_keys": supplement_keys,
            "records": len(supplement_results),
            "passed": sum(bool((row.get("eval") or {}).get("passed")) for row in supplement_results),
            "cost_usd": sum(float(row.get("cost_usd") or 0.0) for row in supplement_results),
            "run_fingerprint": (supplement.get("run_fingerprint") or {}).get("digest"),
            "source_digest": (supplement.get("reproducibility") or {}).get("source_digest"),
            "asset_digest": (supplement.get("reproducibility") or {}).get("asset_digest"),
            "artifacts": supplement_hashes,
        },
        "merge_reports": reports,
        "merged_artifacts": artifact_hashes,
        "original_run_config": original,
    }
    write_json_atomic(run_dir / "config.json", payload)
    print(json.dumps({
        "run_dir": str(run_dir),
        "tasks": len(task_ids),
        "trials": len(results),
        "models": models,
        "conditions": conditions,
        "merge_reports": reports,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

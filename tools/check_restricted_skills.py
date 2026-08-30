#!/usr/bin/env python3
"""Check LongWoF-Bench restricted Skill boundaries without downloading anything.

This helper deliberately has no network, archive extraction, credential, or
copying code.  It has two read-only modes:

* ``--public-bundle`` verifies that excluded third-party Skill directories are
  absent from a public data bundle.
* ``--skills-root`` verifies that a user-managed, already-authorized local
  directory contains the Skill directories needed by one task or by all
  restricted tasks.

The presence check is not an authorization decision.  Users are responsible
for obtaining and using any local copy under their own agreement with the
rightsholder.  LongWoF-Bench does not provide those copies or a downloader.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAPPING = ROOT / "release" / "restricted_skills.v1.json"
TASK_ID_RE = re.compile(r"^T\d{4}$")


def _load_mapping(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read mapping {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
        raise SystemExit(f"invalid restricted Skill mapping: {path}")
    return payload


def _selected_tasks(mapping: dict[str, Any], task_id: str | None) -> list[dict[str, Any]]:
    tasks = [task for task in mapping["tasks"] if isinstance(task, dict)]
    if task_id is None:
        return tasks
    if not TASK_ID_RE.fullmatch(task_id):
        raise SystemExit(f"invalid task id: {task_id}")
    selected = [task for task in tasks if task.get("task_id") == task_id]
    if not selected:
        raise SystemExit(f"task is not in the restricted Skill mapping: {task_id}")
    return selected


def _restricted_paths(tasks: list[dict[str, Any]], prefix: Path) -> list[tuple[str, Path]]:
    paths: list[tuple[str, Path]] = []
    for task in tasks:
        task_id = str(task["task_id"])
        for skill_dir in task.get("restricted_skill_dirs", []):
            if not isinstance(skill_dir, str) or not skill_dir:
                raise SystemExit(f"invalid restricted Skill directory for {task_id}")
            paths.append((task_id, prefix / task_id / "skill" / skill_dir))
    return paths


def _public_bundle_check(
    bundle_root: Path, tasks: list[dict[str, Any]]
) -> tuple[bool, dict[str, Any]]:
    checks = []
    violations = []
    for task_id, path in _restricted_paths(tasks, bundle_root / "runtime"):
        present = path.exists()
        record = {
            "task_id": task_id,
            "path": path.relative_to(bundle_root).as_posix(),
            "present": present,
        }
        checks.append(record)
        if present:
            violations.append(record)
    return not violations, {
        "mode": "public_bundle",
        "bundle_root": str(bundle_root),
        "checked": len(checks),
        "violations": violations,
        "checks": checks,
    }


def _find_local_path(root: Path, task_id: str, skill_dir: str) -> Path:
    candidates = (
        root / task_id / "skill" / skill_dir,
        root / "scenarios" / task_id / "skill" / skill_dir,
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def _local_check(
    skills_root: Path, tasks: list[dict[str, Any]]
) -> tuple[bool, dict[str, Any]]:
    checks = []
    missing = []
    for task in tasks:
        task_id = str(task["task_id"])
        for skill_dir in task.get("restricted_skill_dirs", []):
            path = _find_local_path(skills_root, task_id, str(skill_dir))
            present = path.is_dir()
            record = {
                "task_id": task_id,
                "skill_dir": skill_dir,
                "path": str(path),
                "present": present,
                "authorization_required": True,
            }
            checks.append(record)
            if not present:
                missing.append(record)
    return not missing, {
        "mode": "authorized_local_presence",
        "skills_root": str(skills_root),
        "checked": len(checks),
        "missing": missing,
        "checks": checks,
        "note": "Presence is not authorization; no network or file copying was performed.",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--task-id", default=None)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument(
        "--public-bundle",
        type=Path,
        help="read-only check: restricted package directories must be absent",
    )
    modes.add_argument(
        "--skills-root",
        type=Path,
        help="read-only check of a user-managed authorized local tree",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    mapping = _load_mapping(args.mapping.resolve())
    tasks = _selected_tasks(mapping, args.task_id)
    if args.public_bundle is not None:
        ok, result = _public_bundle_check(args.public_bundle.resolve(), tasks)
    else:
        ok, result = _local_check(args.skills_root.resolve(), tasks)
    result["status"] = "passed" if ok else "failed"
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

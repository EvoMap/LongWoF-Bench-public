#!/usr/bin/env python3
"""Promote sanitized skills to canonical SKILL.md inside tasks_final/scenarios.

Before (808 tasks):
  scenarios/<id>/SKILL.md            # oracle skill (leaky upper bound)
  scenarios/<id>/SKILL_sanitized.md  # fair rewritten skill

After:
  scenarios/<id>/SKILL.md            # sanitized (canonical for eval)
  scenarios/<id>/SKILL_oracle.md     # archived oracle (optional delete later)

The external archive ``skills_rewritten_full/<id>.md`` is left untouched.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


HERE = Path(__file__).resolve().parent
V3_ROOT = HERE.parent
DEFAULT_POOL = V3_ROOT / "tasks_final"


def classify_task(task_dir: Path) -> str:
    oracle = task_dir / "SKILL.md"
    sanitized = task_dir / "SKILL_sanitized.md"
    archived = task_dir / "SKILL_oracle.md"

    if not sanitized.exists():
        if oracle.exists() and archived.exists() and not sanitized.exists():
            return "skip_already_promoted"
        return "skip_no_sanitized"
    if not oracle.exists():
        return "promote_only"
    if archived.exists() and oracle.read_text() == sanitized.read_text():
        return "skip_already_promoted"
    return "needs_promotion"


def promote_one(task_dir: Path, *, delete_oracle: bool) -> str:
    status = classify_task(task_dir)
    if status != "needs_promotion" and status != "promote_only":
        return status

    oracle = task_dir / "SKILL.md"
    sanitized = task_dir / "SKILL_sanitized.md"
    archived = task_dir / "SKILL_oracle.md"

    if status == "promote_only":
        sanitized.rename(oracle)
        return "promoted_only"

    if delete_oracle:
        oracle.unlink()
        sanitized.rename(oracle)
        return "promoted_deleted_oracle"

    if archived.exists():
        archived.unlink()
    oracle.rename(archived)
    sanitized.rename(oracle)
    return "promoted_archived_oracle"


def patch_manifest(manifest_path: Path) -> None:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    for row in payload.get("tasks") or []:
        files = row.setdefault("files", {})
        files["skill"] = "SKILL.md"
        files["skill_oracle"] = "SKILL_oracle.md"
    payload["skill_promotion"] = {
        "promoted_at": datetime.now(timezone.utc).isoformat(),
        "note": "SKILL.md is sanitized/fair; SKILL_oracle.md is the archived synthesis oracle.",
    }
    manifest_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def patch_derived_manifest(path: Path) -> None:
    if not path.exists():
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    for row in payload.get("tasks") or []:
        files = row.setdefault("files", {})
        files["skill"] = "SKILL.md"
    payload["skill_promotion"] = {
        "promoted_at": datetime.now(timezone.utc).isoformat(),
        "note": "Canonical skill path is scenarios/<id>/SKILL.md (sanitized).",
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pool-root", type=Path, default=DEFAULT_POOL)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--delete-oracle",
        action="store_true",
        help="delete oracle SKILL.md instead of archiving to SKILL_oracle.md",
    )
    p.add_argument(
        "--no-archive-oracle",
        action="store_true",
        help="same as --delete-oracle (oracle removed, not archived)",
    )
    args = p.parse_args()

    pool = args.pool_root.resolve()
    scenarios = pool / "scenarios"
    delete_oracle = args.delete_oracle or args.no_archive_oracle

    counts: dict[str, int] = {}
    for task_dir in sorted(p for p in scenarios.iterdir() if p.is_dir() and p.name.startswith("T")):
        if args.dry_run:
            status = classify_task(task_dir)
            if status == "needs_promotion":
                status = (
                    "would_promote_deleted_oracle"
                    if delete_oracle
                    else "would_promote_archived_oracle"
                )
            elif status == "promote_only":
                status = "would_promote_only"
        else:
            status = promote_one(task_dir, delete_oracle=delete_oracle)
        counts[status] = counts.get(status, 0) + 1

    print(json.dumps({"pool_root": str(pool), "dry_run": args.dry_run, "counts": counts}, indent=2))

    if args.dry_run:
        return 0

    patch_manifest(pool / "manifest.json")
    patch_derived_manifest(pool / "skills_rewritten_full" / "manifest_rewritten_skill.json")
    patch_derived_manifest(pool / "skills_rewritten_full" / "manifest_gene_skill_common778.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

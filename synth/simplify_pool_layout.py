#!/usr/bin/env python3
"""Flatten tasks_final into a single scenarios folder and prune debris.

Default behavior:
  - Reads `gene_bench_v3/tasks_final/manifest.json`
  - Excludes source `v3_guidebench`
  - Rewrites layout to:
      tasks_final/
        manifest.json
        scenarios/<flat_id>/
        _quarantine/
  - Removes intermediate/generated artifacts from each task directory.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
V3_ROOT = HERE.parent
DEFAULT_POOL_ROOT = V3_ROOT / "tasks_final"

DROP_FILE_NAMES = {
    "_design.json",
    "_fixture_manifest.json",
    "_calibration.json",
    "generated.py",
    "model_output.txt",
}
DROP_FILE_GLOBS = (
    "_trace*",
    "_stage1_raw*",
    "_s3_generate_*.py",
    "_s5_attempt*.py",
)
DROP_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ipynb_checkpoints",
    "_calibration",
    "_bad_solutions",
}


def _load_manifest(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("manifest must be a dict")
    tasks = raw.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("manifest missing tasks[]")
    rows = [x for x in tasks if isinstance(x, dict)]
    return raw, rows


def _row_rel_dir(row: dict[str, Any]) -> str:
    rel = row.get("rel_dir")
    if isinstance(rel, str) and rel.strip():
        return rel.strip()
    return str(row.get("task_id", "")).strip()


def _drop_file(name: str) -> bool:
    if name in DROP_FILE_NAMES:
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in DROP_FILE_GLOBS)


def _copy_tree_pruned(src: Path, dst: Path) -> None:
    if not src.exists() or not src.is_dir():
        raise FileNotFoundError(f"task dir missing: {src}")
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)

    for root, dirs, files in os.walk(src):
        root_path = Path(root)
        rel = root_path.relative_to(src)
        out_dir = dst / rel
        out_dir.mkdir(parents=True, exist_ok=True)

        dirs[:] = [
            d for d in dirs
            if d not in DROP_DIR_NAMES and not any(fnmatch.fnmatch(d, pat) for pat in DROP_FILE_GLOBS)
        ]
        for name in files:
            if _drop_file(name):
                continue
            shutil.copy2(root_path / name, out_dir / name)

    # Keep a root-level SKILL.md for easy browsing.
    root_skill = dst / "SKILL.md"
    if not root_skill.exists():
        skill_dir = dst / "skill"
        if skill_dir.exists() and skill_dir.is_dir():
            candidates = sorted(skill_dir.glob("**/SKILL.md"), key=lambda p: p.as_posix())
            if candidates:
                shutil.copy2(candidates[0], root_skill)


def _task_files_entry(task_dir: Path) -> dict[str, str | None]:
    def maybe(*candidates: str) -> str | None:
        for rel in candidates:
            if (task_dir / rel).exists():
                return rel
        return None

    return {
        "task": maybe("task.md"),
        "skill": maybe("SKILL.md", "skill/SKILL.md"),
        "ref": maybe("reference_solution.py"),
        "oracle": maybe("test_script.py"),
        "scenario": maybe("scenario.yaml"),
    }


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_family: dict[str, int] = {}
    by_mode: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for row in rows:
        fam = str(row.get("family"))
        mode = str(row.get("execution_mode"))
        source = str(row.get("source"))
        by_family[fam] = by_family.get(fam, 0) + 1
        by_mode[mode] = by_mode.get(mode, 0) + 1
        by_source[source] = by_source.get(source, 0) + 1
    return {
        "total_tasks": len(rows),
        "by_family": dict(sorted(by_family.items())),
        "by_execution_mode": dict(sorted(by_mode.items())),
        "by_source": dict(sorted(by_source.items())),
    }


def simplify_layout(args: argparse.Namespace) -> dict[str, Any]:
    pool_root = Path(args.pool_root).resolve()
    manifest_path = pool_root / "manifest.json"
    manifest_obj, rows = _load_manifest(manifest_path)
    exclude_sources = set(args.exclude_source)

    parent = pool_root.parent
    tmp_root = parent / f".{pool_root.name}.tmp.{int(time.time())}"
    if tmp_root.exists():
        shutil.rmtree(tmp_root)
    (tmp_root / args.flat_dir).mkdir(parents=True, exist_ok=True)
    (tmp_root / "_quarantine").mkdir(parents=True, exist_ok=True)

    kept_rows: list[dict[str, Any]] = []
    dropped = 0

    candidate_rows: list[dict[str, Any]] = []
    for row in rows:
        source = str(row.get("source"))
        if source in exclude_sources:
            dropped += 1
            continue
        candidate_rows.append(row)

    candidate_rows.sort(key=lambda r: (str(r.get("family")), str(r.get("task_id"))))
    width = max(int(args.id_width), len(str(max(len(candidate_rows), 1))))
    collisions = 0
    used_flat_ids: set[str] = set()

    for idx, row in enumerate(candidate_rows, start=1):
        src_rel = _row_rel_dir(row)
        src_dir = pool_root / src_rel

        legacy_task_id = str(row.get("legacy_task_id") or row.get("task_id") or src_rel)
        flat_id = f"{args.id_prefix}{idx:0{width}d}"
        suffix = 1
        while flat_id in used_flat_ids:
            suffix += 1
            collisions += 1
            flat_id = f"{args.id_prefix}{idx:0{width}d}__{suffix:02d}"
        used_flat_ids.add(flat_id)

        rel_dir = f"{args.flat_dir}/{flat_id}"
        dst_dir = tmp_root / rel_dir
        _copy_tree_pruned(src_dir, dst_dir)

        new_row = dict(row)
        new_row["legacy_task_id"] = legacy_task_id
        new_row["task_id"] = flat_id
        new_row["rel_dir"] = rel_dir
        new_row["files"] = _task_files_entry(dst_dir)
        kept_rows.append(new_row)

    kept_rows.sort(key=lambda r: (str(r.get("family")), str(r.get("task_id"))))

    new_manifest = dict(manifest_obj)
    new_manifest["pool"] = "gene_bench_v3_tasks_final_flat"
    new_manifest["generated_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    source_paths = dict(manifest_obj.get("source_paths") or {})
    source_paths["layout_flattened_from"] = str(pool_root)
    source_paths["out_root"] = str(pool_root)
    new_manifest["source_paths"] = source_paths
    new_manifest["summary"] = _summary(kept_rows)
    new_manifest["tasks"] = kept_rows
    new_manifest["layout"] = {
        "type": "flat",
        "tasks_dir": args.flat_dir,
        "excluded_sources": sorted(exclude_sources),
    }
    simplify_stats = {
        "input_tasks": len(rows),
        "output_tasks": len(kept_rows),
        "dropped_tasks": dropped,
        "id_collisions": collisions,
        "id_format": {"prefix": args.id_prefix, "width": width},
        "drop_rules": {
            "drop_file_names": sorted(DROP_FILE_NAMES),
            "drop_file_globs": list(DROP_FILE_GLOBS),
            "drop_dir_names": sorted(DROP_DIR_NAMES),
        },
    }
    existing = dict(new_manifest.get("build_stats") or {})
    existing["simplify_layout"] = simplify_stats
    new_manifest["build_stats"] = existing

    (tmp_root / "manifest.json").write_text(
        json.dumps(new_manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if args.dry_run:
        return {"manifest": new_manifest, "tmp_root": str(tmp_root), "dry_run": True}

    if pool_root.exists():
        shutil.rmtree(pool_root)
    tmp_root.rename(pool_root)
    return {"manifest": new_manifest, "tmp_root": str(tmp_root), "dry_run": False}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Flatten and prune tasks_final layout.")
    ap.add_argument("--pool-root", default=str(DEFAULT_POOL_ROOT), help="Path to tasks_final root.")
    ap.add_argument("--flat-dir", default="scenarios", help="Flat task directory name under pool root.")
    ap.add_argument(
        "--exclude-source",
        action="append",
        default=["v3_guidebench"],
        help="Manifest source values to exclude (can repeat).",
    )
    ap.add_argument("--id-prefix", default="T", help="Flat task id prefix, e.g. T -> T0001.")
    ap.add_argument("--id-width", type=int, default=4, help="Zero-padding width for flat task IDs.")
    ap.add_argument("--dry-run", action="store_true", help="Build temp output only; do not replace pool root.")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    t0 = time.time()
    res = simplify_layout(args)
    manifest = res["manifest"]
    elapsed = round(time.time() - t0, 2)
    summary = manifest.get("summary") or {}
    print(f"[simplify] pool_root={args.pool_root}")
    print(f"[simplify] total={summary.get('total_tasks', 0)} elapsed_s={elapsed} dry_run={res.get('dry_run')}")
    for fam, cnt in sorted((summary.get("by_family") or {}).items()):
        print(f"  - {fam}: {cnt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

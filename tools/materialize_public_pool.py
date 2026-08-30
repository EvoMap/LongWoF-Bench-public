#!/usr/bin/env python3
"""Rebuild the authoring ``tasks_final/`` pool layout from a public data archive.

The public archive ships a distribution layout -- ``data/tasks``,
``data/contexts``, ``data/runtime``, ``data/environments`` -- while
``eval.run_official`` consumes the authoring layout, where every asset of a task
sits together under ``scenarios/<task_id>/``. Nothing is lost in the split:
``data/release.json`` records both paths for every asset, so the authoring
layout is reconstructed exactly rather than guessed at.

    python tools/materialize_public_pool.py \\
      --bundle-root taskgenome-bench-public-data-v1.0.2 \\
      --out tasks_final

The result is a directory the runner accepts directly:

    tasks_final/manifest.json
    tasks_final/scenarios/<task_id>/{task.md,SKILL.md,...}
    tasks_final/genes_opus48/<task_id>.json
    tasks_final/genes_gemini31pro/<task_id>.json

Every file is checked against the SHA-256 recorded in ``release.json`` as it is
copied, so a corrupted or tampered archive fails here rather than halfway
through an evaluation run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path


class MaterializeError(RuntimeError):
    """The public archive cannot be rebuilt into an authoring pool."""


def _safe_relative(value: str, label: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise MaterializeError(f"unsafe {label}: {value!r}")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(release: dict) -> dict:
    """Derive a runner manifest from the public release records."""
    rows = []
    for asset in release.get("assets", []):
        if asset.get("role") != "prompt.task":
            continue
        task_id = asset.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise MaterializeError("a prompt.task asset carries no task_id")
        rows.append({
            "task_id": task_id,
            "family": asset.get("family"),
            "execution_mode": asset.get("execution_mode"),
            "source": "public_bundle",
            "answer_format": None,
            "files": {"task": "task.md", "skill": "SKILL.md"},
            "rel_dir": f"scenarios/{task_id}",
        })
    rows.sort(key=lambda row: row["task_id"])
    expected = release.get("canonical", {}).get("task_count")
    if expected is not None and expected != len(rows):
        raise MaterializeError(
            f"task count mismatch: canonical={expected!r}, prompts={len(rows)}")
    return {
        "pool": "longwof_public_data",
        "generated_at": "1970-01-01T00:00:00Z",
        "summary": {"total_tasks": len(rows)},
        "tasks": rows,
        "layout": {"task_root": "scenarios", "manifest": "manifest.json"},
    }


def materialize(bundle_root: Path, out: Path, *, verify: bool = True) -> dict:
    data_root = (bundle_root / "data").resolve()
    if not data_root.is_dir():
        raise MaterializeError(f"no data/ directory under {bundle_root}")
    release = json.loads((data_root / "release.json").read_text(encoding="utf-8"))

    out = out.resolve()
    if out.exists() and any(out.iterdir()):
        raise MaterializeError(f"destination must be empty: {out}")
    out.mkdir(parents=True, exist_ok=True)

    copied = 0
    mismatched: list[str] = []
    for asset in release.get("assets", []):
        source = data_root / _safe_relative(str(asset["bundle_path"]), "bundle path")
        target = out / _safe_relative(str(asset["source_relpath"]), "source path")
        if not source.is_file():
            raise MaterializeError(f"archive is missing {asset['bundle_path']}")
        if verify and _sha256(source) != asset.get("sha256"):
            mismatched.append(asset["bundle_path"])
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1

    if mismatched:
        raise MaterializeError(
            f"{len(mismatched)} archive files do not match their recorded digest, "
            f"first: {mismatched[0]}")

    manifest = build_manifest(release)
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")

    return {
        "status": "materialized",
        "pool_root": str(out),
        "manifest": str(out / "manifest.json"),
        "task_count": manifest["summary"]["total_tasks"],
        "assets_copied": copied,
        "digests_verified": verify,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle-root", required=True,
                        help="extracted public data archive root")
    parser.add_argument("--out", required=True,
                        help="empty destination for the authoring pool layout")
    parser.add_argument("--skip-digest-check", action="store_true",
                        help="copy without re-checking each file against release.json")
    args = parser.parse_args(argv)
    try:
        result = materialize(Path(args.bundle_root), Path(args.out),
                             verify=not args.skip_digest_check)
    except MaterializeError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, indent=2))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

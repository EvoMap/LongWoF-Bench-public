#!/usr/bin/env python3
"""Smoke-test an extracted public data archive without private task assets.

The public archive deliberately uses a distribution layout (``data/tasks``,
``data/contexts``, and so on), while ``eval.run_official`` consumes the
authoring-era ``tasks_final/manifest.json`` layout.  This adapter creates a
temporary, sanitized manifest from the public ``release.json`` records and
runs the runner's credential-free dry-run path.  It never copies private
judges, gold outputs, reference solutions, traces, or source paths.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONDITIONS = "no_context,with_skill,with_gene_opus"


class PublicDataSmokeError(RuntimeError):
    """The extracted public archive cannot support a dry-run smoke test."""


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicDataSmokeError(f"invalid {label}: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PublicDataSmokeError(f"{label} must be a JSON object: {path}")
    return payload


def _manifest_from_public_release(release: dict[str, Any]) -> dict[str, Any]:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise PublicDataSmokeError("public release.json is missing assets[]")
    prompts: dict[str, dict[str, Any]] = {}
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("role") != "prompt.task":
            continue
        task_id = asset.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise PublicDataSmokeError("prompt.task asset has no task_id")
        if task_id in prompts:
            raise PublicDataSmokeError(f"duplicate public prompt asset: {task_id}")
        prompts[task_id] = {
            "task_id": task_id,
            "family": asset.get("family"),
            "execution_mode": asset.get("execution_mode"),
            "source": "public_bundle",
            "answer_format": None,
            "files": {"task": "task.md", "skill": "SKILL.md"},
            "rel_dir": f"scenarios/{task_id}",
        }
    expected = release.get("canonical", {}).get("task_count")
    if expected != len(prompts) or expected != 778:
        raise PublicDataSmokeError(
            f"public release task count mismatch: canonical={expected!r}, prompts={len(prompts)}"
        )
    rows = [prompts[task_id] for task_id in sorted(prompts)]
    return {
        "pool": "longwof_public_data_preview",
        "generated_at": "1970-01-01T00:00:00Z",
        "summary": {"total_tasks": len(rows)},
        "tasks": rows,
        "layout": {"task_root": "scenarios", "manifest": "manifest.json"},
    }


def smoke(
    bundle_root: Path,
    *,
    ids: str,
    models: str,
    conditions: str = DEFAULT_CONDITIONS,
    run_id: str = "public-data-smoke",
) -> dict[str, Any]:
    bundle_root = bundle_root.resolve()
    data_root = bundle_root / "data"
    release_path = data_root / "release.json"
    release = _load_json(release_path, "public release manifest")
    manifest = _manifest_from_public_release(release)
    gene_dir = data_root / "contexts" / "gene_opus48"
    if not gene_dir.is_dir():
        raise PublicDataSmokeError(f"public Opus Gene directory is missing: {gene_dir}")

    with tempfile.TemporaryDirectory(prefix="longwof-public-smoke-") as work:
        work_root = Path(work)
        manifest_path = work_root / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        runs_root = work_root / "runs"
        command = [
            sys.executable,
            str(ROOT / "eval" / "run_official.py"),
            "--manifest",
            str(manifest_path),
            "--pool-root",
            str(data_root),
            "--gene-opus-dir",
            str(gene_dir),
            "--ids",
            ids,
            "--models",
            models,
            "--conditions",
            conditions,
            "--dry-run",
            "--run-id",
            run_id,
            "--runs-root",
            str(runs_root),
        ]
        completed = subprocess.run(command, capture_output=True, text=True)
        output = (completed.stdout + completed.stderr).strip()
    if completed.returncode != 0:
        raise PublicDataSmokeError(f"official dry-run failed ({completed.returncode}): {output}")
    expected_line = f"loaded {manifest['summary']['total_tasks']} tasks from"
    if expected_line not in output:
        raise PublicDataSmokeError(f"dry-run did not report 778 tasks: {output}")
    return {
        "status": "passed",
        "task_count": manifest["summary"]["total_tasks"],
        "ids": ids,
        "models": models,
        "conditions": conditions,
        "output": output,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", required=True, help="extracted archive root")
    parser.add_argument("--ids", default="T0499")
    parser.add_argument("--models", default="gemini_flash")
    parser.add_argument("--conditions", default=DEFAULT_CONDITIONS)
    parser.add_argument("--run-id", default="public-data-smoke")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = smoke(
            Path(args.bundle_root),
            ids=args.ids,
            models=args.models,
            conditions=args.conditions,
            run_id=args.run_id,
        )
    except (OSError, PublicDataSmokeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

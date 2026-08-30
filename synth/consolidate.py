#!/usr/bin/env python3
"""Consolidate all inputs into the unified LongWoF-Bench tasks_final pool.

Target output layout:
  gene_bench_v3/tasks_final/
    manifest.json
    math_reasoning/m_0001/...
    rule_following/r_0001/...
    agent_env_synth/a_0001/...
    code_generation/cg_0001/...
    _quarantine/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - optional dependency fallback
    yaml = None


HERE = Path(__file__).resolve().parent
V3_ROOT = HERE.parent
DEFAULT_V3_SOURCE = HERE / "candidates_sf"
DEFAULT_V3_GUIDEBENCH = V3_ROOT / "rule_following"
# Optional inputs from the earlier code-generation track. They are not part
# of the public release, so they default to empty and are opt-in by flag.
DEFAULT_V26_RUNTIME = ""
DEFAULT_V25_CURATED_DIR = ""
DEFAULT_V25_SKILLS_DIR = ""
DEFAULT_OUT_ROOT = V3_ROOT / "tasks_final"

V3_FAMILIES = ("math_reasoning", "rule_following", "agent_env_synth")
EXECUTION_MODE = {
    "math_reasoning": "text_short_answer",
    "rule_following": "text_short_answer",
    "agent_env_synth": "pytest_pkg",
    "code_generation": "subprocess_cli",
    "guidebench_rule_following": "guidebench_model_output_pytest",
}
ID_PREFIX = {
    "math_reasoning": "m",
    "rule_following": "r",
    "agent_env_synth": "a",
    "code_generation": "cg",
}

REQUIRED_FILES = (
    "task.md",
    "SKILL.md",
    "reference_solution.py",
    "test_script.py",
    "scenario.yaml",
    "_design.json",
)
OPTIONAL_FILES = (
    "_fixture_manifest.json",
    "_calibration.json",
    "metadata.json",
)
OPTIONAL_DIRS = (
    "data",
    "_fixtures",
    "_gold",
    "package",
    "_bad_solutions",
    "skill",
)


@dataclass(frozen=True)
class SourceTask:
    src_dir: Path
    family: str
    source: str
    orig_id: str
    calibration: Optional[dict[str, Any]]
    answer_format: Optional[str]
    execution_mode: Optional[str] = None
    skill_source_dir: Optional[Path] = None
    extra: Optional[dict[str, Any]] = None
    run_name: Optional[str] = None
    dedup_key: Optional[str] = None


def _load_json(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(obj, dict):
        return obj
    return None


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8", errors="replace")
    if yaml is not None:
        try:
            obj = yaml.safe_load(raw)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    # Lightweight fallback for top-level "key: value" YAML.
    out: dict[str, Any] = {}
    for line in raw.splitlines():
        if not line or line.startswith("#") or line.startswith(" "):
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


def _scenario_meta(task_dir: Path) -> tuple[Optional[str], Optional[str]]:
    scen = _load_yaml(task_dir / "scenario.yaml")
    name = scen.get("name")
    answer_format = scen.get("answer_format")
    name_s = str(name).strip() if name is not None else ""
    fmt_s = str(answer_format).strip() if answer_format is not None else ""
    return (name_s or None, fmt_s or None)


def _normalize_calibration(calib: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not calib:
        return {"anchor": None, "n_pass": None, "n_trials": None, "verdict": None}
    return {
        "anchor": calib.get("anchor"),
        "n_pass": calib.get("n_pass"),
        "n_trials": calib.get("n_trials"),
        "verdict": calib.get("verdict"),
    }


def _parse_v3_calibration(task_dir: Path) -> Optional[dict[str, Any]]:
    summary = _load_json(task_dir / "_calibration" / "summary.json")
    if not summary:
        return None
    n_pass = summary.get("n_pass")
    if n_pass is None:
        n_pass = summary.get("n_solved")
    return {
        "anchor": summary.get("anchor_model"),
        "n_pass": n_pass,
        "n_trials": summary.get("n_trials"),
        "verdict": summary.get("verdict"),
    }


def _parse_v26_calibration(task_dir: Path) -> Optional[dict[str, Any]]:
    calib = _load_json(task_dir / "_calibration.json")
    if not calib:
        return None
    anchors = calib.get("anchors")
    first = anchors[0] if isinstance(anchors, list) and anchors and isinstance(anchors[0], dict) else {}
    n_pass = first.get("n_pass")
    n_trials = first.get("n_trials")
    if n_pass is None:
        n_pass = calib.get("total_pass")
    if n_trials is None:
        n_trials = calib.get("total_trials")
    verdict = calib.get("final_label")
    if verdict is None:
        verdict = calib.get("verdict")
    return {
        "anchor": first.get("model"),
        "n_pass": n_pass,
        "n_trials": n_trials,
        "verdict": verdict,
    }


def _ignore_name(name: str) -> bool:
    if name in ("__pycache__", ".pytest_cache", "generated.py"):
        return True
    if name.startswith("_trace"):
        return True
    if name.startswith("_stage1_raw"):
        return True
    if name.startswith("_s3_generate_"):
        return True
    if name.startswith("_s5_attempt"):
        return True
    return False


def _copy_tree_filtered(src: Path, dst: Path) -> None:
    if not src.exists() or not src.is_dir():
        return
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in os.walk(src):
        root_path = Path(root)
        rel = root_path.relative_to(src)
        out_dir = dst / rel
        out_dir.mkdir(parents=True, exist_ok=True)
        dirs[:] = [d for d in dirs if not _ignore_name(d)]
        for filename in files:
            if _ignore_name(filename):
                continue
            src_file = root_path / filename
            dst_file = out_dir / filename
            shutil.copy2(src_file, dst_file)


def _first_skill_file(root: Path) -> Optional[Path]:
    if not root.exists():
        return None
    direct = root / "SKILL.md"
    if direct.exists():
        return direct
    for skill_path in sorted(root.glob("**/SKILL.md"), key=lambda p: p.as_posix()):
        if skill_path.is_file():
            return skill_path
    return None


def _ensure_root_skill(dst_dir: Path) -> None:
    root_skill = dst_dir / "SKILL.md"
    if root_skill.exists():
        return
    candidate: Optional[Path] = None
    skill_dir = dst_dir / "skill"
    if skill_dir.exists():
        candidate = _first_skill_file(skill_dir)
    if candidate is not None and candidate.exists():
        shutil.copy2(candidate, root_skill)


def _copy_task_payload(src_dir: Path, dst_dir: Path, skill_source_dir: Optional[Path] = None) -> None:
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)

    for name in REQUIRED_FILES:
        src = src_dir / name
        if src.exists() and src.is_file():
            (dst_dir / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst_dir / name)

    for name in OPTIONAL_FILES:
        src = src_dir / name
        if src.exists() and src.is_file():
            (dst_dir / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst_dir / name)

    for dname in OPTIONAL_DIRS:
        src = src_dir / dname
        dst = dst_dir / dname
        if src.exists() and src.is_dir():
            _copy_tree_filtered(src, dst)

    # Copy any additional task-local dependencies (e.g., helper modules, answer
    # fixtures, environment/ packages) while still filtering known synth debris.
    for child in sorted(src_dir.iterdir(), key=lambda p: p.name):
        if _ignore_name(child.name):
            continue
        dst_child = dst_dir / child.name
        if dst_child.exists():
            continue
        if child.is_file():
            shutil.copy2(child, dst_child)
        elif child.is_dir():
            _copy_tree_filtered(child, dst_child)

    # Imported curated skills live outside task dirs (skills/<task_id>/direct/...).
    if skill_source_dir is not None and skill_source_dir.exists() and skill_source_dir.is_dir():
        _copy_tree_filtered(skill_source_dir, dst_dir / "skill")

    # Keep calibration summary provenance if present.
    src_summary = src_dir / "_calibration" / "summary.json"
    if src_summary.exists() and src_summary.is_file():
        out_summary = dst_dir / "_calibration" / "summary.json"
        out_summary.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_summary, out_summary)

    _ensure_root_skill(dst_dir)


def _parse_run_timestamp(run_name: str) -> tuple[str, str]:
    m = re.match(r"^(\d{8}T\d{6})", run_name)
    ts = m.group(1) if m else ""
    return (ts, run_name)


def _v26_dedup_key(task_dir: Path) -> str:
    name, _ = _scenario_meta(task_dir)
    if name:
        return f"scenario_name:{name}"

    task_text = (task_dir / "task.md").read_text(encoding="utf-8", errors="replace") if (task_dir / "task.md").exists() else ""
    ref_text = (
        (task_dir / "reference_solution.py").read_text(encoding="utf-8", errors="replace")
        if (task_dir / "reference_solution.py").exists()
        else ""
    )
    digest = hashlib.sha1((task_text + "\n<<<SEP>>>\n" + ref_text).encode("utf-8")).hexdigest()
    return f"content_sha1:{digest}"


def collect_v3_tasks(v3_source: Path) -> tuple[list[SourceTask], dict[str, int]]:
    kept: list[SourceTask] = []
    stats = {
        "seen": 0,
        "kept": 0,
        "drop_not_pass_pre_calibration": 0,
        "drop_reject_trivially_easy": 0,
        "drop_invalid_run_record": 0,
    }
    for family in V3_FAMILIES:
        fam_dir = v3_source / family
        if not fam_dir.exists():
            continue
        candidates = sorted([p for p in fam_dir.iterdir() if p.is_dir() and p.name.endswith("sf")], key=lambda p: p.name)
        for task_dir in candidates:
            stats["seen"] += 1
            run_record = _load_json(task_dir / "_run_record.json")
            if not run_record:
                stats["drop_invalid_run_record"] += 1
                continue
            if run_record.get("verdict") != "pass_pre_calibration":
                stats["drop_not_pass_pre_calibration"] += 1
                continue
            calibration = _parse_v3_calibration(task_dir)
            if calibration and calibration.get("verdict") == "REJECT_trivially_easy":
                stats["drop_reject_trivially_easy"] += 1
                continue
            _, answer_format = _scenario_meta(task_dir)
            kept.append(
                SourceTask(
                    src_dir=task_dir,
                    family=family,
                    source="v3_solution_first",
                    orig_id=task_dir.name,
                    calibration=calibration,
                    answer_format=answer_format,
                )
            )
            stats["kept"] += 1
    kept.sort(key=lambda t: (t.family, t.orig_id))
    return kept, stats


def collect_v3_guidebench_tasks(guidebench_dir: Path) -> tuple[list[SourceTask], dict[str, int]]:
    kept: list[SourceTask] = []
    stats = {
        "seen": 0,
        "kept": 0,
        "drop_missing_task": 0,
        "drop_missing_test_script": 0,
    }
    pattern = re.compile(r"^S\d{3}-Guide$")
    for task_dir in sorted([p for p in guidebench_dir.iterdir() if p.is_dir()], key=lambda p: p.name):
        if not pattern.match(task_dir.name):
            continue
        stats["seen"] += 1
        if not (task_dir / "task.md").exists():
            stats["drop_missing_task"] += 1
            continue
        if not (task_dir / "test_script.py").exists():
            stats["drop_missing_test_script"] += 1
            continue
        kept.append(
            SourceTask(
                src_dir=task_dir,
                family="rule_following",
                source="v3_guidebench",
                orig_id=task_dir.name,
                calibration=None,
                answer_format="enum",
                execution_mode=EXECUTION_MODE["guidebench_rule_following"],
                skill_source_dir=(task_dir / "skill") if (task_dir / "skill").exists() else None,
            )
        )
        stats["kept"] += 1
    return kept, stats


def _is_v25_curated_task_dir(task_dir: Path) -> bool:
    return task_dir.is_dir() and bool(re.match(r"^S\d{3}_.+", task_dir.name))


def _detect_codegen_execution_mode(task_dir: Path) -> str:
    if not (task_dir / "reference_solution.py").exists():
        return "subprocess_cli_no_ref"
    test_script = task_dir / "test_script.py"
    if test_script.exists():
        text = test_script.read_text(encoding="utf-8", errors="replace")
        if "generated.py" in text:
            return "subprocess_cli"
    return "subprocess_ref_runner"


def collect_v25_curated_tasks(v25_curated_dir: Path, v25_skills_dir: Path) -> tuple[list[SourceTask], dict[str, int]]:
    kept: list[SourceTask] = []
    stats = {
        "seen": 0,
        "kept": 0,
        "with_reference_solution": 0,
        "without_reference_solution": 0,
        "mode_subprocess_cli": 0,
        "mode_subprocess_ref_runner": 0,
        "mode_subprocess_cli_no_ref": 0,
        "drop_missing_task": 0,
        "drop_missing_test_script": 0,
    }
    for task_dir in sorted([p for p in v25_curated_dir.iterdir() if _is_v25_curated_task_dir(p)], key=lambda p: p.name):
        stats["seen"] += 1
        if not (task_dir / "task.md").exists():
            stats["drop_missing_task"] += 1
            continue
        if not (task_dir / "test_script.py").exists():
            stats["drop_missing_test_script"] += 1
            continue
        execution_mode = _detect_codegen_execution_mode(task_dir)
        has_ref = execution_mode != "subprocess_cli_no_ref"
        if has_ref:
            stats["with_reference_solution"] += 1
        else:
            stats["without_reference_solution"] += 1
        if execution_mode == "subprocess_cli":
            stats["mode_subprocess_cli"] += 1
        elif execution_mode == "subprocess_ref_runner":
            stats["mode_subprocess_ref_runner"] += 1
        elif execution_mode == "subprocess_cli_no_ref":
            stats["mode_subprocess_cli_no_ref"] += 1
        kept.append(
            SourceTask(
                src_dir=task_dir,
                family="code_generation",
                source="v3_imported_curated_tasks_final",
                orig_id=task_dir.name,
                calibration=None,
                answer_format=None,
                execution_mode=execution_mode,
                skill_source_dir=v25_skills_dir / task_dir.name / "direct",
                extra={"has_reference_solution": has_ref},
            )
        )
        stats["kept"] += 1
    return kept, stats


def collect_v26_tasks(v26_runtime: Path) -> tuple[list[SourceTask], dict[str, int]]:
    all_candidates: list[tuple[tuple[str, str], Path, str]] = []
    for run_dir in sorted([p for p in v26_runtime.iterdir() if p.is_dir()], key=lambda p: p.name):
        kept_band = run_dir / "kept_middle_band"
        if not kept_band.exists() or not kept_band.is_dir():
            continue
        for task_dir in sorted([p for p in kept_band.iterdir() if p.is_dir() and p.name.startswith("C")], key=lambda p: p.name):
            all_candidates.append((_parse_run_timestamp(run_dir.name), task_dir, run_dir.name))

    all_candidates.sort(key=lambda x: (x[0][0], x[0][1], x[1].name))

    dedup_map: dict[str, tuple[tuple[str, str], Path, str]] = {}
    collisions = 0
    for run_sort_key, task_dir, run_name in all_candidates:
        key = _v26_dedup_key(task_dir)
        if key in dedup_map:
            collisions += 1
        dedup_map[key] = (run_sort_key, task_dir, run_name)

    survivors_raw = sorted(
        [
            (run_sort_key, task_dir, run_name, key)
            for key, (run_sort_key, task_dir, run_name) in dedup_map.items()
        ],
        key=lambda x: (x[0][0], x[0][1], x[2], x[1].name),
    )

    survivors: list[SourceTask] = []
    for _, task_dir, run_name, dedup_key in survivors_raw:
        _, answer_format = _scenario_meta(task_dir)
        survivors.append(
            SourceTask(
                src_dir=task_dir,
                family="code_generation",
                source="v3_imported_solution_first_runtime",
                orig_id=task_dir.name,
                calibration=_parse_v26_calibration(task_dir),
                answer_format=answer_format,
                run_name=run_name,
                dedup_key=dedup_key,
            )
        )

    stats = {
        "seen": len(all_candidates),
        "survivors": len(survivors),
        "collisions": collisions,
        "dropped_by_dedup": len(all_candidates) - len(survivors),
    }
    return survivors, stats


def _build_manifest_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_family: dict[str, int] = {}
    by_mode: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for row in rows:
        fam = str(row.get("family"))
        mode = str(row.get("execution_mode"))
        src = str(row.get("source"))
        by_family[fam] = by_family.get(fam, 0) + 1
        by_mode[mode] = by_mode.get(mode, 0) + 1
        by_source[src] = by_source.get(src, 0) + 1
    return {
        "total_tasks": len(rows),
        "by_family": dict(sorted(by_family.items())),
        "by_execution_mode": dict(sorted(by_mode.items())),
        "by_source": dict(sorted(by_source.items())),
    }


def _task_files_entry(task_dir: Path) -> dict[str, Optional[str]]:
    def maybe(name: str) -> Optional[str]:
        return name if (task_dir / name).exists() else None

    return {
        "task": maybe("task.md"),
        "skill": maybe("SKILL.md"),
        "ref": maybe("reference_solution.py"),
        "oracle": maybe("test_script.py"),
        "scenario": maybe("scenario.yaml"),
    }


def _prepare_output_root(out_root: Path, clean: bool) -> None:
    if clean and out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "_quarantine").mkdir(parents=True, exist_ok=True)


def consolidate(args: argparse.Namespace) -> dict[str, Any]:
    v3_source = Path(args.v3_source).resolve()
    v3_guidebench_dir = Path(args.v3_guidebench_dir).resolve()
    v26_runtime = Path(args.v26_runtime).resolve()
    v25_curated_dir = Path(args.v25_curated_dir).resolve()
    v25_skills_dir = Path(args.v25_skills_dir).resolve()
    out_root = Path(args.out_root).resolve()

    if not v3_source.exists():
        raise FileNotFoundError(f"v3 source not found: {v3_source}")
    if not args.no_guidebench and not v3_guidebench_dir.exists():
        raise FileNotFoundError(f"v3 guidebench dir not found: {v3_guidebench_dir}")
    if not args.no_v26 and not v26_runtime.exists():
        raise FileNotFoundError(f"imported solution-first runtime not found: {v26_runtime}")
    if not args.no_v25_curated and not v25_curated_dir.exists():
        raise FileNotFoundError(f"imported curated dir not found: {v25_curated_dir}")
    if not args.no_v25_curated and not v25_skills_dir.exists():
        raise FileNotFoundError(f"imported curated skills dir not found: {v25_skills_dir}")

    _prepare_output_root(out_root, clean=args.clean)

    manifest_rows: list[dict[str, Any]] = []
    build_stats: dict[str, Any] = {}

    v3_tasks, v3_stats = collect_v3_tasks(v3_source)
    build_stats["v3"] = v3_stats
    family_counters = {fam: 1 for fam in V3_FAMILIES}

    for source_task in v3_tasks:
        fam = source_task.family
        local_id = f"{ID_PREFIX[fam]}_{family_counters[fam]:04d}"
        family_counters[fam] += 1
        task_id = f"{fam}/{local_id}"
        dst_dir = out_root / fam / local_id
        _copy_task_payload(source_task.src_dir, dst_dir, skill_source_dir=source_task.skill_source_dir)
        files_entry = _task_files_entry(dst_dir)

        mode = source_task.execution_mode or EXECUTION_MODE[fam]
        manifest_rows.append(
            {
                "task_id": task_id,
                "family": fam,
                "execution_mode": mode,
                "source": source_task.source,
                "orig_id": source_task.orig_id,
                "answer_format": source_task.answer_format,
                "files": files_entry,
                "calibration": _normalize_calibration(source_task.calibration),
            }
        )

    if not args.no_guidebench:
        guidebench_tasks, guidebench_stats = collect_v3_guidebench_tasks(v3_guidebench_dir)
        build_stats["v3_guidebench"] = guidebench_stats
        for source_task in guidebench_tasks:
            fam = source_task.family
            local_id = f"{ID_PREFIX[fam]}_{family_counters[fam]:04d}"
            family_counters[fam] += 1
            task_id = f"{fam}/{local_id}"
            dst_dir = out_root / fam / local_id
            _copy_task_payload(source_task.src_dir, dst_dir, skill_source_dir=source_task.skill_source_dir)
            files_entry = _task_files_entry(dst_dir)

            row = {
                "task_id": task_id,
                "family": fam,
                "execution_mode": source_task.execution_mode or EXECUTION_MODE[fam],
                "source": source_task.source,
                "orig_id": source_task.orig_id,
                "answer_format": source_task.answer_format,
                "files": files_entry,
                "calibration": _normalize_calibration(source_task.calibration),
            }
            if source_task.extra:
                row.update(source_task.extra)
            manifest_rows.append(row)
    else:
        build_stats["v3_guidebench"] = {"seen": 0, "kept": 0, "drop_missing_task": 0, "drop_missing_test_script": 0}

    cg_counter = 1
    if not args.no_v26:
        v26_tasks, v26_stats = collect_v26_tasks(v26_runtime)
        build_stats["v26"] = v26_stats
        for source_task in v26_tasks:
            local_id = f"{ID_PREFIX['code_generation']}_{cg_counter:04d}"
            cg_counter += 1
            task_id = f"code_generation/{local_id}"
            dst_dir = out_root / "code_generation" / local_id
            _copy_task_payload(source_task.src_dir, dst_dir, skill_source_dir=source_task.skill_source_dir)
            files_entry = _task_files_entry(dst_dir)

            manifest_rows.append(
                {
                    "task_id": task_id,
                    "family": "code_generation",
                    "execution_mode": source_task.execution_mode or EXECUTION_MODE["code_generation"],
                    "source": source_task.source,
                    "orig_id": source_task.orig_id,
                    "answer_format": source_task.answer_format,
                    "files": files_entry,
                    "calibration": _normalize_calibration(source_task.calibration),
                    "source_run": source_task.run_name,
                    "dedup_key": source_task.dedup_key,
                }
            )
    else:
        build_stats["v26"] = {"seen": 0, "survivors": 0, "collisions": 0, "dropped_by_dedup": 0}

    if not args.no_v25_curated:
        v25_curated_tasks, v25_curated_stats = collect_v25_curated_tasks(v25_curated_dir, v25_skills_dir)
        build_stats["v25_curated"] = v25_curated_stats
        for source_task in v25_curated_tasks:
            local_id = f"{ID_PREFIX['code_generation']}_{cg_counter:04d}"
            cg_counter += 1
            task_id = f"code_generation/{local_id}"
            dst_dir = out_root / "code_generation" / local_id
            _copy_task_payload(source_task.src_dir, dst_dir, skill_source_dir=source_task.skill_source_dir)
            files_entry = _task_files_entry(dst_dir)

            row = {
                "task_id": task_id,
                "family": "code_generation",
                "execution_mode": source_task.execution_mode or EXECUTION_MODE["code_generation"],
                "source": source_task.source,
                "orig_id": source_task.orig_id,
                "answer_format": source_task.answer_format,
                "files": files_entry,
                "calibration": _normalize_calibration(source_task.calibration),
            }
            if source_task.extra:
                row.update(source_task.extra)
            manifest_rows.append(row)
    else:
        build_stats["v25_curated"] = {
            "seen": 0,
            "kept": 0,
            "with_reference_solution": 0,
            "without_reference_solution": 0,
            "mode_subprocess_cli": 0,
            "mode_subprocess_ref_runner": 0,
            "mode_subprocess_cli_no_ref": 0,
            "drop_missing_task": 0,
            "drop_missing_test_script": 0,
        }

    manifest_rows.sort(key=lambda r: (r["family"], r["task_id"]))
    summary = _build_manifest_summary(manifest_rows)
    manifest = {
        "pool": "gene_bench_v3_tasks_final",
        "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_paths": {
            "v3_source": str(v3_source),
            "v3_guidebench_dir": str(v3_guidebench_dir) if not args.no_guidebench else None,
            "v3_imported_solution_first_runtime_source": str(v26_runtime) if not args.no_v26 else None,
            "v3_imported_curated_tasks_source": str(v25_curated_dir) if not args.no_v25_curated else None,
            "v3_imported_curated_skills_source": str(v25_skills_dir) if not args.no_v25_curated else None,
            "out_root": str(out_root),
        },
        "summary": summary,
        "build_stats": build_stats,
        "tasks": manifest_rows,
    }

    manifest_path = out_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Consolidate native and imported inputs into gene_bench_v3/tasks_final with v3 source labels."
    )
    ap.add_argument("--v3-source", default=str(DEFAULT_V3_SOURCE), help="Path to v3 candidates_sf directory.")
    ap.add_argument(
        "--v3-guidebench-dir",
        default=str(DEFAULT_V3_GUIDEBENCH),
        help="Path to v3 GuideBench rule_following directory.",
    )
    ap.add_argument(
        "--v26-runtime",
        default=DEFAULT_V26_RUNTIME,
        help="Path to imported solution-first runtime root (.../_v26_solution_first_runtime).",
    )
    ap.add_argument(
        "--v25-curated-dir",
        default=DEFAULT_V25_CURATED_DIR,
        help="Path to imported curated tasks_final directory (curated S* tasks).",
    )
    ap.add_argument(
        "--v25-skills-dir",
        default=DEFAULT_V25_SKILLS_DIR,
        help="Path to imported curated skills directory.",
    )
    ap.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT), help="Output tasks_final root.")
    ap.add_argument("--no-v26", action="store_true", help="Skip imported solution-first code_generation source.")
    ap.add_argument("--no-guidebench", action="store_true", help="Skip v3 GuideBench rule_following source.")
    ap.add_argument("--no-v25-curated", action="store_true", help="Skip imported curated code_generation source.")
    ap.add_argument("--clean", action="store_true", help="Remove existing out-root before writing.")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    t0 = time.time()
    manifest = consolidate(args)
    elapsed = round(time.time() - t0, 2)
    summary = manifest.get("summary", {})
    print(f"[consolidate] out_root={manifest['source_paths']['out_root']}")
    print(f"[consolidate] total={summary.get('total_tasks', 0)} elapsed_s={elapsed}")
    for fam, cnt in sorted((summary.get("by_family") or {}).items()):
        print(f"  - {fam}: {cnt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

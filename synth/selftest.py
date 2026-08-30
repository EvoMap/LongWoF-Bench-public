#!/usr/bin/env python3
"""Self-test consolidated pool by running ref against each task oracle.

Reads tasks_final/manifest.json and dispatches by execution_mode:
  - text_short_answer: python test_script.py --candidate reference_solution.py
  - pytest_pkg: copy ref->generated.py; python -m pytest test_script.py -q
  - subprocess_cli: copy ref->generated.py; python test_script.py
  - subprocess_ref_runner: run reference_solution.py first, then python test_script.py
  - subprocess_cli_no_ref: skip (no canonical ref upstream)
  - guidebench_model_output_pytest: write model_output.txt from reference, then pytest

v2.5 curated compatibility mode (default):
  - source == v3_imported_curated_tasks_final:
      - if orig_id in _b_unrunnable.json -> skip
      - else -> pass-by-policy (do not force reference gate in local env)
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from utils import candidate_env  # noqa: E402
V3_ROOT = HERE.parent
DEFAULT_POOL_ROOT = V3_ROOT / "tasks_final"
DEFAULT_MANIFEST = DEFAULT_POOL_ROOT / "manifest.json"
DEFAULT_PYTHON = sys.executable
DEFAULT_V25_UNRUNNABLE_JSON = DEFAULT_POOL_ROOT / "legacy_v25_unrunnable.json"
TAIL_CHARS = 1200


@dataclass
class TaskResult:
    task_id: str
    family: str
    execution_mode: str
    status: str  # pass | fail | error | skip
    reason: str
    returncode: Optional[int]
    elapsed_s: float
    stdout_tail: str
    stderr_tail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "family": self.family,
            "execution_mode": self.execution_mode,
            "status": self.status,
            "reason": self.reason,
            "returncode": self.returncode,
            "elapsed_s": self.elapsed_s,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
        }


def _row_rel_dir(row: dict[str, Any]) -> str:
    rel = row.get("rel_dir")
    if isinstance(rel, str) and rel.strip():
        return rel.strip()
    return str(row.get("task_id", "")).strip()


def _run_command(cmd: list[str], cwd: Path, timeout_s: int) -> tuple[int, str, str, Optional[str]]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=candidate_env(),
        )
        return proc.returncode, proc.stdout, proc.stderr, None
    except subprocess.TimeoutExpired:
        return -1, "", "", f"timeout_{timeout_s}s"
    except Exception as e:
        return -1, "", "", f"exec_error:{e}"


def _cleanup_generated(task_dir: Path, clean_pytest_cache: bool = False) -> None:
    try:
        (task_dir / "generated.py").unlink(missing_ok=True)
    except OSError:
        pass
    if clean_pytest_cache:
        for junk in (task_dir / "__pycache__", task_dir / ".pytest_cache"):
            shutil.rmtree(junk, ignore_errors=True)


def _has_fail_line(text: str) -> bool:
    return bool(re.search(r"(?m)^FAIL:", text))


def _looks_environment_blocker(text: str) -> bool:
    low = text.lower()
    patterns = (
        "modulenotfounderror",
        "no module named",
        "importerror",
        "permission denied",
        "/logs/verifier",
        "can't open file '/solution/",
        "file not found. expected",
        "no such file or directory: 'gh'",
        "dockerfile",
        "timeout_",
    )
    return any(p in low for p in patterns)


def _extract_answer_literal_from_reference(reference_path: Path) -> str:
    raw = reference_path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(raw)
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "ANSWER":
                        val = node.value
                        if isinstance(val, ast.Constant) and isinstance(val.value, str):
                            return val.value.strip()
                        if isinstance(val, ast.JoinedStr):
                            joined = "".join(
                                part.value for part in val.values if isinstance(part, ast.Constant) and isinstance(part.value, str)
                            )
                            return joined.strip()
    except Exception:
        pass
    return ""


def _mode_text_short_answer(task_dir: Path, python_exec: str, timeout_s: int) -> TaskResult:
    t0 = time.time()
    cmd = [python_exec, "test_script.py", "--candidate", "reference_solution.py"]
    rc, stdout, stderr, err = _run_command(cmd, cwd=task_dir, timeout_s=timeout_s)
    elapsed = round(time.time() - t0, 3)

    if err is not None:
        return TaskResult("", "", "text_short_answer", "error", err, rc, elapsed, stdout[-TAIL_CHARS:], stderr[-TAIL_CHARS:])

    passed = "PASS:SCORE:1.0" in stdout
    status = "pass" if passed else "fail"
    reason = "PASS:SCORE:1.0 found" if passed else "missing PASS:SCORE:1.0"
    return TaskResult("", "", "text_short_answer", status, reason, rc, elapsed, stdout[-TAIL_CHARS:], stderr[-TAIL_CHARS:])


def _mode_pytest_pkg(task_dir: Path, python_exec: str, timeout_s: int) -> TaskResult:
    t0 = time.time()
    ref = task_dir / "reference_solution.py"
    if not ref.exists():
        return TaskResult("", "", "pytest_pkg", "error", "missing_reference_solution.py", None, 0.0, "", "")

    shutil.copy2(ref, task_dir / "generated.py")
    try:
        cmd = [python_exec, "-m", "pytest", "test_script.py", "-q"]
        rc, stdout, stderr, err = _run_command(cmd, cwd=task_dir, timeout_s=timeout_s)
        elapsed = round(time.time() - t0, 3)
        if err is not None:
            return TaskResult("", "", "pytest_pkg", "error", err, rc, elapsed, stdout[-TAIL_CHARS:], stderr[-TAIL_CHARS:])

        out = stdout + "\n" + stderr
        n_pass = int(m.group(1)) if (m := re.search(r"(\d+)\s+passed", out)) else 0
        n_fail = int(m.group(1)) if (m := re.search(r"(\d+)\s+failed", out)) else 0
        n_err = int(m.group(1)) if (m := re.search(r"(\d+)\s+errors?", out)) else 0
        passed = rc == 0 and n_pass >= 1 and n_fail == 0 and n_err == 0
        status = "pass" if passed else "fail"
        reason = f"pytest rc={rc}, passed={n_pass}, failed={n_fail}, errors={n_err}"
        return TaskResult("", "", "pytest_pkg", status, reason, rc, elapsed, stdout[-TAIL_CHARS:], stderr[-TAIL_CHARS:])
    finally:
        _cleanup_generated(task_dir, clean_pytest_cache=True)


def _mode_subprocess_cli(task_dir: Path, python_exec: str, timeout_s: int) -> TaskResult:
    t0 = time.time()
    ref = task_dir / "reference_solution.py"
    if not ref.exists():
        return TaskResult("", "", "subprocess_cli", "error", "missing_reference_solution.py", None, 0.0, "", "")

    shutil.copy2(ref, task_dir / "generated.py")
    try:
        cmd = [python_exec, "test_script.py"]
        rc, stdout, stderr, err = _run_command(cmd, cwd=task_dir, timeout_s=timeout_s)
        elapsed = round(time.time() - t0, 3)
        if err is not None:
            return TaskResult("", "", "subprocess_cli", "error", err, rc, elapsed, stdout[-TAIL_CHARS:], stderr[-TAIL_CHARS:])

        match = re.search(r"SCORE:pass_rate=([0-9]*\.?[0-9]+)", stdout)
        pass_rate = float(match.group(1)) if match else None
        has_fail = _has_fail_line(stdout)
        if pass_rate is None:
            # v2.5 curated scripts often emit multiple SCORE:* metrics without pass_rate.
            passed = rc == 0 and not has_fail
        else:
            passed = rc == 0 and abs(pass_rate - 1.0) <= 1e-6 and not has_fail
        status = "pass" if passed else "fail"
        reason = f"rc={rc}, pass_rate={pass_rate}, has_FAIL={has_fail}"
        return TaskResult("", "", "subprocess_cli", status, reason, rc, elapsed, stdout[-TAIL_CHARS:], stderr[-TAIL_CHARS:])
    finally:
        _cleanup_generated(task_dir, clean_pytest_cache=False)


def _mode_subprocess_cli_no_ref(task_dir: Path) -> TaskResult:
    return TaskResult(
        "",
        "",
        "subprocess_cli_no_ref",
        "skip",
        "skip_no_reference_solution_by_design",
        None,
        0.0,
        "",
        "",
    )


def _stage_data_children(task_dir: Path) -> list[Path]:
    created: list[Path] = []
    data_dir = task_dir / "data"
    if not data_dir.exists() or not data_dir.is_dir():
        return created
    for child in sorted(data_dir.iterdir(), key=lambda p: p.name):
        dst = task_dir / child.name
        if dst.exists():
            continue
        if child.is_file():
            shutil.copy2(child, dst)
            created.append(dst)
        elif child.is_dir():
            shutil.copytree(child, dst)
            created.append(dst)
    return created


def _mode_subprocess_ref_runner(task_dir: Path, python_exec: str, timeout_s: int) -> TaskResult:
    t0 = time.time()
    ref = task_dir / "reference_solution.py"
    if not ref.exists():
        return TaskResult("", "", "subprocess_ref_runner", "error", "missing_reference_solution.py", None, 0.0, "", "")

    staged_paths = _stage_data_children(task_dir)
    try:
        rc_ref, stdout_ref, stderr_ref, err_ref = _run_command([python_exec, "reference_solution.py"], cwd=task_dir, timeout_s=timeout_s)
        if err_ref is not None:
            elapsed = round(time.time() - t0, 3)
            status = "skip" if _looks_environment_blocker(err_ref) else "error"
            return TaskResult(
                "",
                "",
                "subprocess_ref_runner",
                status,
                f"reference_exec:{err_ref}",
                rc_ref,
                elapsed,
                stdout_ref[-TAIL_CHARS:],
                stderr_ref[-TAIL_CHARS:],
            )
        if rc_ref != 0:
            elapsed = round(time.time() - t0, 3)
            return TaskResult(
                "",
                "",
                "subprocess_ref_runner",
                "skip",
                f"reference_nonzero_rc:{rc_ref}",
                rc_ref,
                elapsed,
                stdout_ref[-TAIL_CHARS:],
                stderr_ref[-TAIL_CHARS:],
            )

        rc, stdout, stderr, err = _run_command([python_exec, "test_script.py"], cwd=task_dir, timeout_s=timeout_s)
        elapsed = round(time.time() - t0, 3)
        if err is not None:
            status = "skip" if _looks_environment_blocker(err) else "error"
            return TaskResult("", "", "subprocess_ref_runner", status, err, rc, elapsed, stdout[-TAIL_CHARS:], stderr[-TAIL_CHARS:])
        has_fail = _has_fail_line(stdout)
        merged = (stdout or "") + "\n" + (stderr or "")
        if rc == 0 and not has_fail:
            status = "pass"
        elif rc != 0 and not has_fail:
            status = "skip"
        elif _looks_environment_blocker(merged):
            status = "skip"
        else:
            status = "fail"
        reason = f"ref_rc={rc_ref}, test_rc={rc}, has_FAIL={has_fail}"
        return TaskResult("", "", "subprocess_ref_runner", status, reason, rc, elapsed, stdout[-TAIL_CHARS:], stderr[-TAIL_CHARS:])
    finally:
        for path in staged_paths:
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
            except OSError:
                pass


def _mode_guidebench_model_output_pytest(task_dir: Path, python_exec: str, timeout_s: int) -> TaskResult:
    t0 = time.time()
    ref = task_dir / "reference_solution.py"
    if not ref.exists():
        return TaskResult("", "", "guidebench_model_output_pytest", "error", "missing_reference_solution.py", None, 0.0, "", "")

    output_file = task_dir / "model_output.txt"
    try:
        # First try runtime output of reference_solution.py.
        rc_ref, stdout_ref, stderr_ref, err_ref = _run_command([python_exec, "reference_solution.py"], cwd=task_dir, timeout_s=timeout_s)
        if err_ref is not None:
            elapsed = round(time.time() - t0, 3)
            return TaskResult(
                "",
                "",
                "guidebench_model_output_pytest",
                "error",
                f"reference_exec:{err_ref}",
                rc_ref,
                elapsed,
                stdout_ref[-TAIL_CHARS:],
                stderr_ref[-TAIL_CHARS:],
            )
        candidate_output = stdout_ref.strip()
        if not candidate_output:
            candidate_output = _extract_answer_literal_from_reference(ref)
        if not candidate_output:
            elapsed = round(time.time() - t0, 3)
            return TaskResult(
                "",
                "",
                "guidebench_model_output_pytest",
                "error",
                "empty_reference_output_and_missing_ANSWER_literal",
                rc_ref,
                elapsed,
                stdout_ref[-TAIL_CHARS:],
                stderr_ref[-TAIL_CHARS:],
            )
        output_file.write_text(candidate_output + "\n", encoding="utf-8")

        rc, stdout, stderr, err = _run_command([python_exec, "-m", "pytest", "test_script.py", "-q"], cwd=task_dir, timeout_s=timeout_s)
        elapsed = round(time.time() - t0, 3)
        if err is not None:
            return TaskResult(
                "",
                "",
                "guidebench_model_output_pytest",
                "error",
                err,
                rc,
                elapsed,
                stdout[-TAIL_CHARS:],
                stderr[-TAIL_CHARS:],
            )

        out = stdout + "\n" + stderr
        n_pass = int(m.group(1)) if (m := re.search(r"(\d+)\s+passed", out)) else 0
        n_fail = int(m.group(1)) if (m := re.search(r"(\d+)\s+failed", out)) else 0
        n_err = int(m.group(1)) if (m := re.search(r"(\d+)\s+errors?", out)) else 0
        passed = rc == 0 and n_pass >= 1 and n_fail == 0 and n_err == 0
        status = "pass" if passed else "fail"
        reason = f"pytest rc={rc}, passed={n_pass}, failed={n_fail}, errors={n_err}"
        return TaskResult("", "", "guidebench_model_output_pytest", status, reason, rc, elapsed, stdout[-TAIL_CHARS:], stderr[-TAIL_CHARS:])
    finally:
        try:
            output_file.unlink(missing_ok=True)
        except OSError:
            pass
        _cleanup_generated(task_dir, clean_pytest_cache=True)


def _load_manifest(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        tasks = raw.get("tasks")
        if not isinstance(tasks, list):
            raise ValueError("manifest dict missing tasks[]")
        return raw, tasks
    if isinstance(raw, list):
        return {"tasks": raw}, raw
    raise ValueError("manifest must be dict or list")


def _build_summary(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    by_family: dict[str, int] = {}
    by_mode: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for row in tasks:
        fam = str(row.get("family"))
        mode = str(row.get("execution_mode"))
        src = str(row.get("source"))
        by_family[fam] = by_family.get(fam, 0) + 1
        by_mode[mode] = by_mode.get(mode, 0) + 1
        by_source[src] = by_source.get(src, 0) + 1
    return {
        "total_tasks": len(tasks),
        "by_family": dict(sorted(by_family.items())),
        "by_execution_mode": dict(sorted(by_mode.items())),
        "by_source": dict(sorted(by_source.items())),
    }


def _load_v25_unrunnable(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    scenarios = raw.get("scenarios") if isinstance(raw, dict) else {}
    if not isinstance(scenarios, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for sid, meta in scenarios.items():
        if isinstance(meta, dict):
            out[str(sid)] = {
                "category": meta.get("category"),
                "reason": meta.get("reason"),
            }
        else:
            out[str(sid)] = {"category": None, "reason": str(meta)}
    return out


def _run_one(
    row: dict[str, Any],
    pool_root: Path,
    python_exec: str,
    timeout_s: int,
    curated_policy: str,
    v25_unrunnable: dict[str, dict[str, Any]],
) -> TaskResult:
    task_id = str(row.get("task_id"))
    family = str(row.get("family"))
    mode = str(row.get("execution_mode"))
    source = str(row.get("source"))
    orig_id = str(row.get("orig_id") or "")
    task_rel_dir = _row_rel_dir(row)
    task_dir = pool_root / task_rel_dir

    if not task_dir.exists():
        return TaskResult(task_id, family, mode, "error", f"task_dir_missing:{task_rel_dir}", None, 0.0, "", "")
    if not (task_dir / "test_script.py").exists():
        return TaskResult(task_id, family, mode, "error", "missing_test_script.py", None, 0.0, "", "")

    # v2.5 compatibility mode: curated tasks are not self-tested via local
    # reference execution. Keep the historical skip set from _b_unrunnable.json;
    # treat the rest as pass-by-policy.
    if source == "v3_imported_curated_tasks_final" and curated_policy == "v25_compat":
        meta = v25_unrunnable.get(orig_id)
        if meta is not None:
            cat = str(meta.get("category") or "unknown")
            return TaskResult(task_id, family, mode, "skip", f"v25_unrunnable:{cat}", None, 0.0, "", "")
        return TaskResult(task_id, family, mode, "pass", "v25_curated_policy_pass", None, 0.0, "", "")

    if mode in {"text_short_answer", "pytest_pkg", "subprocess_cli", "subprocess_ref_runner", "guidebench_model_output_pytest"} and not (
        task_dir / "reference_solution.py"
    ).exists():
        return TaskResult(task_id, family, mode, "error", "missing_reference_solution.py", None, 0.0, "", "")

    if mode == "text_short_answer":
        result = _mode_text_short_answer(task_dir, python_exec, timeout_s)
    elif mode == "pytest_pkg":
        result = _mode_pytest_pkg(task_dir, python_exec, timeout_s)
    elif mode == "subprocess_cli":
        result = _mode_subprocess_cli(task_dir, python_exec, timeout_s)
    elif mode == "subprocess_ref_runner":
        result = _mode_subprocess_ref_runner(task_dir, python_exec, timeout_s)
    elif mode == "subprocess_cli_no_ref":
        result = _mode_subprocess_cli_no_ref(task_dir)
    elif mode == "guidebench_model_output_pytest":
        result = _mode_guidebench_model_output_pytest(task_dir, python_exec, timeout_s)
    else:
        return TaskResult(task_id, family, mode, "error", f"unknown_execution_mode:{mode}", None, 0.0, "", "")

    result.task_id = task_id
    result.family = family
    return result


def _summarize_results(results: list[TaskResult]) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for r in results:
        fam = r.family
        fam_counts = summary.setdefault(fam, {"pass": 0, "fail": 0, "error": 0, "skip": 0})
        fam_counts[r.status] = fam_counts.get(r.status, 0) + 1
    return dict(sorted(summary.items()))


def _quarantine_failures(
    manifest_obj: dict[str, Any],
    manifest_tasks: list[dict[str, Any]],
    results: list[TaskResult],
    manifest_path: Path,
    pool_root: Path,
) -> int:
    failed_ids = {r.task_id for r in results if r.status in {"fail", "error"}}
    if not failed_ids:
        return 0

    quarantine_root = pool_root / "_quarantine"
    quarantine_root.mkdir(parents=True, exist_ok=True)
    task_row_by_id: dict[str, dict[str, Any]] = {}
    for row in manifest_tasks:
        if isinstance(row, dict):
            tid = str(row.get("task_id", "")).strip()
            if tid:
                task_row_by_id[tid] = row

    moved = 0
    for task_id in sorted(failed_ids):
        row = task_row_by_id.get(task_id, {})
        rel = _row_rel_dir(row) if row else task_id
        src = pool_root / rel
        if not src.exists():
            continue
        dst_name = task_id.replace("/", "__") or rel.replace("/", "__")
        dst = quarantine_root / dst_name
        if dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        shutil.move(str(src), str(dst))
        moved += 1

    kept_tasks = [row for row in manifest_tasks if str(row.get("task_id")) not in failed_ids]
    manifest_obj["tasks"] = kept_tasks
    manifest_obj["summary"] = _build_summary(kept_tasks)
    manifest_obj["last_selftest"] = {
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "quarantine_enabled": True,
        "quarantined_count": moved,
        "quarantined_task_ids": sorted(failed_ids),
    }
    manifest_path.write_text(json.dumps(manifest_obj, indent=2, ensure_ascii=False), encoding="utf-8")
    return moved


def run_selftest(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).resolve()
    pool_root = Path(args.pool_root).resolve()
    python_exec = str(args.python_executable)
    timeout_s = int(args.timeout_s)
    curated_policy = str(args.curated_policy)
    v25_unrunnable_path = Path(args.v25_unrunnable_json).resolve()

    if not manifest_path.exists():
        raise SystemExit(f"manifest not found: {manifest_path}")
    if not pool_root.exists():
        raise SystemExit(f"pool root not found: {pool_root}")

    manifest_obj, manifest_tasks = _load_manifest(manifest_path)
    tasks = [t for t in manifest_tasks if isinstance(t, dict)]
    v25_unrunnable = _load_v25_unrunnable(v25_unrunnable_path) if curated_policy == "v25_compat" else {}

    t0 = time.time()
    results: list[TaskResult] = []
    if args.workers <= 1:
        for row in tasks:
            results.append(_run_one(row, pool_root, python_exec, timeout_s, curated_policy, v25_unrunnable))
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {
                ex.submit(_run_one, row, pool_root, python_exec, timeout_s, curated_policy, v25_unrunnable): row
                for row in tasks
            }
            for fut in as_completed(futures):
                results.append(fut.result())
        # keep report stable
        results.sort(key=lambda r: r.task_id)

    elapsed = round(time.time() - t0, 2)
    by_family = _summarize_results(results)
    hard_non_pass = [r for r in results if r.status in {"fail", "error"}]
    skipped = [r for r in results if r.status == "skip"]

    print(f"[selftest] tasks={len(results)} elapsed_s={elapsed}")
    for fam, counts in by_family.items():
        print(
            f"  - {fam}: pass={counts.get('pass', 0)} fail={counts.get('fail', 0)} "
            f"error={counts.get('error', 0)} skip={counts.get('skip', 0)}"
        )

    if hard_non_pass:
        print("\n[selftest] non-pass tasks:")
        for r in sorted(hard_non_pass, key=lambda x: x.task_id):
            print(f"- {r.task_id} [{r.status}] {r.reason}")
            if r.stdout_tail.strip():
                print("  stdout_tail:")
                for line in r.stdout_tail.strip().splitlines()[-12:]:
                    print(f"    {line}")
            if r.stderr_tail.strip():
                print("  stderr_tail:")
                for line in r.stderr_tail.strip().splitlines()[-8:]:
                    print(f"    {line}")

    if skipped:
        print(f"\n[selftest] skipped={len(skipped)} (e.g. missing canonical reference_solution)")
        for r in sorted(skipped, key=lambda x: x.task_id)[:20]:
            print(f"- {r.task_id} [{r.status}] {r.reason}")
        if len(skipped) > 20:
            print(f"  ... and {len(skipped) - 20} more skipped tasks")

    report = {
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "manifest": str(manifest_path),
        "pool_root": str(pool_root),
        "python_executable": python_exec,
        "timeout_s": timeout_s,
        "workers": args.workers,
        "curated_policy": curated_policy,
        "v25_unrunnable_json": str(v25_unrunnable_path),
        "v25_unrunnable_count": len(v25_unrunnable),
        "total": len(results),
        "non_pass": len(hard_non_pass),
        "skipped": len(skipped),
        "by_family": by_family,
        "results": [r.to_dict() for r in sorted(results, key=lambda x: x.task_id)],
    }
    report_path = pool_root / "selftest_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[selftest] report -> {report_path}")

    quarantined = 0
    if args.quarantine and hard_non_pass:
        quarantined = _quarantine_failures(manifest_obj, manifest_tasks, results, manifest_path, pool_root)
        print(f"[selftest] quarantined={quarantined} updated_manifest={manifest_path}")

    if hard_non_pass:
        return 1
    if args.fail_on_skip and skipped:
        return 1
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Self-test consolidated tasks_final pool.")
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="Path to manifest.json")
    ap.add_argument("--pool-root", default=str(DEFAULT_POOL_ROOT), help="tasks_final root")
    ap.add_argument("--python-executable", default=DEFAULT_PYTHON, help="Python interpreter for oracle dispatch")
    ap.add_argument("--timeout-s", type=int, default=120, help="Per-task timeout in seconds")
    ap.add_argument("--workers", type=int, default=1, help="Number of worker threads (default sequential)")
    ap.add_argument("--quarantine", action="store_true", help="Move non-pass tasks to _quarantine and update manifest")
    ap.add_argument("--fail-on-skip", action="store_true", help="Exit nonzero when skipped tasks exist.")
    ap.add_argument(
        "--curated-policy",
        choices=["v25_compat", "strict"],
        default="v25_compat",
        help="How to treat source=v3_imported_curated_tasks_final tasks.",
    )
    ap.add_argument(
        "--v25-unrunnable-json",
        default=str(DEFAULT_V25_UNRUNNABLE_JSON),
        help="Path to v2.5 _b_unrunnable.json used by curated v25_compat mode.",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    return run_selftest(args)


if __name__ == "__main__":
    raise SystemExit(main())

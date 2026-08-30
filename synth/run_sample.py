#!/usr/bin/env python3
"""LongWoF-Bench — pre-Step-A 10-sample vibe check.

For each family in {rule_following, math_reasoning, agent_env_synth},
run Stage 1 (LLM draft → 4-or-more files) over 10 seeds, then for the
two text-short-answer families also run Stage 2 (ref smoke) and the
family-shared Stage 3 (test_script.py.tmpl on ref → must PASS).

This is a deliberately minimal driver — NOT the full v3 framework. It
reuses v2.5's LLMClient + parse_file_blocks helpers without committing
to a directory layout. The purpose is to validate prompts quickly
before Step A's framework is built.

Usage:
    python run_sample.py --family rule_following --n 10
    python run_sample.py --all
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHAPES_DIR = HERE / "shapes"
CANDIDATES_DIR = HERE / "candidates"

# The LLM client and file-block helpers are vendored beside this module so the
# pipeline runs from a clean checkout with no path outside the repository.
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from llm_client import GeminiClient, LLMResponse  # noqa: E402
from utils import candidate_env, parse_file_blocks, _safe_join  # noqa: E402


FAMILIES = ["rule_following", "math_reasoning", "agent_env_synth"]
ID_PREFIX = {
    "rule_following": "R",
    "math_reasoning": "M",
    "agent_env_synth": "A",
}


def load_seeds(family: str) -> list[tuple[str, str]]:
    raw = (SHAPES_DIR / family / "seeds.txt").read_text(encoding="utf-8")
    out: list[tuple[str, str]] = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if "|" not in ln:
            continue
        domain, idea = ln.split("|", 1)
        out.append((domain.strip(), idea.strip()))
    return out


def render_prompt(template: str, **kw) -> str:
    out = template
    for k, v in kw.items():
        out = out.replace("{" + k + "}", v)
    return out


def call_llm(client: GeminiClient, prompt: str, *, model: str, max_tokens: int) -> LLMResponse:
    return client.chat(prompt, max_tokens=max_tokens, temperature=0.4, model=model)


def write_files_safe(target: Path, files: dict[str, str]) -> list[str]:
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for relpath, content in files.items():
        try:
            p = _safe_join(target, relpath)
        except ValueError as e:
            written.append(f"!unsafe:{relpath}:{e}")
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        written.append(relpath)
    return written


def stage1(client, family: str, domain: str, idea: str, scenario_id: str,
           cand_dir: Path, model: str) -> dict:
    prompt_path = SHAPES_DIR / family / "prompts" / "1_scenario_draft.md"
    template = prompt_path.read_text(encoding="utf-8")
    prompt = render_prompt(
        template, domain=domain, task_idea=idea, scenario_id=scenario_id
    )
    t0 = time.time()
    resp = call_llm(client, prompt, model=model, max_tokens=24000)
    elapsed = round(time.time() - t0, 1)
    raw_path = cand_dir / "_stage1_raw.txt"
    cand_dir.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(resp.text)
    files = parse_file_blocks(resp.text)
    written = write_files_safe(cand_dir, files)
    return {
        "stage": "stage1",
        "model": model,
        "elapsed_s": elapsed,
        "in_tokens": resp.input_tokens,
        "out_tokens": resp.output_tokens,
        "stop_reason": resp.stop_reason,
        "files_emitted": list(files),
        "files_written": written,
        "raw_chars": len(resp.text),
        "raw_path": str(raw_path),
    }


def stage2_text_short(cand_dir: Path) -> dict:
    """Run reference_solution.py and parse ANSWER: line. Used by
    rule_following / math_reasoning. Also surfaces the new v2 prompt
    fields `predicted_no_context_answer` (rule) and `effective_seed`
    (math) so calibration / audit can see them."""
    ref = cand_dir / "reference_solution.py"
    if not ref.exists():
        return {"stage": "stage2", "ok": False, "reason": "no_reference_solution"}
    try:
        proc = subprocess.run(
            [sys.executable, str(ref)],
            cwd=cand_dir, capture_output=True, text=True, timeout=30,
            env=candidate_env(),
        )
    except subprocess.TimeoutExpired:
        return {"stage": "stage2", "ok": False, "reason": "timeout_30s"}
    out = proc.stdout
    has_answer = any(line.strip().lower().startswith("answer:") for line in out.splitlines())

    extras: dict = {}
    scen_path = cand_dir / "scenario.yaml"
    if scen_path.exists():
        try:
            import yaml as _y
            scen = _y.safe_load(scen_path.read_text(encoding="utf-8")) or {}
            for k in ("predicted_no_context_answer", "effective_seed"):
                if k in scen:
                    extras[k] = scen[k]
        except Exception:
            pass
    return {
        "stage": "stage2",
        "ok": has_answer,
        "rc": proc.returncode,
        "stdout_tail": out[-300:],
        "stderr_tail": proc.stderr[-300:],
        "has_answer_line": has_answer,
        "scenario_extras": extras,
    }


def stage3_text_short(family: str, cand_dir: Path) -> dict:
    """Copy the family-shared test_script.py.tmpl into the candidate dir,
    run it on reference_solution.py, expect PASS:SCORE:1.0."""
    tmpl = SHAPES_DIR / family / "test_script.py.tmpl"
    test_path = cand_dir / "test_script.py"
    shutil.copy2(tmpl, test_path)
    ref = cand_dir / "reference_solution.py"
    try:
        proc = subprocess.run(
            [sys.executable, str(test_path), "--candidate", str(ref)],
            cwd=cand_dir, capture_output=True, text=True, timeout=60,
            env=candidate_env(),
        )
    except subprocess.TimeoutExpired:
        return {"stage": "stage3", "ok": False, "reason": "timeout_60s"}
    out = proc.stdout
    pass_score = "PASS:SCORE:1.0" in out
    return {
        "stage": "stage3",
        "ok": pass_score,
        "rc": proc.returncode,
        "stdout": out,
        "stderr_tail": proc.stderr[-300:],
        "pass_score_1": pass_score,
    }


def stage2_host_pytest_env(cand_dir: Path) -> dict:
    """In-process Stage 2/3 for agent_env_synth: run reference_solution.py
    in the candidate dir (cwd=cand_dir, so `from package.<m> import ...`
    resolves), then run pytest test_script.py.

    NOT Docker-isolated. Safe for synthesis-time validation because we
    control the LLM's output; NOT safe for eval-time (untrusted candidate
    code). The eval driver still needs Docker; this is for vibe-check /
    Step A self-check only.
    """
    ref = cand_dir / "reference_solution.py"
    test_path = cand_dir / "test_script.py"
    record = {
        "stage": "stage2_host_pytest",
        "ok": False,
        "ref_rc": None,
        "ref_stdout_tail": "",
        "ref_stderr_tail": "",
        "pytest_rc": None,
        "pytest_passed": None,
        "pytest_failed": None,
        "pytest_summary_tail": "",
    }
    if not ref.exists():
        record["reason"] = "no_reference_solution"
        return record
    if not test_path.exists():
        record["reason"] = "no_test_script"
        return record

    try:
        ref_proc = subprocess.run(
            [sys.executable, "reference_solution.py"],
            cwd=cand_dir, capture_output=True, text=True, timeout=60,
            env=candidate_env(),
        )
    except subprocess.TimeoutExpired:
        record["reason"] = "ref_timeout_60s"
        return record
    record["ref_rc"] = ref_proc.returncode
    record["ref_stdout_tail"] = ref_proc.stdout[-300:]
    record["ref_stderr_tail"] = ref_proc.stderr[-300:]
    if ref_proc.returncode != 0:
        record["reason"] = "ref_nonzero_exit"
        return record

    try:
        test_proc = subprocess.run(
            [sys.executable, "-m", "pytest", "test_script.py", "--tb=short", "-q"],
            cwd=cand_dir, capture_output=True, text=True, timeout=60,
            env=candidate_env(),
        )
    except subprocess.TimeoutExpired:
        record["reason"] = "pytest_timeout_60s"
        return record
    record["pytest_rc"] = test_proc.returncode
    out = test_proc.stdout + test_proc.stderr
    record["pytest_summary_tail"] = out[-400:]
    import re as _re
    m = _re.search(r"(\d+) passed", out)
    record["pytest_passed"] = int(m.group(1)) if m else 0
    m = _re.search(r"(\d+) failed", out)
    record["pytest_failed"] = int(m.group(1)) if m else 0
    record["ok"] = test_proc.returncode == 0 and record["pytest_passed"] >= 1
    return record


def stage1_static_audit_env(cand_dir: Path) -> dict:
    """Cheap static audit for agent_env_synth Stage 1 output (no Docker
    build, no execution): check expected files exist, ref imports from
    package, package modules parse, test_script has ≥ 2 test_* funcs."""
    findings = []
    must_have_paths = [
        "task.md", "SKILL.md", "reference_solution.py", "test_script.py",
        "scenario.yaml", "environment/Dockerfile", "environment/requirements.txt",
        "package/__init__.py",
    ]
    for p in must_have_paths:
        if not (cand_dir / p).exists():
            findings.append(f"missing:{p}")

    pkg_dir = cand_dir / "package"
    if pkg_dir.exists():
        modules = sorted(p.name for p in pkg_dir.glob("*.py") if p.name != "__init__.py")
        if not modules:
            findings.append("package_has_no_modules")
    else:
        modules = []

    data_dir = cand_dir / "data"
    if not data_dir.exists() or not any(data_dir.iterdir()):
        findings.append("data_dir_empty_or_missing")

    ref = cand_dir / "reference_solution.py"
    if ref.exists():
        ref_text = ref.read_text(encoding="utf-8", errors="replace")
        if "from package" not in ref_text and "import package" not in ref_text:
            findings.append("ref_does_not_import_package")
        try:
            import ast
            ast.parse(ref_text)
        except SyntaxError as e:
            findings.append(f"ref_syntax_error:{e}")

    test_path = cand_dir / "test_script.py"
    test_count = 0
    if test_path.exists():
        test_text = test_path.read_text(encoding="utf-8", errors="replace")
        try:
            import ast
            tree = ast.parse(test_text)
            test_count = sum(
                1 for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
            )
            if test_count < 2:
                findings.append(f"test_script_has_only_{test_count}_test_funcs")
        except SyntaxError as e:
            findings.append(f"test_syntax_error:{e}")

    for mod in modules:
        try:
            import ast
            ast.parse((pkg_dir / mod).read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as e:
            findings.append(f"package_module_{mod}_syntax_error:{e}")

    return {
        "stage": "stage1_audit",
        "ok": len(findings) == 0,
        "findings": findings,
        "package_modules": modules,
        "test_func_count": test_count,
    }


def run_one(client, family: str, domain: str, idea: str, idx: int,
            stage1_model: str) -> dict:
    scenario_id = f"{ID_PREFIX[family]}{idx:04d}"
    cand_dir = CANDIDATES_DIR / family / scenario_id
    if cand_dir.exists():
        shutil.rmtree(cand_dir)

    record = {
        "scenario_id": scenario_id,
        "family": family,
        "domain": domain,
        "task_idea": idea,
        "stages": [],
        "verdict": "pending",
    }
    try:
        s1 = stage1(client, family, domain, idea, scenario_id, cand_dir,
                    model=stage1_model)
        record["stages"].append(s1)
        files_ok = ("reference_solution.py" in s1["files_written"]
                    and "task.md" in s1["files_written"]
                    and "SKILL.md" in s1["files_written"]
                    and "scenario.yaml" in s1["files_written"])
        if not files_ok:
            record["verdict"] = "stage1_missing_required_files"
            return record

        if family in ("rule_following", "math_reasoning"):
            s2 = stage2_text_short(cand_dir)
            record["stages"].append(s2)
            if not s2["ok"]:
                record["verdict"] = "stage2_ref_did_not_emit_answer"
                return record
            s3 = stage3_text_short(family, cand_dir)
            record["stages"].append(s3)
            record["verdict"] = "pass" if s3["ok"] else "stage3_oracle_fail_on_ref"
        else:
            audit = stage1_static_audit_env(cand_dir)
            record["stages"].append(audit)
            if not audit["ok"]:
                record["verdict"] = "stage1_audit_fail"
                return record
            host = stage2_host_pytest_env(cand_dir)
            record["stages"].append(host)
            record["verdict"] = "pass" if host["ok"] else "stage2_host_pytest_fail"
    except Exception:
        record["error"] = traceback.format_exc()
        record["verdict"] = "exception"
    finally:
        (cand_dir / "_trace.json").write_text(
            json.dumps(record, indent=2, default=str)
        ) if cand_dir.exists() else None
    return record


def make_client() -> GeminiClient:
    return GeminiClient()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", choices=FAMILIES, default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--stage1-model",
                    default=os.environ.get("STAGE1_MODEL", "gemini-3.1-pro-preview"))
    ap.add_argument("--summary-out", default=str(HERE / "summary.json"))
    args = ap.parse_args()

    if not args.family and not args.all:
        ap.error("specify --family <name> or --all")

    fams = FAMILIES if args.all else [args.family]
    client = make_client()

    overall = {"started": time.strftime("%Y-%m-%d %H:%M:%S"),
               "stage1_model": args.stage1_model,
               "results": {}}
    for family in fams:
        seeds = load_seeds(family)[: args.n]
        if len(seeds) < args.n:
            print(f"[{family}] WARNING: only {len(seeds)} seeds available "
                  f"(requested {args.n})", file=sys.stderr)
        family_records = []
        print(f"\n=== {family}: {len(seeds)} candidates ===")
        for i, (domain, idea) in enumerate(seeds, start=1):
            print(f"  [{i}/{len(seeds)}] domain={domain!r} idea={idea[:80]!r}")
            rec = run_one(client, family, domain, idea, i,
                          stage1_model=args.stage1_model)
            tail_msg = rec["verdict"]
            if rec["stages"]:
                last = rec["stages"][-1]
                if "findings" in last and last["findings"]:
                    tail_msg += f" findings={last['findings']}"
            print(f"      → {tail_msg}")
            family_records.append(rec)
        overall["results"][family] = family_records
        passed = sum(1 for r in family_records if r["verdict"] == "pass")
        print(f"  {family}: {passed}/{len(family_records)} pass")

    Path(args.summary_out).write_text(json.dumps(overall, indent=2, default=str))
    print(f"\nSummary written to: {args.summary_out}")

    print("\n=== overall ===")
    for fam, recs in overall["results"].items():
        passed = sum(1 for r in recs if r["verdict"] == "pass")
        print(f"  {fam}: {passed}/{len(recs)} pass")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Mini-calibration: Pro no_context k-trial strong-anchor probe.

For each candidate in {rule_following, math_reasoning, agent_env_synth}:
  - render a no_context prompt (task.md only — NO SKILL.md, NO ref)
  - call the ANCHOR_MODEL k times (temperature 0.5 to spread difficulty signal)
  - grade each trial via the family's natural oracle
  - classify by true 3-band policy with GENUINE REJECTION:

  True-rejection policy:
    n_pass == k          → trivially_easy_REJECT   (Pro always solves → too easy)
    n_pass == 0          → likely_broken_ISOLATE    (Pro never solves → bad task or reasoning-hard keep)
    1 <= n_pass <= k-1   → discriminating_KEEP      (← what we want)

  WHY change from flash-lite to Pro:
    flash-lite 0/3 does NOT mean Pro cannot solve it. The v3 pilot's
    MINI_CAL_REPORT binary distribution (0/3 or 3/3) is flash-lite's weakness
    ceiling, not the task difficulty. The v2.5 synth 88% pass rate was produced
    under exactly this anchor. We must use Pro to get real difficulty signal.

Output:
  - per-candidate: candidates/<family>/<id>/_calibration/trial_<n>_*.{txt,py}
                   + _calibration/summary.json
  - top-level:     mini_calibration.json

Usage:
    python mini_calibration.py --all
    python mini_calibration.py --family math_reasoning
    python mini_calibration.py --family math_reasoning --anchor-model gemini-3.1-pro-preview
    python mini_calibration.py --candidates-root candidates_sf/  # for solution-first output
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from fractions import Fraction
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
CAND_ROOT = HERE / "candidates"

# The LLM client and file-block helpers are vendored beside this module so the
# pipeline runs from a clean checkout with no path outside the repository.
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from llm_client import GeminiClient  # noqa: E402
from utils import candidate_env  # noqa: E402

ANSWER_RE = re.compile(r"^ANSWER:\s*([^\n\r]+?)\s*$", re.MULTILINE | re.IGNORECASE)
CODE_FENCE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)

# ── Anchor model config ────────────────────────────────────────────────────
# Changed from flash-lite to Pro.
# flash-lite 0/3 ≠ Pro cannot solve → the old anchor produced a binary
# distribution that masked that synth tasks were trivially easy for Pro.
ANCHOR_MODEL = os.environ.get("MINI_CAL_MODEL", "gemini-3.1-pro-preview")
# Keep flash alias for backwards compat with old callers that set MINI_CAL_MODEL
FLASH_MODEL = ANCHOR_MODEL  # intentional alias — both point to the anchor
N_TRIALS = int(os.environ.get("MINI_CAL_TRIALS", "3"))
TEMPERATURE = 0.5
MAX_TOKENS_TEXT = 1024   # Pro needs more tokens for richer CoT
MAX_TOKENS_CODE = 4096


def make_client() -> GeminiClient:
    return GeminiClient()


def _unbool(x):
    if isinstance(x, bool):
        return "yes" if x else "no"
    return str(x).strip()


def _strip_code_fence(text: str) -> str:
    """If the response is wrapped in ```python ... ``` fences, extract the code.
    Otherwise return the text unchanged. Useful for agent_env Flash output."""
    m = CODE_FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


# ───────────────────── text-short-answer families ─────────────────────

NO_CTX_TEXT_PROMPT = """You will read a task description below and produce \
an answer in the requested output schema. Output EXACTLY two lines:

    ANSWER: <your answer in the format declared by the task>
    ANALYSIS: <one short paragraph explaining your reasoning, ≤ 80 words>

You have NO additional reference material. Reason from the task statement only.

# Task

{task_md}
"""


def grade_rule_following(scen: dict, raw_text: str) -> dict:
    expected = _unbool(scen.get("expected_answer", ""))
    answer_space = [_unbool(x) for x in scen.get("answer_space", [])]
    m = ANSWER_RE.search(raw_text)
    if not m:
        return {"solved": False, "reason": "no_ANSWER_line",
                "got": None, "expected": expected}
    got = _unbool(m.group(1))
    if got.lower() not in [a.lower() for a in answer_space]:
        return {"solved": False, "reason": "out_of_space",
                "got": got, "expected": expected, "space": answer_space}
    solved = got.lower() == expected.lower()
    return {"solved": solved, "reason": "match" if solved else "wrong_value",
            "got": got, "expected": expected}


def _canon_math(raw: str, fmt: str, answer_space):
    s = raw.strip()
    if fmt == "integer":
        try:
            return str(int(s)), None
        except ValueError:
            return s, "not_integer"
    if fmt == "fraction":
        s_clean = s.replace(" ", "")
        try:
            f = Fraction(s_clean)
            if f.denominator == 1:
                return str(f.numerator), None
            return f"{f.numerator}/{f.denominator}", None
        except (ValueError, ZeroDivisionError):
            return s, "not_fraction"
    if fmt in ("pair_int", "tuple_int"):
        s_clean = s.replace(" ", "").strip("()[]")
        try:
            ints = tuple(int(p) for p in s_clean.split(","))
        except ValueError:
            return s, "not_int_tuple"
        return "(" + ",".join(str(i) for i in ints) + ")", None
    if fmt == "enum":
        space = [_unbool(a).lower() for a in (answer_space or [])]
        if s.lower() not in space:
            return s, "not_in_enum"
        return s.lower(), None
    return s, f"unknown_format:{fmt}"


def grade_math_reasoning(scen: dict, raw_text: str) -> dict:
    expected = _unbool(scen.get("expected_answer", ""))
    fmt = scen.get("answer_format")
    answer_space = scen.get("answer_space")
    m = ANSWER_RE.search(raw_text)
    if not m:
        return {"solved": False, "reason": "no_ANSWER_line",
                "got": None, "expected": expected}
    raw_got = m.group(1).strip()
    canon_got, err1 = _canon_math(raw_got, fmt, answer_space)
    if err1:
        return {"solved": False, "reason": f"canonicalize_failed:{err1}",
                "got_raw": raw_got, "expected": expected}
    canon_exp, err2 = _canon_math(expected, fmt, answer_space)
    if err2:
        return {"solved": False, "reason": f"expected_invalid:{err2}",
                "got_raw": raw_got, "expected": expected}
    return {"solved": canon_got == canon_exp,
            "reason": "match" if canon_got == canon_exp else "wrong_value",
            "got_raw": raw_got, "got_canon": canon_got,
            "expected": expected, "expected_canon": canon_exp}


def calibrate_text_short(family: str, cand_dir: Path, client) -> dict:
    scen = yaml.safe_load((cand_dir / "scenario.yaml").read_text())
    task_md = (cand_dir / "task.md").read_text()
    prompt = NO_CTX_TEXT_PROMPT.format(task_md=task_md)
    cal_dir = cand_dir / "_calibration"
    cal_dir.mkdir(exist_ok=True)
    grader = grade_rule_following if family == "rule_following" else grade_math_reasoning

    # Run all N_TRIALS — no early stop.
    # Always run the full n_trials so the pass count is an unbiased
    # difficulty estimate. Early-stopping on first fail made the
    # old flash-lite anchor artificially binary.
    trials = []
    for i in range(1, N_TRIALS + 1):
        t0 = time.time()
        resp = client.chat(prompt, model=ANCHOR_MODEL,
                           max_tokens=MAX_TOKENS_TEXT,
                           temperature=TEMPERATURE)
        elapsed = round(time.time() - t0, 1)
        (cal_dir / f"trial_{i}_response.txt").write_text(resp.text)
        verdict = grader(scen, resp.text)
        verdict["trial"] = i
        verdict["elapsed_s"] = elapsed
        verdict["in_tokens"] = resp.input_tokens
        verdict["out_tokens"] = resp.output_tokens
        verdict["stop_reason"] = resp.stop_reason
        trials.append(verdict)
    n_solved = sum(1 for t in trials if t["solved"])
    return {"family": family, "anchor_model": ANCHOR_MODEL,
            "n_trials": N_TRIALS, "n_solved": n_solved,
            "band": _band(n_solved), "verdict": _verdict(n_solved),
            "trials": trials}


# ───────────────────── agent_env_synth ─────────────────────

NO_CTX_ENV_PROMPT = """You are a Python coding agent. You must write \
`generated.py` to solve the task below. You have NO design document or \
API reference. You can see only:

  - The task description (task.md)
  - The names of files in the candidate's `package/` and `data/` folders
  - The first few lines of each data file (so you know its format)

You CANNOT see any package source. You must guess reasonable APIs from
the task description and the package module names.

Write a complete, runnable `generated.py` that, when invoked as
`python generated.py` with the candidate root as the working directory,
reads inputs from `./data/` and writes outputs to the path specified in
the task. Use `from package.<module> import <names_you_guess>`.

Output ONLY the Python source, optionally wrapped in a single
```python ... ``` fence. NO commentary outside the fence.

# Task

{task_md}

# Package layout (names only — no source code)

{package_layout}

# Data file peek

{data_peek}
"""


def _build_env_no_ctx_prompt(cand_dir: Path) -> str:
    task_md = (cand_dir / "task.md").read_text()
    pkg_dir = cand_dir / "package"
    if pkg_dir.exists():
        pkg_layout_lines = []
        for p in sorted(pkg_dir.glob("*.py")):
            pkg_layout_lines.append(f"  package/{p.name}")
        package_layout = "\n".join(pkg_layout_lines)
    else:
        package_layout = "  (no package/ directory)"

    data_dir = cand_dir / "data"
    peek_lines = []
    if data_dir.exists():
        for p in sorted(data_dir.iterdir()):
            if not p.is_file():
                continue
            try:
                head = p.read_text(encoding="utf-8", errors="replace")
                head_lines = head.splitlines()[:6]
                peek_lines.append(f"## data/{p.name}")
                peek_lines.append("```")
                peek_lines.extend(head_lines)
                if len(head.splitlines()) > 6:
                    peek_lines.append("... (truncated)")
                peek_lines.append("```")
            except Exception as e:
                peek_lines.append(f"## data/{p.name} (read error: {e})")
    return NO_CTX_ENV_PROMPT.format(
        task_md=task_md,
        package_layout=package_layout,
        data_peek="\n".join(peek_lines) if peek_lines else "(no data files)",
    )


def _run_env_trial(cand_dir: Path, generated_py_text: str,
                   trial_dir: Path) -> dict:
    """Copy candidate dir → trial_dir, replace reference_solution.py with
    Flash's generated.py, then run python generated.py + pytest.

    Returns: {ref_rc, pytest_rc, pytest_passed, pytest_failed, solved}
    """
    if trial_dir.exists():
        shutil.rmtree(trial_dir)
    shutil.copytree(cand_dir, trial_dir,
                    ignore=shutil.ignore_patterns("_calibration",
                                                   "_stage1_raw.txt",
                                                   "_trace.json",
                                                   "output.json"))
    ref_path = trial_dir / "reference_solution.py"
    if ref_path.exists():
        ref_path.unlink()
    (trial_dir / "generated.py").write_text(generated_py_text)

    rec = {
        "generated_rc": None, "generated_stderr_tail": "",
        "pytest_rc": None, "pytest_passed": 0, "pytest_failed": 0,
        "pytest_summary_tail": "",
        "solved": False,
    }
    try:
        gen_proc = subprocess.run(
            [sys.executable, "generated.py"],
            cwd=trial_dir, capture_output=True, text=True, timeout=60,
            env=candidate_env(),
        )
    except subprocess.TimeoutExpired:
        rec["reason"] = "generated_timeout"
        return rec
    rec["generated_rc"] = gen_proc.returncode
    rec["generated_stderr_tail"] = gen_proc.stderr[-300:]
    if gen_proc.returncode != 0:
        rec["reason"] = "generated_nonzero"
        return rec
    try:
        test_proc = subprocess.run(
            [sys.executable, "-m", "pytest", "test_script.py", "--tb=line", "-q"],
            cwd=trial_dir, capture_output=True, text=True, timeout=60,
            env=candidate_env(),
        )
    except subprocess.TimeoutExpired:
        rec["reason"] = "pytest_timeout"
        return rec
    rec["pytest_rc"] = test_proc.returncode
    out = test_proc.stdout + test_proc.stderr
    rec["pytest_summary_tail"] = out[-400:]
    m = re.search(r"(\d+) passed", out)
    rec["pytest_passed"] = int(m.group(1)) if m else 0
    m = re.search(r"(\d+) failed", out)
    rec["pytest_failed"] = int(m.group(1)) if m else 0
    rec["solved"] = (test_proc.returncode == 0
                     and rec["pytest_passed"] >= 1
                     and rec["pytest_failed"] == 0)
    return rec


def calibrate_env(cand_dir: Path, client) -> dict:
    prompt = _build_env_no_ctx_prompt(cand_dir)
    cal_dir = cand_dir / "_calibration"
    cal_dir.mkdir(exist_ok=True)

    # Full N_TRIALS — no early stop (same reason as calibrate_text_short).
    trials = []
    for i in range(1, N_TRIALS + 1):
        t0 = time.time()
        resp = client.chat(prompt, model=ANCHOR_MODEL,
                           max_tokens=MAX_TOKENS_CODE,
                           temperature=TEMPERATURE)
        elapsed = round(time.time() - t0, 1)
        (cal_dir / f"trial_{i}_raw.txt").write_text(resp.text)
        gen_text = _strip_code_fence(resp.text)
        if not gen_text:
            gen_text = resp.text
        (cal_dir / f"trial_{i}_generated.py").write_text(gen_text)
        run_rec = _run_env_trial(cand_dir, gen_text,
                                 cal_dir / f"trial_{i}_run")
        verdict = {
            "trial": i, "elapsed_s": elapsed,
            "in_tokens": resp.input_tokens, "out_tokens": resp.output_tokens,
            "stop_reason": resp.stop_reason,
            **run_rec,
        }
        trials.append(verdict)
    n_solved = sum(1 for t in trials if t.get("solved"))
    return {"family": "agent_env_synth", "anchor_model": ANCHOR_MODEL,
            "n_trials": N_TRIALS, "n_solved": n_solved,
            "band": _band(n_solved), "verdict": _verdict(n_solved),
            "trials": trials}


# ───────────────────── runner ─────────────────────

# Calibration intent (default): this probe is no_context-ONLY. A hard task where
# even the strong anchor fails every trial is a GOOD task (keep it). The only
# thing rejected is a task the anchor always solves (trivially easy). Set
# REJECT_ALL_FAIL=True only when the probe sees full context (then 0/k means the
# task is likely broken). See conversation 2026-05-30 intent clarification.
REJECT_ALL_FAIL = False


def _band(n_solved: int) -> str:
    """Descriptive band label (human-readable, used in reports)."""
    if n_solved == 0:
        return f"0_of_{N_TRIALS}_hard_no_solve"
    if n_solved == N_TRIALS:
        return f"{N_TRIALS}_of_{N_TRIALS}_trivially_easy"
    return f"{n_solved}_of_{N_TRIALS}_discriminating"


def _verdict(n_solved: int) -> str:
    """Verdict for a no_context-only probe.

    Default policy (REJECT_ALL_FAIL=False — matches the stated design intent):
      n_pass == k          → REJECT_trivially_easy   (anchor always solves → too easy)
      n_pass == 0          → KEEP_hard               (anchor never solves → good hard task)
      1 <= n_pass <= k-1   → KEEP_discriminating     (anchor sometimes solves → ideal)

    Strict policy (REJECT_ALL_FAIL=True — only for FULL-context probes):
    n_pass == 0 → ISOLATE_likely_broken instead of KEEP_hard.
    Soundness of 0/k tasks is guaranteed upstream by the structural gates
    (constant consistency, non-vacuous deliverables, leak guard) + ref/oracle
    self-consistency, NOT by this no_context probe.
    """
    if n_solved == N_TRIALS:
        return "REJECT_trivially_easy"
    if n_solved == 0:
        return "ISOLATE_likely_broken" if REJECT_ALL_FAIL else "KEEP_hard"
    return "KEEP_discriminating"


def calibrate_one(family: str, cand_dir: Path, client) -> dict:
    if family in ("rule_following", "math_reasoning"):
        return calibrate_text_short(family, cand_dir, client)
    if family == "agent_env_synth":
        return calibrate_env(cand_dir, client)
    raise ValueError(f"unknown family {family}")


def main():
    # Declare globals at the top before any reference to them in this function
    global ANCHOR_MODEL, FLASH_MODEL, N_TRIALS, REJECT_ALL_FAIL

    ap = argparse.ArgumentParser(description="LongWoF-Bench strong-anchor calibration (Pro, true-rejection)")
    ap.add_argument("--family", choices=["rule_following", "math_reasoning",
                                          "agent_env_synth"], default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--summary-out", default=str(HERE / "mini_calibration.json"))
    # Support candidates from either single-shot or solution-first pipeline
    ap.add_argument("--candidates-root", default=None,
                    help="Override default candidates root dir (e.g. candidates_sf/ for solution-first output)")
    ap.add_argument("--anchor-model", default=None,
                    help="Override anchor model (default: ANCHOR_MODEL env var or gemini-3.1-pro-preview)")
    ap.add_argument("--n-trials", type=int, default=None,
                    help="Override n_trials per candidate (default: 3)")
    ap.add_argument("--reject-all-fail", action="store_true",
                    help="STRICT policy: treat 0/k as ISOLATE_likely_broken instead of "
                         "KEEP_hard. Only use for FULL-context probes. Default (no_context "
                         "probe): 0/k is a good hard task and is KEPT.")
    args = ap.parse_args()

    if not args.family and not args.all:
        ap.error("specify --family or --all")
    families = (["rule_following", "math_reasoning", "agent_env_synth"]
                if args.all else [args.family])

    # Apply command-line overrides to module-level config
    if args.anchor_model:
        ANCHOR_MODEL = args.anchor_model
        FLASH_MODEL = args.anchor_model
    if args.n_trials:
        N_TRIALS = args.n_trials
    REJECT_ALL_FAIL = bool(args.reject_all_fail)

    cand_root = Path(args.candidates_root) if args.candidates_root else CAND_ROOT

    client = make_client()
    overall = {
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "anchor_model": ANCHOR_MODEL,
        "n_trials": N_TRIALS,
        "temperature": TEMPERATURE,
        "policy": "true_rejection_v3sf (trivially_easy→REJECT, middle_band→KEEP, likely_broken→ISOLATE)",
        "results": {},
    }

    for family in families:
        fam_dir = cand_root / family
        if not fam_dir.exists():
            print(f"[{family}] no candidates dir at {fam_dir}, skip")
            continue
        ids = sorted(d.name for d in fam_dir.iterdir() if d.is_dir())
        print(f"\n=== {family}: {len(ids)} candidates (anchor={ANCHOR_MODEL}) ===")
        fam_records = []
        for cid in ids:
            cand_dir = fam_dir / cid
            try:
                rec = calibrate_one(family, cand_dir, client)
                rec["scenario_id"] = cid
                verdict = rec.get("verdict", "unknown")
                band = rec["band"]
                n_s = rec["n_solved"]
                print(f"  {cid:<10}  {verdict:<30}  n_solved={n_s}/{N_TRIALS}  [{band}]")
            except Exception:
                rec = {"scenario_id": cid, "family": family,
                       "error": traceback.format_exc(),
                       "band": "exception", "verdict": "CRASH", "n_solved": -1}
                print(f"  {cid:<10}  CRASH: {traceback.format_exc().splitlines()[-1]}")
            fam_records.append(rec)
            cal_dir = cand_dir / "_calibration"
            cal_dir.mkdir(exist_ok=True)
            (cal_dir / "summary.json").write_text(
                json.dumps(rec, indent=2, default=str)
            )
        overall["results"][family] = fam_records

        n_disc = sum(1 for r in fam_records if r.get("verdict") == "KEEP_discriminating")
        n_hard = sum(1 for r in fam_records if r.get("verdict") == "KEEP_hard")
        n_keep = n_disc + n_hard
        n_reject = sum(1 for r in fam_records if r.get("verdict") == "REJECT_trivially_easy")
        n_isolate = sum(1 for r in fam_records if r.get("verdict") == "ISOLATE_likely_broken")
        n_crash = sum(1 for r in fam_records if r.get("verdict") == "CRASH")
        total = len(fam_records)
        print(f"\n  {family} summary (anchor={ANCHOR_MODEL}, no_context probe):")
        print(f"    KEEP total:            {n_keep}/{total} ({100*n_keep//max(total,1)}%)")
        print(f"      ├ KEEP_discriminating (sometimes solved): {n_disc}/{total}")
        print(f"      └ KEEP_hard (never solved, good hard task): {n_hard}/{total}")
        print(f"    REJECT_trivially_easy: {n_reject}/{total}")
        if n_isolate:
            print(f"    ISOLATE_likely_broken: {n_isolate}/{total}  ← (strict policy)")
        print(f"    CRASH:                 {n_crash}/{total}")
        if n_reject > 0:
            print(f"  ⚠ {n_reject} task(s) trivially easy for anchor → discard from pool")

    Path(args.summary_out).write_text(json.dumps(overall, indent=2, default=str))
    print(f"\nSummary written to: {args.summary_out}")
    print("\n=== overall KEEP yield ===")
    for fam, recs in overall["results"].items():
        n_keep = sum(1 for r in recs if str(r.get("verdict", "")).startswith("KEEP"))
        n_reject = sum(1 for r in recs if r.get("verdict") == "REJECT_trivially_easy")
        print(f"  {fam:<22}  KEEP={n_keep}/{len(recs)}  REJECT(easy)={n_reject}/{len(recs)}")

    print("\nGo signal: a family is Phase-B ready if KEEP yield >= 40% with Pro anchor.")
    print("  (vs old flash-lite threshold of 60% which was too weak an anchor)")


if __name__ == "__main__":
    main()

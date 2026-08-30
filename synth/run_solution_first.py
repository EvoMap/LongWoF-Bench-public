#!/usr/bin/env python3
"""LongWoF-Bench — solution-first multi-step driver.

Implements the S1-S6 pipeline documented in synth/README.md.
Converts run_sample.py's single-shot stage1() into six ordered LLM calls,
each with its own gate, retry budget, and trace record.

Pipeline per candidate:
  S1 design skeleton    → _design.json          (D1/D2/D3 hard constraints)
  S2 reference solution → reference_solution.py  (computes answer; verified)
  S3 gold standard      → _gold.json / _fixture_manifest.json
  S4 task back-synthesis → task.md              (hides chain steps + conventions)
  S5 oracle + bad sols  → test_script.py + _bad_solutions/
  S6 SKILL              → SKILL.md              (reveals all conventions)
  scenario.yaml         → written after S2 with gold answer

Calibration (S7) is handled separately by mini_calibration.py with Pro anchor.

Usage:
    python run_solution_first.py --family math_reasoning --n 5
    python run_solution_first.py --all --n 10 --max-retries 3
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
SHAPES_DIR = HERE / "shapes"
CANDIDATES_DIR = HERE / "candidates_sf"  # separate from single-shot candidates/

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

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

class PipelineFail(Exception):
    def __init__(self, gate: str, reason: str):
        super().__init__(f"{gate}: {reason}")
        self.gate = gate
        self.reason = reason


class Trace:
    """Per-candidate trace log, flushed to disk on every event."""

    def __init__(self, cand_dir: Path):
        self.path = cand_dir / "_trace_sf.json"
        self.events: list[dict[str, Any]] = []
        self.t0 = time.time()

    def log(self, event: str, **fields: Any) -> None:
        rec = {"t": round(time.time() - self.t0, 2), "event": event, **fields}
        self.events.append(rec)
        try:
            self.path.write_text(json.dumps(self.events, indent=2, ensure_ascii=False))
        except OSError:
            pass

    def log_llm(self, stage: str, resp: LLMResponse) -> None:
        self.log(
            f"llm.{stage}",
            in_tokens=resp.input_tokens,
            out_tokens=resp.output_tokens,
            latency_s=getattr(resp, "latency_s", None),
            stop_reason=resp.stop_reason,
            text_chars=len(resp.text or ""),
        )


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


def load_prompt(family: str, name: str) -> str:
    path = SHAPES_DIR / family / "prompts" / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found: {path}")
    return path.read_text(encoding="utf-8")


def render_prompt(template: str, **kw: str) -> str:
    out = template
    for k, v in kw.items():
        out = out.replace("{" + k + "}", v)
    return out


def call_llm(
    client: GeminiClient, prompt: str, *, model: str, max_tokens: int, temperature: float = 0.3
) -> LLMResponse:
    return client.chat(prompt, max_tokens=max_tokens, temperature=temperature, model=model)


def parse_single_file(resp_text: str, filename: str) -> str:
    """Extract one file from LLM response. Falls back to first file or code block."""
    files = parse_file_blocks(resp_text)
    if filename in files:
        return files[filename]
    if len(files) == 1:
        return next(iter(files.values()))
    # Try plain code block
    m = re.search(r"```(?:python|json|yaml|markdown)?\s*\n(.*?)```", resp_text, re.DOTALL)
    if m:
        return m.group(1)
    raise PipelineFail("Gate_parse_missing_file", f"missing {filename!r} in response")


def write_file_safe(cand_dir: Path, relpath: str, content: str) -> None:
    try:
        p = _safe_join(cand_dir, relpath)
    except ValueError as e:
        raise PipelineFail("Gate_unsafe_path", str(e))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def run_script(script: Path, cwd: Path, timeout: int = 60, extra_args: list[str] | None = None) -> tuple[int, str, str]:
    cmd = [sys.executable, str(script)] + (extra_args or [])
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                              check=False, env=candidate_env())
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"TIMEOUT after {timeout}s"


def check_python_syntax(code: str) -> Optional[str]:
    try:
        ast.parse(code)
        return None
    except SyntaxError as e:
        return f"SyntaxError: {e}"


def extract_signatures(py_source: str) -> str:
    """Extract function/class signatures from Python source for prompt injection."""
    lines = py_source.splitlines()
    sigs = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("def ") or stripped.startswith("class ") or stripped.startswith("async def "):
            sigs.append(stripped.rstrip(":"))
    return "\n".join(sigs) if sigs else "(no signatures extracted)"


ANSWER_RE = re.compile(r"^ANSWER:\s*([^\n\r]+?)\s*$", re.MULTILINE | re.IGNORECASE)


def _normalize_digits(s: str) -> str:
    """Strip thousands separators / underscores / spaces so '5,978,712,000' and
    '5978712000' compare equal."""
    return re.sub(r"[,_\s]", "", s)


def numeric_input_constants(design: dict[str, Any]) -> dict[str, int | float]:
    """Extract the numeric entries of design.input_constants (ignore the `note`
    prose key and any non-numeric values). These are the authoritative problem
    constants that must flow S1 -> S2 (ref) -> S4 (task) unchanged."""
    out: dict[str, int | float] = {}
    ic = design.get("input_constants")
    if not isinstance(ic, dict):
        return out
    for k, v in ic.items():
        if k == "note":
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out[str(k)] = v
    return out


def missing_constants_in(text: str, consts: dict[str, int | float]) -> list[str]:
    """Return the constant values (as strings) that do NOT appear in `text`
    (digit-normalized). Used to verify ref/task echo the authoritative inputs."""
    norm = _normalize_digits(text)
    missing = []
    for name, val in consts.items():
        needle = _normalize_digits(str(int(val) if isinstance(val, float) and val.is_integer() else val))
        if needle not in norm:
            missing.append(f"{name}={val}")
    return missing

# ─────────────────────────────────────────────────────────────────────────────
# S1: Design Skeleton
# ─────────────────────────────────────────────────────────────────────────────

def _max_chain_depth(chain: list[dict[str, Any]]) -> int:
    """Longest dependency path in derivation_chain / rule_chain (depth, not breadth)."""
    by_id: dict[str, list[str]] = {
        s.get("step_id", ""): [d for d in s.get("depends_on", []) if isinstance(d, str)]
        for s in chain
        if isinstance(s, dict) and s.get("step_id")
    }
    memo: dict[str, int] = {}

    def _depth(sid: str, seen: frozenset[str] = frozenset()) -> int:
        if sid in seen:
            return 0
        if sid in memo:
            return memo[sid]
        deps = by_id.get(sid, [])
        d = 1 + max((_depth(d, seen | {sid}) for d in deps if d in by_id), default=0)
        memo[sid] = d
        return d

    if not by_id:
        return 0
    return max(_depth(sid) for sid in by_id)


def validate_design(family: str, design: dict[str, Any], min_chain: int, min_hidden: int, min_deliverables: int) -> None:
    # Choose chain key
    chain_key = "rule_chain" if family == "rule_following" else "derivation_chain"
    chain = design.get(chain_key) or design.get("derivation_chain") or []
    hidden = design.get("hidden_conventions") or []

    if not isinstance(chain, list) or len(chain) < min_chain:
        raise PipelineFail("GateS1_chain_too_short", f"{chain_key}={len(chain)} < {min_chain}")

    depth = _max_chain_depth(chain)
    if depth < min_chain:
        raise PipelineFail(
            "GateS1_chain_too_shallow",
            f"max chain depth={depth} < {min_chain}. All steps are parallel (depends_on []). "
            "Add sequential depends_on links.",
        )

    if not isinstance(hidden, list) or len(hidden) < min_hidden:
        raise PipelineFail("GateS1_hidden_too_few", f"hidden_conventions={len(hidden)} < {min_hidden}")

    for idx, item in enumerate(hidden):
        if not isinstance(item, dict):
            raise PipelineFail("GateS1_hidden_schema", f"hidden_conventions[{idx}] not a dict")
        if not str(item.get("detail_for_oracle", "")).strip():
            raise PipelineFail("GateS1_hidden_detail", f"hidden_conventions[{idx}].detail_for_oracle empty")
        if not str(item.get("recoverability_reason", "")).strip():
            raise PipelineFail("GateS1_hidden_recover", f"hidden_conventions[{idx}].recoverability_reason empty")

    # math_reasoning: answer_format must be present and valid
    if family == "math_reasoning":
        valid_fmts = {"integer", "fraction", "pair_int", "tuple_int", "enum"}
        af = design.get("answer_format")
        if not af:
            raise PipelineFail(
                "GateS1_math_no_answer_format",
                "math design missing answer_format; must be one of: " + ", ".join(sorted(valid_fmts)),
            )
        if str(af) not in valid_fmts:
            raise PipelineFail(
                "GateS1_math_bad_answer_format",
                f"answer_format={af!r} not in {valid_fmts}",
            )
        # Single source of truth for problem constants:
        # input_constants flow S1 -> S2 (ref hardcodes) -> S4 (task states),
        # checked at each step. Prevents the modulus-drift bug (M0001).
        consts = numeric_input_constants(design)
        if not consts:
            raise PipelineFail(
                "GateS1_math_no_input_constants",
                "math design must declare a non-empty input_constants object with at least "
                "one numeric value (the authoritative problem inputs)",
            )

    # For env family, check deliverables and io_contract
    if family == "agent_env_synth":
        deliverables = design.get("deliverables") or []
        if not isinstance(deliverables, list) or len(deliverables) < min_deliverables:
            raise PipelineFail("GateS1_deliverables_too_few", f"deliverables={len(deliverables)} < {min_deliverables}")
        io = design.get("io_contract")
        if not isinstance(io, dict):
            raise PipelineFail("GateS1_io_contract_missing", "io_contract missing or not a dict")
        inv = str(io.get("invocation", ""))
        if "--input" not in inv or "--output" not in inv:
            raise PipelineFail("GateS1_io_contract_invocation", f"invocation must have --input/--output, got: {inv!r}")
        out_files = io.get("output_files") or []
        if len(out_files) != len(deliverables):
            raise PipelineFail(
                "GateS1_io_mismatch",
                f"io_contract.output_files={len(out_files)} != deliverables={len(deliverables)}",
            )

    # For rule: check expected_answer in answer_space
    if family == "rule_following":
        expected = design.get("expected_answer")
        answer_space = design.get("answer_space") or []
        if not expected:
            raise PipelineFail("GateS1_rule_no_expected", "expected_answer missing from design")
        if answer_space and str(expected) not in [str(a) for a in answer_space]:
            raise PipelineFail(
                "GateS1_rule_expected_not_in_space",
                f"expected_answer={expected!r} not in answer_space={answer_space}",
            )
        predicted = design.get("predicted_no_context_answer")
        if predicted and str(predicted) == str(expected):
            raise PipelineFail(
                "GateS1_rule_no_trap",
                "predicted_no_context_answer == expected_answer; scenario has no trap",
            )


def stage_s1_design(
    client: GeminiClient,
    family: str,
    domain: str,
    task_idea: str,
    candidate_id: str,
    cand_dir: Path,
    trace: Trace,
    model: str,
    min_chain: int,
    min_hidden: int,
    min_deliverables: int,
    max_retries: int,
) -> dict[str, Any]:
    template = load_prompt(family, "1_design.md")
    prompt = render_prompt(template, domain=domain, task_idea=task_idea, candidate_id=candidate_id)
    last_err = "never attempted"

    for attempt in range(1, max_retries + 1):
        resp = call_llm(client, prompt, model=model, max_tokens=32000, temperature=0.3)
        trace.log_llm(f"s1_design.attempt{attempt}", resp)
        if not resp.text or resp.stop_reason == "MAX_TOKENS":
            last_err = f"empty or truncated (stop={resp.stop_reason})"
            trace.log("s1_design.skip", attempt=attempt, error=last_err)
            continue
        try:
            raw_json = parse_single_file(resp.text, "_design.json")
            design = json.loads(raw_json)
            validate_design(family, design, min_chain, min_hidden, min_deliverables)
            write_file_safe(cand_dir, "_design.json", json.dumps(design, indent=2, ensure_ascii=False))
            trace.log("s1_design.pass", attempt=attempt,
                      chain_len=len(design.get("derivation_chain") or design.get("rule_chain") or []),
                      hidden=len(design.get("hidden_conventions") or []))
            return design
        except (json.JSONDecodeError, PipelineFail, ValueError) as e:
            last_err = str(e)
            trace.log("s1_design.fail", attempt=attempt, error=last_err)

    raise PipelineFail("GateS1_unresolvable", last_err)


# ─────────────────────────────────────────────────────────────────────────────
# S2: Reference Solution
# ─────────────────────────────────────────────────────────────────────────────

def validate_reference(family: str, ref_text: str, min_helpers: int,
                        cand_dir: Optional[Path] = None,
                        design: Optional[dict[str, Any]] = None) -> Optional[str]:
    err = check_python_syntax(ref_text)
    if err:
        return err

    # math: the reference must hardcode the authoritative input_constants
    # (single source of truth). Catches S2 inventing a different modulus/dim.
    if family == "math_reasoning" and design is not None:
        miss = missing_constants_in(ref_text, numeric_input_constants(design))
        if miss:
            return (
                f"reference_solution.py does not use input_constants {miss}; "
                "ref must hardcode the exact authoritative values from _design.json"
            )

    if family == "agent_env_synth":
        # For env, the derivation-chain helpers live in package/*.py, not in
        # reference_solution.py (which is just a thin --input/--output orchestrator).
        # Count FunctionDefs across all package modules written so far.
        if "--input" not in ref_text or "--output" not in ref_text:
            return "reference_solution.py for agent_env must use --input/--output argparse args"
        if cand_dir is not None:
            pkg_dir = cand_dir / "package"
            total_pkg_fns = 0
            for mod in sorted(pkg_dir.glob("*.py")) if pkg_dir.exists() else []:
                try:
                    tree = ast.parse(mod.read_text(encoding="utf-8", errors="replace"))
                    total_pkg_fns += sum(
                        1 for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                    )
                except SyntaxError:
                    pass
            if total_pkg_fns < min_helpers:
                return (
                    f"package/ has only {total_pkg_fns} function(s) across all modules, "
                    f"need >= {min_helpers} (derivation chain helpers go in package/, "
                    "not reference_solution.py)"
                )
        # reference_solution.py itself can have just main() — that's fine for env
        return None

    # math_reasoning / rule_following: helpers must be in reference_solution.py
    # (the whole derivation chain must be auditable there)
    try:
        tree = ast.parse(ref_text)
        fn_names = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        non_main = [n for n in fn_names if n not in ("main", "__main__")]
        if len(non_main) < min_helpers:
            return (
                f"only {len(non_main)} helper function(s) in reference_solution.py, "
                f"need >= {min_helpers} (one per derivation chain step)"
            )
    except Exception:
        pass
    return None


def run_reference_text_short(cand_dir: Path, timeout: int = 30) -> tuple[bool, str]:
    """Run reference_solution.py (no args), return (has_answer_line, stdout)."""
    rc, stdout, stderr = run_script(cand_dir / "reference_solution.py", cand_dir, timeout=timeout)
    if rc == -1:
        return False, f"TIMEOUT: {stderr}"
    if rc != 0:
        return False, f"rc={rc} stderr={stderr[-300:]}"
    has_answer = bool(ANSWER_RE.search(stdout))
    return has_answer, stdout


def stage_s2_reference(
    client: GeminiClient,
    family: str,
    design: dict[str, Any],
    cand_dir: Path,
    trace: Trace,
    model: str,
    min_helpers: int,
    max_retries: int,
) -> str:
    template = load_prompt(family, "2_reference.md")
    prompt = render_prompt(
        template,
        design_json=json.dumps(design, indent=2, ensure_ascii=False),
        candidate_id=design.get("scenario_name", "candidate"),
    )
    last_err = "never attempted"

    for attempt in range(1, max_retries + 1):
        resp = call_llm(client, prompt, model=model, max_tokens=32000, temperature=0.25)
        trace.log_llm(f"s2_reference.attempt{attempt}", resp)
        if not resp.text or resp.stop_reason == "MAX_TOKENS":
            last_err = f"empty or truncated (stop={resp.stop_reason})"
            trace.log("s2_reference.skip", attempt=attempt, error=last_err)
            continue

        try:
            files = parse_file_blocks(resp.text)
        except Exception as e:
            last_err = f"parse_file_blocks: {e}"
            trace.log("s2_reference.parse_fail", attempt=attempt, error=last_err)
            continue

        # For env family, write all emitted files (package/*, etc.)
        if family == "agent_env_synth":
            for relpath, content in files.items():
                try:
                    write_file_safe(cand_dir, relpath, content)
                except PipelineFail:
                    pass
            ref_text = files.get("reference_solution.py", "")
        else:
            ref_text = files.get("reference_solution.py", "")
            if not ref_text and len(files) == 1:
                ref_text = next(iter(files.values()))

        if not ref_text:
            last_err = "reference_solution.py not found in LLM response"
            trace.log("s2_reference.no_ref", attempt=attempt, error=last_err)
            continue

        static_err = validate_reference(family, ref_text, min_helpers, cand_dir=cand_dir, design=design)
        if static_err:
            last_err = f"static check: {static_err}"
            trace.log("s2_reference.static_fail", attempt=attempt, error=last_err)
            continue

        write_file_safe(cand_dir, "reference_solution.py", ref_text)

        # Smoke test
        if family in ("math_reasoning", "rule_following"):
            ok, stdout = run_reference_text_short(cand_dir)
            if not ok:
                last_err = f"ref smoke fail: {stdout[:300]}"
                trace.log("s2_reference.smoke_fail", attempt=attempt, error=last_err)
                continue
            trace.log("s2_reference.pass", attempt=attempt,
                      answer_line=ANSWER_RE.search(stdout).group(0) if ANSWER_RE.search(stdout) else "")
        else:
            # For env: just syntax check (running needs data/ which S3 creates)
            trace.log("s2_reference.pass_static_only", attempt=attempt)

        return ref_text

    raise PipelineFail("GateS2_reference_unwritable", last_err)


# ─────────────────────────────────────────────────────────────────────────────
# S3: Gold Standard
# ─────────────────────────────────────────────────────────────────────────────

def stage_s3_gold_text_short(family: str, cand_dir: Path, trace: Trace,
                             design: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Run reference solution and capture gold answer for math/rule families."""
    ok, stdout = run_reference_text_short(cand_dir, timeout=30)
    if not ok:
        raise PipelineFail("GateS3_ref_failed", f"reference_solution.py failed: {stdout[:300]}")

    m = ANSWER_RE.search(stdout)
    if not m:
        raise PipelineFail("GateS3_no_answer", "reference_solution.py produced no ANSWER: line")

    gold_answer = m.group(1).strip()

    # Verify determinism: run a second time
    ok2, stdout2 = run_reference_text_short(cand_dir, timeout=30)
    m2 = ANSWER_RE.search(stdout2) if ok2 else None
    if not m2 or m2.group(1).strip() != gold_answer:
        raise PipelineFail("GateS3_nondeterministic", "reference_solution.py gives different answers on two runs")

    # Non-vacuous deliverable gate (per sub-item): a composite tuple answer
    # whose components are all equal means the "independent sub-deliverables" carry
    # no independent signal (e.g. M0001 (512,512) from a vacuous constraint, M0004
    # (23,23) from a full-rank-trivial second part). Reject unless the design
    # explicitly declares allow_equal_components.
    if design is not None and not design.get("allow_equal_components"):
        body = gold_answer.strip()
        if body.startswith("(") and "," in body:
            comps = [c.strip() for c in body.strip("()").split(",") if c.strip() != ""]
            if len(comps) >= 2 and len(set(comps)) == 1:
                raise PipelineFail(
                    "GateS3_degenerate_tuple",
                    f"all {len(comps)} answer components are identical ({comps[0]!r}); "
                    "the composite deliverables are not independent (D2 vacuous). "
                    "Redesign so sub-answers differ, or set allow_equal_components=true.",
                )

    gold = {"gold_answer": gold_answer, "stdout": stdout.strip()}
    gold_path = cand_dir / "_gold.json"
    gold_path.write_text(json.dumps(gold, indent=2, ensure_ascii=False))
    trace.log("s3_gold.pass", gold_answer=gold_answer)
    return gold


def stage_s3_fixtures_env(
    client: GeminiClient,
    design: dict[str, Any],
    ref_text: str,
    cand_dir: Path,
    trace: Trace,
    model: str,
    max_retries: int,
    fixture_timeout: int = 120,
) -> dict[str, Any]:
    """Generate data fixtures and gold for agent_env family."""
    template = load_prompt("agent_env_synth", "3_fixtures.md")
    prompt = render_prompt(
        template,
        design_json=json.dumps(design, indent=2, ensure_ascii=False),
        reference_source=ref_text,
        candidate_id=design.get("scenario_name", "candidate"),
    )
    last_err = "never attempted"

    for attempt in range(1, max_retries + 1):
        resp = call_llm(client, prompt, model=model, max_tokens=24000, temperature=0.2)
        trace.log_llm(f"s3_fixtures.attempt{attempt}", resp)
        if not resp.text or resp.stop_reason == "MAX_TOKENS":
            last_err = f"empty or truncated (stop={resp.stop_reason})"
            continue

        try:
            script_text = parse_single_file(resp.text, "_s3_generate_fixtures_and_gold.py")
        except PipelineFail as e:
            last_err = str(e)
            trace.log("s3_fixtures.parse_fail", attempt=attempt, error=last_err)
            continue

        syntax_err = check_python_syntax(script_text)
        if syntax_err:
            last_err = f"syntax: {syntax_err}"
            trace.log("s3_fixtures.syntax_fail", attempt=attempt, error=last_err)
            continue

        script_path = cand_dir / "_s3_generate_fixtures_and_gold.py"
        script_path.write_text(script_text, encoding="utf-8")
        rc, stdout, stderr = run_script(script_path, cand_dir, timeout=fixture_timeout)
        trace.log("s3_fixtures.run", attempt=attempt, rc=rc, stderr_tail=stderr[-200:])

        if rc != 0:
            last_err = f"fixture script rc={rc}: {stderr[-300:]}"
            continue

        manifest_path = cand_dir / "_fixture_manifest.json"
        if not manifest_path.exists():
            last_err = "missing _fixture_manifest.json after S3"
            continue

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            last_err = f"manifest json error: {e}"
            continue

        cases = manifest.get("cases") or []
        if not isinstance(cases, list) or len(cases) < 2:
            last_err = f"fixture cases={len(cases)} < 2"
            continue

        # Verify paths exist
        missing = []
        for case in cases:
            if not isinstance(case, dict):
                continue
            ip = cand_dir / str(case.get("input_path", ""))
            gp = cand_dir / str(case.get("gold_path", ""))
            if not ip.exists():
                missing.append(f"input:{ip.name}")
            if not gp.exists():
                missing.append(f"gold:{gp.name}")
        if missing:
            last_err = f"missing paths: {missing[:6]}"
            continue

        # Convention coverage gate
        required_conventions = {
            str(c.get("name", "")).strip().lower()
            for c in design.get("hidden_conventions", [])
            if isinstance(c, dict) and c.get("name")
        }
        covered = {
            str(c.get("targets_convention", "")).strip().lower()
            for c in cases
            if isinstance(c, dict) and c.get("targets_convention")
        }
        uncovered = required_conventions - covered
        if uncovered:
            last_err = f"adversarial cases missing for conventions: {sorted(uncovered)[:4]}"
            trace.log("s3_fixtures.coverage_fail", attempt=attempt, uncovered=sorted(uncovered))
            continue

        trace.log("s3_fixtures.pass", attempt=attempt, n_cases=len(cases))
        return manifest

    raise PipelineFail("GateS3_fixtures_unwritable", last_err)


def stage_s3_variants_text_short(
    client: GeminiClient,
    family: str,
    design: dict[str, Any],
    ref_text: str,
    gold: dict[str, Any],
    cand_dir: Path,
    trace: Trace,
    model: str,
    max_retries: int,
) -> dict[str, Any]:
    """Generate adversarial variants for math/rule families."""
    template = load_prompt(family, "3_fixtures.md")
    gold_answer = gold.get("gold_answer", "")
    prompt = render_prompt(
        template,
        design_json=json.dumps(design, indent=2, ensure_ascii=False),
        reference_source=ref_text,
        gold_answer=gold_answer,
        candidate_id=design.get("scenario_name", "candidate"),
    )
    last_err = "never attempted"

    for attempt in range(1, max_retries + 1):
        resp = call_llm(client, prompt, model=model, max_tokens=16000, temperature=0.2)
        trace.log_llm(f"s3_variants.attempt{attempt}", resp)
        if not resp.text or resp.stop_reason == "MAX_TOKENS":
            last_err = f"empty or truncated (stop={resp.stop_reason})"
            continue

        try:
            script_text = parse_single_file(resp.text, "_s3_generate_variants.py")
        except PipelineFail as e:
            last_err = str(e)
            continue

        syntax_err = check_python_syntax(script_text)
        if syntax_err:
            last_err = f"syntax: {syntax_err}"
            continue

        script_path = cand_dir / "_s3_generate_variants.py"
        script_path.write_text(script_text, encoding="utf-8")
        rc, stdout, stderr = run_script(script_path, cand_dir, timeout=30)
        if rc != 0:
            last_err = f"variant script rc={rc}: {stderr[-300:]}"
            continue

        variants_path = cand_dir / "_variants.json"
        if not variants_path.exists():
            last_err = "missing _variants.json after S3"
            continue

        try:
            variants = json.loads(variants_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            last_err = f"variants json error: {e}"
            continue

        variant_list = variants.get("variants") or []
        n_hidden = len(design.get("hidden_conventions") or [])
        if len(variant_list) < n_hidden:
            last_err = f"variants={len(variant_list)} < hidden_conventions={n_hidden}"
            continue

        # Check each variant has naive_wrong != correct
        vacuous = [v.get("variant_id", "?") for v in variant_list
                   if isinstance(v, dict) and v.get("naive_wrong_answer") == v.get("correct_answer")]
        if vacuous:
            last_err = f"vacuous variants (naive==correct): {vacuous[:3]}"
            continue

        trace.log("s3_variants.pass", attempt=attempt, n_variants=len(variant_list))
        return variants

    # Variants are non-blocking — log warning but don't fail
    trace.log("s3_variants.skipped", reason=last_err)
    return {"variants": [], "base_answer": gold_answer}


# ─────────────────────────────────────────────────────────────────────────────
# S4: Task Back-Synthesis
# ─────────────────────────────────────────────────────────────────────────────

def validate_task_md(family: str, task_text: str, design: dict[str, Any]) -> Optional[str]:
    low = task_text.lower()

    # D1 leak guard: hidden convention details must not appear verbatim in task
    for item in design.get("hidden_conventions") or []:
        if not isinstance(item, dict):
            continue
        detail = str(item.get("detail_for_oracle", "")).strip().lower()
        if detail and len(detail) >= 12 and detail in low:
            return f"hidden convention detail leaked: {detail[:60]!r}"

    # D3 guard: task must not enumerate derivation chain steps in order
    chain_key = "rule_chain" if family == "rule_following" else "derivation_chain"
    chain = design.get(chain_key) or []
    step_ids = [
        str(s.get("step_id", "")).lower().replace("_", " ")
        for s in chain
        if isinstance(s, dict) and len(str(s.get("step_id", ""))) >= 4
    ]
    found = sum(1 for sid in step_ids if sid in low)
    if len(step_ids) >= 3 and found >= 2:
        return (
            f"task.md enumerates {found}/{len(step_ids)} derivation chain step_ids — "
            "this is a D0 checklist. Describe problem goal, not the algorithm."
        )

    # math: task must state the authoritative input_constants verbatim (single
    # source of truth — catches the M0001 modulus drift where task said one
    # modulus and ref used another).
    if family == "math_reasoning":
        miss = missing_constants_in(task_text, numeric_input_constants(design))
        if miss:
            return (
                f"task.md does not state input_constants {miss}; the task must use the "
                "exact authoritative numbers the reference computes over"
            )
        # Method-leak guard: operation_name_for_task values that are full method
        # statements (contain formula chars) must NOT be quoted into the task.
        for item in design.get("hidden_conventions") or []:
            if not isinstance(item, dict):
                continue
            op = str(item.get("operation_name_for_task", "")).strip()
            if op and any(ch in op for ch in ("≡", "mod ", "^", "=")) and op.lower() in low:
                return (
                    f"task.md quotes a method statement verbatim: {op[:60]!r}. "
                    "Name only the domain goal, never the operation/method."
                )

    # Family-specific checks
    if family == "math_reasoning":
        if "output format" not in low:
            return "task.md missing '## Output Format' section"
    elif family == "rule_following":
        if "question" not in low:
            return "task.md missing '## Question' section"
        if "output format" not in low:
            return "task.md missing '## Output Format' section"
    elif family == "agent_env_synth":
        if "generated.py" not in task_text and "--input" not in task_text:
            return "task.md missing CLI specification with generated.py / --input"

    return None


def stage_s4_task(
    client: GeminiClient,
    family: str,
    design: dict[str, Any],
    ref_text: str,
    gold: dict[str, Any],
    fixture_info: dict[str, Any],
    cand_dir: Path,
    trace: Trace,
    model: str,
    max_retries: int,
) -> str:
    template = load_prompt(family, "4_task.md")
    signatures = extract_signatures(ref_text)
    gold_answer = gold.get("gold_answer", "")

    if family == "agent_env_synth":
        prompt = render_prompt(
            template,
            design_json=json.dumps(design, indent=2, ensure_ascii=False),
            reference_signatures=signatures,
            fixture_manifest=json.dumps(fixture_info, indent=2, ensure_ascii=False),
        )
    elif family == "math_reasoning":
        # The task is reverse-engineered from the VALIDATED ref, and must
        # state the authoritative input_constants verbatim (single source of truth).
        prompt = render_prompt(
            template,
            design_json=json.dumps(design, indent=2, ensure_ascii=False),
            reference_source=ref_text,
            input_constants=json.dumps(numeric_input_constants(design), indent=2, ensure_ascii=False),
            gold_answer=gold_answer,
        )
    else:  # rule_following (unchanged prompt contract)
        prompt = render_prompt(
            template,
            design_json=json.dumps(design, indent=2, ensure_ascii=False),
            reference_signatures=signatures,
            gold_answer=gold_answer,
        )

    last_err = "never attempted"

    for attempt in range(1, max_retries + 1):
        resp = call_llm(client, prompt, model=model, max_tokens=20000, temperature=0.35)
        trace.log_llm(f"s4_task.attempt{attempt}", resp)
        if not resp.text or resp.stop_reason == "MAX_TOKENS":
            last_err = f"empty or truncated (stop={resp.stop_reason})"
            continue

        try:
            task_text = parse_single_file(resp.text, "task.md")
        except PipelineFail as e:
            last_err = str(e)
            continue

        check_err = validate_task_md(family, task_text, design)
        if check_err:
            last_err = check_err
            trace.log("s4_task.gate_fail", attempt=attempt, error=last_err)
            continue

        write_file_safe(cand_dir, "task.md", task_text)
        trace.log("s4_task.pass", attempt=attempt)
        return task_text

    raise PipelineFail("GateS4_task_unwritable", last_err)


# ─────────────────────────────────────────────────────────────────────────────
# S5: Oracle + Bad Solutions
# ─────────────────────────────────────────────────────────────────────────────

def run_bad_solution(sol_path: Path, oracle_path: Path, cand_dir: Path, timeout: int = 30) -> dict[str, Any]:
    """Grade a bad solution via the family oracle.

    Returns {caught, reason} where `caught=True` means the oracle correctly
    rejected the bad solution (no PASS:SCORE:1.0).

    We run the oracle (not just the bad solution) so that canonicalization is
    applied consistently — e.g. a bad sol printing "3/6" for a fraction task
    where gold is "1/2" would look different via raw string compare but the
    oracle would correctly see them as equal and give PASS:SCORE:1.0.
    """
    oracle_result = run_oracle_on_ref(oracle_path, sol_path, cand_dir, timeout=timeout)
    caught = not oracle_result["passed"]
    return {
        "caught": caught,
        "oracle_passed": oracle_result["passed"],
        "oracle_stdout_tail": oracle_result.get("stdout_tail", "")[-200:],
        "reason": "oracle_not_passed" if caught else "oracle_passed_unexpectedly",
    }


def run_oracle_on_ref(oracle_path: Path, ref_path: Path, cand_dir: Path, timeout: int = 60) -> dict[str, Any]:
    """Run the family oracle against the reference solution. Expects PASS:SCORE:1.0."""
    rc, stdout, stderr = run_script(
        oracle_path, cand_dir, timeout=timeout,
        extra_args=["--candidate", str(ref_path)]
    )
    passed = "PASS:SCORE:1.0" in stdout
    return {
        "passed": passed,
        "rc": rc,
        "stdout_tail": stdout[-400:],
        "stderr_tail": stderr[-200:],
    }


def stage_s5_oracle_text_short(
    client: GeminiClient,
    family: str,
    design: dict[str, Any],
    ref_text: str,
    task_text: str,
    gold: dict[str, Any],
    cand_dir: Path,
    trace: Trace,
    model: str,
    max_retries: int,
) -> None:
    """Generate bad solutions and verify they fail the oracle (math/rule families)."""
    # Copy family test_script.py.tmpl into candidate dir
    oracle_tmpl = SHAPES_DIR / family / "test_script.py.tmpl"
    oracle_path = cand_dir / "test_script.py"
    if oracle_tmpl.exists():
        shutil.copy2(oracle_tmpl, oracle_path)
    else:
        raise PipelineFail("GateS5_no_oracle_tmpl", f"missing {oracle_tmpl}")

    # Verify reference passes oracle
    ref_path = cand_dir / "reference_solution.py"
    oracle_result = run_oracle_on_ref(oracle_path, ref_path, cand_dir)
    if not oracle_result["passed"]:
        raise PipelineFail(
            "GateD_ref_fails_oracle",
            f"reference_solution.py does not get PASS:SCORE:1.0 on oracle. "
            f"stdout_tail={oracle_result['stdout_tail']!r}",
        )
    trace.log("s5_oracle.ref_passes", oracle_stdout=oracle_result["stdout_tail"])

    # Generate bad solutions via LLM
    template = load_prompt(family, "5_oracle.md")
    gold_answer = gold.get("gold_answer", "")
    answer_space = design.get("answer_space") or []
    prompt = render_prompt(
        template,
        task_md=task_text,
        design_json=json.dumps(design, indent=2, ensure_ascii=False),
        reference_signatures=extract_signatures(ref_text),
        gold_answer=gold_answer,
        answer_space=str(answer_space),
    )
    last_err = "never attempted"

    for attempt in range(1, max_retries + 1):
        resp = call_llm(client, prompt, model=model, max_tokens=16000, temperature=0.4)
        trace.log_llm(f"s5_badsols.attempt{attempt}", resp)
        if not resp.text or resp.stop_reason == "MAX_TOKENS":
            last_err = f"empty or truncated (stop={resp.stop_reason})"
            continue

        files = parse_file_blocks(resp.text)
        bad_sol_files = {k: v for k, v in files.items() if "_bad_solutions/" in k}
        if not bad_sol_files:
            last_err = "no _bad_solutions/ files in response"
            continue

        bad_sols_dir = cand_dir / "_bad_solutions"
        bad_sols_dir.mkdir(exist_ok=True)

        for relpath, content in bad_sol_files.items():
            syntax_err = check_python_syntax(content)
            if syntax_err:
                continue  # skip syntactically broken bad sols
            try:
                write_file_safe(cand_dir, relpath, content)
            except PipelineFail:
                pass

        # Verify bad solutions actually give wrong answers
        bad_sol_paths = sorted(bad_sols_dir.glob("*.py"))
        if not bad_sol_paths:
            last_err = "no valid bad solution files written"
            continue

        uncaught = []
        for sol_path in bad_sol_paths:
            result = run_bad_solution(sol_path, oracle_path, cand_dir)
            if not result["caught"]:
                # oracle gave PASS:SCORE:1.0 to a bad solution → not caught
                uncaught.append(
                    f"{sol_path.name}: oracle passed (bad sol should have been rejected)"
                )

        if uncaught:
            last_err = f"bad solutions not caught: {uncaught}"
            trace.log("s5_badsols.uncaught", attempt=attempt, uncaught=uncaught)
            continue

        trace.log("s5_oracle.pass", attempt=attempt, n_bad_sols=len(bad_sol_paths))
        return

    # Bad-solution gate is a warning, not hard fail — log and continue
    trace.log("s5_oracle.bad_sols_warn", reason=last_err)


def run_env_oracle_on_ref(cand_dir: Path, timeout: int = 120) -> dict[str, Any]:
    """Hard check that the env reference passes its OWN oracle.

    The oracle drives `generated.py`, which only exists at eval time. To verify
    ref/gold/oracle consistency at synth time we copy reference_solution.py ->
    generated.py, run pytest, and require every test to pass. Temp file and
    pytest/byte caches are cleaned up afterwards.

    This is the env analogue of math/rule's run_oracle_on_ref. It is essential
    because, under the no_context calibration policy (0/k = KEEP_hard), a broken
    env task is no longer caught downstream — a wrong gold/oracle would otherwise
    enter the pool indistinguishable from a genuinely hard task.
    """
    ref = cand_dir / "reference_solution.py"
    gen = cand_dir / "generated.py"
    if not ref.exists():
        return {"passed": False, "reason": "no_reference_solution"}
    shutil.copy2(ref, gen)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "test_script.py", "--tb=short", "-q"],
            cwd=cand_dir, capture_output=True, text=True, timeout=timeout, check=False,
            env=candidate_env(),
        )
        out = proc.stdout + proc.stderr
        n_pass = int(m.group(1)) if (m := re.search(r"(\d+) passed", out)) else 0
        n_fail = int(m.group(1)) if (m := re.search(r"(\d+) failed", out)) else 0
        n_err = int(m.group(1)) if (m := re.search(r"(\d+) error", out)) else 0
        passed = proc.returncode == 0 and n_pass >= 1 and n_fail == 0 and n_err == 0
        return {"passed": passed, "rc": proc.returncode, "n_pass": n_pass,
                "n_fail": n_fail, "n_err": n_err, "stdout_tail": out[-400:]}
    except subprocess.TimeoutExpired:
        return {"passed": False, "reason": f"pytest_timeout_{timeout}s"}
    finally:
        try:
            gen.unlink(missing_ok=True)
        except OSError:
            pass
        for junk in (cand_dir / "__pycache__", cand_dir / ".pytest_cache"):
            shutil.rmtree(junk, ignore_errors=True)


def stage_s5_oracle_env(
    client: GeminiClient,
    design: dict[str, Any],
    ref_text: str,
    task_text: str,
    fixture_manifest: dict[str, Any],
    cand_dir: Path,
    trace: Trace,
    model: str,
    max_retries: int,
) -> str:
    """Generate test_script.py oracle for agent_env family."""
    # Load fixture and gold contents for prompt injection
    fixture_contents: dict[str, dict[str, str]] = {}
    gold_contents: dict[str, dict[str, str]] = {}
    io_input_files = design.get("io_contract", {}).get("input_files") or []
    io_output_files = design.get("io_contract", {}).get("output_files") or []

    for case in fixture_manifest.get("cases") or []:
        if not isinstance(case, dict):
            continue
        cid = str(case.get("case_id", ""))
        in_dir = cand_dir / str(case.get("input_path", ""))
        gold_dir = cand_dir / str(case.get("gold_path", ""))
        fixture_contents[cid] = {}
        gold_contents[cid] = {}
        for f in io_input_files:
            fname = f.get("name", "")
            p = in_dir / fname
            if p.exists():
                fixture_contents[cid][fname] = p.read_text(encoding="utf-8", errors="replace")[:4000]
        for f in io_output_files:
            fname = f.get("name", "")
            p = gold_dir / fname
            if p.exists():
                gold_contents[cid][fname] = p.read_text(encoding="utf-8", errors="replace")[:4000]

    template = load_prompt("agent_env_synth", "5_oracle.md")
    prompt = render_prompt(
        template,
        task_md=task_text,
        design_json=json.dumps(design, indent=2, ensure_ascii=False),
        reference_signatures=extract_signatures(ref_text),
        fixture_manifest=json.dumps(fixture_manifest, indent=2, ensure_ascii=False),
        fixture_contents_json=json.dumps(fixture_contents, indent=2, ensure_ascii=False),
        gold_contents_json=json.dumps(gold_contents, indent=2, ensure_ascii=False),
        candidate_id=design.get("scenario_name", "candidate"),
    )
    last_err = "never attempted"

    for attempt in range(1, max_retries + 1):
        resp = call_llm(client, prompt, model=model, max_tokens=32000, temperature=0.25)
        trace.log_llm(f"s5_env_oracle.attempt{attempt}", resp)
        if not resp.text or resp.stop_reason == "MAX_TOKENS":
            last_err = f"empty or truncated (stop={resp.stop_reason})"
            continue

        try:
            test_text = parse_single_file(resp.text, "test_script.py")
        except PipelineFail as e:
            last_err = str(e)
            continue

        syntax_err = check_python_syntax(test_text)
        if syntax_err:
            last_err = f"test_script syntax: {syntax_err}"
            continue

        # Protocol checks
        low = test_text.lower()
        if "l1_runs" not in low:
            last_err = "test_script missing L1_runs token"
            continue
        if "l1_output_exists" not in low:
            last_err = "test_script missing L1_output_exists token"
            continue
        l2_matches = set(re.findall(r"\bL2_[A-Za-z0-9_]+", test_text))
        n_l2 = len({m.rstrip("_") for m in l2_matches})
        if n_l2 < 2:
            last_err = f"test_script has only {n_l2} L2_* patterns, need >= 2"
            continue
        if "fixture_data" not in low:
            last_err = "test_script does not reference FIXTURE_DATA"
            continue
        if "hidden_convention_coverage" not in low:
            last_err = "test_script missing HIDDEN_CONVENTION_COVERAGE list"
            continue

        write_file_safe(cand_dir, "test_script.py", test_text)

        # HARD GATE: the reference must pass its own oracle (ref/gold/oracle
        # consistency). Replaces the old soft smoke-check. Required because the
        # no_context calibration policy (0/k = KEEP_hard) no longer filters broken
        # env tasks downstream — this is now the only place they are caught.
        oracle_res = run_env_oracle_on_ref(cand_dir, timeout=120)
        if not oracle_res["passed"]:
            last_err = (
                "reference_solution.py does not pass its own oracle "
                f"(pass={oracle_res.get('n_pass')}, fail={oracle_res.get('n_fail')}, "
                f"err={oracle_res.get('n_err')}): "
                f"{oracle_res.get('reason') or oracle_res.get('stdout_tail', '')[-200:]}"
            )
            trace.log("s5_env_oracle.ref_fails_oracle", attempt=attempt, error=last_err)
            continue

        trace.log("s5_env_oracle.ref_passes", attempt=attempt, n_pass=oracle_res.get("n_pass"))
        trace.log("s5_env_oracle.pass", attempt=attempt)
        return test_text

    raise PipelineFail("GateS5_env_oracle_ref_fails", last_err)


# ─────────────────────────────────────────────────────────────────────────────
# S6: SKILL.md
# ─────────────────────────────────────────────────────────────────────────────

def validate_skill_md(family: str, skill_text: str, design: dict[str, Any]) -> Optional[str]:
    """Structural-only gate. We deliberately DO NOT require the SKILL to restate
    the hidden conventions here: the prior version rejected any SKILL that failed
    to mention a convention keyword, which *forced* the standard answer into the
    file. Answer-leakage is now controlled the gene way -- by audit_skill_leakage
    (mechanical hidden-literal/answer-option rejection) -- so the SKILL teaches a
    *re-derivable* method instead of restating the oracle."""
    low = skill_text.lower()

    if family == "math_reasoning":
        sections = ("## method", "## decision procedure", "## common pitfalls")
    elif family == "rule_following":
        sections = ("## rule set", "## decision procedure", "## common pitfalls")
    elif family == "agent_env_synth":
        sections = ("## overview", "## api reference", "## workflow", "## common pitfalls")
    else:
        sections = ()
    for section in sections:
        if section not in low:
            return f"SKILL.md missing section: {section!r}"

    # The SKILL should still be ABOUT the task's operation (a public term that S4
    # is required to surface in task.md), so a topical mention keeps it useful
    # without leaking the hidden detail. This is a soft, public-only check.
    _STOP = {"the", "and", "for", "with", "from", "that", "this", "must", "not",
             "one", "two", "all", "any", "per", "via", "into", "onto", "each"}

    def _keywords(s: str) -> list[str]:
        return [w for w in re.findall(r"[a-z0-9]+", s.lower())
                if len(w) >= 4 and w not in _STOP]

    for item in design.get("hidden_conventions") or []:
        if not isinstance(item, dict):
            continue
        # Only check the PUBLIC operation_name_for_task (which appears in task.md),
        # never the hidden `name`/`detail_for_oracle`, so we never push private spec in.
        kws = _keywords(str(item.get("operation_name_for_task", "")))
        if kws and not any(kw in low for kw in kws):
            return (
                f"SKILL.md does not even mention the public operation {kws} "
                f"(operation_name_for_task); it is too generic to be a useful skill"
            )

    return None


# ── S6 leakage audit: gene-style mechanical guard (keep method, strip answer) ──
# Mirrors evolve_genes_v3.validate_gene: an answer-determining literal (multi-digit
# number, structured quoted string, CLI flag, enum token) that occurs ONLY in the
# hidden artifacts (reference solution / oracle / design.detail_for_oracle) and not
# in the public task.md must never be restated verbatim in SKILL.md. Public answer
# option decision tokens are likewise blocked. Prose method guidance is allowed.

_SKILL_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_SKILL_QUOTED_RE = re.compile(r"""['"]([^'"]{2,64})['"]""")
_SKILL_CODE_SPAN_RE = re.compile(r"(?<!`)`([^`\n]{2,80})`(?!`)")
_SKILL_FLAG_RE = re.compile(r"--[a-z][a-z0-9_-]*[a-z0-9]")
_SKILL_ANSWER_ONE_OF_RE = re.compile(r"ANSWER:\s*<one of:\s*([^>\n]+)>", re.IGNORECASE)


def _skill_is_trivial_number(tok: str) -> bool:
    try:
        return ("." not in tok) and (abs(int(tok)) < 10)
    except ValueError:
        return False


def _skill_is_structured_literal(tok: str) -> bool:
    s = (tok or "").strip()
    if not s:
        return False
    if _SKILL_NUM_RE.fullmatch(s):
        return not _skill_is_trivial_number(s)
    if s.startswith("--"):
        return True
    if any(ch.isdigit() for ch in s):
        return True
    if any(ch in s for ch in ("_", ".", "/", "\\")):
        return True
    if re.fullmatch(r"[A-Z][A-Z0-9_-]{2,}", s):
        return True
    return False


def _skill_hard_literals(text: str) -> set[str]:
    out: set[str] = set()
    for n in _SKILL_NUM_RE.findall(text or ""):
        if not _skill_is_trivial_number(n):
            out.add(n)
    for q in list(_SKILL_QUOTED_RE.findall(text or "")) + list(_SKILL_CODE_SPAN_RE.findall(text or "")):
        q = q.strip().lower()
        if len(re.findall(r"[a-z0-9]", q)) >= 3 and _skill_is_structured_literal(q):
            out.add(q)
    out.update(_SKILL_FLAG_RE.findall((text or "").lower()))
    return out


def _skill_answer_options(task_text: str) -> set[str]:
    opts: set[str] = set()
    for m in _SKILL_ANSWER_ONE_OF_RE.finditer(task_text or ""):
        for item in re.split(r"[|/,]", m.group(1)):
            item = item.strip().strip("`'\" ").lower()
            if item:
                opts.add(item)
    for line in (task_text or "").splitlines():
        if "which of the following" in line.lower():
            for item in re.findall(r"`([^`]+)`", line):
                item = item.strip().lower()
                if item:
                    opts.add(item)
    return opts


def audit_skill_leakage(
    skill_text: str,
    task_text: str,
    ref_text: str,
    design: dict[str, Any],
    test_text: str = "",
) -> Optional[str]:
    public_hard = _skill_hard_literals(task_text)
    hidden_parts = [ref_text or "", test_text or ""]
    for item in design.get("hidden_conventions") or []:
        if isinstance(item, dict):
            hidden_parts.append(str(item.get("detail_for_oracle", "")))
    private_hard = _skill_hard_literals("\n".join(hidden_parts)) - public_hard

    low = skill_text.lower()
    leaks: list[str] = []
    seen: set[str] = set()
    for tok in private_hard:
        if len(re.findall(r"[A-Za-z0-9]", tok)) < 2 or tok in seen:
            continue
        if re.search(r"[A-Za-z]", tok):
            hit = re.search(rf"(?<![A-Za-z0-9_]){re.escape(tok)}(?![A-Za-z0-9_])", low) is not None
        else:
            hit = re.search(rf"(?<![0-9.]){re.escape(tok)}(?![0-9.])", skill_text) is not None
        if hit:
            seen.add(tok)
            leaks.append(tok)
    for opt in _skill_answer_options(task_text):
        if len(re.findall(r"[A-Za-z0-9]", opt)) < 2 or opt in seen:
            continue
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(opt)}(?![A-Za-z0-9_])", low):
            seen.add(opt)
            leaks.append(opt)
    if leaks:
        return (
            "SKILL.md restates hidden answer literals / answer-option tokens "
            f"(forbidden): {sorted(leaks)[:12]}. Describe the re-derivable method "
            "and pitfalls without naming the exact hidden constant or final token."
        )
    return None


def stage_s6_skill(
    client: GeminiClient,
    family: str,
    design: dict[str, Any],
    ref_text: str,
    task_text: str,
    cand_dir: Path,
    trace: Trace,
    model: str,
    max_retries: int,
) -> str:
    template = load_prompt(family, "6_skill.md")
    prompt = render_prompt(
        template,
        design_json=json.dumps(design, indent=2, ensure_ascii=False),
        task_md=task_text,
        reference_signatures=extract_signatures(ref_text),
    )
    last_err = "never attempted"

    for attempt in range(1, max_retries + 1):
        resp = call_llm(client, prompt, model=model, max_tokens=20000, temperature=0.3)
        trace.log_llm(f"s6_skill.attempt{attempt}", resp)
        if not resp.text or resp.stop_reason == "MAX_TOKENS":
            last_err = f"empty or truncated (stop={resp.stop_reason})"
            continue

        try:
            skill_text = parse_single_file(resp.text, "SKILL.md")
        except PipelineFail as e:
            last_err = str(e)
            continue

        check_err = validate_skill_md(family, skill_text, design)
        if check_err:
            last_err = check_err
            trace.log("s6_skill.gate_fail", attempt=attempt, error=last_err)
            continue

        test_path = cand_dir / "test_script.py"
        test_text = test_path.read_text(encoding="utf-8", errors="replace") if test_path.exists() else ""
        leak_err = audit_skill_leakage(skill_text, task_text, ref_text, design, test_text)
        if leak_err:
            last_err = leak_err
            trace.log("s6_skill.leak_fail", attempt=attempt, error=last_err)
            continue

        write_file_safe(cand_dir, "SKILL.md", skill_text)
        trace.log("s6_skill.pass", attempt=attempt)
        return skill_text

    raise PipelineFail("GateS6_skill_unwritable", last_err)


# ─────────────────────────────────────────────────────────────────────────────
# scenario.yaml generation
# ─────────────────────────────────────────────────────────────────────────────

def write_scenario_yaml(family: str, design: dict[str, Any], gold: dict[str, Any], cand_dir: Path, candidate_id: str) -> None:
    scenario_name = re.sub(r"[^a-zA-Z0-9]+", "_", str(design.get("scenario_name", candidate_id))).strip("_")[:48].lower()
    gold_answer = gold.get("gold_answer", "")
    answer_format = design.get("answer_format", "integer")

    chain_key = "rule_chain" if family == "rule_following" else "derivation_chain"
    chain = design.get(chain_key) or []
    chain_depth = _max_chain_depth(chain)
    n_hidden = len(design.get("hidden_conventions") or [])

    lines = [
        f"id: {candidate_id}",
        f"name: {scenario_name}",
        f"family: {family}",
        f"domain: {design.get('domain', '')}",
        f"shape_version: v3.{family}.sf1",
        "source: synthetic",
        "difficulty: hard",
        f"chain_depth: {chain_depth}",
        f"hidden_convention_count: {n_hidden}",
        "pipeline: solution_first_v3",
    ]

    # answer_format / expected_answer: only for text-short families.
    # agent_env_synth is graded by pytest (no answer_format needed), and
    # gold={} so we'd write junk (expected_answer: "", answer_format: integer).
    if family != "agent_env_synth":
        lines.insert(8, f"answer_format: {answer_format}")
        lines.insert(9, f"expected_answer: \"{gold_answer}\"")

    if family == "rule_following":
        answer_space = design.get("answer_space") or []
        predicted = design.get("predicted_no_context_answer", "")
        trap = design.get("naive_trap", "")
        lines.append("answer_space:")
        for opt in answer_space:
            # YAML 1.1 bool trap: quote yes/no/true/false
            opt_str = str(opt)
            if opt_str.lower() in {"yes", "no", "true", "false", "on", "off", "y", "n"}:
                lines.append(f"  - '{opt_str}'")
            else:
                lines.append(f"  - {opt_str}")
        if predicted:
            lines.append(f"predicted_no_context_answer: {predicted}")
        if trap:
            lines.append(f"trap_summary: \"{trap[:120]}\"")

    elif family == "math_reasoning":
        trap = design.get("naive_trap", "")
        if trap:
            lines.append(f"trap_summary: \"{trap[:120]}\"")
        brute = design.get("brute_force_resistance", "")
        if brute:
            lines.append(f"brute_force_resistance: \"{brute[:120]}\"")

    lines.append("tags:")
    lines.append(f"  - {design.get('domain', family)}")
    lines.append("  - reasoning_v3sf")

    (cand_dir / "scenario.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Top-level: run_one
# ─────────────────────────────────────────────────────────────────────────────

def run_one(
    client: GeminiClient,
    family: str,
    domain: str,
    idea: str,
    idx: int,
    args: argparse.Namespace,
) -> dict[str, Any]:
    candidate_id = f"{ID_PREFIX[family]}{idx:04d}sf"
    cand_dir = CANDIDATES_DIR / family / candidate_id
    if cand_dir.exists():
        shutil.rmtree(cand_dir)
    cand_dir.mkdir(parents=True, exist_ok=True)

    trace = Trace(cand_dir)
    trace.log("pipeline.start", candidate_id=candidate_id, family=family,
              domain=domain, task_idea=idea[:120])

    record: dict[str, Any] = {
        "candidate_id": candidate_id,
        "family": family,
        "domain": domain,
        "task_idea": idea,
        "verdict": "pending",
        "gate": None,
        "reason": None,
        "stages_passed": [],
    }

    try:
        # S1: Design Skeleton
        design = stage_s1_design(
            client, family, domain, idea, candidate_id, cand_dir, trace,
            model=args.model,
            min_chain=args.min_chain_depth,
            min_hidden=args.min_hidden_conventions,
            min_deliverables=args.min_deliverables,
            max_retries=args.max_retries,
        )
        record["stages_passed"].append("s1_design")

        # S2: Reference Solution
        ref_text = stage_s2_reference(
            client, family, design, cand_dir, trace,
            model=args.model,
            min_helpers=args.min_ref_helpers,
            max_retries=args.max_retries,
        )
        record["stages_passed"].append("s2_reference")

        # S3: Gold Standard
        if family in ("math_reasoning", "rule_following"):
            gold = stage_s3_gold_text_short(family, cand_dir, trace, design=design)
            fixture_info: dict[str, Any] = gold

            # Optional: generate adversarial variants
            variants = stage_s3_variants_text_short(
                client, family, design, ref_text, gold, cand_dir, trace,
                model=args.model, max_retries=args.max_retries,
            )
            record["stages_passed"].append("s3_gold")

        else:  # agent_env_synth
            fixture_manifest = stage_s3_fixtures_env(
                client, design, ref_text, cand_dir, trace,
                model=args.model,
                max_retries=args.max_retries,
                fixture_timeout=args.fixture_timeout_s,
            )
            gold = {}
            fixture_info = fixture_manifest
            record["stages_passed"].append("s3_fixtures")

        # Write scenario.yaml HERE — before S4/S5, because the math/rule
        # oracle (test_script.py.tmpl) reads scenario.yaml from the candidate
        # dir via `here / "scenario.yaml"` during S5's run_oracle_on_ref call.
        # Writing it late (after S5) caused FileNotFoundError → GateD_ref_fails_oracle
        # on every math/rule candidate.
        write_scenario_yaml(family, design, gold, cand_dir, candidate_id)
        record["stages_passed"].append("scenario_yaml")

        # S4: Task Back-Synthesis
        task_text = stage_s4_task(
            client, family, design, ref_text, gold, fixture_info, cand_dir, trace,
            model=args.model, max_retries=args.max_retries,
        )
        record["stages_passed"].append("s4_task")

        # S5: Oracle + Bad Solutions
        if family in ("math_reasoning", "rule_following"):
            stage_s5_oracle_text_short(
                client, family, design, ref_text, task_text, gold, cand_dir, trace,
                model=args.model, max_retries=args.max_retries,
            )
        else:
            stage_s5_oracle_env(
                client, design, ref_text, task_text, fixture_info, cand_dir, trace,
                model=args.model, max_retries=args.max_retries,
            )
        record["stages_passed"].append("s5_oracle")

        # S6: SKILL.md
        stage_s6_skill(
            client, family, design, ref_text, task_text, cand_dir, trace,
            model=args.model, max_retries=args.max_retries,
        )
        record["stages_passed"].append("s6_skill")

        record["verdict"] = "pass_pre_calibration"
        trace.log("pipeline.success")

    except PipelineFail as e:
        record["verdict"] = "reject"
        record["gate"] = e.gate
        record["reason"] = e.reason
        trace.log("pipeline.fail", gate=e.gate, reason=e.reason)
    except Exception:
        tb = traceback.format_exc()
        record["verdict"] = "crash"
        record["gate"] = "Gate_exception"
        record["reason"] = tb[:500]
        trace.log("pipeline.crash", traceback=tb[:500])
    finally:
        (cand_dir / "_run_record.json").write_text(
            json.dumps(record, indent=2, default=str), encoding="utf-8"
        )

    return record


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────

def make_client() -> GeminiClient:
    return GeminiClient()


def main() -> int:
    ap = argparse.ArgumentParser(description="LongWoF-Bench solution-first pipeline driver")
    ap.add_argument("--family", choices=FAMILIES, default=None)
    ap.add_argument("--all", action="store_true", help="Run all three families")
    ap.add_argument("--n", type=int, default=5, help="Number of candidates per family")
    ap.add_argument("--seed-offset", type=int, default=0, help="Skip first N seeds")

    # Model routing
    ap.add_argument("--model", default=os.environ.get("SF_MODEL", "gemini-3.1-pro-preview"),
                    help="LLM model for all generation stages S1-S6")

    # Gate thresholds
    ap.add_argument("--min-chain-depth", type=int, default=3,
                    help="Minimum derivation chain depth for S1 gate")
    ap.add_argument("--min-hidden-conventions", type=int, default=2,
                    help="Minimum hidden conventions for S1 gate")
    ap.add_argument("--min-deliverables", type=int, default=2,
                    help="Minimum deliverables for agent_env S1 gate")
    ap.add_argument("--min-ref-helpers", type=int, default=3,
                    help="Minimum helper functions in reference_solution.py")
    ap.add_argument("--max-retries", type=int, default=3,
                    help="Max retries per stage on gate failure")
    ap.add_argument("--fixture-timeout-s", type=int, default=120,
                    help="Timeout for S3 fixture generation script (env family)")

    ap.add_argument("--summary-out", default=str(HERE / "summary_sf.json"))
    args = ap.parse_args()

    if not args.family and not args.all:
        ap.error("specify --family <name> or --all")

    families = FAMILIES if args.all else [args.family]
    client = make_client()

    overall: dict[str, Any] = {
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": args.model,
        "pipeline": "solution_first_v3",
        "results": {},
    }

    for family in families:
        seeds = load_seeds(family)
        selected = seeds[args.seed_offset: args.seed_offset + args.n]
        if not selected:
            print(f"[{family}] no seeds after offset {args.seed_offset}, skip")
            continue

        print(f"\n=== {family}: {len(selected)} candidates (offset={args.seed_offset}) ===")
        family_records: list[dict[str, Any]] = []

        for i, (domain, idea) in enumerate(selected, start=1 + args.seed_offset):
            print(f"  [{i - args.seed_offset}/{len(selected)}] {domain!r} | {idea[:70]!r}")
            t0 = time.time()
            rec = run_one(client, family, domain, idea, i, args)
            elapsed = round(time.time() - t0, 1)
            rec["elapsed_s"] = elapsed
            family_records.append(rec)

            v = rec["verdict"]
            g = rec.get("gate") or ""
            stages = len(rec.get("stages_passed") or [])
            print(f"      → {v} stages={stages} {g} ({elapsed}s)")

        overall["results"][family] = family_records
        passed = sum(1 for r in family_records if r["verdict"] == "pass_pre_calibration")
        print(f"\n  {family}: {passed}/{len(family_records)} pass_pre_calibration")
        if family_records:
            gate_counts: dict[str, int] = {}
            for r in family_records:
                if r["verdict"] != "pass_pre_calibration":
                    g = r.get("gate") or "unknown"
                    gate_counts[g] = gate_counts.get(g, 0) + 1
            if gate_counts:
                print(f"  Reject gates: {dict(sorted(gate_counts.items(), key=lambda x: -x[1]))}")

    Path(args.summary_out).write_text(json.dumps(overall, indent=2, default=str))
    print(f"\nSummary → {args.summary_out}")

    print("\n=== overall ===")
    for fam, recs in overall["results"].items():
        passed = sum(1 for r in recs if r["verdict"] == "pass_pre_calibration")
        print(f"  {fam:<22} {passed}/{len(recs)} pass_pre_calibration")

    print("\nNext step: run mini_calibration.py --family <fam> on the candidates_sf/ dir")
    print("  (uses a Pro anchor with true rejection; see synth/README.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

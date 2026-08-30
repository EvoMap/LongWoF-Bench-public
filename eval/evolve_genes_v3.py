#!/usr/bin/env python3
"""Domain-agnostic experiential Gene *Evolver* for LongWoF-Bench.

This is the general (publishable) replacement for ``gen_genes_llm_v3.py``. It
contains **no per-family branches, keyword lists, or scenario-schema readers**.
A Gene is produced by the same generic GEP-style mechanism for every task:

    Stage 1 (Evolve)  : solve -> run the black-box verifier -> if it FAILS,
                        feed back ONLY the verdict signal (error_type + a
                        sanitized stderr/stdout tail) -> mutate -> retry up to
                        K times. Keep the first candidate that PASSES. We record
                        the trajectory, the mutation log, and the success rate.
    Stage 2 (Distill) : from the VERIFIED-CORRECT trajectory (never from a
                        failure) distill one reusable GDIv2 Gene with a single
                        generic prompt.
    Stage 3 (Audit)   : mechanical leakage audit. private = tokens that occur in
                        the task's HIDDEN artifacts (oracle / reference solution
                        / scenario gold) but NOT in the PUBLIC task.md. Any gene
                        string containing a private token is rejected. Pure
                        set-difference; no domain knowledge.
Setup is 1:1 (in-sample): every selected task gets its OWN gene written as
<task_id>.json, and the gene is later evaluated on that same task by the
existing ``run_official.py --condition with_gene``. There is no train/test split
and no retrieval -- a gene is always tested on the task it was distilled from.

Only the public task prompt + the black-box pass/fail verdict ever influence a
gene. Hidden artifacts are read ONLY to power the mechanical leakage audit and
NEVER enter any prompt.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
V3_ROOT = HERE.parent
POOL_ROOT = V3_ROOT / "tasks_final"
DEFAULT_MANIFEST = POOL_ROOT / "manifest.json"
DEFAULT_CASES = V3_ROOT / "_runs" / "v3_gemini31_flash_pro_full" / "cases.jsonl"

# Keep this eval directory first so sibling imports resolve to this tree's API
# registry, including Bedrock aliases such as bedrock_opus.
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import run_official as ro  # noqa: E402  (reuse the official black-box verifier)
import gen_genes_llm_v3 as gg  # noqa: E402  (reuse GDIv2 schema utils + wrapper)
from api import MODEL_REGISTRY  # noqa: E402


DEFAULT_OUT_DIR = POOL_ROOT / "genes_evolved"

# ---------------------------------------------------------------------------
# Generic prompts (NO family-specific content).
# ---------------------------------------------------------------------------

MUTATE_SYSTEM = """You are iteratively solving a single benchmark task. Your previous solution was executed against a hidden automated checker and it FAILED. You are given only the failure signal (an error category plus a short, sanitized tail of the program output). You are NOT given the checker, the expected answer, or any hidden file.

Diagnose the most likely cause from the failure signal and your previous attempt, then produce a corrected full solution.

Output format:
- For a coding task: output ONLY the complete corrected program inside a single ```python code block.
- For a short-answer task: output ONLY the exact required answer lines (e.g. `ANSWER:` and `ANALYSIS:`), no code block, no extra prose."""

MUTATE_USER_TEMPLATE = """--- task ---
{task}

--- your previous attempt ---
{attempt}

--- execution feedback (sanitized; no hidden data) ---
{feedback}

Produce the corrected solution now."""


DISTILL_SYSTEM = """You distill ONE reusable GDIv2 "Gene": procedural knowledge that helps a fresh solver succeed on problems SIMILAR to (but different from) the one shown.

You are given a problem and a solution trajectory that PASSED the hidden checker, together with the list of error categories that earlier attempts had to overcome. Capture the transferable method and the failure modes that had to be defended against.

Keep the standard GDIv2 payload shape. Do NOT invent extra fields such as `operational_recipe` or `edge_cases`; instead encode those ideas inside the official fields:
- `summary`: one concrete single-sentence capability description, not a vague slogan.
- `signals_match`: trigger phrases for retrieval; use 5-10 concise class-level signals.
- `strategy`: the main operational recipe. Prefer 4-8 imperative steps with algorithmic detail, ordering, boundary checks, output-contract checks, and lightweight pseudocode when useful.
- `preconditions`: 0-4 verifiable edge cases or failure lessons that must be checked before trusting the approach.
- `constraints`: normal GDIv2 edit-scope guard.
- `validation`: [] only; do not add bogus console-log validations.

Generalization and leakage rules:
- The Gene will be applied to OTHER tasks, never this exact one, so avoid instance-specific values.
- You may mention public task-contract terms that a fresh solver is allowed to see, such as CLI flags, file names, column names, output keys, or required output formats, when they are essential to the reusable method.
- Never include the final answer, public answer-option labels/decision tokens, hidden-only numeric constants, hidden-only quoted strings, private names, or private identifiers copied from hidden artifacts.
- For rule-style tasks, describe threshold/precedence mechanics without naming the final action token.
- For code/math tasks, be concrete enough that a solver can implement the recipe; avoid filler like "standard processing" or "initial logic is validated".

Schema: output exactly one JSON object with EXACTLY these 8 fields:
{
  "type": "Gene",
  "summary": "<50-300 chars, single sentence>",
  "category": "<one of innovate|optimize|refactor|debug|secure|document|test|other>",
  "signals_match": ["<5-10 short trigger phrases describing when this Gene applies>"],
  "strategy": ["<3-8 imperative procedural steps, each 15-300 chars>"],
  "preconditions": ["<0-4 verifiable prerequisites distilled from overcome failures>"],
  "constraints": {"max_files": <int 1-5>, "forbidden_paths": [".env", "secrets", "credentials"]},
  "validation": []
}

Output only the JSON object. No markdown fences. No extra text."""

DISTILL_USER_TEMPLATE = """--- task ---
{task}

--- a solution that PASSED the hidden checker ---
{solution}

--- error categories overcome on the way to success ---
{mutation_log}

Author the reusable Gene JSON now. Output ONLY the JSON object."""

DISTILL_RETRY_SUFFIX = """

PREVIOUS OUTPUT WAS REJECTED:
{error}

Regenerate. Keep exactly the 8 standard GDIv2 fields, keep lengths in range, and
remove private literals, final answers, and answer-option decision tokens. Make
`strategy` concrete and operational rather than generic. Output ONLY corrected JSON."""


# ---------------------------------------------------------------------------
# Generic tokenization (used by both the leakage audit and retrieval).
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_QUOTED_RE = re.compile(r"""(?<![A-Za-z0-9])['"]([^'"\n]{2,64})['"](?![A-Za-z0-9])""")
_CODE_SPAN_RE = re.compile(r"(?<!`)`([^`\n]{2,80})`(?!`)")
_FLAG_RE = re.compile(r"--[a-z][a-z0-9_-]*[a-z0-9]")
_BARE_STRUCTURED_RE = re.compile(
    r"(?<![A-Za-z0-9_./\\-])"
    r"[A-Za-z0-9][A-Za-z0-9_./\\-]{1,80}"
    r"(?![A-Za-z0-9_./\\-])"
)

# Generic English/Python/benchmark-scaffold stopwords. NOT domain specific.
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "your", "you",
    "are", "not", "use", "using", "must", "should", "will", "can", "may", "any",
    "all", "each", "one", "two", "given", "input", "output", "value", "values",
    "result", "results", "return", "returns", "function", "functions", "code",
    "task", "tasks", "test", "tests", "assert", "import", "def", "class", "self",
    "true", "false", "none", "print", "data", "list", "dict", "string", "str",
    "int", "float", "bool", "file", "files", "path", "line", "lines", "answer",
    "analysis", "solution", "program", "python", "run", "running", "expected",
    "actual", "case", "cases", "example", "examples", "following", "above",
    "below", "number", "numbers", "set", "get", "name", "names", "format",
    "required", "exactly", "scenario", "problem", "compute", "calculate",
}


def _words(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "") if w.lower() not in _STOPWORDS}


def _is_trivial_number(tok: str) -> bool:
    """Single-digit integers are ubiquitous scaffolding (loop bounds, "step 1")
    and almost always appear in the public text too; treating them as private
    constants only creates false positives. This is a generic rule, not a
    family heuristic."""
    try:
        return ("." not in tok) and (abs(int(tok)) < 10)
    except ValueError:
        return False


def _is_structured_literal(tok: str) -> bool:
    """True for literals that are more likely to be private contract/answer data
    than ordinary method vocabulary.

    Hidden artifacts quote many harmless words ("date", "price", "timestamp").
    Treating every quoted word as private makes code Genes unable to describe the
    operation they perform. Keep the hard channel focused on structured strings:
    identifiers, file/path-ish strings, numeric labels, flags, and enum-like
    uppercase tokens.
    """
    s = (tok or "").strip()
    if not s:
        return False
    if _NUM_RE.fullmatch(s):
        return not _is_trivial_number(s)
    if s.startswith("--"):
        return True
    if any(ch.isdigit() for ch in s):
        return True
    if any(ch in s for ch in ("_", ".", "/", "\\")):
        return True
    if re.fullmatch(r"[A-Z][A-Z0-9_-]{2,}", s):
        return True
    return False


def _content_tokens(text: str) -> tuple[set[str], set[str]]:
    """Return (word_tokens, hard_tokens) where hard_tokens are
    leakage-relevant literals: multi-digit numbers, quoted strings, CLI flags."""
    words = _words(text)
    hard: set[str] = set()
    for n in _NUM_RE.findall(text or ""):
        if not _is_trivial_number(n):
            hard.add(n)
    for q in _QUOTED_RE.findall(text or ""):
        q = q.strip().lower()
        # Only structured quoted values are hard leakage candidates. Plain words
        # quoted inside reference/test code are often column concepts or method
        # vocabulary and should not muzzle the Gene.
        if len(re.findall(r"[a-z0-9]", q)) >= 3 and q not in _STOPWORDS and _is_structured_literal(q):
            hard.add(q)
    for q in _CODE_SPAN_RE.findall(text or ""):
        q = q.strip().lower()
        # Public task contracts are often written as Markdown code spans:
        # `region_id`, `--input`, `diagnostics.csv`, etc. Treat those as public
        # literals so the hidden-reference audit does not falsely block code
        # Genes from mentioning fields and output files the solver is allowed to see.
        if len(re.findall(r"[a-z0-9]", q)) >= 3 and q not in _STOPWORDS and _is_structured_literal(q):
            hard.add(q)
    hard.update(_FLAG_RE.findall((text or "").lower()))
    return words, hard


def _public_hard_tokens(text: str) -> set[str]:
    """Hard literals visible in task.md.

    The hidden-literal audit compares hidden artifacts to public task text.
    Public contracts are often written as headings or prose, not only as code
    spans: e.g. "### host_diagnostics.json" or "columns sample_id and
    estimated_cfu_per_ml". Treat those bare structured strings as public so the
    audit does not falsely reject useful code-generation Genes.
    """
    _, hard = _content_tokens(text)
    if re.search(r"%|\bpercent(?:age)?\b", text or "", re.IGNORECASE):
        hard.add("100")
    for match in _BARE_STRUCTURED_RE.findall(text or ""):
        tok = match.strip("`'\".,:;()[]{}<>").lower()
        if (
            len(re.findall(r"[a-z0-9]", tok)) >= 2
            and tok not in _STOPWORDS
            and _is_structured_literal(tok)
        ):
            hard.add(tok)
            if "." in tok and not _NUM_RE.fullmatch(tok):
                stem = re.split(r"[/\\]", tok)[-1].rsplit(".", 1)[0]
                if (
                    len(re.findall(r"[a-z0-9]", stem)) >= 2
                    and stem not in _STOPWORDS
                    and _is_structured_literal(stem)
                ):
                    hard.add(stem)
    return _expand_public_hard_tokens(hard)


def _expand_public_hard_tokens(hard: set[str]) -> set[str]:
    """Add generic path/name variants for public literals.

    Public prompts often expose `./dir/file.ext`, while hidden scripts refer to
    the same object as `file.ext` or `file`. These are the same public contract,
    not a hidden leak.
    """
    out = set(hard)
    for tok in list(hard):
        variants: set[str] = set()
        cleaned = tok.lstrip("./")
        if cleaned and cleaned != tok:
            variants.add(cleaned)
        basename = re.split(r"[/\\]", cleaned or tok)[-1]
        if basename and basename != tok:
            variants.add(basename)
        if "." in basename and not _NUM_RE.fullmatch(basename):
            variants.add(basename.rsplit(".", 1)[0])
        for variant in variants:
            variant = variant.strip("`'\".,:;()[]{}<>").lower()
            if (
                len(re.findall(r"[a-z0-9]", variant)) >= 2
                and variant not in _STOPWORDS
                and _is_structured_literal(variant)
            ):
                out.add(variant)
    return out


# ---------------------------------------------------------------------------
# Mechanical leakage audit (Stage 3): private = hidden tokens - public tokens.
# ---------------------------------------------------------------------------

def _read_role(task_dir: Path, files: dict[str, Any], role: str, default: str) -> str:
    rel = files.get(role) or default
    p = task_dir / rel
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")


def read_reference_solution(row: dict[str, Any], task_dir: Path) -> str:
    """The official worked solution. Only used by the explicit reference modes
    (--from-reference / --fallback-reference). It DOES enter the distill prompt
    in those modes (teacher demonstration); the mechanical audit still strips
    every instance constant the model might copy from it."""
    files = row.get("files") if isinstance(row.get("files"), dict) else {}
    for role, default in (("ref", "reference_solution.py"), ("ref", "reference_solution.sh")):
        txt = _read_role(task_dir, files, role, default)
        if txt.strip():
            return txt
    return ""


def build_private_vocab(row: dict[str, Any], task_dir: Path, public_task_md: str) -> set[str]:
    """Hard literals (multi-digit numbers, quoted strings, CLI flags) that occur
    ONLY in the task's HIDDEN artifacts (oracle / reference solution / scenario
    gold) and not in the public task. These are exactly the answer-determining
    constants a Gene must never restate. We deliberately do NOT diff ordinary
    *words*: hidden files contain prose (trap notes, reference comments) using
    generic method vocabulary, and blocking those would muzzle the Gene without
    preventing any real answer leak. Family-agnostic: roles come from the
    manifest and files are read as opaque blobs (no schema knowledge)."""
    files = row.get("files") if isinstance(row.get("files"), dict) else {}
    public_hard = _public_hard_tokens(public_task_md)

    hidden_blob = "\n".join(
        _read_role(task_dir, files, role, default)
        for role, default in (
            ("oracle", "test_script.py"),
            ("ref", "reference_solution.py"),
            ("scenario", "scenario.yaml"),
        )
    )
    _, hidden_hard = _content_tokens(hidden_blob)
    return hidden_hard - public_hard


def find_mechanical_leakage(payload: dict[str, Any], private_hard: set[str]) -> list[tuple[str, str, str]]:
    if not private_hard:
        return []
    leaks: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for loc, value in gg._iter_payload_strings(payload):
        low = value.lower()
        for tok in private_hard:
            # Safety: never match on near-empty / punctuation-only tokens.
            if len(re.findall(r"[A-Za-z0-9]", tok)) < 2:
                continue
            if re.search(r"[A-Za-z]", tok):
                hit = re.search(rf"(?<![A-Za-z0-9_]){re.escape(tok)}(?![A-Za-z0-9_])", low) is not None
            else:
                hit = re.search(rf"(?<![A-Za-z0-9_.]){re.escape(tok)}(?![A-Za-z0-9_.])", value) is not None
            if hit:
                item = (tok, loc, "hidden_literal")
                if item not in seen:
                    leaks.append(item)
                    seen.add(item)
    return leaks


def _literal_replacement(tok: str) -> str:
    if _NUM_RE.fullmatch(tok):
        return "the task-specific numeric value"
    if any(ch in tok for ch in ("/", "\\", ".")):
        return "the task-specified file or path"
    if "_" in tok:
        return "the task-specific field"
    return "the task-specific term"


def _replace_literal(text: str, tok: str) -> str:
    replacement = _literal_replacement(tok)
    if re.search(r"[A-Za-z]", tok):
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(tok)}(?![A-Za-z0-9_])"
        return re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    pattern = rf"(?<![A-Za-z0-9_.]){re.escape(tok)}(?![A-Za-z0-9_.])"
    return re.sub(pattern, replacement, text)


def _fit_redacted_payload_schema(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    summary = out.get("summary")
    if isinstance(summary, str) and len(summary) > 300:
        out["summary"] = summary[:297].rstrip() + "..."
    strategy = out.get("strategy")
    if isinstance(strategy, list):
        out["strategy"] = [
            (item[:297].rstrip() + "...") if isinstance(item, str) and len(item) > 300 else item
            for item in strategy
        ]
    return out


def _task_text_for_distill_prompt(row: dict[str, Any], public_task_md: str) -> str:
    if str(row.get("execution_mode") or "") != "text_short_answer":
        return public_task_md
    return (
        "Short-answer rule-application benchmark. The concrete scenario text is "
        "withheld from the distillation prompt to avoid provider safety filters "
        "and answer-label copying. Distill only the reusable procedure: extract "
        "public facts, apply the stated framework rules in precedence order, "
        "handle modifiers and threshold comparisons carefully, then emit exactly "
        "the required ANSWER and ANALYSIS lines. Do not include any public option "
        "label, final action token, entity name, or numeric scenario constant in the Gene."
    )


def _solution_text_for_distill_prompt(row: dict[str, Any], solution: str) -> str:
    if str(row.get("execution_mode") or "") != "text_short_answer":
        return solution
    return (
        "A reference reasoning trace exists but is withheld from the distillation "
        "prompt for this short-answer rule task. Produce a generic, reusable "
        "rule-following Gene without scenario constants or decision labels."
    )


def read_skill_doc(row: dict[str, Any], task_dir: Path) -> str:
    files = row.get("files") if isinstance(row.get("files"), dict) else {}
    for rel in (files.get("skill"), "SKILL.md"):
        if not rel:
            continue
        p = task_dir / str(rel)
        if p.exists():
            txt = p.read_text(encoding="utf-8", errors="replace")
            if txt.strip():
                return txt
    return ""


def redact_private_literals(payload: dict[str, Any], private_hard: set[str]) -> dict[str, Any]:
    """Return a copy with any hidden-only literal generalized.

    This is used only after a model emits a schema-valid Gene that fails the
    hidden-literal audit. Redaction is safer than accepting leaked constants and
    more productive than discarding an otherwise reusable method payload.
    """
    out = json.loads(json.dumps(payload, ensure_ascii=False))
    for field in ("summary", "signals_match", "strategy", "preconditions"):
        value = out.get(field)
        if isinstance(value, str):
            for tok in sorted(private_hard, key=len, reverse=True):
                value = _replace_literal(value, tok)
            out[field] = value
        elif isinstance(value, list):
            fixed: list[Any] = []
            for item in value:
                if isinstance(item, str):
                    for tok in sorted(private_hard, key=len, reverse=True):
                        item = _replace_literal(item, tok)
                fixed.append(item)
            out[field] = fixed
    return _fit_redacted_payload_schema(out)


def extract_public_answer_options(task_md: str) -> set[str]:
    """Return public decision labels from short-answer multiple-choice formats.

    These labels are visible to the solver, but including the *right* one in a
    Gene distilled from a passed trajectory is still answer leakage. We keep this
    narrow so public code contracts (file names, CLI flags, CSV columns) remain
    available to code-generation Genes.
    """
    text = task_md or ""
    options: set[str] = set()

    for match in re.finditer(r"ANSWER:\s*<one of:\s*([^>\n]+)>", text, flags=re.IGNORECASE):
        blob = match.group(1)
        for item in re.split(r"[|/,]", blob):
            item = item.strip().strip("`'\" ")
            if item:
                options.add(item.lower())

    for line in text.splitlines():
        low = line.lower()
        if "which of the following" in low or "which of the following applies" in low:
            for item in re.findall(r"`([^`]+)`", line):
                item = item.strip()
                if item:
                    options.add(item.lower())

    return options


def find_answer_option_leakage(payload: dict[str, Any], answer_options: set[str]) -> list[tuple[str, str]]:
    if not answer_options:
        return []
    leaks: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for loc, value in gg._iter_payload_strings(payload):
        low = value.lower()
        for tok in answer_options:
            if len(re.findall(r"[A-Za-z0-9]", tok)) < 2:
                continue
            hit = re.search(rf"(?<![A-Za-z0-9_]){re.escape(tok)}(?![A-Za-z0-9_])", low) is not None
            if hit:
                item = (tok, loc)
                if item not in seen:
                    leaks.append(item)
                    seen.add(item)
    return leaks


def validate_gene(payload: dict[str, Any], public_task_md: str, private_hard: set[str]) -> tuple[bool, str]:
    """General audit layers, no family branches:
      L1 public answer-option guard: do not copy decision labels from task.md.
      L2 hidden-literal guard: do not contain any number / quoted string / flag
         that exists only in hidden answer/oracle/reference artifacts.

    Public code contracts are intentionally allowed. File names, CLI flags, CSV
    columns, and output keys from task.md are part of what a fresh solver sees,
    and forbidding them makes code Genes too vague to be useful.
    """
    ok, err = gg.validate_payload(payload)
    if not ok:
        return False, f"schema: {err}"
    ans_leaks = find_answer_option_leakage(payload, extract_public_answer_options(public_task_md))
    if ans_leaks:
        listing = "; ".join(f"`{tok}` in {loc}" for tok, loc in ans_leaks[:12])
        return False, f"answer-option leakage: {listing}"
    hid_leaks = find_mechanical_leakage(payload, private_hard)
    if hid_leaks:
        listing = "; ".join(f"`{tok}` in {loc}" for tok, loc, _kind in hid_leaks[:12])
        return False, f"hidden-literal leakage: {listing}"
    return True, ""


# ---------------------------------------------------------------------------
# Stage 1: Evolve (black-box verifier feedback only).
# ---------------------------------------------------------------------------

@dataclass
class Trajectory:
    task_id: str
    solved: bool
    n_iters: int
    mutation_log: list[str]            # error_type at each failed step
    final_response: str                # the passing (or last) raw response
    final_solution: str                # extracted code / sanitized answer prose
    success_rate: float                # solved ? 1/n_iters : 0
    calls: list[dict[str, Any]] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)


def _sanitize_solution_for_distill(raw_response: str, execution_mode: str) -> str:
    if execution_mode == "text_short_answer":
        # Keep the ANALYSIS reasoning; drop only the gradable ANSWER line.
        kept = [ln for ln in (raw_response or "").splitlines()
                if not re.match(r"^\s*ANSWER\s*:", ln.strip(), re.IGNORECASE)]
        return "\n".join(kept).strip() or "(reasoning only; answer line withheld)"
    code = ro.extract_python_code(raw_response)
    return code or (raw_response or "").strip()


def _feedback_from_eval(ev: dict[str, Any]) -> str:
    lines = [f"error_type: {ev.get('error_type', 'unknown')}"]
    if ev.get("n_fail") is not None:
        lines.append(f"failed_checks: {ev.get('n_fail')}")
    stderr = gg._sanitize_verdict_tail(str(ev.get("stderr_tail") or ""))
    stdout = gg._sanitize_verdict_tail(str(ev.get("stdout_tail") or ""))
    if stderr:
        lines.append("stderr_tail:\n" + gg._truncate(stderr, 900))
    if stdout:
        lines.append("stdout_tail:\n" + gg._truncate(stdout, 700))
    if not stderr and not stdout:
        lines.append("(the checker reported the answer/output was incorrect; no diagnostic detail available)")
    return "\n".join(lines)


def _token_record(resp: dict[str, Any] | None = None) -> dict[str, Any]:
    resp = resp or {}
    input_tokens = int(resp.get("input_tokens") or 0)
    output_tokens = int(resp.get("output_tokens") or 0)
    thoughts_tokens = int(resp.get("thoughts_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thoughts_tokens": thoughts_tokens,
        "completion_plus_thoughts": output_tokens + thoughts_tokens,
        "total_tokens": input_tokens + output_tokens + thoughts_tokens,
        "stop_reason": resp.get("stop_reason", ""),
        # Bedrock thinking evidence (folded into output_tokens; thoughts_tokens stays 0).
        "had_reasoning": bool(resp.get("had_reasoning", False)),
        "reasoning_chars": int(resp.get("reasoning_chars") or 0),
        "bedrock_effort": resp.get("bedrock_effort", "off"),
    }


def _call_summary(
    *,
    step: int,
    kind: str,
    ok: bool,
    api_call: bool,
    resp: dict[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    out = {
        "step": step,
        "rollout": step + 1,
        "kind": kind,
        "ok": ok,
        "api_call": api_call,
        "error": error,
    }
    out.update(_token_record(resp))
    return out


def evolve_one(
    row: dict[str, Any],
    pool_root: Path,
    keys: dict[str, str],
    model_alias: str,
    max_iters: int,
    gen_timeout: int,
    test_timeout: int,
    seed_response: str | None,
    max_tokens: int,
) -> Trajectory:
    task_id = str(row.get("task_id"))
    task_dir = ro._task_dir(row, pool_root)
    execution_mode = str(row.get("execution_mode") or "")
    public_prompt = ro._build_user_prompt(row, task_dir)

    mutation_log: list[str] = []
    calls: list[dict[str, Any]] = []
    rollouts: list[dict[str, Any]] = []
    response = seed_response or ""
    feedback = ""

    # Escalating thinking effort per rollout:
    #   step 0 -> "off"  (no thinking, fast baseline)
    #   step 1 -> "low"
    #   step 2+ -> "high"
    _STEP_EFFORT = {0: "off", 1: "low"}

    for step in range(max(1, max_iters)):
        kind = "solve" if step == 0 else "mutate"
        step_effort = _STEP_EFFORT.get(step, "high")
        system_prompt = ""
        user_prompt = public_prompt
        api_call = True
        resp: dict[str, Any] | None = None

        if step == 0 and seed_response is not None:
            kind = "seed"
            api_call = False
        elif step == 0:
            # Fresh no-context solve (only the public prompt, no skill/gene).
            try:
                resp = gg._llm_chat(model_alias, user_prompt, system_prompt, keys, max_tokens=max_tokens, effort=step_effort)
                response = str(resp.get("response") or "")
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                calls.append(_call_summary(step=step, kind=kind, ok=False, api_call=True, error=error))
                rollouts.append({
                    "step": step,
                    "rollout": step + 1,
                    "kind": kind,
                    "api_call": True,
                    "ok": False,
                    "error": error,
                    "system_prompt": system_prompt,
                    "user_prompt": user_prompt,
                })
                trace = {"task_id": task_id, "rollouts": rollouts, "calls": calls}
                return Trajectory(task_id, False, step, mutation_log, "", "", 0.0, calls, trace)
        else:
            # Mutate-on-error: feed back ONLY the generic verdict signal.
            system_prompt = MUTATE_SYSTEM
            user_prompt = MUTATE_USER_TEMPLATE.format(
                task=gg._truncate(public_prompt, 3500),
                attempt=gg._truncate(response, 4000),
                feedback=gg._truncate(feedback, 1600),
            )
            try:
                resp = gg._llm_chat(model_alias, user_prompt, system_prompt, keys, max_tokens=max_tokens, effort=step_effort)
                response = str(resp.get("response") or "")
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                calls.append(_call_summary(step=step, kind=kind, ok=False, api_call=True, error=error))
                rollouts.append({
                    "step": step,
                    "rollout": step + 1,
                    "kind": kind,
                    "api_call": True,
                    "ok": False,
                    "error": error,
                    "system_prompt": system_prompt,
                    "user_prompt": user_prompt,
                })
                break

        ev, extracted_code = ro._evaluate_response(response, row, task_dir, gen_timeout, test_timeout)
        passed = bool(ev.get("passed"))
        rollout_rec = {
            "step": step,
            "rollout": step + 1,
            "kind": kind,
            "api_call": api_call,
            "ok": True,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "raw_response": response,
            "extracted_code": extracted_code,
            "eval": ev,
        }
        rollout_rec.update(_token_record(resp))
        if ev.get("passed"):
            calls.append(_call_summary(step=step, kind=kind, ok=True, api_call=api_call, resp=resp))
            rollouts.append(rollout_rec)
            trace = {"task_id": task_id, "rollouts": rollouts, "calls": calls}
            return Trajectory(
                task_id=task_id,
                solved=True,
                n_iters=step + 1,
                mutation_log=mutation_log,
                final_response=response,
                final_solution=_sanitize_solution_for_distill(response, execution_mode),
                success_rate=round(1.0 / (step + 1), 4),
                calls=calls,
                trace=trace,
            )

        mutation_log.append(str(ev.get("error_type") or "unknown"))
        feedback = _feedback_from_eval(ev)
        rollout_rec["feedback_for_next_rollout"] = feedback
        calls.append(_call_summary(step=step, kind=kind, ok=True, api_call=api_call, resp=resp))
        rollouts.append(rollout_rec)
        if step == max_iters - 1:
            break

    trace = {"task_id": task_id, "rollouts": rollouts, "calls": calls}
    return Trajectory(
        task_id=task_id,
        solved=False,
        n_iters=len(mutation_log),
        mutation_log=mutation_log,
        final_response=response or "",
        final_solution=_sanitize_solution_for_distill(response or "", execution_mode),
        success_rate=0.0,
        calls=calls,
        trace=trace,
    )


# ---------------------------------------------------------------------------
# Stage 2: Distill (only from a verified-correct trajectory).
# ---------------------------------------------------------------------------

def distill_one(
    row: dict[str, Any],
    traj: Trajectory,
    public_task_md: str,
    private_hard: set[str],
    keys: dict[str, str],
    model_alias: str,
    max_tokens: int,
    attempts: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    full_calls: list[dict[str, Any]] = []
    mut = ", ".join(traj.mutation_log) if traj.mutation_log else "(passed on first attempt; no errors to overcome)"
    user = DISTILL_USER_TEMPLATE.format(
        task=gg._truncate(_task_text_for_distill_prompt(row, public_task_md), 3500),
        solution=gg._truncate(_solution_text_for_distill_prompt(row, traj.final_solution), 6000),
        mutation_log=mut,
    )
    last_err = ""
    for idx in range(max(1, attempts)):
        system = DISTILL_SYSTEM if idx == 0 else DISTILL_SYSTEM + DISTILL_RETRY_SUFFIX.format(error=last_err)
        stage = "distill" if idx == 0 else f"distill_retry_{idx}"
        try:
            # Distill = reformat the solved attempt into a structured gene payload.
            # No thinking: distill_max_tokens is small and thinking would truncate it.
            resp = gg._llm_chat(model_alias, user, system, keys, max_tokens=max_tokens, effort="off")
        except Exception as exc:
            last_err = f"api_error: {type(exc).__name__}: {exc}"
            calls.append({"stage": stage, "ok": False, "error": last_err})
            full_calls.append({
                "stage": stage,
                "ok": False,
                "error": last_err,
                "system_prompt": system,
                "user_prompt": user,
            })
            continue

        raw_response = str(resp.get("response") or "")
        redacted_private_literals = False
        try:
            payload = gg.soft_fix_payload(gg.parse_llm_json(raw_response))
            ok, err = validate_gene(payload, public_task_md, private_hard)
            if not ok and err.startswith("hidden-literal leakage"):
                redacted_payload = redact_private_literals(payload, private_hard)
                ok_after_redact, err_after_redact = validate_gene(redacted_payload, public_task_md, private_hard)
                if ok_after_redact:
                    payload = redacted_payload
                    ok = True
                    err = ""
                    redacted_private_literals = True
                else:
                    err = f"{err}; after_redaction: {err_after_redact}"
        except Exception as exc:
            payload = None
            ok = False
            err = f"parse_or_audit_error: {type(exc).__name__}: {exc}"

        summary = {"stage": stage, "ok": ok, "error": "" if ok else err}
        if redacted_private_literals:
            summary["redacted_private_literals"] = True
        summary.update(_token_record(resp))
        calls.append(summary)
        full_call = {
            "stage": stage,
            "ok": ok,
            "error": "" if ok else err,
            "system_prompt": system,
            "user_prompt": user,
            "raw_response": raw_response,
            "parsed_payload": payload,
            "redacted_private_literals": redacted_private_literals,
        }
        full_call.update(_token_record(resp))
        full_calls.append(full_call)
        if ok and payload is not None:
            return payload, calls, full_calls
        last_err = err
    return None, calls, full_calls


# ---------------------------------------------------------------------------
# Task selection (1:1, no split).
# ---------------------------------------------------------------------------

def select_per_family(rows: list[dict[str, Any]], per_family_limit: int, seed: int) -> list[dict[str, Any]]:
    """Deterministically cap how many tasks per family we generate genes for.
    0 = take all. This is the ONLY selection step: every selected task gets its
    own gene and is later evaluated on itself (1:1, no train/test split)."""
    if per_family_limit <= 0:
        return rows
    by_family: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_family.setdefault(str(r.get("family")), []).append(r)
    rng = random.Random(seed)
    out: list[dict[str, Any]] = []
    for fam in sorted(by_family):
        items = sorted(by_family[fam], key=lambda r: str(r.get("task_id")))
        rng.shuffle(items)
        out.extend(items[:per_family_limit])
    return out


def _evolved_counts_by_family(rows: list[dict[str, Any]], out_dir: Path) -> collections.Counter[str]:
    """Count existing evolved genes so resumed target-per-family runs can stop correctly."""
    by_task_id = {str(row.get("task_id")): str(row.get("family")) for row in rows}
    counts: collections.Counter[str] = collections.Counter()
    for task_id, family in by_task_id.items():
        gene_path = out_dir / f"{task_id}.json"
        if not gene_path.exists():
            continue
        try:
            asset = json.loads(gene_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if asset.get("generation_source") == "evolved":
            counts[family] += 1
    return counts


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------

def _load_seed_responses(cases_path: Path, cache_model: str) -> dict[str, str]:
    """Optional: reuse cached no_context responses as the evolve seed (step 0)
    to save one API call per task. Falls back to a fresh solve when absent."""
    out: dict[str, str] = {}
    if not cases_path.exists():
        return out
    by_key = gg._load_jsonl_by_trial_key(cases_path)
    prefix = f"{cache_model}::no_context::"
    for key, row in by_key.items():
        if key.startswith(prefix):
            tid = key[len(prefix):]
            out[tid] = str(row.get("raw_response") or row.get("extracted_code") or "")
    return out


def run(args: argparse.Namespace) -> int:
    pool_root = Path(args.pool_root).resolve()
    manifest_path = Path(args.manifest).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    _, all_rows = ro._load_manifest(manifest_path)
    rows = list(all_rows)
    if args.families:
        wanted = set(gg._csv_arg(args.families))
        rows = [r for r in rows if str(r.get("family")) in wanted]
    if args.ids:
        wanted = set(gg._csv_arg(args.ids))
        rows = [r for r in rows if str(r.get("task_id")) in wanted]

    # 1:1 setup: every selected task gets its own gene, evaluated on itself.
    task_rows = select_per_family(rows, args.per_family_limit, args.seed)

    keys = gg._resolve_keys(args)
    if MODEL_REGISTRY[args.model][1] == "gemini" and not keys["gemini_key"]:
        raise SystemExit("gemini key required (GEMINI_KEY/GEMINI_API_KEY/GOOGLE_API_KEY or --gemini-key)")
    if args.fallback_skill_for_no_ref and not args.fallback_reference:
        raise SystemExit("--fallback-skill-for-no-ref requires --fallback-reference")

    seed_responses = _load_seed_responses(Path(args.cases).resolve(), args.cache_model) if args.use_cache_seed else {}
    created_at = datetime.now(timezone.utc).isoformat()

    fam_counts = collections.Counter(str(r.get("family")) for r in task_rows)
    (out_dir / "selected_ids.json").write_text(
        json.dumps({"seed": args.seed, "ids": [str(r.get("task_id")) for r in task_rows]}, indent=2),
        encoding="utf-8")

    print(f"manifest: {manifest_path}")
    print(f"selected tasks (1:1 genes): {len(task_rows)}  per-family: {dict(fam_counts)}")
    print(f"model: {args.model} -> {MODEL_REGISTRY[args.model][0]}  max_iters: {args.max_iters}")
    print(f"out_dir: {out_dir}")

    if args.dry_run:
        for r in task_rows[:20]:
            tid = str(r.get("task_id"))
            print(f"  {tid:6s} {str(r.get('family')):16s} seed={'Y' if tid in seed_responses else 'N'}")
        print(f"  ... total {len(task_rows)}")
        return 0

    log_path = out_dir / "_evolve_log.jsonl"
    trace_dir = out_dir / "_traces"
    trace_dir.mkdir(parents=True, exist_ok=True)

    pending_rows = task_rows
    if args.skip_existing:
        pending_rows = [r for r in task_rows
                        if not (out_dir / f"{str(r.get('task_id'))}.json").exists()]
        print(f"skip-existing: {len(task_rows) - len(pending_rows)} genes already present; "
              f"{len(pending_rows)} remaining to generate")

    target_evolved = max(0, args.target_evolved_per_family)
    target_families = sorted({str(r.get("family")) for r in task_rows})
    evolved_by_family: collections.Counter[str] = collections.Counter()
    if target_evolved > 0 and args.skip_existing:
        evolved_by_family.update(_evolved_counts_by_family(task_rows, out_dir))
    if target_evolved > 0:
        print(f"target evolved genes per family: {target_evolved}")
        print(f"initial evolved counts: {dict(evolved_by_family)}")
        if args.workers != 1:
            print("target mode uses workers=1 so it can stop exactly when every family reaches target")
            args.workers = 1

    def target_reached() -> bool:
        return target_evolved > 0 and all(evolved_by_family[fam] >= target_evolved for fam in target_families)

    def process_task(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
        """Evolve+distill ONE task. Pure compute (LLM + verifier + distill); does
        no file/log writing so it is safe to run across a thread pool. Returns
        (log_record, {task_id, asset} or None, full_trace)."""
        task_id = str(row.get("task_id"))
        task_dir = ro._task_dir(row, pool_root)
        files = row.get("files") if isinstance(row.get("files"), dict) else {}
        public_task_md = ro._load_text(task_dir / (files.get("task") or "task.md"))
        execution_mode = str(row.get("execution_mode") or "")
        t0 = time.time()
        trace: dict[str, Any] = {
            "schema_version": "evolve_trace_v1",
            "created_at": created_at,
            "task_id": task_id,
            "family": str(row.get("family")),
            "execution_mode": execution_mode,
            "model_alias": args.model,
            "model_id": MODEL_REGISTRY[args.model][0],
            "run_config": {
                "max_iters": args.max_iters,
                "solve_max_tokens": args.solve_max_tokens,
                "distill_max_tokens": args.distill_max_tokens,
                "distill_attempts": args.distill_attempts,
                "gen_timeout": args.gen_timeout,
                "test_timeout": args.test_timeout,
                "use_cache_seed": args.use_cache_seed,
                "cache_model": args.cache_model,
            },
            "public_task_md": public_task_md,
        }

        if args.from_reference:
            ref = read_reference_solution(row, task_dir)
            distill_solution = _sanitize_solution_for_distill(ref, execution_mode)
            traj = Trajectory(task_id, bool(ref), 0, [], ref,
                              distill_solution, 0.0, [])
            source = "reference_distilled"
            rec: dict[str, Any] = {"task_id": task_id, "family": str(row.get("family")),
                                   "mode": "from_reference", "has_reference": bool(ref)}
            trace["reference"] = {
                "mode": "from_reference",
                "has_reference": bool(ref),
                "raw_reference": ref,
                "distill_solution": distill_solution,
            }
            if not ref:
                rec["status"] = "no_reference"
                trace["status"] = rec["status"]
                return rec, None, trace
        else:
            traj = evolve_one(
                row=row, pool_root=pool_root, keys=keys, model_alias=args.model,
                max_iters=args.max_iters, gen_timeout=args.gen_timeout, test_timeout=args.test_timeout,
                seed_response=seed_responses.get(task_id), max_tokens=args.solve_max_tokens,
            )
            trace["evolve"] = {
                **traj.trace,
                "solved": traj.solved,
                "n_iters": traj.n_iters,
                "success_rate": traj.success_rate,
                "mutation_log": traj.mutation_log,
            }
            rec = {"task_id": task_id, "family": str(row.get("family")),
                   "solved": traj.solved, "n_iters": traj.n_iters,
                   "mutation_log": traj.mutation_log, "success_rate": traj.success_rate,
                   "evolve_calls": traj.calls, "elapsed_s": round(time.time() - t0, 2)}
            if traj.solved:
                source = "evolved"
            elif args.fallback_reference:
                ref = read_reference_solution(row, task_dir)
                if not ref:
                    if args.fallback_skill_for_no_ref:
                        skill = read_skill_doc(row, task_dir)
                        if not skill:
                            rec["status"] = "unsolved_no_reference"
                            trace["status"] = rec["status"]
                            return rec, None, trace
                        traj = Trajectory(task_id, True, traj.n_iters, traj.mutation_log, skill,
                                          skill, 0.0, traj.calls, traj.trace)
                        trace["fallback_skill"] = {
                            "used": True,
                            "distill_solution": skill,
                        }
                        source = "skill_distilled"
                    else:
                        rec["status"] = "unsolved_no_reference"
                        trace["status"] = rec["status"]
                        return rec, None, trace
                else:
                    distill_solution = _sanitize_solution_for_distill(ref, execution_mode)
                    traj = Trajectory(task_id, True, traj.n_iters, traj.mutation_log, ref,
                                      distill_solution, 0.0, traj.calls, traj.trace)
                    trace["fallback_reference"] = {
                        "used": True,
                        "raw_reference": ref,
                        "distill_solution": distill_solution,
                    }
                    source = "reference_distilled"
            else:
                rec["status"] = "unsolved_no_gene"
                trace["status"] = rec["status"]
                return rec, None, trace

        private_hard = build_private_vocab(row, task_dir, public_task_md)
        payload, dcalls, dfull_calls = distill_one(
            row=row, traj=traj, public_task_md=public_task_md, private_hard=private_hard,
            keys=keys, model_alias=args.model, max_tokens=args.distill_max_tokens,
            attempts=args.distill_attempts,
        )
        rec["distill_calls"] = dcalls
        rec["source"] = source
        trace["source"] = source
        trace["distill"] = {"calls": dcalls, "full_calls": dfull_calls}
        if payload is None:
            rec["status"] = "distill_failed"
            trace["status"] = rec["status"]
            return rec, None, trace

        asset = gg.build_wrapper(payload, row, created_at, source, MODEL_REGISTRY[args.model][0])
        asset["pipeline_mode"] = "evolved_v3"
        asset["reads_reference"] = (source == "reference_distilled")
        asset["evolve"] = {"source": source, "solved": traj.solved, "n_iters": traj.n_iters,
                           "success_rate": traj.success_rate, "mutation_log": traj.mutation_log,
                           "calls": traj.calls}
        rec["status"] = "ok"
        trace["status"] = rec["status"]
        trace["asset_id"] = asset.get("asset_id")
        return rec, {"task_id": task_id, "asset": asset}, trace

    total = len(pending_rows)
    done = n_gene = n_evolved = n_ref = n_skill = 0

    def record_result(rec: dict[str, Any], gene: dict[str, Any] | None, trace: dict[str, Any]) -> None:
        nonlocal n_gene, n_evolved, n_ref, n_skill
        trace_file = f"_traces/{gg._safe_id(str(rec['task_id']))}.json"
        trace_path = out_dir / trace_file
        rec["trace_file"] = trace_file
        if gene is not None:
            gene["asset"].setdefault("evolve", {})["trace_file"] = trace_file
            trace["gene_file"] = f"{gene['task_id']}.json"
            (out_dir / f"{gene['task_id']}.json").write_text(
                json.dumps(gene["asset"], indent=2, ensure_ascii=False), encoding="utf-8")
            n_gene += 1
            if rec.get("source") == "evolved":
                n_evolved += 1
                evolved_by_family[str(rec.get("family"))] += 1
            elif rec.get("source") == "skill_distilled":
                n_skill += 1
            else:
                n_ref += 1
        trace_path.write_text(json.dumps(trace, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        log_fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        log_fh.flush()
        st = rec.get("status")
        src = rec.get("source", "")
        tag = f" ({src})" if (src and st == "ok") else ""
        counts = f" evolved_by_family={dict(evolved_by_family)}" if target_evolved > 0 else ""
        print(f"[{done}/{total}] {rec['task_id']} -> {st}{tag}{counts}", flush=True)

    with log_path.open("a" if args.skip_existing else "w", encoding="utf-8") as log_fh:
        if target_evolved > 0:
            for row in pending_rows:
                if target_reached():
                    print("target reached; stopping evolve run")
                    break
                family = str(row.get("family"))
                if evolved_by_family[family] >= target_evolved:
                    continue
                rec, gene, trace = process_task(row)
                done += 1
                record_result(rec, gene, trace)
        else:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = [pool.submit(process_task, row) for row in pending_rows]
                for fut in as_completed(futures):
                    rec, gene, trace = fut.result()
                    done += 1
                    record_result(rec, gene, trace)

    ids_csv = ",".join(str(r.get("task_id")) for r in task_rows)
    print()
    print("done.")
    print(f"  genes written: {n_gene}/{len(task_rows)}  (evolved={n_evolved}, reference_distilled={n_ref}, skill_distilled={n_skill})")
    if target_evolved > 0:
        print(f"  final evolved counts by family: {dict(evolved_by_family)}")
    print(f"  genes dir (feed to run_official --genes-dir): {out_dir}")
    print(f"  rollout traces: {trace_dir}")
    print(f"  selected ids: {out_dir / 'selected_ids.json'}")
    print()
    print("next: evaluate these tasks on their OWN genes (1:1), three conditions:")
    print(f"  python run_official.py --models gemini_pro,gemini_flash \\")
    print(f"    --conditions no_context,with_skill,with_gene \\")
    print(f"    --genes-dir {out_dir} --ids '{ids_csv if len(ids_csv) <= 200 else '<ids from selected_ids.json>'}'")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    p.add_argument("--pool-root", default=str(POOL_ROOT))
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    p.add_argument("--cases", default=str(DEFAULT_CASES), help="cached run for optional evolve seed")
    p.add_argument("--use-cache-seed", action="store_true", help="seed evolve step 0 from cached no_context response")
    p.add_argument("--cache-model", default="gemini_pro")
    p.add_argument("--model", default="gemini_pro", help="solver+distiller model alias")
    p.add_argument("--families", default="", help="comma-separated family filter")
    p.add_argument("--ids", default="", help="comma-separated task_id filter")
    p.add_argument("--seed", type=int, default=42, help="deterministic per-family sampling")
    p.add_argument("--per-family-limit", type=int, default=0, help="cap tasks per family (e.g. 50); 0 = all")
    p.add_argument("--target-evolved-per-family", type=int, default=0,
                   help="stop after this many generation_source=evolved genes per selected family; 0 = disabled")
    p.add_argument("--workers", type=int, default=4, help="parallel tasks (LLM+verifier are I/O bound)")
    p.add_argument("--skip-existing", action="store_true",
                   help="resume: skip tasks whose <task_id>.json already exists in out_dir (append to log)")
    p.add_argument("--max-iters", type=int, default=4, help="max evolve iterations (1 solve + up to K-1 mutations)")
    p.add_argument("--fallback-reference", action="store_true",
                   help="when evolve fails after K iters, distill from reference_solution instead of skipping")
    p.add_argument("--fallback-skill-for-no-ref", action="store_true",
                   help="with --fallback-reference, use sanitized SKILL.md as teacher only when no reference_solution exists")
    p.add_argument("--from-reference", action="store_true",
                   help="skip evolution; distill every gene directly from reference_solution (cheapest, ~1 call/task)")
    p.add_argument("--solve-max-tokens", type=int, default=32000,
                   help="max output tokens for solve/mutate calls (Bedrock hard ceiling = 32000)")
    p.add_argument("--distill-max-tokens", type=int, default=4096,
                   help="max output tokens for distill call")
    p.add_argument("--distill-attempts", type=int, default=3)
    p.add_argument("--gen-timeout", type=int, default=120)
    p.add_argument("--test-timeout", type=int, default=180)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--gemini-key", default="")
    p.add_argument("--yunwu-key", default="")
    p.add_argument("--siliconflow-key", default="")
    p.add_argument("--evomap-key", default="")
    p.add_argument("--bedrock-key", default="")
    p.add_argument("--local-base-url", default="")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv or sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())

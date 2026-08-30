#!/usr/bin/env python3
"""Generate answer-free static Agent Skill assets for LongWoF-Bench.

This is the Skill baseline that mirrors common market Agent Skill practice:
a strong model authors a static task manual from allowed task materials. The
recommended teacher-artifact mode lets the author model inspect real
development/evaluation artifacts (tests, reference solution, gold output
schemas, scenario metadata, optional feedback), but the generated Skill is
audited so the solver never receives final answers or verifier-private output.

Output:
  - ``<out_dir>/<task_id>.md`` archive copies.
  - optionally ``scenarios/<task_id>/AGENT_SKILL.md`` via ``--write-inplace``.
  - optionally ``<out_dir>/manifest_agent_skill.json`` via ``--emit-manifest``;
    this patches ``files.agent_skill`` and is consumed by
    ``run_official.py --conditions with_public_agent_skill``.
"""

from __future__ import annotations

import argparse
import csv
import collections
import copy
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
V3_ROOT = HERE.parent
POOL_ROOT = V3_ROOT / "tasks_final"
DEFAULT_MANIFEST = POOL_ROOT / "manifest.json"
DEFAULT_OUT_DIR = POOL_ROOT / "agent_skills"

# Keep this eval directory first so sibling imports resolve to this tree's API
# registry, including Bedrock aliases such as bedrock_opus.
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import run_official as ro  # noqa: E402
import gen_genes_llm_v3 as gg  # noqa: E402
import evolve_genes_v3 as ev  # noqa: E402
from api import MODEL_REGISTRY  # noqa: E402

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


AGENT_SKILL_SYSTEM = """You author a STATIC Agent Skill for a benchmark task.

The skill must resemble common production Agent Skill documentation: a concise
manual that explains the public interface, the recommended workflow, expected
failure modes, and output contract. It is a static developer-authored skill, not
an experiential Gene and not an oracle solution.

Source boundary:
- Use ONLY the public task prompt and the supplied allowed authoring context.
- The authoring context may include sensitive development or evaluation-side
  artifacts. Use them only to infer answer-free workflow guidance, interface
  contracts, schema expectations, edge cases, and common failure modes.
- Treat the allowed authoring context as authoritative for operational
  behavior. The public prompt is the user-facing request; the authoring context
  may refine, specialize, or correct its procedural details.
- Do not mention the source artifacts themselves, such as tests, reference
  solutions, gold files, validators, graders, hidden checks, or feedback logs.
- Extract concrete interfaces, file formats, columns, config fields,
  documented package APIs, non-obvious constraints, and workflow order. Do not
  merely restate the task prompt.
- For code and agent tasks, include concrete algorithmic formulas, thresholds,
  ordering rules, tie-breakers, and edge-case handling from the allowed
  authoring context when they are process rules rather than final output values.
- If the allowed authoring context reveals a more specific validated behavior
  than the public prompt, write the behavior as an operational task convention;
  do not describe it as a discrepancy or cite the source artifact.
- Do not preserve a public formula, threshold, ordering rule, or boundary rule
  when the allowed authoring context gives a different executable behavior.
- If an allowed task-author manual is provided, preserve its procedural rules,
  precedence rules, interface notes, and common pitfalls, but rewrite them in
  answer-free form without evaluator-specific wording.

Leakage rules:
- Never include a final answer, answer-option token, expected answer, answer
  key, or task-specific decision label.
- Never mention oracle, hidden tests, checker, validator, grader, gold data,
  reference solution, private constants, or verifier expectations.
- Never repeat redaction placeholders such as `<redacted>` or
  `<final_answer_redacted>`.
- You may mention allowed file names, CLI flags, columns, package names, output
  fields, config keys, APIs, and format contracts that appear in the supplied
  task context.
- For rule-following tasks, give a generic rule-application process. Do not
  invent missing private rules or pick a winning answer option.
- Avoid illustrative numeric constants, thresholds, answer labels, or enum
  values unless they are explicitly part of the public task interface. Prefer
  generic placeholders such as `<value>` when a concrete value is unnecessary.

Output format:
- Markdown only.
- Use EXACTLY these headings in order:
  ## Overview
  ## When to Use
  ## Public Interface
  ## Workflow
  ## Common Pitfalls
  ## Output Contract
- Do not wrap the document in code fences. Do not add preamble or commentary."""

STRICT_PUBLIC_DOCS_SUFFIX = """

Strict public-docs mode:
- This Skill must model what a careful human author could write from the public
  task prompt plus public documentation/data samples, without inspecting task
  implementation source code.
- Do not include exact package import paths, class names, method names, function
  signatures, default parameter values, keyword-only constraints, internal
  attributes, hidden behaviors, or implementation-specific workarounds unless
  they are explicitly stated in the public task prompt or non-source
  documentation supplied in the context.
- If the public materials do not document a library API, describe the workflow
  at the conceptual level and avoid fabricating concrete API calls.
- Prefer stable input/output contracts, schema details, generic algorithms,
  validation checks, and user-visible pitfalls over source-derived internals."""

AGENT_SKILL_USER_TEMPLATE = """--- public task prompt ---
{task}

{public_context_block}
Write the Agent Skill now. Output only the markdown."""

RETRY_SUFFIX = """

PREVIOUS OUTPUT WAS REJECTED:
{error}

Regenerate the Agent Skill. Remove every evaluator-related term, answer-option
token, task-specific literal, concrete numeric example, or non-public claim.
Keep exactly the required headings and write only markdown."""

REQUIRED_HEADINGS = [
    "## Overview",
    "## When to Use",
    "## Public Interface",
    "## Workflow",
    "## Common Pitfalls",
    "## Output Contract",
]

FORBIDDEN_PATTERNS = {
    "hidden": re.compile(r"\bhidden\b", re.IGNORECASE),
    "oracle": re.compile(r"\boracle\b", re.IGNORECASE),
    "checker": re.compile(r"\bchecker\b", re.IGNORECASE),
    "validator": re.compile(r"\bvalidator\b", re.IGNORECASE),
    "grader": re.compile(r"\bgrader\b", re.IGNORECASE),
    "gold": re.compile(r"\bgold\b", re.IGNORECASE),
    "reference_solution": re.compile(r"\breference solution\b", re.IGNORECASE),
    "answer_key": re.compile(r"\banswer key\b", re.IGNORECASE),
    "expected_answer": re.compile(r"\bexpected answer\b", re.IGNORECASE),
    "canonical_answer": re.compile(r"\bcanonical answer\b", re.IGNORECASE),
    "private": re.compile(r"\bprivate\b", re.IGNORECASE),
    "test_script": re.compile(r"\btest_script\.py\b", re.IGNORECASE),
    "scenario_yaml": re.compile(r"\bscenario\.ya?ml\b", re.IGNORECASE),
    "gold_file": re.compile(r"\b_gold\b", re.IGNORECASE),
    "redacted": re.compile(r"<\s*(?:final_answer_)?redacted\s*>", re.IGNORECASE),
    "public_prompt": re.compile(r"\bpublic prompt\b", re.IGNORECASE),
    "authoring_context": re.compile(r"\bauthoring context\b", re.IGNORECASE),
}

STRICT_PUBLIC_DOC_FORBIDDEN_PATTERNS = {
    "keyword_only": re.compile(r"\bkeyword[- ]only\b", re.IGNORECASE),
    "keyword_arguments": re.compile(r"\bkeyword arguments?\b", re.IGNORECASE),
    "internal_attribute": re.compile(r"\binternal attributes?\b|\bunderlying (?:data|array)\b|\.(?:data|values)\b", re.IGNORECASE),
    "hidden_behavior": re.compile(r"\bhidden behaviors?\b|\bimplementation-specific\b", re.IGNORECASE),
    "api_speculation": re.compile(
        r"\bmay require\b[^.\n]*(?:parameters?|arguments?)\b|"
        r"\bmay return\b[^.\n]*(?:tuple|custom object|complex type)\b|"
        r"\bif required by the package\b",
        re.IGNORECASE,
    ),
}

PUBLIC_CONTEXT_EXCLUDE_NAMES = {
    "SKILL.md",
    "SKILL_oracle.md",
    "SKILL_sanitized.md",
    "AGENT_SKILL.md",
    "reference_solution.py",
    "reference_solution.sh",
    "test_script.py",
    "scenario.yaml",
    "metadata.json",
    "_gold.json",
    "_variants.json",
    "_run_record.json",
}

PUBLIC_ASSET_ROOTS = {"data", "environment", "package", "skill", "docs", "doc"}
PUBLIC_DOC_ASSET_ROOTS = {"data", "environment", "skill", "docs", "doc"}
PUBLIC_DOC_TEXT_SUFFIXES = {
    ".csv", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".conf", ".nml", ".txt", ".md", ".rst",
}
TEXT_SUFFIXES = {
    ".csv", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".conf", ".nml", ".txt", ".md", ".rst", ".py", ".sh", ".sql", ".Dockerfile",
}
_TEACHER_FEEDBACK_BY_TASK: dict[str, list[str]] = {}
DEFAULT_TEACHER_ARTIFACT_SOURCES = "scenario,reference,test,gold,feedback"
VALID_TEACHER_ARTIFACT_SOURCES = {"scenario", "reference", "test", "gold", "feedback"}

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n(.*?)\n```\s*$", re.DOTALL)


def _strip_outer_fence(text: str) -> str:
    text = (text or "").strip()
    match = _FENCE_RE.match(text)
    if match:
        return match.group(1).strip()
    return text


def _read_public_context(task_dir: Path, globs: str, max_chars: int) -> str:
    if not globs.strip() or max_chars <= 0:
        return ""
    chunks: list[str] = []
    remaining = max_chars
    for pattern in gg._csv_arg(globs):
        for path in sorted(task_dir.glob(pattern)):
            if remaining <= 0:
                break
            if not path.is_file() or path.name in PUBLIC_CONTEXT_EXCLUDE_NAMES:
                continue
            rel = path.relative_to(task_dir)
            if any(part.startswith("_gold") or part == "__pycache__" for part in rel.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if not text:
                continue
            snippet = text[:remaining]
            chunks.append(f"--- public context: {rel} ---\n{snippet}")
            remaining -= len(snippet)
    if not chunks:
        return ""
    return "\n".join(chunks)


def _is_hidden_or_generated_context_path(rel: Path) -> bool:
    if any(part == "__pycache__" or part.startswith("_gold") for part in rel.parts):
        return True
    if rel.name in PUBLIC_CONTEXT_EXCLUDE_NAMES and not (rel.parts and rel.parts[0] == "skill" and rel.name == "SKILL.md"):
        return True
    if rel.name.endswith((".pyc", ".pyo", ".so", ".dll", ".dylib", ".exe", ".nc")):
        return True
    return False


def _is_public_asset_path(rel: Path) -> bool:
    if _is_hidden_or_generated_context_path(rel):
        return False
    if rel.parts and rel.parts[0] in PUBLIC_ASSET_ROOTS:
        return True
    if rel.name.lower().startswith("readme") and rel.suffix.lower() in {".md", ".txt", ".rst"}:
        return True
    return False


def _is_public_doc_asset_path(rel: Path) -> bool:
    if _is_hidden_or_generated_context_path(rel):
        return False
    if rel.parts and rel.parts[0] == "package":
        return False
    if rel.parts and rel.parts[0] in PUBLIC_DOC_ASSET_ROOTS:
        return True
    if rel.name.lower().startswith("readme") and rel.suffix.lower() in {".md", ".txt", ".rst"}:
        return True
    return False


def _read_text_limited(path: Path, max_chars: int) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except OSError:
        return ""


def _summarize_csv(path: Path, max_rows: int = 8, max_chars: int = 2200) -> str:
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.reader(fh)
            rows: list[list[str]] = []
            for idx, row in enumerate(reader):
                rows.append(row[:20])
                if idx >= max_rows:
                    break
    except (OSError, csv.Error, UnicodeError):
        return ""
    if not rows:
        return ""
    lines = [",".join(row) for row in rows]
    return "\n".join(lines)[:max_chars]


def _summarize_public_asset_file(path: Path, rel: Path, max_chars: int, *, allow_source_code: bool = True) -> str:
    suffix = path.suffix.lower()
    name = path.name
    if suffix == ".csv":
        body = _summarize_csv(path, max_chars=min(max_chars, 2200))
        kind = "csv header/sample"
    elif allow_source_code and (
        suffix in TEXT_SUFFIXES or name in {"Dockerfile", "Makefile"} or rel.parts[:1] == ("skill",)
    ):
        body = _read_text_limited(path, min(max_chars, 3000))
        kind = "text"
    elif not allow_source_code and (
        suffix in PUBLIC_DOC_TEXT_SUFFIXES or name in {"Dockerfile", "Makefile"} or rel.parts[:1] == ("skill",)
    ):
        body = _read_text_limited(path, min(max_chars, 3000))
        kind = "public doc/data text"
    else:
        return ""
    body = body.strip()
    if not body:
        return ""
    return f"--- allowed public asset ({kind}): {rel} ---\n{body}"


def _collect_public_asset_context(task_dir: Path, max_chars: int, *, docs_only: bool = False) -> str:
    if max_chars <= 0:
        return ""
    chunks: list[str] = []
    remaining = max_chars
    for path in sorted(task_dir.rglob("*")):
        if remaining <= 0:
            break
        if not path.is_file():
            continue
        rel = path.relative_to(task_dir)
        if docs_only:
            if not _is_public_doc_asset_path(rel):
                continue
        elif not _is_public_asset_path(rel):
            continue
        chunk = _summarize_public_asset_file(path, rel, remaining, allow_source_code=not docs_only)
        if not chunk:
            continue
        chunk = chunk[:remaining]
        chunks.append(chunk)
        remaining -= len(chunk)
    return "\n\n".join(chunks)


def _redact_answer_options(text: str, answer_options: set[str]) -> str:
    out = text
    for opt in sorted(answer_options, key=len, reverse=True):
        if len(re.findall(r"[A-Za-z0-9]", opt)) < 2:
            continue
        out = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(opt)}(?![A-Za-z0-9_])",
            "<answer_option>",
            out,
            flags=re.IGNORECASE,
        )
    return out


def _json_scalar_strings(value: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(value, dict):
        for item in value.values():
            out.update(_json_scalar_strings(item))
    elif isinstance(value, list):
        for item in value:
            out.update(_json_scalar_strings(item))
    elif isinstance(value, (str, int, float)):
        s = str(value).strip()
        if s:
            out.add(s)
    return out


def _is_public_literal(text: str, public_task_md: str) -> bool:
    literal = (text or "").strip()
    if not literal:
        return True
    return literal.lower() in (public_task_md or "").lower()


def _add_leak_literal(
    out: set[str],
    value: Any,
    public_task_md: str,
    *,
    always: bool = False,
    include_numbers: bool = True,
) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _add_leak_literal(out, item, public_task_md, always=always, include_numbers=include_numbers)
        return
    if isinstance(value, list):
        for item in value:
            _add_leak_literal(out, item, public_task_md, always=always, include_numbers=include_numbers)
        return
    if not isinstance(value, (str, int, float)):
        return
    literal = str(value).strip()
    if not literal or len(literal) > 500:
        return
    if len(re.findall(r"[A-Za-z0-9]", literal)) < 2:
        return
    if not include_numbers and ev._NUM_RE.fullmatch(literal):
        return
    if not always and _is_public_literal(literal, public_task_md):
        return
    out.add(literal)


def _collect_output_string_literals(text: str, public_task_md: str, *, include_numbers: bool = False) -> set[str]:
    """Collect likely answer/output values from a serialized target artifact.

    Schema names and public enum labels are intentionally not treated as leaks;
    row values and scalar output leaves are.
    """
    out: set[str] = set()
    raw = (text or "").strip()
    if not raw:
        return out

    try:
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001
        parsed = None
    if parsed is not None:
        _add_leak_literal(out, parsed, public_task_md, include_numbers=include_numbers)
        return out

    first_line = raw.splitlines()[0] if raw.splitlines() else ""
    if "," in first_line and len(raw.splitlines()) > 1:
        try:
            reader = csv.DictReader(raw.splitlines())
            for row in reader:
                for value in row.values():
                    _add_leak_literal(out, value, public_task_md, include_numbers=include_numbers)
            return out
        except csv.Error:
            pass

    for line in raw.splitlines():
        cleaned = line.strip()
        if cleaned:
            _add_leak_literal(out, cleaned, public_task_md, include_numbers=include_numbers)
    return out


def _collect_gold_tree_literals(task_dir: Path, public_task_md: str) -> set[str]:
    out: set[str] = set()
    root = task_dir / "_gold"
    if root.exists():
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            suffix = path.suffix.lower()
            if suffix == ".csv":
                out.update(_collect_output_string_literals(raw, public_task_md, include_numbers=False))
            elif suffix in {".json", ".jsonl", ".yaml", ".yml", ".txt", ".md"}:
                out.update(_collect_output_string_literals(raw, public_task_md, include_numbers=False))
    gold_json = task_dir / "_gold.json"
    if gold_json.exists():
        try:
            data = json.loads(gold_json.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = None
        if data is not None:
            _add_leak_literal(out, data, public_task_md, always=True)
    return out


def _collect_python_gold_literals(path: Path, public_task_md: str) -> set[str]:
    out: set[str] = set()
    if not path.exists():
        return out
    try:
        import ast

        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return out
    target_names = {"gold", "expected", "answer", "canonical"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names: list[str] = []
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.append(target.id.lower())
        if not names or not any(any(marker in name for marker in target_names) for name in names):
            continue
        try:
            value = ast.literal_eval(node.value)
        except Exception:  # noqa: BLE001
            continue
        for literal in _json_scalar_strings(value):
            out.update(_collect_output_string_literals(literal, public_task_md, include_numbers=False))
    return out


def _collect_answer_literals(task_dir: Path, public_task_md: str = "") -> set[str]:
    literals: set[str] = set()
    for path in (task_dir / "_gold.json", task_dir / "scenario.yaml"):
        if not path.exists():
            continue
        try:
            if path.suffix in {".yaml", ".yml"} and HAS_YAML:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            else:
                data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict):
            for key in ("gold_answer", "expected_answer", "canonical_answer", "answer_key", "stdout"):
                if key in data:
                    _add_leak_literal(literals, data[key], public_task_md, always=True)
            if "answer_space" in data:
                _add_leak_literal(literals, data["answer_space"], public_task_md)
    literals.update(_collect_gold_tree_literals(task_dir, public_task_md))
    literals.update(_collect_python_gold_literals(task_dir / "test_script.py", public_task_md))
    return literals


def _redact_literal_values(text: str, literals: set[str]) -> str:
    out = text
    for lit in sorted(literals, key=len, reverse=True):
        if len(re.findall(r"[A-Za-z0-9]", lit)) < 2 or len(lit) > 500:
            continue
        out = out.replace(lit, "<final_answer_redacted>")
        if "\n" not in lit:
            out = re.sub(re.escape(lit), "<final_answer_redacted>", out, flags=re.IGNORECASE)
    return out


def _sanitize_teacher_text(text: str, answer_options: set[str], answer_literals: set[str]) -> str:
    text = _redact_literal_values(text or "", answer_literals)
    text = _redact_answer_options(text, answer_options)
    replacements = [
        (r"\bhidden_convention", "non_obvious_convention"),
        (r"\bhidden_", "non_obvious_"),
        (r"\bhidden\b", "non-obvious"),
        (r"\boracle\b", "task-author"),
        (r"\btest_script\.py\b", "development check file"),
        (r"\breference_solution\b", "worked solution"),
        (r"\breference solution\b", "worked solution"),
        (r"\bgold_answer\b", "target answer"),
        (r"\bexpected_answer\b", "target answer"),
        (r"\bGOLD_DATA\b", "TARGET_DATA"),
        (r"\bGOLD_", "TARGET_"),
        (r"\bgold_", "target_"),
        (r"\bgold data\b", "target data"),
        (r"\bgold\b", "target"),
        (r"\bchecker|validator|grader\b", "evaluation tool"),
        (r"\bprivate\b", "internal"),
    ]
    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
    text = re.sub(r"(expected\s*=\s*)['\"][^'\"]+['\"]", r"\1'<redacted>'", text, flags=re.IGNORECASE)
    text = re.sub(r"(got\s*=\s*)['\"][^'\"]+['\"]", r"\1'<candidate_output>'", text, flags=re.IGNORECASE)
    text = re.sub(r"(expected\s*[:=]\s*)([^,;\n\r]+)", r"\1<redacted>", text, flags=re.IGNORECASE)
    text = re.sub(r"(got\s*[:=]\s*)([^,;\n\r]+)", r"\1<candidate_output>", text, flags=re.IGNORECASE)
    text = re.sub(r"(ANSWER:\s*)[^\n\r]+", r"\1<redacted>", text, flags=re.IGNORECASE)
    return text


def _read_sanitized_teacher_file(
    path: Path,
    rel_label: str,
    answer_options: set[str],
    answer_literals: set[str],
    max_chars: int,
) -> str:
    if max_chars <= 0 or not path.exists() or not path.is_file():
        return ""
    text = _read_text_limited(path, max_chars)
    if not text.strip():
        return ""
    text = _sanitize_teacher_text(text, answer_options, answer_literals)
    return f"--- allowed authoring excerpt: {rel_label} ---\n{text[:max_chars]}"


def _json_shape(value: Any, depth: int = 0) -> str:
    if depth >= 3:
        return type(value).__name__
    if isinstance(value, dict):
        if not value:
            return "object{}"
        parts = []
        for key, item in list(value.items())[:12]:
            parts.append(f"{key}: {_json_shape(item, depth + 1)}")
        suffix = ", ..." if len(value) > 12 else ""
        return "object{" + ", ".join(parts) + suffix + "}"
    if isinstance(value, list):
        if not value:
            return "list[]"
        return f"list[{len(value)}] of {_json_shape(value[0], depth + 1)}"
    return type(value).__name__


def _summarize_csv_schema(path: Path, max_chars: int = 900) -> str:
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.reader(fh)
            header = next(reader, [])
    except (OSError, csv.Error, UnicodeError):
        return ""
    if not header:
        return ""
    return ("csv columns: " + ", ".join(header[:30]))[:max_chars]


def _summarize_gold_artifacts(task_dir: Path, answer_options: set[str], answer_literals: set[str], max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    chunks: list[str] = []
    remaining = max_chars
    root = task_dir / "_gold"
    if root.exists():
        for path in sorted(root.rglob("*")):
            if remaining <= 0:
                break
            if not path.is_file():
                continue
            rel = path.relative_to(task_dir)
            suffix = path.suffix.lower()
            body = ""
            if suffix == ".csv":
                body = _summarize_csv_schema(path, max_chars=min(remaining, 900))
            elif suffix in {".json", ".jsonl", ".yaml", ".yml", ".txt"}:
                raw = _read_text_limited(path, min(max(remaining, 1200), 5000))
                parsed: Any | None = None
                if suffix in {".yaml", ".yml"} and HAS_YAML:
                    try:
                        parsed = yaml.safe_load(raw)
                    except Exception:  # noqa: BLE001
                        parsed = None
                else:
                    try:
                        parsed = json.loads(raw)
                    except Exception:  # noqa: BLE001
                        parsed = None
                if parsed is not None:
                    body = "data shape: " + _json_shape(parsed)
                else:
                    nonempty = len([line for line in raw.splitlines() if line.strip()])
                    body = f"text output shape: {nonempty} non-empty lines"
            if not body.strip():
                continue
            body = _sanitize_teacher_text(body, answer_options, answer_literals)
            rel_clean = "/".join(part for part in rel.parts if not part.startswith("_gold"))
            chunk = f"--- target output schema summary: {rel_clean or path.name} ---\n{body}"
            chunk = chunk[:remaining]
            chunks.append(chunk)
            remaining -= len(chunk)
    gold_json = task_dir / "_gold.json"
    if remaining > 0 and gold_json.exists():
        raw = _read_text_limited(gold_json, min(max(remaining, 1200), 5000))
        try:
            parsed = json.loads(raw)
            body = "data shape: " + _json_shape(parsed)
        except Exception:  # noqa: BLE001
            body = "single target output record"
        body = _sanitize_teacher_text(body, answer_options, answer_literals)
        chunks.append(f"--- target answer schema summary ---\n{body[:remaining]}")
    return "\n\n".join(chunks)


def _strip_large_literal_assignments(text: str) -> str:
    lines = (text or "").splitlines()
    out: list[str] = []
    skipping = False
    depth = 0
    skip_name = ""
    for line in lines:
        if not skipping:
            match = re.match(r"^([A-Z][A-Z0-9_]*|[A-Za-z_]*EXPECTED[A-Za-z0-9_]*|[A-Za-z_]*GOLD[A-Za-z0-9_]*)\s*=\s*([\[{])", line)
            if match:
                skip_name = match.group(1)
                opener = match.group(2)
                closer = "}" if opener == "{" else "]"
                depth = line.count(opener) - line.count(closer)
                out.append(f"{skip_name} = <case/output literals redacted>")
                if depth > 0:
                    skipping = True
                continue
            out.append(line)
            continue

        if re.match(r"^(def|class)\s+", line):
            skipping = False
            skip_name = ""
            out.append(line)
            continue
        depth += line.count("{") + line.count("[") - line.count("}") - line.count("]")
        if depth <= 0:
            skipping = False
            skip_name = ""
    return "\n".join(out)


def _summarize_test_script(
    path: Path,
    answer_options: set[str],
    answer_literals: set[str],
    max_chars: int,
) -> str:
    if max_chars <= 0 or not path.exists():
        return ""
    raw = _read_text_limited(path, max(max_chars * 2, 8000))
    raw = _strip_large_literal_assignments(raw)
    raw = _sanitize_teacher_text(raw, answer_options, answer_literals)
    selected: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        keep = False
        if re.match(r"^(def|class)\s+", stripped):
            keep = True
        elif stripped.startswith(("import ", "from ")):
            keep = True
        elif re.match(r"^[A-Z][A-Z0-9_]*\s*=", stripped):
            keep = True
        elif any(token in stripped for token in (
            "argparse", "subprocess.run", "sys.executable", "timeout=",
            "os.path.exists", "Path(", "open(", "json.load", "json.loads",
            "csv.DictReader", "math.isclose", "rel_tol", "abs_tol",
            "print(", "PASS:", "FAIL:", "SCORE:", "assert ",
            "return False", "return True",
        )):
            keep = True
        elif any(token in stripped.lower() for token in (
            "candidate", "output", "deliverable", "required", "missing",
            "mismatch", "columns", "keys mismatch", "row keys",
        )):
            keep = True
        if keep:
            selected.append(line[:220])
        if len("\n".join(selected)) >= max_chars:
            break
    if not selected:
        return ""
    body = "\n".join(selected)[:max_chars]
    return f"--- allowed authoring excerpt: development check behavior summary ---\n{body}"


def _collect_teacher_metadata_context(task_dir: Path, answer_options: set[str], max_chars: int) -> str:
    """Read non-answer task-author metadata.

    This is intentionally a separate context policy from pure public assets:
    scenario metadata can describe traps and non-obvious conventions, but it
    must not expose expected answers, answer keys, gold outputs, or verifier
    code.
    """
    if max_chars <= 0:
        return ""
    scenario_path = task_dir / "scenario.yaml"
    if not scenario_path.exists() or not HAS_YAML:
        return ""
    try:
        data = yaml.safe_load(scenario_path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return ""
    allowed: dict[str, Any] = {}
    for key in ("name", "family", "domain", "difficulty", "chain_depth", "tags"):
        if key in data:
            allowed[key] = data[key]
    if "hidden_convention_count" in data:
        allowed["non_obvious_convention_count"] = data.get("hidden_convention_count")
    if "trap_summary" in data:
        allowed["non_answer_trap_summary"] = data.get("trap_summary")
    if not allowed:
        return ""
    text = json.dumps(allowed, ensure_ascii=False, indent=2)
    text = re.sub(r"\bhidden\b", "non-obvious", text, flags=re.IGNORECASE)
    text = re.sub(r"\bexpected\b", "target", text, flags=re.IGNORECASE)
    text = _redact_answer_options(text, answer_options)
    return f"--- allowed non-answer task metadata: scenario summary ---\n{text[:max_chars]}"


def _collect_task_manual_context(task_dir: Path, answer_options: set[str], max_chars: int) -> str:
    """Read the task's existing manual as non-evaluation teacher material.

    This policy intentionally uses a stronger source than pure public assets,
    but still excludes final evaluation artifacts. The manual is redacted before
    authoring so answer option strings and evaluator terminology are not copied.
    """
    if max_chars <= 0:
        return ""
    manual_path = task_dir / "SKILL.md"
    if not manual_path.exists():
        return ""
    text = manual_path.read_text(encoding="utf-8", errors="replace")
    text = _redact_answer_options(text, answer_options)
    text = re.sub(r"\bhidden\b", "non-obvious", text, flags=re.IGNORECASE)
    text = re.sub(r"\boracle\b", "task-author", text, flags=re.IGNORECASE)
    text = re.sub(r"\bchecker|validator|grader\b", "evaluation tool", text, flags=re.IGNORECASE)
    text = re.sub(r"\bgold\b", "target", text, flags=re.IGNORECASE)
    return f"--- allowed task-author manual: SKILL.md ---\n{text[:max_chars]}"


def _collect_teacher_artifacts_context(
    row: dict[str, Any],
    task_dir: Path,
    public_task_md: str,
    answer_options: set[str],
    max_chars: int,
    sources: set[str],
) -> str:
    if max_chars <= 0:
        return ""
    answer_literals = _collect_answer_literals(task_dir, public_task_md)
    chunks: list[str] = []
    remaining = max_chars

    scenario = task_dir / "scenario.yaml"
    if "scenario" in sources and scenario.exists() and remaining > 0:
        chunk = _read_sanitized_teacher_file(
            scenario,
            "scenario metadata",
            answer_options,
            answer_literals,
            min(remaining, 1600),
        )
        if chunk:
            chunks.append(chunk)
            remaining -= len(chunk)

    if "reference" in sources:
        for name in ("reference_solution.py", "reference_solution.sh"):
            path = task_dir / name
            if path.exists() and remaining > 0:
                chunk = _read_sanitized_teacher_file(
                    path,
                    "worked solution excerpt",
                    answer_options,
                    answer_literals,
                    min(remaining, 7000),
                )
                if chunk:
                    chunks.append(chunk)
                    remaining -= len(chunk)

    test_path = task_dir / "test_script.py"
    if "test" in sources and test_path.exists() and remaining > 0:
        chunk = _summarize_test_script(
            test_path,
            answer_options,
            answer_literals,
            min(remaining, 3000),
        )
        if chunk:
            chunks.append(chunk)
            remaining -= len(chunk)

    if "gold" in sources and remaining > 0:
        gold_summary = _summarize_gold_artifacts(task_dir, answer_options, answer_literals, min(remaining, 2500))
        if gold_summary:
            chunks.append(gold_summary)
            remaining -= len(gold_summary)

    if "feedback" in sources:
        feedback_chunks = _TEACHER_FEEDBACK_BY_TASK.get(str(row.get("task_id")), [])
        for feedback in feedback_chunks:
            if remaining <= 0:
                break
            cleaned = _sanitize_teacher_text(feedback, answer_options, answer_literals)
            chunk = f"--- allowed authoring excerpt: prior evaluation feedback ---\n{cleaned[:remaining]}"
            chunks.append(chunk)
            remaining -= len(chunk)

    return "\n\n".join(chunks)


def _load_teacher_feedback(path_value: str) -> dict[str, list[str]]:
    if not path_value.strip():
        return {}
    path = Path(path_value).resolve()
    if not path.exists():
        raise SystemExit(f"teacher feedback results file not found: {path}")
    feedback: dict[str, list[str]] = collections.defaultdict(list)
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        trial = record.get("trial") if isinstance(record.get("trial"), dict) else {}
        eval_payload = record.get("eval") if isinstance(record.get("eval"), dict) else {}
        task_id = str(trial.get("task_id") or "").strip()
        if not task_id:
            continue
        condition = str(trial.get("condition") or "")
        # Prefer no-context failures: they describe what a solver missed before
        # auxiliary guidance, without being conditioned on a particular Skill/Gene.
        if condition and condition != "no_context":
            continue
        text = "\n".join(
            part
            for part in (
                f"model={trial.get('model')} condition={condition} error_type={eval_payload.get('error_type')}",
                str(eval_payload.get("stdout_tail") or "")[-1200:],
                str(eval_payload.get("stderr_tail") or "")[-800:],
            )
            if part
        ).strip()
        if text:
            feedback[task_id].append(text[:2200])
    return dict(feedback)


def _parse_teacher_artifact_sources(value: str) -> set[str]:
    sources = set(gg._csv_arg(value or DEFAULT_TEACHER_ARTIFACT_SOURCES))
    unknown = sources - VALID_TEACHER_ARTIFACT_SOURCES
    if unknown:
        raise SystemExit(
            "unknown --teacher-artifact-sources values: "
            + ", ".join(sorted(unknown))
            + f"; valid: {sorted(VALID_TEACHER_ARTIFACT_SOURCES)}"
        )
    if not sources:
        raise SystemExit("--teacher-artifact-sources cannot be empty for context-policy=teacher_artifacts")
    return sources


def _build_allowed_context(
    row: dict[str, Any],
    task_dir: Path,
    public_task_md: str,
    context_policy: str,
    public_context_globs: str,
    public_context_max_chars: int,
    teacher_artifact_sources: set[str],
) -> str:
    answer_options = ev.extract_public_answer_options(public_task_md)
    budget = public_context_max_chars
    chunks: list[str] = []
    manual = _read_public_context(task_dir, public_context_globs, budget)
    if manual:
        chunks.append(manual)
        budget = max(0, budget - len(manual))
    if context_policy in {"public_assets", "public_docs", "teacher_metadata"}:
        asset_context = _collect_public_asset_context(task_dir, budget, docs_only=context_policy == "public_docs")
        if asset_context:
            chunks.append(asset_context)
            budget = max(0, budget - len(asset_context))
    if context_policy == "teacher_metadata":
        metadata_context = _collect_teacher_metadata_context(task_dir, answer_options, budget)
        if metadata_context:
            chunks.append(metadata_context)
    if context_policy == "task_manual":
        manual_context = _collect_task_manual_context(task_dir, answer_options, budget)
        if manual_context:
            chunks.append(manual_context)
    if context_policy == "teacher_artifacts":
        teacher_context = _collect_teacher_artifacts_context(
            row,
            task_dir,
            public_task_md,
            answer_options,
            budget,
            teacher_artifact_sources,
        )
        if teacher_context:
            chunks.append(teacher_context)
    return "\n\n".join(chunks)


def scan_forbidden_terms(text: str, public_allowed_text: str = "") -> list[str]:
    hits: list[str] = []
    for name, pattern in FORBIDDEN_PATTERNS.items():
        if not pattern.search(text or ""):
            continue
        if public_allowed_text and pattern.search(public_allowed_text):
            continue
        hits.append(name)
    return hits


def _scan_text_leakage(
    text: str,
    private_hard: set[str],
    answer_options: set[str],
    public_allowed_text: str = "",
) -> list[str]:
    low = text.lower()
    leaks: list[str] = []
    seen: set[str] = set()
    for tok in private_hard:
        if len(re.findall(r"[A-Za-z0-9]", tok)) < 2 or tok in seen:
            continue
        if public_allowed_text and _is_public_literal(tok, public_allowed_text):
            continue
        if re.search(r"[A-Za-z]", tok):
            hit = re.search(rf"(?<![A-Za-z0-9_]){re.escape(tok)}(?![A-Za-z0-9_])", low) is not None
        else:
            hit = re.search(rf"(?<![0-9.]){re.escape(tok)}(?![0-9.])", text) is not None
        if hit:
            seen.add(tok)
            leaks.append(tok)
    for opt in answer_options:
        if len(re.findall(r"[A-Za-z0-9]", opt)) < 2 or opt in seen:
            continue
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(opt)}(?![A-Za-z0-9_])", low):
            seen.add(opt)
            leaks.append(opt)
    return leaks


def validate_agent_skill(
    text: str,
    private_hard: set[str],
    answer_options: set[str],
    *,
    strict_public_docs: bool = False,
    public_allowed_text: str = "",
) -> tuple[bool, str]:
    if not text or len(re.findall(r"\b\w+\b", text)) < 60:
        return False, "too short / empty"
    low = text.lower()
    missing = [heading for heading in REQUIRED_HEADINGS if heading.lower() not in low]
    if missing:
        return False, f"missing headings: {missing}"
    forbidden = scan_forbidden_terms(text, public_allowed_text=public_allowed_text)
    if forbidden:
        return False, "forbidden terms: " + ", ".join(forbidden)
    if strict_public_docs:
        strict_forbidden = [
            name for name, pattern in STRICT_PUBLIC_DOC_FORBIDDEN_PATTERNS.items()
            if pattern.search(text or "")
            and not (public_allowed_text and pattern.search(public_allowed_text))
        ]
        if strict_forbidden:
            return False, "strict public-docs terms: " + ", ".join(strict_forbidden)
    leaks = _scan_text_leakage(text, private_hard, answer_options, public_allowed_text=public_allowed_text)
    if leaks:
        return False, "leakage: " + ", ".join(sorted(leaks)[:12])
    return True, ""


def _redact_retry_error(err: str) -> str:
    """Return retry feedback that never exposes hidden audit literals."""
    if err.startswith("leakage:"):
        return (
            "The audit detected task-specific literals, answer labels, or "
            "concrete numeric examples. Use generic process guidance and "
            "placeholders instead of any task-specific constants or decisions. "
            "Do not invent exact JSON field names, enum values, or snake_case "
            "identifiers when the public materials describe them only in prose."
        )
    if err.startswith("forbidden terms:"):
        return (
            "The audit detected evaluator-related wording. Describe only the "
            "public interface, workflow, constraints, and pitfalls."
        )
    if err.startswith("strict public-docs terms:"):
        return (
            "The audit detected implementation-derived API details. In strict "
            "public-docs mode, avoid keyword-only/default/signature/internal "
            "attribute claims unless they are explicitly documented in the "
            "public task prompt. Use conceptual workflow guidance instead."
        )
    if err.startswith("missing headings:"):
        return err
    if err.startswith("too short"):
        return err
    if err:
        return "The previous attempt failed. Regenerate a compliant public-only Agent Skill."
    return ""


@dataclass
class AgentSkillResult:
    task_id: str
    family: str
    status: str
    attempts: int
    calls: list[dict[str, Any]]
    skill_text: str = ""


def generate_one(
    row: dict[str, Any],
    pool_root: Path,
    keys: dict[str, str],
    model_alias: str,
    max_tokens: int,
    attempts: int,
    context_policy: str,
    public_context_globs: str,
    public_context_max_chars: int,
    teacher_artifact_sources: set[str],
) -> AgentSkillResult:
    task_id = str(row.get("task_id"))
    family = str(row.get("family"))
    task_dir = ro._task_dir(row, pool_root)
    files = row.get("files") if isinstance(row.get("files"), dict) else {}
    public_task_md = ro._load_text(task_dir / (files.get("task") or "task.md"))
    effective_context_chars = public_context_max_chars
    if effective_context_chars <= 0 and context_policy != "task_only":
        effective_context_chars = 10000
    public_context = _build_allowed_context(
        row,
        task_dir,
        public_task_md,
        context_policy,
        public_context_globs,
        effective_context_chars,
        teacher_artifact_sources,
    )
    public_context_block = f"\n--- additional allowed task context ---\n{public_context}\n\n" if public_context else ""

    answer_options = ev.extract_public_answer_options(public_task_md)
    if context_policy == "teacher_artifacts":
        private_hard = _collect_answer_literals(task_dir, public_task_md)
    else:
        audit_public_text = public_task_md + "\n" + public_context
        private_hard = ev.build_private_vocab(row, task_dir, audit_public_text)
    author_task_md = _redact_answer_options(public_task_md, answer_options)
    user = AGENT_SKILL_USER_TEMPLATE.format(
        task=gg._truncate(author_task_md, 6000),
        public_context_block=gg._truncate(public_context_block, effective_context_chars + 200),
    )

    calls: list[dict[str, Any]] = []
    last_retry_error = ""
    for idx in range(max(1, attempts)):
        base_system = AGENT_SKILL_SYSTEM + (STRICT_PUBLIC_DOCS_SUFFIX if context_policy == "public_docs" else "")
        system = (
            base_system
            if idx == 0
            else base_system + RETRY_SUFFIX.format(error=last_retry_error)
        )
        try:
            resp = gg._llm_chat(model_alias, user, system, keys, max_tokens=max_tokens, effort="off")
            text = _strip_outer_fence(str(resp.get("response") or ""))
            ok, err = validate_agent_skill(
                text,
                private_hard,
                answer_options,
                strict_public_docs=context_policy == "public_docs",
                public_allowed_text=audit_public_text if context_policy == "public_docs" else "",
            )
            calls.append({
                "attempt": idx,
                "ok": ok,
                "error": "" if ok else err,
                "input_tokens": int(resp.get("input_tokens") or 0),
                "output_tokens": int(resp.get("output_tokens") or 0),
            })
            if ok:
                return AgentSkillResult(task_id, family, "ok", idx + 1, calls, text)
            last_retry_error = _redact_retry_error(err)
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
            last_retry_error = _redact_retry_error(err)
            calls.append({"attempt": idx, "ok": False, "error": err})
    return AgentSkillResult(task_id, family, "failed", attempts, calls)


def run(args: argparse.Namespace) -> int:
    global _TEACHER_FEEDBACK_BY_TASK
    pool_root = Path(args.pool_root).resolve()
    manifest_path = Path(args.manifest).resolve()
    out_dir = Path(args.out_dir).resolve()

    payload, all_rows = ro._load_manifest(manifest_path)
    rows = list(all_rows)
    if args.families:
        wanted = set(gg._csv_arg(args.families))
        rows = [r for r in rows if str(r.get("family")) in wanted]
    if args.ids:
        wanted = set(gg._csv_arg(args.ids))
        rows = [r for r in rows if str(r.get("task_id")) in wanted]

    task_rows = ev.select_per_family(rows, args.per_family_limit, args.seed)
    if args.model not in MODEL_REGISTRY:
        raise SystemExit(f"unknown model {args.model}; known: {sorted(MODEL_REGISTRY)}")
    fam_counts = collections.Counter(str(r.get("family")) for r in task_rows)
    _TEACHER_FEEDBACK_BY_TASK = _load_teacher_feedback(args.teacher_feedback_results)
    teacher_artifact_sources = _parse_teacher_artifact_sources(args.teacher_artifact_sources)

    print(f"manifest: {manifest_path}")
    print(f"selected tasks (1 public Agent Skill each): {len(task_rows)}  per-family: {dict(fam_counts)}")
    print(f"model: {args.model} -> {MODEL_REGISTRY[args.model][0]}  attempts: {args.attempts}")
    print(f"out_dir: {out_dir}  write_inplace: {args.write_inplace} ({args.inplace_name})")
    print(
        f"context policy: {args.context_policy}; prompt sources: public task.md"
        + (" + allowed public assets" if args.context_policy in {"public_assets", "teacher_metadata"} else "")
        + (" + public docs/data assets without package source" if args.context_policy == "public_docs" else "")
        + (" + non-answer task metadata" if args.context_policy == "teacher_metadata" else "")
        + (" + answer-free task-author manual" if args.context_policy == "task_manual" else "")
        + (f" + sanitized teacher artifacts ({','.join(sorted(teacher_artifact_sources))})" if args.context_policy == "teacher_artifacts" else "")
        + (f" + globs {args.public_context_globs}" if args.public_context_globs else "")
    )
    if _TEACHER_FEEDBACK_BY_TASK:
        print(f"teacher feedback tasks loaded: {len(_TEACHER_FEEDBACK_BY_TASK)}")

    if args.dry_run:
        for row in task_rows[:20]:
            print(f"  {str(row.get('task_id')):6s} {str(row.get('family')):16s}")
        print(f"  ... total {len(task_rows)}")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "selected_ids.json").write_text(
        json.dumps({"seed": args.seed, "ids": [str(r.get("task_id")) for r in task_rows]}, indent=2),
        encoding="utf-8",
    )
    keys = gg._resolve_keys(args)
    if MODEL_REGISTRY[args.model][1] == "gemini" and not keys["gemini_key"]:
        raise SystemExit("gemini key required (GEMINI_KEY/GEMINI_API_KEY/GOOGLE_API_KEY or --gemini-key)")

    pending_rows = task_rows
    if args.skip_existing:
        pending_rows = [
            row for row in task_rows
            if not (out_dir / f"{str(row.get('task_id'))}.md").exists()
        ]
        print(
            f"skip-existing: {len(task_rows) - len(pending_rows)} skills already present; "
            f"{len(pending_rows)} remaining to generate"
        )

    log_path = out_dir / "_agent_skill_log.jsonl"
    written_ids: set[str] = set()

    def process(row: dict[str, Any]) -> AgentSkillResult:
        t0 = time.time()
        result = generate_one(
            row,
            pool_root,
            keys,
            args.model,
            args.skill_max_tokens,
            args.attempts,
            args.context_policy,
            args.public_context_globs,
            args.public_context_max_chars,
            teacher_artifact_sources,
        )
        result.calls.append({"elapsed_s": round(time.time() - t0, 2)})
        return result

    total = len(pending_rows)
    done = ok_count = 0
    with log_path.open("a" if args.skip_existing else "w", encoding="utf-8") as log_fh:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(process, row): row for row in pending_rows}
            for fut in as_completed(futures):
                row = futures[fut]
                result = fut.result()
                done += 1
                if result.status == "ok":
                    (out_dir / f"{result.task_id}.md").write_text(result.skill_text, encoding="utf-8")
                    if args.write_inplace:
                        task_dir = ro._task_dir(row, pool_root)
                        (task_dir / args.inplace_name).write_text(result.skill_text, encoding="utf-8")
                    written_ids.add(result.task_id)
                    ok_count += 1
                log_fh.write(json.dumps({
                    "task_id": result.task_id,
                    "family": result.family,
                    "status": result.status,
                    "attempts": result.attempts,
                    "calls": result.calls,
                }, ensure_ascii=False) + "\n")
                log_fh.flush()
                print(f"[{done}/{total}] {result.task_id} -> {result.status}", flush=True)

    if args.emit_manifest:
        done_ids = {p.stem for p in out_dir.glob("T*.md")} | written_ids
        patched = copy.deepcopy(payload)
        for row in patched.get("tasks", []):
            if isinstance(row, dict) and str(row.get("task_id")) in done_ids:
                files = row.get("files") if isinstance(row.get("files"), dict) else {}
                files["agent_skill"] = args.inplace_name
                row["files"] = files
        manifest_out = out_dir / "manifest_agent_skill.json"
        manifest_out.write_text(json.dumps(patched, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"patched manifest (files.agent_skill -> {args.inplace_name}): {manifest_out}")

    meta = {
        "kind": "public_agent_skill_generation",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "manifest": str(manifest_path),
        "prompt_sources": {
            "task_md": True,
            "reference_solution": args.context_policy == "teacher_artifacts" and "reference" in teacher_artifact_sources,
            "oracle": args.context_policy == "teacher_artifacts" and "test" in teacher_artifact_sources,
            "scenario_yaml_raw": args.context_policy == "teacher_artifacts" and "scenario" in teacher_artifact_sources,
            "scenario_yaml_non_answer_metadata": args.context_policy == "teacher_metadata",
            "task_author_manual": args.context_policy == "task_manual",
            "teacher_artifacts": args.context_policy == "teacher_artifacts",
            "teacher_artifact_sources": sorted(teacher_artifact_sources) if args.context_policy == "teacher_artifacts" else [],
            "gold_schema": args.context_policy == "teacher_artifacts" and "gold" in teacher_artifact_sources,
            "evaluation_feedback": bool(_TEACHER_FEEDBACK_BY_TASK) and "feedback" in teacher_artifact_sources,
            "context_policy": args.context_policy,
            "public_context_globs": args.public_context_globs,
        },
        "audit": {
            "private_literal_scan": True,
            "answer_option_scan": True,
            "forbidden_terms": sorted(FORBIDDEN_PATTERNS),
        },
    }
    (out_dir / "_agent_skill_metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print()
    print("done.")
    print(f"  public Agent Skills written: {ok_count}/{total}")
    print(f"  out_dir: {out_dir}")
    if args.write_inplace and args.emit_manifest:
        print()
        print("next: evaluate the static Agent Skill baseline:")
        print("  python eval/run_official.py \\")
        print(f"    --conditions with_public_agent_skill \\")
        print(f"    --manifest {out_dir / 'manifest_agent_skill.json'}")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--pool-root", default=str(POOL_ROOT))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--model", default="gemini_pro", help="author model alias")
    parser.add_argument("--families", default="", help="comma-separated family filter")
    parser.add_argument("--ids", default="", help="comma-separated task_id filter")
    parser.add_argument("--seed", type=int, default=42, help="deterministic per-family sampling")
    parser.add_argument("--per-family-limit", type=int, default=0, help="cap tasks per family (0 = all)")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--skill-max-tokens", type=int, default=2200)
    parser.add_argument(
        "--context-policy",
        choices=("task_only", "public_docs", "public_assets", "teacher_metadata", "task_manual", "teacher_artifacts"),
        default="task_only",
        help=(
            "Allowed context for authoring skills. task_only uses task.md; "
            "public_docs also summarizes public docs/data/environment/skill assets but excludes package source; "
            "public_assets also summarizes public data/environment/package/skill docs, including source-like package files; "
            "teacher_metadata additionally includes sanitized non-answer scenario metadata; "
            "task_manual uses the existing task-author SKILL.md after answer/evaluator redaction; "
            "teacher_artifacts uses sanitized test/ref/gold/scenario/feedback artifacts."
        ),
    )
    parser.add_argument("--write-inplace", action="store_true")
    parser.add_argument("--inplace-name", default="AGENT_SKILL.md")
    parser.add_argument("--emit-manifest", action="store_true")
    parser.add_argument("--public-context-globs", default="", help="optional comma-separated globs for extra public docs")
    parser.add_argument("--public-context-max-chars", type=int, default=0)
    parser.add_argument(
        "--teacher-artifact-sources",
        default=DEFAULT_TEACHER_ARTIFACT_SOURCES,
        help=(
            "Comma-separated sources for context-policy=teacher_artifacts. "
            "Valid: scenario,reference,test,gold,feedback. "
            "Use scenario,gold,feedback or scenario,reference,gold,feedback to exclude test_script.py from the author prompt."
        ),
    )
    parser.add_argument(
        "--teacher-feedback-results",
        default="",
        help="Optional results.jsonl file whose sanitized failure feedback is available in context-policy=teacher_artifacts",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--gemini-key", default="")
    parser.add_argument("--yunwu-key", default="")
    parser.add_argument("--siliconflow-key", default="")
    parser.add_argument("--evomap-key", default="")
    parser.add_argument("--bedrock-key", default="")
    parser.add_argument("--local-base-url", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv or sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())

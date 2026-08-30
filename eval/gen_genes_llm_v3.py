#!/usr/bin/env python3
"""Generate experiential GDIv2 Gene assets for LongWoF-Bench.

NOTE: This is the earlier cached no-context distillation pipeline. For current
Opus 4.8 experiments that require rollout, verifier feedback, and
``generation_source == "evolved"`` Genes, use ``evolve_genes_v3.py`` instead.
Keep this file only for reproducing the older approach.

Pipeline (v3 experiential):
1) Read one measured no-context attempt from a completed official run
   (`cases.jsonl`, default: gemini_pro::no_context::*).
2) Join with the same trial's real evaluation verdict from `results.jsonl`
   (passed/error_type/stderr_tail/stdout_tail/n_fail).
3) Ask the same model family for retro-reflection on its own attempt.
4) Distill one GDIv2 payload (8 fields) from:
      task.md + sanitized attempt + sanitized verdict + retro-reflection.
5) Apply schema validation + task-instance leakage audit + answer-token audit.
6) Wrap and write to a legacy run-artifact directory unless --out-dir is set.

Hard constraints:
- Prompt sources are restricted to task.md + model attempt + verdict signals.
- scenario/gold data is used ONLY for post-generation leakage audit and NEVER
  enters prompts.
- Text-answer attempts are redacted before prompting (ANSWER:/ANALYSIS: lines
  removed) to avoid answer leakage from cached outputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - fallback path handles no-yaml envs
    yaml = None


HERE = Path(__file__).resolve().parent
V3_ROOT = HERE.parent
POOL_ROOT = V3_ROOT / "tasks_final"

DEFAULT_MANIFEST = POOL_ROOT / "manifest.json"
DEFAULT_CASES = V3_ROOT / "_runs" / "v3_gemini31_flash_pro_full" / "cases.jsonl"
DEFAULT_RESULTS = V3_ROOT / "_runs" / "v3_gemini31_flash_pro_full" / "results.jsonl"
DEFAULT_OUT_DIR = V3_ROOT / "_runs" / "legacy_genes_gemini31pro_nocontext"

# Keep this eval directory first so sibling imports resolve to this tree's API
# registry, including Bedrock aliases such as bedrock_opus.
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from api import MODEL_REGISTRY, call_llm  # noqa: E402


GENE_SCHEMA_VERSION = "1.5.0"
STRATEGY_MAX = 8
SIGNALS_MAX = 10
DEFAULT_FORBIDDEN_PATHS = [".env", "secrets", "credentials"]

CATEGORY_ENUMS = {
    "innovate", "optimize", "refactor", "debug",
    "secure", "document", "test", "other",
}
# Map common off-enum categories the model invents (e.g. "robustness",
# "simulate") onto the nearest valid GDIv2 enum so a single bad category word
# does not waste all Stage-B retries. Unknown words fall back to "other".
CATEGORY_ALIASES = {
    "robustness": "debug", "reliability": "debug", "fix": "debug", "repair": "debug",
    "simulate": "innovate", "simulation": "innovate", "integrate": "innovate",
    "implement": "innovate", "build": "innovate", "create": "innovate", "design": "innovate",
    "performance": "optimize", "efficiency": "optimize", "speed": "optimize", "optimization": "optimize",
    "cleanup": "refactor", "restructure": "refactor", "refactoring": "refactor",
    "security": "secure",
    "validation": "test", "verify": "test", "testing": "test", "analyze": "test",
    "documentation": "document",
}

GENE_MODEL_ALIAS_DEFAULT = "gemini_pro"
GENE_MODEL_NAME_DEFAULT = MODEL_REGISTRY.get(GENE_MODEL_ALIAS_DEFAULT, ("gemini-3.1-pro-preview", "", ""))[0]
CACHE_MODEL_DEFAULT = "gemini_pro"

# These three values and SIMULATION_NOTE are written into every generated
# Gene (see build below) and are required fields of schemas/gene.schema.json.
# They keep the project's historical name on purpose: renaming them would
# change the content of generated Genes and break comparability with the
# Genes published in the data archive.
ASSET_SOURCE_NODE = "node_taskgenome_bench_sim"
ASSET_SOURCE_ALIAS = "taskgenome_bench_experiential_distiller"
ASSET_AUTHOR = "node_taskgenome_bench_sim"
SIMULATION_NOTE = (
    "Simulated GEP Gene asset for TaskGenome Bench. Payload follows GDIv2 §2.1 "
    "8-field protocol and is distilled from model no-context attempts plus "
    "real eval verdict signals."
)

LOG_NAME = "_generation_log.jsonl"
FAILED_NAME = "_failed_ids.json"


RETRO_REFLECTION_SYSTEM = """You are reviewing a no-context attempt that you (the same model family) previously produced for a coding/reasoning benchmark task.

Write retrospective bullets (4-7 lines, each prefixed with `- `) that capture:
- 2-3 concrete tools/algorithms/method choices actually used in the attempt.
- 1-2 fragile assumptions that could fail (phrase each as a precondition to verify).
- 1-2 edge cases that were handled OR should have been handled.

You are given real verdict signals (pass/fail + error type + sanitized tails). Use them to explain likely failure causes when relevant.

STRICT:
- Do not repeat any final answer, numeric final result, enum option, or task-specific threshold.
- Use abstract placeholders for task fields/files instead of copying identifiers.
- Output ONLY bullet lines, no preamble, no code fences."""


RETRO_REFLECTION_USER_TEMPLATE = """FAMILY: {family}
EXECUTION_MODE: {execution_mode}

--- task.md ---
{task_md}

--- your prior no-context attempt (sanitized) ---
{attempt}

--- real eval verdict signals (sanitized) ---
{verdict}

Write retrospective bullets now."""


FAMILY_GUIDANCE = {
    "agent_env_synth": """Family transform rules:
- Distill reusable pipeline discipline: parse events -> compile state -> simulate -> extract outputs -> serialize.
- Preconditions should come from real failure modes: schema/key/type mismatches, missing output files, empty-input behavior, non-negative physical constraints.
- Never copy hidden package private APIs, exact test assertions, or task output filenames.""",
    "code_generation": """Family transform rules:
- Keep concrete algorithm/tool choices (e.g., scipy/numpy/protocol contracts) and boundary/numeric discipline.
- Focus on reusable CLI/IO and robustness patterns, not task instance literals.
- Never copy exact flag names, task-specific column names, output keys, or reference implementation details.""",
    "math_reasoning": """Family transform rules (high leak risk):
- Distill method skeletons only: decomposition, counting strategy, modular/arithmetic scaffolding.
- NEVER include any numeric final answer, tuple components, or task constants.
- Preconditions should target common traps (e.g., continuity/block assumptions, symmetry handling, canonical output discipline).""",
    "rule_following": """Family transform rules (high leak risk):
- Distill rule-application procedure: classify -> derive baseline -> apply modifiers -> compare -> decide.
- NEVER include decision enum tokens, concrete thresholds, entity names, or scenario constants.
- Preconditions should encode ordering/precedence traps and "use rulebook only, no external real-world cutoff" discipline.""",
}


STAGE_B_SYSTEM_BASE = """You are distilling ONE reusable GDIv2 Gene payload from model experience.

Input you receive:
1) task.md (problem family context)
2) one measured no-context attempt
3) real evaluation verdict signals
4) retro-reflection bullets

Distillation objective:
- Convert observed method patterns into reusable procedural knowledge.
- Use real failures to shape `preconditions`.
- Keep Gene weaker than full skill docs: retain only what the model learned from its own attempt + verdict.

CRITICAL anti-leak rules:
- Do NOT copy task-instance identifiers (CLI flags, snake_case field names, exact file names, output schema keys).
- Do NOT include final answer strings, enum decision tokens, concrete expected-answer numbers, or task-instance thresholds.
- Summarize methods, not instance outputs.

Schema: output exactly one JSON object with EXACTLY these 8 fields:
{
  "type": "Gene",
  "summary": "<50-300 chars, single sentence>",
  "category": "<one of innovate|optimize|refactor|debug|secure|document|test|other>",
  "signals_match": ["<5-10 short trigger phrases>"],
  "strategy": ["<3-8 imperative procedural steps, each 15-300 chars>"],
  "preconditions": ["<0-4 verifiable prerequisites>"],
  "constraints": {"max_files": <int 1-5>, "forbidden_paths": [".env", "secrets", "credentials"]},
  "validation": []
}

Output only the JSON object. No markdown fences. No extra text."""


STAGE_B_USER_TEMPLATE = """FAMILY: {family}
EXECUTION_MODE: {execution_mode}
DOMAIN: {domain}

Family-specific guidance:
{family_guidance}

--- task.md ---
{task_md}

--- measured no-context attempt + verdict + retro-reflection ---
{attempt}

Author the Gene JSON now. Output ONLY the JSON object."""


RETRY_SYSTEM_SUFFIX = """

PREVIOUS OUTPUT FAILED VALIDATION/AUDIT:
{error}

Fix the JSON and regenerate.
Remember:
- Keep exact 8 fields only.
- Keep answer/threshold/enum tokens out.
- Keep strategy in [3,8], signals in [5,10], preconditions in [0,4].
Output ONLY corrected JSON."""


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_LEAK_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_LEAK_FLAG_RE = re.compile(r"--[a-z][a-z0-9_-]*[a-z0-9]")
_LEAK_FILE_RE = re.compile(
    r"\b([a-z][a-z0-9_-]*\.(?:py|h5|hdf5|csv|tsv|json|jsonl|yaml|yml|nc|mat|pdf|pptx|xlsx))\b",
    re.IGNORECASE,
)

LEAKAGE_FILE_ALLOWLIST = {
    "task.md", "skill.md", "readme.md", "scenario.yaml",
    "manifest.json", "config.json", "results.jsonl", "cases.jsonl",
}
LEAKAGE_SNAKE_ALLOWLIST = {
    "max_files", "forbidden_paths",
    "x_train", "y_train", "x_test", "y_test", "n_samples", "n_features",
    "input_path", "output_path", "file_path",
}

ANSWER_ANALYSIS_LINE_RE = re.compile(r"^\s*(ANSWER|ANALYSIS)\s*:", re.IGNORECASE)
VERDICT_STRIP_RE = re.compile(r"(?i)(PASS:|FAIL:|SCORE:|expected\s*=)")
ABS_PATH_RE = re.compile(r"/[A-Za-z0-9_.\-~/]+(?:/[A-Za-z0-9_.\-~]+)+")
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
ENUM_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_]{2,}$")


@dataclass
class AttemptBundle:
    task_id: str
    task_dir: Path
    task_md: str
    family: str
    execution_mode: str
    domain: str
    raw_response: str
    prompt_attempt: str
    verdict_block: str
    reflection: str
    combined_attempt: str
    eval_record: dict[str, Any]


@dataclass
class AnswerAudit:
    expected_texts: set[str]
    enum_tokens: set[str]
    numeric_tokens: set[str]


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 40)].rstrip() + "\n... [truncated]"


def _csv_arg(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def _task_dir(row: dict[str, Any], pool_root: Path) -> Path:
    rel = row.get("rel_dir")
    if isinstance(rel, str) and rel.strip():
        return pool_root / rel.strip()
    return pool_root / str(row.get("task_id"))


def _safe_id(task_id: str) -> str:
    return task_id.replace(":", "_")


def _resolve_keys(args: argparse.Namespace) -> dict[str, str]:
    def first(*names: str) -> str:
        for name in names:
            value = os.environ.get(name, "")
            if value:
                return value
        return ""

    return {
        "yunwu_key": args.yunwu_key or first("YUNWU_KEY", "YUNWU_API_KEY"),
        "gemini_key": args.gemini_key or first("GEMINI_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "siliconflow_key": args.siliconflow_key or first("SILICONFLOW_KEY", "SILICONFLOW_API_KEY", "SF_API_KEY"),
        "evomap_key": args.evomap_key or first("EVOMAP_KEY", "EVOMAP_API_KEY"),
        "bedrock_key": args.bedrock_key or first("AWS_BEARER_TOKEN_BEDROCK", "BEDROCK_KEY", "BEDROCK_API_KEY"),
        "local_base_url": args.local_base_url or os.environ.get("LOCAL_BASE_URL", "http://localhost:8000/v1"),
    }


def _load_manifest_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("tasks")
    if not isinstance(rows, list):
        raise ValueError(f"manifest tasks[] missing: {path}")
    return [r for r in rows if isinstance(r, dict)]


def _load_jsonl_by_trial_key(path: Path) -> dict[str, dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            trial_key = row.get("trial_key")
            if not trial_key:
                trial = row.get("trial") or {}
                if isinstance(trial, dict):
                    trial_key = trial.get("trial_key")
            if isinstance(trial_key, str) and trial_key:
                by_key[trial_key] = row
    return by_key


def _sanitize_text_attempt(raw_response: str) -> str:
    lines = (raw_response or "").splitlines()
    kept = [ln for ln in lines if not ANSWER_ANALYSIS_LINE_RE.match(ln.strip())]
    sanitized = "\n".join(kept).strip()
    # Extra safety for text tasks: mask literal numbers from residual prose
    # so expected-answer digits do not leak into reflection/distillation.
    sanitized = NUMBER_RE.sub("<num>", sanitized)
    if sanitized:
        return sanitized
    return "(text response redacted: ANSWER/ANALYSIS removed)"


def _sanitize_verdict_tail(text: str) -> str:
    lines = []
    for line in (text or "").splitlines():
        if VERDICT_STRIP_RE.search(line):
            continue
        if "short test summary info" in line.lower():
            continue
        cleaned = ABS_PATH_RE.sub("<path>", line).strip()
        if cleaned:
            lines.append(cleaned)
    if not lines:
        return ""
    return "\n".join(lines[-24:])


def _build_verdict_block(eval_row: dict[str, Any]) -> str:
    passed = bool(eval_row.get("passed"))
    lines = [
        f"passed: {str(passed).lower()}",
        f"error_type: {eval_row.get('error_type', 'unknown')}",
        f"n_pass: {eval_row.get('n_pass', 0)}",
        f"n_fail: {eval_row.get('n_fail', 0)}",
        f"pass_rate: {eval_row.get('pass_rate', 0.0)}",
    ]
    stdout_tail = _sanitize_verdict_tail(str(eval_row.get("stdout_tail") or ""))
    stderr_tail = _sanitize_verdict_tail(str(eval_row.get("stderr_tail") or ""))
    if stdout_tail:
        lines.append("stdout_tail_sanitized:")
        lines.append(stdout_tail)
    if stderr_tail:
        lines.append("stderr_tail_sanitized:")
        lines.append(stderr_tail)
    return "\n".join(lines)


def parse_llm_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("empty response")
    m = _JSON_FENCE_RE.search(text)
    if m:
        text = m.group(1)
    if not text.startswith("{"):
        first = text.find("{")
        last = text.rfind("}")
        if first == -1 or last == -1 or last <= first:
            raise ValueError(f"no JSON object found: {text[:240]!r}")
        text = text[first : last + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"json decode failed: {exc}") from exc


def soft_fix_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    out = dict(payload)
    if isinstance(out.get("strategy"), list) and len(out["strategy"]) > STRATEGY_MAX:
        out["strategy"] = out["strategy"][:STRATEGY_MAX]
    if isinstance(out.get("signals_match"), list) and len(out["signals_match"]) > SIGNALS_MAX:
        out["signals_match"] = out["signals_match"][:SIGNALS_MAX]
    if isinstance(out.get("preconditions"), list) and len(out["preconditions"]) > 4:
        out["preconditions"] = out["preconditions"][:4]
    cat = out.get("category")
    if isinstance(cat, str) and cat not in CATEGORY_ENUMS:
        out["category"] = CATEGORY_ALIASES.get(cat.strip().lower(), "other")
    constraints = out.get("constraints")
    if isinstance(constraints, dict):
        fp = constraints.get("forbidden_paths")
        if not isinstance(fp, list) or not fp:
            out["constraints"] = dict(constraints, forbidden_paths=list(DEFAULT_FORBIDDEN_PATHS))
    if out.get("validation"):
        out["validation"] = []
    return out


def validate_payload(payload: dict[str, Any]) -> tuple[bool, str]:
    required = {
        "type",
        "summary",
        "category",
        "signals_match",
        "strategy",
        "preconditions",
        "constraints",
        "validation",
    }
    if set(payload.keys()) != required:
        extra = sorted(set(payload.keys()) - required)
        missing = sorted(required - set(payload.keys()))
        bits = []
        if missing:
            bits.append(f"missing fields: {missing}")
        if extra:
            bits.append(f"unexpected fields: {extra}")
        return False, "; ".join(bits)
    if payload["type"] != "Gene":
        return False, f"type must be 'Gene', got {payload['type']!r}"
    if payload["category"] not in CATEGORY_ENUMS:
        return False, f"invalid category: {payload['category']!r}"
    summary = payload["summary"]
    if not isinstance(summary, str) or not (50 <= len(summary) <= 300):
        return False, f"summary length out of range: {len(summary) if isinstance(summary, str) else 'NA'}"
    signals = payload["signals_match"]
    if not isinstance(signals, list) or not (5 <= len(signals) <= SIGNALS_MAX):
        return False, f"signals_match length out of range: {len(signals) if isinstance(signals, list) else 'NA'}"
    if not all(isinstance(x, str) and x.strip() for x in signals):
        return False, "signals_match must be non-empty strings"
    strategy = payload["strategy"]
    if not isinstance(strategy, list) or not (3 <= len(strategy) <= STRATEGY_MAX):
        return False, f"strategy length out of range: {len(strategy) if isinstance(strategy, list) else 'NA'}"
    if not all(isinstance(x, str) and 15 <= len(x) <= 300 for x in strategy):
        return False, "strategy items must be 15-300 chars strings"
    preconditions = payload["preconditions"]
    if not isinstance(preconditions, list) or len(preconditions) > 4:
        return False, "preconditions must be list with <=4 entries"
    if not all(isinstance(x, str) and x.strip() for x in preconditions):
        return False, "preconditions must be non-empty strings"
    constraints = payload["constraints"]
    if not isinstance(constraints, dict):
        return False, "constraints must be object"
    max_files = constraints.get("max_files")
    if not isinstance(max_files, int) or not (1 <= max_files <= 50):
        return False, "constraints.max_files must be int in [1, 50]"
    forbidden = constraints.get("forbidden_paths")
    if not isinstance(forbidden, list) or not forbidden:
        return False, "constraints.forbidden_paths must be non-empty list"
    if payload["validation"] != []:
        return False, "validation must be []"
    return True, ""


def extract_task_tokens(task_md: str) -> set[str]:
    text = (task_md or "").lower()
    if not text:
        return set()
    tokens: set[str] = set()
    for item in _LEAK_SNAKE_RE.findall(text):
        if item in LEAKAGE_SNAKE_ALLOWLIST:
            continue
        tokens.add(item)
    tokens.update(_LEAK_FLAG_RE.findall(text))
    for item in _LEAK_FILE_RE.findall(text):
        lo = item.lower()
        if lo in LEAKAGE_FILE_ALLOWLIST:
            continue
        tokens.add(lo)
    return tokens


def _iter_payload_strings(payload: dict[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for field in ("summary", "signals_match", "strategy", "preconditions"):
        value = payload.get(field)
        if isinstance(value, str):
            out.append((field, value))
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                if isinstance(item, str):
                    out.append((f"{field}[{idx}]", item))
    return out


def find_payload_leakage(task_md: str, payload: dict[str, Any]) -> list[tuple[str, str]]:
    candidates = extract_task_tokens(task_md)
    if not candidates:
        return []
    leaks: list[tuple[str, str]] = []
    for loc, value in _iter_payload_strings(payload):
        lower = value.lower()
        for token in candidates:
            if token in lower:
                leaks.append((token, loc))
    return leaks


def _flatten_scalars(value: Any) -> list[str]:
    out: list[str] = []
    if value is None:
        return out
    if isinstance(value, (str, int, float, bool)):
        out.append(str(value))
        return out
    if isinstance(value, dict):
        for v in value.values():
            out.extend(_flatten_scalars(v))
        return out
    if isinstance(value, (list, tuple, set)):
        for v in value:
            out.extend(_flatten_scalars(v))
        return out
    return out


def _load_scenario_object(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {}, ""
    raw = path.read_text(encoding="utf-8", errors="replace")
    if yaml is not None:
        try:
            parsed = yaml.safe_load(raw)
            if isinstance(parsed, dict):
                return parsed, raw
        except Exception:
            pass
    # Minimal fallback: no structured parse; keep raw text for numeric scan.
    return {}, raw


def _collect_rule_threshold_numbers(task_md: str, scenario_raw: str) -> set[str]:
    keywords = (
        "threshold", "cutoff", "baseline", "window", "hours", "hour",
        "days", "day", "stipend", "rating", "tier", "band", "rule",
    )
    numbers: set[str] = set()
    for line in (task_md + "\n" + scenario_raw).splitlines():
        lo = line.lower()
        if any(k in lo for k in keywords):
            for token in NUMBER_RE.findall(line):
                numbers.add(token)
    return numbers


def build_answer_audit(row: dict[str, Any], task_dir: Path, task_md: str) -> AnswerAudit:
    if str(row.get("execution_mode")) != "text_short_answer":
        return AnswerAudit(expected_texts=set(), enum_tokens=set(), numeric_tokens=set())

    files = row.get("files") or {}
    scenario_rel = files.get("scenario") or "scenario.yaml"
    scenario_path = task_dir / scenario_rel
    scenario_obj, scenario_raw = _load_scenario_object(scenario_path)

    expected_texts: set[str] = set()
    enum_tokens: set[str] = set()
    numeric_tokens: set[str] = set()

    expected_value = None
    if isinstance(scenario_obj, dict):
        expected_value = scenario_obj.get("expected_answer")
        if expected_value is None:
            expected_value = scenario_obj.get("gold_answer")

        answer_space = scenario_obj.get("answer_space")
        if isinstance(answer_space, list):
            for item in answer_space:
                if isinstance(item, str):
                    token = item.strip().lower()
                    if token:
                        enum_tokens.add(token)

    for scalar in _flatten_scalars(expected_value):
        clean = scalar.strip()
        if not clean:
            continue
        expected_texts.add(re.sub(r"\s+", "", clean.lower()))
        if ENUM_TOKEN_RE.match(clean.lower()):
            enum_tokens.add(clean.lower())
        for num in NUMBER_RE.findall(clean):
            numeric_tokens.add(num)

    if str(row.get("family")) == "rule_following":
        numeric_tokens.update(_collect_rule_threshold_numbers(task_md, scenario_raw))

    return AnswerAudit(
        expected_texts=expected_texts,
        enum_tokens=enum_tokens,
        numeric_tokens=numeric_tokens,
    )


def find_answer_leakage(payload: dict[str, Any], audit: AnswerAudit) -> list[tuple[str, str, str]]:
    if not (audit.expected_texts or audit.enum_tokens or audit.numeric_tokens):
        return []

    leaks: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for loc, value in _iter_payload_strings(payload):
        lower = value.lower()
        compact = re.sub(r"\s+", "", lower)

        for expected in audit.expected_texts:
            if expected and expected in compact:
                item = (expected, loc, "expected_answer")
                if item not in seen:
                    leaks.append(item)
                    seen.add(item)

        for token in audit.enum_tokens:
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])", lower):
                item = (token, loc, "enum_token")
                if item not in seen:
                    leaks.append(item)
                    seen.add(item)

        for number in audit.numeric_tokens:
            if re.search(rf"(?<![0-9]){re.escape(number)}(?![0-9])", value):
                item = (number, loc, "numeric_token")
                if item not in seen:
                    leaks.append(item)
                    seen.add(item)

    return leaks


def validate_payload_with_audit(
    payload: dict[str, Any],
    task_md: str,
    answer_audit: AnswerAudit,
) -> tuple[bool, str, list[tuple[str, str]], list[tuple[str, str, str]]]:
    ok_schema, err_schema = validate_payload(payload)
    if not ok_schema:
        return False, f"schema: {err_schema}", [], []

    leaks = find_payload_leakage(task_md, payload)
    if leaks:
        listing = "; ".join(f"`{tok}` in {loc}" for tok, loc in leaks[:12])
        return False, f"task-instance leakage: {listing}", leaks, []

    answer_leaks = find_answer_leakage(payload, answer_audit)
    if answer_leaks:
        listing = "; ".join(f"{kind} `{tok}` in {loc}" for tok, loc, kind in answer_leaks[:12])
        return False, f"answer leakage: {listing}", [], answer_leaks

    return True, "", [], []


def _build_retro_user(bundle: AttemptBundle) -> str:
    return RETRO_REFLECTION_USER_TEMPLATE.format(
        family=bundle.family,
        execution_mode=bundle.execution_mode,
        task_md=_truncate(bundle.task_md, 3500),
        attempt=_truncate(bundle.prompt_attempt, 4500),
        verdict=_truncate(bundle.verdict_block, 1800),
    )


def _build_stage_b_user(bundle: AttemptBundle) -> str:
    return STAGE_B_USER_TEMPLATE.format(
        family=bundle.family,
        execution_mode=bundle.execution_mode,
        domain=bundle.domain,
        family_guidance=FAMILY_GUIDANCE.get(bundle.family, "Use generic reusable procedural distillation."),
        task_md=_truncate(bundle.task_md, 3600),
        attempt=_truncate(bundle.combined_attempt, 6000),
    )


def _llm_chat(
    model_alias: str,
    user_prompt: str,
    system_prompt: str,
    keys: dict[str, str],
    max_tokens: int,
    effort: str | None = None,
) -> dict[str, Any]:
    return call_llm(
        model_alias,
        user_prompt,
        system_prompt,
        yunwu_key=keys["yunwu_key"],
        gemini_key=keys["gemini_key"],
        siliconflow_key=keys["siliconflow_key"],
        evomap_key=keys["evomap_key"],
        bedrock_key=keys["bedrock_key"],
        local_base_url=keys["local_base_url"],
        max_tokens=max_tokens,
        effort=effort,
    )


def run_retro_reflection(
    bundle: AttemptBundle,
    model_alias: str,
    keys: dict[str, str],
    max_tokens: int,
) -> tuple[str, dict[str, Any]]:
    user = _build_retro_user(bundle)
    try:
        # Reflection rewrite, not solving — keep thinking off (avoids burning budget
        # and truncation if a small max_tokens is passed).
        resp = _llm_chat(model_alias, user, RETRO_REFLECTION_SYSTEM, keys, max_tokens=max_tokens, effort="off")
        text = str(resp.get("response") or "").strip()
        ok = bool(text) and ("- " in text or "* " in text)
        if not ok:
            text = "- Verify assumptions against runtime contract before implementation.\n- Guard edge cases and empty inputs explicitly."
        return text, {
            "ok": ok,
            "error": "" if ok else "retro_reflection_unusable",
            "input_tokens": int(resp.get("input_tokens") or 0),
            "output_tokens": int(resp.get("output_tokens") or 0),
            "thoughts_tokens": int(resp.get("thoughts_tokens") or 0),
        }
    except Exception as exc:
        fallback = "- Verify hidden assumptions using observed failure signals before finalizing logic."
        return fallback, {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "input_tokens": 0,
            "output_tokens": 0,
            "thoughts_tokens": 0,
        }


def run_stage_b_distill(
    bundle: AttemptBundle,
    answer_audit: AnswerAudit,
    model_alias: str,
    keys: dict[str, str],
    max_tokens: int,
    attempts: int,
) -> tuple[dict[str, Any] | None, str, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    user = _build_stage_b_user(bundle)
    system = STAGE_B_SYSTEM_BASE + "\n\n" + FAMILY_GUIDANCE.get(bundle.family, "")
    last_err = ""

    for idx in range(max(1, attempts)):
        system_retry = system if idx == 0 else (system + RETRY_SYSTEM_SUFFIX.format(error=last_err))
        stage_name = "stage_b" if idx == 0 else f"stage_b_retry_{idx}"
        try:
            # Distill = reformat a solved attempt into structured payload; no thinking
            # (and max_tokens here is typically small, so thinking would truncate it).
            resp = _llm_chat(model_alias, user, system_retry, keys, max_tokens=max_tokens, effort="off")
            payload = soft_fix_payload(parse_llm_json(str(resp.get("response") or "")))
            ok, err, leaks, answer_leaks = validate_payload_with_audit(payload, bundle.task_md, answer_audit)
            calls.append({
                "stage": stage_name,
                "ok": ok,
                "error": "" if ok else err,
                "n_leaks": len(leaks),
                "n_answer_leaks": len(answer_leaks),
                "input_tokens": int(resp.get("input_tokens") or 0),
                "output_tokens": int(resp.get("output_tokens") or 0),
                "thoughts_tokens": int(resp.get("thoughts_tokens") or 0),
            })
            if ok:
                return payload, "experiential" if idx == 0 else "experiential_retry", calls
            last_err = err
        except Exception as exc:
            last_err = f"parse_or_api_error: {type(exc).__name__}: {exc}"
            calls.append({
                "stage": stage_name,
                "ok": False,
                "error": last_err,
                "n_leaks": 0,
                "n_answer_leaks": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "thoughts_tokens": 0,
            })

    return None, "stage_b_failed", calls


def compute_asset_id(payload: dict[str, Any]) -> str:
    canon = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canon.encode()).hexdigest()}"


def build_wrapper(
    payload: dict[str, Any],
    row: dict[str, Any],
    created_at: str,
    generation_source: str,
    model_name: str,
) -> dict[str, Any]:
    task_id = str(row.get("task_id"))
    short_title = task_id
    domain = str(row.get("domain") or row.get("family") or "unknown")
    return {
        "asset_id": compute_asset_id(payload),
        "asset_type": "Gene",
        "schema_version": GENE_SCHEMA_VERSION,
        "source_node_id": ASSET_SOURCE_NODE,
        "source_node_alias": ASSET_SOURCE_ALIAS,
        "author": ASSET_AUTHOR,
        "created_at": created_at,
        "domain": domain,
        "short_title": short_title,
        "model_name": model_name,
        "simulation": True,
        "simulation_note": SIMULATION_NOTE,
        "generation_source": generation_source,
        "pipeline_mode": "experiential_v3",
        "local_id": f"gene_{_safe_id(task_id)}",
        "source_track": str(row.get("source", "unknown")),
        "payload": payload,
    }


def _select_rows(rows: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    out = list(rows)
    if args.ids:
        wanted = set(_csv_arg(args.ids))
        out = [r for r in out if str(r.get("task_id")) in wanted]
    if args.families:
        wanted = set(_csv_arg(args.families))
        out = [r for r in out if str(r.get("family")) in wanted]
    if args.shuffle:
        random.seed(args.seed)
        random.shuffle(out)
    if args.limit > 0:
        out = out[: args.limit]
    return out


def _prepare_bundle(
    row: dict[str, Any],
    case_row: dict[str, Any],
    result_row: dict[str, Any],
    pool_root: Path,
) -> AttemptBundle:
    task_id = str(row.get("task_id"))
    task_dir = _task_dir(row, pool_root)
    task_rel = ((row.get("files") or {}).get("task")) or "task.md"
    task_md_path = task_dir / task_rel
    task_md = task_md_path.read_text(encoding="utf-8", errors="replace") if task_md_path.exists() else ""

    raw_response = str(case_row.get("raw_response") or case_row.get("extracted_code") or "")
    execution_mode = str(row.get("execution_mode") or "")
    if execution_mode == "text_short_answer":
        prompt_attempt = _sanitize_text_attempt(raw_response)
    else:
        prompt_attempt = raw_response.strip()

    eval_record = result_row.get("eval") if isinstance(result_row.get("eval"), dict) else {}
    verdict_block = _build_verdict_block(eval_record)

    return AttemptBundle(
        task_id=task_id,
        task_dir=task_dir,
        task_md=task_md,
        family=str(row.get("family") or "unknown"),
        execution_mode=execution_mode,
        domain=str(row.get("domain") or row.get("family") or "unknown"),
        raw_response=raw_response,
        prompt_attempt=prompt_attempt,
        verdict_block=verdict_block,
        reflection="",
        combined_attempt="",
        eval_record=eval_record,
    )


def run(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).resolve()
    pool_root = Path(args.pool_root).resolve()
    cases_path = Path(args.cases).resolve()
    results_path = Path(args.results).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    created_at = args.created_at or datetime.now(timezone.utc).isoformat()
    rows = _load_manifest_rows(manifest_path)
    rows = _select_rows(rows, args)

    if args.model not in MODEL_REGISTRY:
        raise SystemExit(f"unknown --model {args.model}; known: {sorted(MODEL_REGISTRY)}")
    if args.cache_model not in MODEL_REGISTRY:
        raise SystemExit(f"unknown --cache-model {args.cache_model}; known: {sorted(MODEL_REGISTRY)}")

    cases_by_key = _load_jsonl_by_trial_key(cases_path)
    results_by_key = _load_jsonl_by_trial_key(results_path)
    keys = _resolve_keys(args)

    if MODEL_REGISTRY[args.model][1] == "gemini" and not keys["gemini_key"]:
        raise SystemExit("gemini key is required (set GEMINI_KEY/GEMINI_API_KEY/GOOGLE_API_KEY or --gemini-key)")

    print(f"manifest: {manifest_path}")
    print(f"selected rows: {len(rows)}")
    print(f"cases: {cases_path}")
    print(f"results: {results_path}")
    print(f"out_dir: {out_dir}")
    print(f"distill_model: {args.model} -> {MODEL_REGISTRY[args.model][0]}")
    print(f"cache_model: {args.cache_model}")

    if args.dry_run:
        preview = rows[: min(len(rows), 20)]
        print(f"dry-run preview ({len(preview)} shown):")
        for row in preview:
            task_id = str(row.get("task_id"))
            trial_key = f"{args.cache_model}::no_context::{task_id}"
            has_case = trial_key in cases_by_key
            has_result = trial_key in results_by_key
            print(
                f"  {task_id} family={row.get('family')} mode={row.get('execution_mode')} "
                f"cache={'Y' if has_case else 'N'} result={'Y' if has_result else 'N'}"
            )
        if len(rows) > len(preview):
            print(f"  ... and {len(rows) - len(preview)} more")
        return 0

    done = 0
    skipped_resume = 0
    missing_cache = 0
    failed = 0
    failed_ids: list[dict[str, Any]] = []
    source_counts: dict[str, int] = {}

    log_path = out_dir / LOG_NAME
    log_mode = "a" if (not args.no_resume and log_path.exists()) else "w"

    with log_path.open(log_mode, encoding="utf-8") as log_fh:
        for idx, row in enumerate(rows, 1):
            task_id = str(row.get("task_id"))
            out_path = out_dir / f"{task_id}.json"
            if out_path.exists() and not args.no_resume:
                skipped_resume += 1
                continue

            trial_key = f"{args.cache_model}::no_context::{task_id}"
            case_row = cases_by_key.get(trial_key)
            result_row = results_by_key.get(trial_key)
            if not case_row or not result_row:
                missing_cache += 1
                rec = {
                    "task_id": task_id,
                    "trial_key": trial_key,
                    "status": "missing_cache_or_result",
                }
                failed_ids.append(rec)
                log_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print(f"[{idx}/{len(rows)}] {task_id} -> missing cache/result")
                continue

            try:
                bundle = _prepare_bundle(row, case_row, result_row, pool_root)
                reflection, retro_meta = run_retro_reflection(
                    bundle,
                    model_alias=args.model,
                    keys=keys,
                    max_tokens=args.retro_max_tokens,
                )
                bundle.reflection = reflection
                bundle.combined_attempt = (
                    "--- MODEL_ATTEMPT ---\n"
                    f"{bundle.prompt_attempt}\n\n"
                    "--- VERDICT ---\n"
                    f"{bundle.verdict_block}\n\n"
                    "--- RETRO_REFLECTION ---\n"
                    f"{bundle.reflection}\n"
                )

                answer_audit = build_answer_audit(row, bundle.task_dir, bundle.task_md)
                payload, source, stage_b_calls = run_stage_b_distill(
                    bundle=bundle,
                    answer_audit=answer_audit,
                    model_alias=args.model,
                    keys=keys,
                    max_tokens=args.stage_b_max_tokens,
                    attempts=args.stage_b_attempts,
                )
                if payload is None:
                    failed += 1
                    rec = {
                        "task_id": task_id,
                        "trial_key": trial_key,
                        "status": "stage_b_failed",
                        "retro": retro_meta,
                        "stage_b_calls": stage_b_calls,
                    }
                    failed_ids.append(rec)
                    log_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    print(f"[{idx}/{len(rows)}] {task_id} -> stage_b_failed")
                    continue

                asset = build_wrapper(
                    payload=payload,
                    row=row,
                    created_at=created_at,
                    generation_source=source,
                    model_name=MODEL_REGISTRY[args.model][0],
                )
                out_path.write_text(json.dumps(asset, indent=2, ensure_ascii=False), encoding="utf-8")

                done += 1
                source_counts[source] = source_counts.get(source, 0) + 1
                rec = {
                    "task_id": task_id,
                    "trial_key": trial_key,
                    "status": "ok",
                    "source": source,
                    "retro": retro_meta,
                    "stage_b_calls": stage_b_calls,
                }
                log_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print(f"[{idx}/{len(rows)}] {task_id} -> {source}")
            except Exception as exc:
                failed += 1
                rec = {
                    "task_id": task_id,
                    "trial_key": trial_key,
                    "status": "exception",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                failed_ids.append(rec)
                log_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print(f"[{idx}/{len(rows)}] {task_id} -> exception: {type(exc).__name__}: {exc}")

    failed_path = out_dir / FAILED_NAME
    failed_path.write_text(json.dumps(failed_ids, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print("done.")
    print(f"  generated: {done}")
    print(f"  skipped (resume): {skipped_resume}")
    print(f"  missing cache/result: {missing_cache}")
    print(f"  failed: {failed}")
    print(f"  source breakdown: {source_counts}")
    print(f"  log: {log_path}")
    print(f"  failed list: {failed_path}")
    return 0 if failed == 0 else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--pool-root", default=str(POOL_ROOT))
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--results", default=str(DEFAULT_RESULTS))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--model", default=GENE_MODEL_ALIAS_DEFAULT, help="Distillation model alias (default: gemini_pro)")
    parser.add_argument("--cache-model", default=CACHE_MODEL_DEFAULT, help="Model alias used in cached no_context trial_key")
    parser.add_argument("--stage-b-attempts", type=int, default=3, help="Stage-B attempts including retries")
    parser.add_argument("--stage-b-max-tokens", type=int, default=2600)
    parser.add_argument("--retro-max-tokens", type=int, default=700)
    parser.add_argument("--ids", default="", help="Comma-separated task_id subset")
    parser.add_argument("--families", default="", help="Comma-separated family subset")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--shuffle", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--created-at", default="")
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

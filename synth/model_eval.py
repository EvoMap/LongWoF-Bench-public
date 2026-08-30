#!/usr/bin/env python3
"""Model evaluation harness for gene_bench_v3 with no_context/with_skill/with_gene.

Supports the three conditions used in gene_bench v2.5:
  - no_context
  - with_skill
  - with_gene

Key behavior:
  1) Reuses the consolidated v3 manifest (`tasks_final/manifest.json`).
  2) Dispatches oracle execution by `execution_mode` (v3/v2.6/v2.5-curated mixed pool).
  3) For `with_gene`, can auto-bootstrap genes in v2.5 style:
       - run Gemini 3.1 Pro no_context first
       - distill per-task gene JSONs from those no_context outputs with Gemini 3.1 Pro
       - evaluate with_gene using the generated genes
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import random
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


HERE = Path(__file__).resolve().parent
V3_ROOT = HERE.parent
DEFAULT_POOL_ROOT = V3_ROOT / "tasks_final"
DEFAULT_MANIFEST = DEFAULT_POOL_ROOT / "manifest.json"
DEFAULT_RUNS_ROOT = DEFAULT_POOL_ROOT / "_model_runs"
DEFAULT_GENES_DIR = DEFAULT_RUNS_ROOT / "legacy_genes_gemini31pro_nocontext"
DEFAULT_PYTHON = sys.executable
# Optional exclusion list carried over from an earlier code-generation
# track. It is not part of the public release; pass the flag to supply one.
DEFAULT_UNRUNNABLE_JSON = ""
TAIL_CHARS = 1600

CONDITIONS = ("no_context", "with_skill", "with_gene")
GENE_REQUIRED_FIELDS = {
    "type",
    "summary",
    "category",
    "signals_match",
    "strategy",
    "preconditions",
    "constraints",
    "validation",
}
GENE_CATEGORY_ENUMS = {
    "innovate",
    "optimize",
    "refactor",
    "debug",
    "secure",
    "document",
    "test",
    "other",
}

CODE_INSTRUCTION = (
    "\n\nWrite a complete, self-contained Python solution. "
    "Output ONLY the code inside a single ```python code block. "
    "Do not include explanations outside the code block."
)
SANDBOX_NOTE = (
    "\n\n# Runtime note\n"
    "Your code will be saved as `generated.py` and evaluated in the task directory. "
    "If the task specifies exact CLI flags or output format, follow it verbatim."
)

GENE_SYSTEM_PROMPT = """You are distilling a reusable Gene asset from one measured no-context solve attempt.

Goal:
- Read task.md and the model's no-context attempt.
- Output ONE JSON object that captures reusable strategy (not task-instance memorization).

Schema (EXACT keys, no extras):
{
  "type": "Gene",
  "summary": "<50-300 chars, one sentence>",
  "category": "<one of innovate|optimize|refactor|debug|secure|document|test|other>",
  "signals_match": ["<5-10 concise trigger phrases>"],
  "strategy": ["<3-8 imperative reusable steps, 15-300 chars each>"],
  "preconditions": ["<0-4 verifiable prerequisites/edge-case checks>"],
  "constraints": {"max_files": <int 1-50>, "forbidden_paths": [".env", "secrets", "credentials"]},
  "validation": []
}

Rules:
- Output ONLY JSON (no markdown fences, no extra text).
- Keep content reusable for similar problem families.
- Avoid copying task-specific file names / flags verbatim when possible.
"""

GENE_RETRY_SUFFIX = """

Previous output failed validation:
{error}

Please fix and output ONLY corrected JSON.
"""

_FAIL_LINE_RE = re.compile(r"(?m)^FAIL:")
_SCORE_RE = re.compile(
    r"^SCORE:([A-Za-z_][A-Za-z0-9_]*)[:=]\s*(-?\d+(?:\.\d+)?)"
    r"|^(?:Final\s+)?[Ss]core[\s:=]+\s*(-?\d+(?:\.\d+)?)",
    re.MULTILINE,
)
_PYTEST_IMPORT_RE = re.compile(r"^\s*(?:import\s+pytest|from\s+pytest\b)", re.MULTILINE)
_PYTEST_NAME_RE = re.compile(
    r"^\s*(?:def\s+test_[A-Za-z0-9_]+\s*\(|class\s+Test[A-Za-z0-9_]*\s*[\(:])",
    re.MULTILINE,
)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 80)].rstrip() + "\n... [truncated]"


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())


def _row_rel_dir(row: dict[str, Any]) -> str:
    rel = row.get("rel_dir")
    if isinstance(rel, str) and rel.strip():
        return rel.strip()
    return str(row.get("task_id", "")).strip()


def _load_manifest(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        tasks = raw.get("tasks")
        if not isinstance(tasks, list):
            raise ValueError("manifest dict missing tasks[]")
        return raw, [t for t in tasks if isinstance(t, dict)]
    if isinstance(raw, list):
        return {"tasks": raw}, [t for t in raw if isinstance(t, dict)]
    raise ValueError("manifest must be dict or list")


def _load_v25_unrunnable(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if isinstance(raw, dict) and isinstance(raw.get("scenarios"), dict):
        return set(str(k) for k in raw["scenarios"].keys())
    if isinstance(raw, dict) and isinstance(raw.get("ids"), list):
        return set(str(x) for x in raw["ids"])
    if isinstance(raw, list):
        return set(str(x) for x in raw)
    return set()


def _resolve_unrunnable_task_ids(tasks: list[dict[str, Any]], unrunnable_orig_ids: set[str]) -> set[str]:
    out: set[str] = set()
    if not unrunnable_orig_ids:
        return out
    for row in tasks:
        if str(row.get("source")) != "v3_imported_curated_tasks_final":
            continue
        orig = str(row.get("orig_id") or "")
        if orig in unrunnable_orig_ids:
            out.add(str(row.get("task_id")))
    return out


def _resolve_keys(args: argparse.Namespace) -> dict[str, str]:
    def _first(*names: str) -> str:
        for n in names:
            v = os.environ.get(n, "")
            if v:
                return v
        return ""

    return {
        "yunwu_key": args.yunwu_key or _first("YUNWU_KEY", "YUNWU_API_KEY"),
        "gemini_key": args.gemini_key or _first("GEMINI_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "siliconflow_key": args.siliconflow_key or _first("SILICONFLOW_KEY", "SILICONFLOW_API_KEY", "SF_API_KEY"),
        "evomap_key": args.evomap_key or _first("EVOMAP_KEY", "EVOMAP_API_KEY"),
        "bedrock_key": args.bedrock_key or _first("AWS_BEARER_TOKEN_BEDROCK", "BEDROCK_KEY", "BEDROCK_API_KEY"),
        "local_base_url": args.local_base_url or os.environ.get("LOCAL_BASE_URL", "http://localhost:8000/v1"),
    }


def _compute_cost(model_id: str, in_tok: int, out_tok: int, price_table: dict[str, tuple[float, float]]) -> float:
    inp, outp = price_table.get(model_id, (0.0, 0.0))
    return round(in_tok * inp / 1_000_000 + out_tok * outp / 1_000_000, 6)


def _select_tasks(all_tasks: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    tasks = list(all_tasks)
    if args.families:
        keep = {x.strip() for x in args.families.split(",") if x.strip()}
        tasks = [t for t in tasks if str(t.get("family")) in keep]

    if args.ids:
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        tasks = [t for t in tasks if str(t.get("task_id")) in wanted]
        missing = wanted - {str(t.get("task_id")) for t in tasks}
        if missing:
            print(f"[warn] --ids not found: {sorted(missing)}")
        print(f"[select] by ids -> {len(tasks)} tasks")
    else:
        if args.shuffle:
            random.seed(args.seed)
            random.shuffle(tasks)
        if args.limit > 0:
            tasks = tasks[: args.limit]
            print(f"[select] limited to {len(tasks)} tasks")

    tasks.sort(key=lambda r: str(r.get("task_id")))
    return tasks


def _collect_skill_chunks(task_dir: Path, max_files: int, max_chars: int) -> list[tuple[str, str]]:
    paths: list[Path] = []
    root_skill = task_dir / "SKILL.md"
    if root_skill.exists() and root_skill.is_file():
        paths.append(root_skill)
    skill_dir = task_dir / "skill"
    if skill_dir.exists() and skill_dir.is_dir():
        for p in sorted(skill_dir.rglob("*.md"), key=lambda x: str(x)):
            if p.is_file():
                paths.append(p)

    # de-dup while preserving order
    seen: set[str] = set()
    chunks: list[tuple[str, str]] = []
    for p in paths:
        sp = str(p.resolve())
        if sp in seen:
            continue
        seen.add(sp)
        rel = str(p.relative_to(task_dir))
        text = p.read_text(encoding="utf-8", errors="replace")
        chunks.append((rel, _truncate(text, max_chars)))
        if len(chunks) >= max_files:
            break
    return chunks


def _wrap_skill_prompt(skill_chunks: list[tuple[str, str]]) -> str:
    if not skill_chunks:
        return ""
    files = [f"<file path=\"{rel}\">\n{content}\n</file>" for rel, content in skill_chunks]
    return (
        "You are given the following skill package to guide your work. "
        "Follow it strictly when implementing the solution.\n\n"
        "<skill-package>\n"
        + "\n".join(files)
        + "\n</skill-package>"
    )


def _unwrap_gene(gene: dict[str, Any]) -> dict[str, Any]:
    payload = gene.get("payload")
    if not isinstance(payload, dict):
        return gene
    if gene.get("asset_type") == "Gene" or gene.get("assetType") == "Gene":
        return payload
    return gene


def _serialize_gene(gene: dict[str, Any]) -> str:
    if not gene:
        return ""
    gene = _unwrap_gene(gene)
    parts: list[str] = []
    signals = gene.get("signals_match") or gene.get("keywords") or []
    if isinstance(signals, list) and signals:
        parts.append("Domain keywords: " + ", ".join(str(x) for x in signals))
    summary = str(gene.get("summary") or "").strip()
    if summary:
        parts.append(f"Summary: {summary}")
    strategy = gene.get("strategy") or []
    if isinstance(strategy, list) and strategy:
        body = "\n".join(f"  {i+1}. {str(step)}" for i, step in enumerate(strategy))
        parts.append(f"Strategy:\n{body}")
    pre = gene.get("preconditions") or []
    if isinstance(pre, list) and pre:
        body = "\n".join(f"  - {str(x)}" for x in pre)
        parts.append(f"Key concepts:\n{body}")
    return "\n".join(parts)


def _wrap_gene_prompt(gene_text: str) -> str:
    if not gene_text:
        return ""
    return (
        "You are given the following distilled task strategy (Gene). "
        "Use it to guide your implementation.\n\n"
        f"<gene>\n{gene_text}\n</gene>"
    )


def _build_system_prompt(
    condition: str,
    row: dict[str, Any],
    pool_root: Path,
    genes_dir: Path,
    skill_max_files: int,
    skill_max_chars: int,
) -> tuple[str, bool]:
    if condition == "no_context":
        return "", False

    task_dir = pool_root / _row_rel_dir(row)
    if condition == "with_skill":
        chunks = _collect_skill_chunks(task_dir, skill_max_files, skill_max_chars)
        return _wrap_skill_prompt(chunks), False

    if condition == "with_gene":
        gene_path = genes_dir / f"{_safe_name(str(row.get('task_id')))}.json"
        if not gene_path.exists():
            return "", True
        try:
            gene = json.loads(gene_path.read_text(encoding="utf-8"))
        except Exception:
            return "", True
        return _wrap_gene_prompt(_serialize_gene(gene)), False

    raise ValueError(f"unknown condition: {condition}")


def _build_user_prompt(row: dict[str, Any], pool_root: Path) -> str:
    task_dir = pool_root / _row_rel_dir(row)
    task_md = task_dir / "task.md"
    if not task_md.exists():
        raise FileNotFoundError(f"task.md missing: {task_md}")
    text = task_md.read_text(encoding="utf-8", errors="replace").strip()
    return text + SANDBOX_NOTE + CODE_INSTRUCTION


def _parse_llm_json(text: str) -> dict[str, Any]:
    s = text.strip()
    if not s:
        raise ValueError("empty response")
    m = _JSON_FENCE_RE.search(s)
    if m:
        s = m.group(1)
    if not s.startswith("{"):
        first = s.find("{")
        last = s.rfind("}")
        if first == -1 or last == -1 or last <= first:
            raise ValueError("no JSON object found")
        s = s[first : last + 1]
    return json.loads(s)


def _soft_fix_gene_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    out = dict(payload)
    if isinstance(out.get("signals_match"), list) and len(out["signals_match"]) > 10:
        out["signals_match"] = out["signals_match"][:10]
    if isinstance(out.get("strategy"), list) and len(out["strategy"]) > 8:
        out["strategy"] = out["strategy"][:8]
    if isinstance(out.get("preconditions"), list) and len(out["preconditions"]) > 4:
        out["preconditions"] = out["preconditions"][:4]
    if out.get("validation") is None or out.get("validation") != []:
        out["validation"] = []
    constraints = out.get("constraints")
    if not isinstance(constraints, dict):
        out["constraints"] = {"max_files": 3, "forbidden_paths": [".env", "secrets", "credentials"]}
    else:
        max_files = constraints.get("max_files")
        if not isinstance(max_files, int):
            max_files = 3
        max_files = min(max(1, max_files), 50)
        forbidden = constraints.get("forbidden_paths")
        if not isinstance(forbidden, list) or not forbidden:
            forbidden = [".env", "secrets", "credentials"]
        out["constraints"] = {"max_files": max_files, "forbidden_paths": [str(x) for x in forbidden]}
    return out


def _validate_gene_payload(payload: dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(payload, dict):
        return False, "payload is not object"
    keys = set(payload.keys())
    if keys != GENE_REQUIRED_FIELDS:
        missing = sorted(GENE_REQUIRED_FIELDS - keys)
        extra = sorted(keys - GENE_REQUIRED_FIELDS)
        return False, f"bad fields missing={missing} extra={extra}"
    if payload.get("type") != "Gene":
        return False, "type must be Gene"
    summary = payload.get("summary")
    if not isinstance(summary, str) or not (50 <= len(summary) <= 300):
        return False, "summary length must be 50-300"
    category = payload.get("category")
    if category not in GENE_CATEGORY_ENUMS:
        return False, f"invalid category {category!r}"
    signals = payload.get("signals_match")
    if not isinstance(signals, list) or not (5 <= len(signals) <= 10):
        return False, "signals_match must be list of length 5-10"
    if not all(isinstance(x, str) and x.strip() for x in signals):
        return False, "signals_match contains empty items"
    strategy = payload.get("strategy")
    if not isinstance(strategy, list) or not (3 <= len(strategy) <= 8):
        return False, "strategy must be list of length 3-8"
    if not all(isinstance(x, str) and 15 <= len(x) <= 300 for x in strategy):
        return False, "strategy items must be 15-300 chars"
    pre = payload.get("preconditions")
    if not isinstance(pre, list) or len(pre) > 4:
        return False, "preconditions must be list with <=4 items"
    if not all(isinstance(x, str) for x in pre):
        return False, "preconditions must be string list"
    constraints = payload.get("constraints")
    if not isinstance(constraints, dict):
        return False, "constraints must be object"
    if not isinstance(constraints.get("max_files"), int) or not (1 <= constraints["max_files"] <= 50):
        return False, "constraints.max_files must be int 1-50"
    forbidden = constraints.get("forbidden_paths")
    if not isinstance(forbidden, list) or not forbidden:
        return False, "constraints.forbidden_paths must be non-empty list"
    if payload.get("validation") != []:
        return False, "validation must be []"
    return True, ""


def _fallback_gene_payload(task_text: str) -> dict[str, Any]:
    first_line = ""
    for line in task_text.splitlines():
        s = line.strip()
        if s:
            first_line = s
            break
    if not first_line:
        first_line = "Solve structured scientific coding tasks with robust IO and deterministic outputs."
    summary = (
        "Implement deterministic data-processing pipelines that parse required inputs, "
        "apply explicit transformation logic, and emit schema-compliant outputs with edge-case guards."
    )
    if len(summary) < 50:
        summary = summary + " Ensure output validity and reproducibility."
    return {
        "type": "Gene",
        "summary": summary,
        "category": "optimize",
        "signals_match": [
            "deterministic processing",
            "strict I/O contracts",
            "schema validation",
            "edge-case handling",
            "numerical robustness",
        ],
        "strategy": [
            "Parse inputs using the exact interfaces and field names required by the task specification.",
            "Build a deterministic transformation pipeline with explicit handling for malformed or boundary data.",
            "Generate outputs in the exact required files and schema with stable ordering and formatting.",
            "Validate intermediate and final artifacts to prevent silent failures before returning success.",
        ],
        "preconditions": [
            "Verify all required input files and columns exist before computation.",
            "Verify numeric operations handle NaN, empty slices, and divide-by-zero cases safely.",
        ],
        "constraints": {"max_files": 3, "forbidden_paths": [".env", "secrets", "credentials"]},
        "validation": [],
    }


def _build_gene_user_prompt(task_md: str, attempt_text: str, max_task_chars: int, max_code_chars: int) -> str:
    return (
        "TASK DESCRIPTION:\n"
        f"{_truncate(task_md, max_task_chars)}\n\n"
        "NO-CONTEXT ATTEMPT (from model run):\n"
        "```python\n"
        f"{_truncate(attempt_text, max_code_chars)}\n"
        "```\n\n"
        "Distill one reusable Gene JSON now."
    )


def _load_no_context_cases(cases_path: Path, source_model: str, extract_python_code_fn) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    if not cases_path.exists():
        return out
    prefix = f"{source_model}::no_context::"
    with cases_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            trial_key = str(row.get("trial_key") or "")
            if not trial_key.startswith(prefix):
                continue
            task_id = trial_key[len(prefix) :]
            raw = str(row.get("raw_response") or "")
            extracted = str(row.get("extracted_code") or "")
            if not extracted and raw:
                extracted = extract_python_code_fn(raw)
            out[task_id] = {"raw_response": raw, "extracted_code": extracted}
    return out


def _record_json_line(path: Path, payload: dict[str, Any], lock: threading.Lock) -> None:
    line = json.dumps(payload, ensure_ascii=False, default=str)
    with lock:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _generate_genes_from_cases(
    *,
    tasks: list[dict[str, Any]],
    pool_root: Path,
    cases_path: Path,
    source_model: str,
    generator_model: str,
    genes_dir: Path,
    keys: dict[str, str],
    call_llm_fn,
    model_registry: dict[str, tuple[str, str, str]],
    price_table: dict[str, tuple[float, float]],
    extract_python_code_fn,
    max_task_chars: int,
    max_code_chars: int,
    retries: int,
    dry_run: bool,
    skip_task_ids: set[str],
) -> dict[str, Any]:
    genes_dir.mkdir(parents=True, exist_ok=True)
    log_path = genes_dir / "_gene_generation_log.jsonl"
    write_lock = threading.Lock()
    cache = _load_no_context_cases(cases_path, source_model, extract_python_code_fn)
    print(f"[gene] loaded no_context cache hits: {len(cache)} from {cases_path}")

    totals = {
        "written": 0,
        "llm_success": 0,
        "fallback": 0,
        "skip_unrunnable": 0,
        "missing_case": 0,
        "llm_fail": 0,
    }
    total_in_tok = 0
    total_out_tok = 0
    total_cost = 0.0
    t0 = time.time()

    if dry_run:
        print("[gene][dry-run] skip API generation, only preview first task prompt.")
        if tasks:
            first = tasks[0]
            task_dir = pool_root / _row_rel_dir(first)
            task_md = (task_dir / "task.md").read_text(encoding="utf-8", errors="replace") if (task_dir / "task.md").exists() else ""
            sample = cache.get(str(first.get("task_id")), {}).get("extracted_code") or "print('placeholder')"
            preview = _build_gene_user_prompt(task_md, sample, max_task_chars, max_code_chars)
            print(preview[:2000])
        return {
            "log_path": str(log_path),
            "elapsed_s": 0.0,
            "totals": totals,
            "token_usage": {"input": 0, "output": 0},
            "cost_usd": 0.0,
        }

    model_id = model_registry[generator_model][0]
    created_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    for idx, row in enumerate(tasks, start=1):
        task_id = str(row.get("task_id"))
        task_dir = pool_root / _row_rel_dir(row)
        task_md_path = task_dir / "task.md"
        task_md = task_md_path.read_text(encoding="utf-8", errors="replace") if task_md_path.exists() else ""

        if task_id in skip_task_ids:
            totals["skip_unrunnable"] += 1
            _record_json_line(
                log_path,
                {"task_id": task_id, "source": "skip_unrunnable"},
                write_lock,
            )
            continue

        case = cache.get(task_id)
        source_marker = "experiential"
        payload: Optional[dict[str, Any]] = None
        error_msg = ""
        calls: list[dict[str, Any]] = []

        if case is None:
            totals["missing_case"] += 1
            source_marker = "rule_based_fallback"
            payload = _fallback_gene_payload(task_md)
            error_msg = "missing_no_context_case"
        else:
            attempt_text = case.get("extracted_code") or case.get("raw_response") or ""
            user = _build_gene_user_prompt(task_md, attempt_text, max_task_chars, max_code_chars)
            system = GENE_SYSTEM_PROMPT
            for k in range(retries + 1):
                try:
                    api = call_llm_fn(
                        generator_model,
                        user,
                        system,
                        yunwu_key=keys["yunwu_key"],
                        gemini_key=keys["gemini_key"],
                        siliconflow_key=keys["siliconflow_key"],
                        evomap_key=keys["evomap_key"],
                        bedrock_key=keys["bedrock_key"],
                        local_base_url=keys["local_base_url"],
                    )
                    in_tok = int(api.get("input_tokens", 0) or 0)
                    out_tok = int(api.get("output_tokens", 0) or 0)
                    total_in_tok += in_tok
                    total_out_tok += out_tok
                    total_cost += _compute_cost(model_id, in_tok, out_tok, price_table)
                    raw = str(api.get("response") or "")
                    candidate = _soft_fix_gene_payload(_parse_llm_json(raw))
                    ok, reason = _validate_gene_payload(candidate)
                    calls.append(
                        {
                            "try": k + 1,
                            "input_tokens": in_tok,
                            "output_tokens": out_tok,
                            "ok": ok,
                            "error": "" if ok else reason,
                        }
                    )
                    if ok:
                        payload = candidate
                        source_marker = "experiential" if k == 0 else "experiential_retry"
                        break
                    system = GENE_SYSTEM_PROMPT + GENE_RETRY_SUFFIX.format(error=reason)
                except Exception as e:
                    calls.append(
                        {
                            "try": k + 1,
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "ok": False,
                            "error": f"{type(e).__name__}: {e}",
                        }
                    )
                    error_msg = f"{type(e).__name__}: {e}"

            if payload is None:
                source_marker = "rule_based_fallback"
                payload = _fallback_gene_payload(task_md)
                totals["llm_fail"] += 1
                if not error_msg:
                    error_msg = "validation_failed_after_retries"

        assert payload is not None
        if source_marker.startswith("experiential"):
            totals["llm_success"] += 1
        else:
            totals["fallback"] += 1

        asset = {
            "asset_type": "Gene",
            "schema_version": "gene_bench_v3/gep_sim_v1",
            "task_id": task_id,
            "source_track": str(row.get("source") or "unknown"),
            "model_name": model_id if source_marker.startswith("experiential") else "rule_based_fallback_v1",
            "generation_source": source_marker,
            "source_trial_key": f"{source_model}::no_context::{task_id}",
            "created_at": created_at,
            "payload": payload,
        }
        out_path = genes_dir / f"{_safe_name(task_id)}.json"
        out_path.write_text(json.dumps(asset, ensure_ascii=False, indent=2), encoding="utf-8")
        totals["written"] += 1
        _record_json_line(
            log_path,
            {
                "task_id": task_id,
                "source": source_marker,
                "error": error_msg,
                "calls": calls,
                "output": str(out_path),
            },
            write_lock,
        )
        if idx % 25 == 0:
            print(f"[gene] {idx}/{len(tasks)} processed")

    elapsed = round(time.time() - t0, 2)
    summary = {
        "log_path": str(log_path),
        "elapsed_s": elapsed,
        "totals": totals,
        "token_usage": {"input": total_in_tok, "output": total_out_tok},
        "cost_usd": round(total_cost, 6),
        "source_model": source_model,
        "generator_model": generator_model,
        "cases_path": str(cases_path),
        "genes_dir": str(genes_dir),
    }
    print(f"[gene] done in {elapsed}s -> {totals} cost=${summary['cost_usd']:.4f}")
    return summary


def _run_command(cmd: list[str], cwd: Path, timeout_s: int) -> tuple[int, str, str, Optional[str]]:
    try:
        proc = __import__("subprocess").run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or "", None
    except __import__("subprocess").TimeoutExpired:
        return -1, "", "", f"timeout_{timeout_s}s"
    except Exception as e:
        return -1, "", "", f"exec_error:{type(e).__name__}:{e}"


def _has_fail_line(text: str) -> bool:
    return bool(_FAIL_LINE_RE.search(text or ""))


def _parse_scores(stdout: str) -> dict[str, float]:
    scores: dict[str, float] = {}
    bare_idx = 0
    for m in _SCORE_RE.finditer(stdout or ""):
        try:
            if m.group(1):
                scores[m.group(1)] = float(m.group(2))
            elif m.group(3):
                bare_idx += 1
                scores[f"score_{bare_idx}"] = float(m.group(3))
        except Exception:
            continue
    return scores


def _aggregate_score(scores: dict[str, float]) -> Optional[float]:
    if not scores:
        return None
    for key in ("pass_rate", "overall", "final", "aggregate", "total", "mean", "weighted", "average"):
        if key in scores:
            return float(scores[key])
    return float(sum(scores.values()) / len(scores))


def _count_pytest_outcome(text: str) -> tuple[int, int, int]:
    n_pass = int(m.group(1)) if (m := re.search(r"(\d+)\s+passed", text)) else 0
    n_fail = int(m.group(1)) if (m := re.search(r"(\d+)\s+failed", text)) else 0
    n_err = int(m.group(1)) if (m := re.search(r"(\d+)\s+errors?", text)) else 0
    return n_pass, n_fail, n_err


def _detect_test_style(test_script_path: Path) -> str:
    if not test_script_path.exists():
        return "script"
    text = test_script_path.read_text(encoding="utf-8", errors="replace")
    if _PYTEST_IMPORT_RE.search(text) or _PYTEST_NAME_RE.search(text):
        return "pytest"
    return "script"


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


class _CandidateFileGuard:
    _SENTINEL = object()

    def __init__(self, task_dir: Path, code: str):
        self.task_dir = task_dir
        self.code = code
        self._backups: dict[Path, Any] = {}

    def __enter__(self):
        for name in ("generated.py", "solution.py"):
            p = self.task_dir / name
            if p.exists() and p.is_file():
                self._backups[p] = p.read_bytes()
            else:
                self._backups[p] = self._SENTINEL
            p.write_text(self.code, encoding="utf-8")
        return self

    def __exit__(self, exc_type, exc, tb):
        for p, blob in self._backups.items():
            try:
                if blob is self._SENTINEL:
                    p.unlink(missing_ok=True)
                else:
                    p.write_bytes(blob)
            except Exception:
                pass
        return False


def _eval_text_short_answer(task_dir: Path, python_exec: str, test_timeout: int) -> dict[str, Any]:
    rc, stdout, stderr, err = _run_command([python_exec, "test_script.py", "--candidate", "generated.py"], task_dir, test_timeout)
    if err is not None:
        return {
            "mode": "text_short_answer",
            "passed": False,
            "n_pass": 0,
            "n_fail": 1,
            "n_total": 1,
            "pass_rate": 0.0,
            "agg_score": None,
            "scores": {},
            "error_type": "test_exec_error",
            "reason": err,
            "returncode": rc,
            "stdout_tail": stdout[-TAIL_CHARS:],
            "stderr_tail": stderr[-TAIL_CHARS:],
        }
    passed = rc == 0 and "PASS:SCORE:1.0" in stdout
    return {
        "mode": "text_short_answer",
        "passed": passed,
        "n_pass": 1 if passed else 0,
        "n_fail": 0 if passed else 1,
        "n_total": 1,
        "pass_rate": 1.0 if passed else 0.0,
        "agg_score": 1.0 if passed else 0.0,
        "scores": {},
        "error_type": "" if passed else "test_failure",
        "reason": "PASS:SCORE:1.0 found" if passed else "missing PASS:SCORE:1.0",
        "returncode": rc,
        "stdout_tail": stdout[-TAIL_CHARS:],
        "stderr_tail": stderr[-TAIL_CHARS:],
    }


def _eval_pytest_like(task_dir: Path, python_exec: str, test_timeout: int, mode_name: str) -> dict[str, Any]:
    rc, stdout, stderr, err = _run_command([python_exec, "-m", "pytest", "test_script.py", "-q"], task_dir, test_timeout)
    if err is not None:
        return {
            "mode": mode_name,
            "passed": False,
            "n_pass": 0,
            "n_fail": 1,
            "n_total": 1,
            "pass_rate": 0.0,
            "agg_score": None,
            "scores": {},
            "error_type": "pytest_exec_error",
            "reason": err,
            "returncode": rc,
            "stdout_tail": stdout[-TAIL_CHARS:],
            "stderr_tail": stderr[-TAIL_CHARS:],
        }
    out = (stdout or "") + "\n" + (stderr or "")
    n_pass, n_fail, n_err = _count_pytest_outcome(out)
    passed = rc == 0 and n_pass >= 1 and n_fail == 0 and n_err == 0
    return {
        "mode": mode_name,
        "passed": passed,
        "n_pass": n_pass,
        "n_fail": n_fail + n_err,
        "n_total": max(1, n_pass + n_fail + n_err),
        "pass_rate": float(n_pass / max(1, n_pass + n_fail + n_err)),
        "agg_score": float(n_pass / max(1, n_pass + n_fail + n_err)),
        "scores": {},
        "error_type": "" if passed else "test_failure",
        "reason": f"pytest rc={rc}, passed={n_pass}, failed={n_fail}, errors={n_err}",
        "returncode": rc,
        "stdout_tail": stdout[-TAIL_CHARS:],
        "stderr_tail": stderr[-TAIL_CHARS:],
    }


def _eval_subprocess_cli(
    task_dir: Path,
    python_exec: str,
    test_timeout: int,
    score_threshold: float,
    mode_name: str,
) -> dict[str, Any]:
    rc, stdout, stderr, err = _run_command([python_exec, "test_script.py"], task_dir, test_timeout)
    if err is not None:
        return {
            "mode": mode_name,
            "passed": False,
            "n_pass": 0,
            "n_fail": 1,
            "n_total": 1,
            "pass_rate": 0.0,
            "agg_score": None,
            "scores": {},
            "error_type": "test_exec_error",
            "reason": err,
            "returncode": rc,
            "stdout_tail": stdout[-TAIL_CHARS:],
            "stderr_tail": stderr[-TAIL_CHARS:],
        }
    scores = _parse_scores(stdout)
    agg = _aggregate_score(scores)
    pass_rate = scores.get("pass_rate")
    has_fail = _has_fail_line(stdout)
    if pass_rate is None:
        passed = rc == 0 and not has_fail
        pass_rate = 1.0 if passed else 0.0
    else:
        passed = rc == 0 and (pass_rate >= score_threshold) and not has_fail
    return {
        "mode": mode_name,
        "passed": passed,
        "n_pass": 1 if passed else 0,
        "n_fail": 0 if passed else 1,
        "n_total": 1,
        "pass_rate": float(pass_rate),
        "agg_score": float(agg if agg is not None else pass_rate),
        "scores": scores,
        "error_type": "" if passed else "test_failure",
        "reason": f"rc={rc}, pass_rate={pass_rate}, has_FAIL={has_fail}",
        "returncode": rc,
        "stdout_tail": stdout[-TAIL_CHARS:],
        "stderr_tail": stderr[-TAIL_CHARS:],
    }


def _eval_subprocess_ref_runner(
    task_dir: Path,
    python_exec: str,
    gen_timeout: int,
    test_timeout: int,
) -> dict[str, Any]:
    staged = _stage_data_children(task_dir)
    output_dir = task_dir / "output"
    try:
        if output_dir.exists():
            if output_dir.is_dir():
                shutil.rmtree(output_dir, ignore_errors=True)
            else:
                output_dir.unlink(missing_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    try:
        rc_gen, stdout_gen, stderr_gen, err_gen = _run_command([python_exec, "generated.py"], task_dir, gen_timeout)
        if err_gen is not None:
            return {
                "mode": "subprocess_ref_runner",
                "passed": False,
                "n_pass": 0,
                "n_fail": 1,
                "n_total": 1,
                "pass_rate": 0.0,
                "agg_score": None,
                "scores": {},
                "error_type": "generated_exec_error",
                "reason": err_gen,
                "returncode": rc_gen,
                "stdout_tail": stdout_gen[-TAIL_CHARS:],
                "stderr_tail": stderr_gen[-TAIL_CHARS:],
            }
        if rc_gen != 0:
            return {
                "mode": "subprocess_ref_runner",
                "passed": False,
                "n_pass": 0,
                "n_fail": 1,
                "n_total": 1,
                "pass_rate": 0.0,
                "agg_score": 0.0,
                "scores": {},
                "error_type": "generated_nonzero",
                "reason": f"generated.py rc={rc_gen}",
                "returncode": rc_gen,
                "stdout_tail": stdout_gen[-TAIL_CHARS:],
                "stderr_tail": stderr_gen[-TAIL_CHARS:],
            }

        test_style = _detect_test_style(task_dir / "test_script.py")
        if test_style == "pytest":
            eval_dict = _eval_pytest_like(task_dir, python_exec, test_timeout, "subprocess_ref_runner")
            eval_dict["reason"] = f"generated_rc={rc_gen}; {eval_dict.get('reason', '')}"
            return eval_dict

        rc, stdout, stderr, err = _run_command([python_exec, "test_script.py"], task_dir, test_timeout)
        if err is not None:
            return {
                "mode": "subprocess_ref_runner",
                "passed": False,
                "n_pass": 0,
                "n_fail": 1,
                "n_total": 1,
                "pass_rate": 0.0,
                "agg_score": None,
                "scores": {},
                "error_type": "test_exec_error",
                "reason": err,
                "returncode": rc,
                "stdout_tail": stdout[-TAIL_CHARS:],
                "stderr_tail": stderr[-TAIL_CHARS:],
            }
        scores = _parse_scores(stdout)
        agg = _aggregate_score(scores)
        has_fail = _has_fail_line(stdout)
        passed = rc == 0 and not has_fail
        return {
            "mode": "subprocess_ref_runner",
            "passed": passed,
            "n_pass": 1 if passed else 0,
            "n_fail": 0 if passed else 1,
            "n_total": 1,
            "pass_rate": 1.0 if passed else 0.0,
            "agg_score": float(agg if agg is not None else (1.0 if passed else 0.0)),
            "scores": scores,
            "error_type": "" if passed else "test_failure",
            "reason": f"generated_rc={rc_gen}, test_rc={rc}, has_FAIL={has_fail}",
            "returncode": rc,
            "stdout_tail": stdout[-TAIL_CHARS:],
            "stderr_tail": stderr[-TAIL_CHARS:],
        }
    finally:
        for p in staged:
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
            except Exception:
                pass
        for junk in (task_dir / "__pycache__", task_dir / ".pytest_cache"):
            shutil.rmtree(junk, ignore_errors=True)


def _evaluate_generated_code(
    *,
    row: dict[str, Any],
    code: str,
    pool_root: Path,
    python_exec: str,
    gen_timeout: int,
    test_timeout: int,
    score_threshold: float,
) -> dict[str, Any]:
    task_dir = pool_root / _row_rel_dir(row)
    if not task_dir.exists():
        return {
            "mode": "none",
            "passed": False,
            "n_pass": 0,
            "n_fail": 0,
            "n_total": 0,
            "pass_rate": 0.0,
            "agg_score": None,
            "scores": {},
            "error_type": "no_task_dir",
            "reason": f"missing task dir: {_row_rel_dir(row)}",
            "returncode": None,
            "stdout_tail": "",
            "stderr_tail": "",
        }
    if not (task_dir / "test_script.py").exists():
        return {
            "mode": "none",
            "passed": False,
            "n_pass": 0,
            "n_fail": 0,
            "n_total": 0,
            "pass_rate": 0.0,
            "agg_score": None,
            "scores": {},
            "error_type": "no_test_script",
            "reason": "missing test_script.py",
            "returncode": None,
            "stdout_tail": "",
            "stderr_tail": "",
        }
    if not code.strip():
        return {
            "mode": "none",
            "passed": False,
            "n_pass": 0,
            "n_fail": 0,
            "n_total": 0,
            "pass_rate": 0.0,
            "agg_score": None,
            "scores": {},
            "error_type": "no_code",
            "reason": "empty extracted code",
            "returncode": None,
            "stdout_tail": "",
            "stderr_tail": "",
        }

    mode = str(row.get("execution_mode"))
    with _CandidateFileGuard(task_dir, code):
        if mode == "text_short_answer":
            return _eval_text_short_answer(task_dir, python_exec, test_timeout)
        if mode == "pytest_pkg":
            return _eval_pytest_like(task_dir, python_exec, test_timeout, "pytest_pkg")
        if mode == "subprocess_cli":
            return _eval_subprocess_cli(task_dir, python_exec, test_timeout, score_threshold, "subprocess_cli")
        if mode == "subprocess_cli_no_ref":
            return _eval_subprocess_cli(task_dir, python_exec, test_timeout, score_threshold, "subprocess_cli_no_ref")
        if mode == "subprocess_ref_runner":
            return _eval_subprocess_ref_runner(task_dir, python_exec, gen_timeout, test_timeout)
        return {
            "mode": mode,
            "passed": False,
            "n_pass": 0,
            "n_fail": 1,
            "n_total": 1,
            "pass_rate": 0.0,
            "agg_score": None,
            "scores": {},
            "error_type": "unknown_execution_mode",
            "reason": mode,
            "returncode": None,
            "stdout_tail": "",
            "stderr_tail": "",
        }


@dataclass(frozen=True)
class Trial:
    task_id: str
    family: str
    execution_mode: str
    source: str
    orig_id: str
    model: str
    condition: str
    rel_dir: str

    @property
    def trial_key(self) -> str:
        return f"{self.model}::{self.condition}::{self.task_id}"


def _make_trials(tasks: list[dict[str, Any]], models: list[str], conditions: list[str]) -> list[tuple[Trial, dict[str, Any]]]:
    out: list[tuple[Trial, dict[str, Any]]] = []
    for row in tasks:
        task_id = str(row.get("task_id"))
        fam = str(row.get("family"))
        mode = str(row.get("execution_mode"))
        src = str(row.get("source"))
        orig = str(row.get("orig_id") or "")
        rel = _row_rel_dir(row)
        for m in models:
            for c in conditions:
                out.append(
                    (
                        Trial(
                            task_id=task_id,
                            family=fam,
                            execution_mode=mode,
                            source=src,
                            orig_id=orig,
                            model=m,
                            condition=c,
                            rel_dir=rel,
                        ),
                        row,
                    )
                )
    return out


def _load_completed_keys(results_path: Path) -> tuple[set[str], set[str]]:
    done: set[str] = set()
    api_err: set[str] = set()
    if not results_path.exists():
        return done, api_err
    with results_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            trial = row.get("trial") or {}
            key = str(trial.get("trial_key") or "")
            if not key:
                continue
            err = str((row.get("eval") or {}).get("error_type") or "")
            if err == "api_error":
                api_err.add(key)
            else:
                done.add(key)
    return done, api_err


def _classify_status(eval_row: dict[str, Any]) -> str:
    if eval_row.get("passed"):
        return "pass"
    err = str(eval_row.get("error_type") or "")
    if err == "skipped_unrunnable":
        return "skip"
    if err in {"api_error", "no_code", "no_task_dir", "no_test_script", "missing_gene", "prompt_build_error"}:
        return "error"
    return "fail"


def _write_summary(run_dir: Path, results_path: Path, extra: dict[str, Any]) -> dict[str, Any]:
    latest: dict[str, dict[str, Any]] = {}
    if results_path.exists():
        with results_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                trial = row.get("trial") or {}
                key = str(trial.get("trial_key") or "")
                if key:
                    latest[key] = row

    by_model_condition: dict[str, dict[str, int]] = {}
    by_family: dict[str, dict[str, int]] = {}
    by_condition: dict[str, dict[str, int]] = {}
    total_cost = 0.0
    total_trials = 0
    for row in latest.values():
        trial = row.get("trial") or {}
        ev = row.get("eval") or {}
        status = _classify_status(ev)
        key_mc = f"{trial.get('model')}::{trial.get('condition')}"
        key_c = str(trial.get("condition"))
        fam = str(trial.get("family"))
        for bucket_key, bucket in ((key_mc, by_model_condition), (key_c, by_condition), (fam, by_family)):
            slot = bucket.setdefault(bucket_key, {"pass": 0, "fail": 0, "error": 0, "skip": 0})
            slot[status] = slot.get(status, 0) + 1
        total_cost += float(row.get("cost_usd") or 0.0)
        total_trials += 1

    summary = {
        "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_trials_latest": total_trials,
        "cost_usd_latest": round(total_cost, 6),
        "by_model_condition": dict(sorted(by_model_condition.items())),
        "by_condition": dict(sorted(by_condition.items())),
        "by_family": dict(sorted(by_family.items())),
        **extra,
    }
    out_path = run_dir / "summary.json"
    out_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def _execute_trials(
    *,
    tasks: list[dict[str, Any]],
    models: list[str],
    conditions: list[str],
    run_dir: Path,
    args: argparse.Namespace,
    keys: dict[str, str],
    genes_dir: Path,
    skip_task_ids: set[str],
    stage_name: str,
    call_llm_fn,
    extract_python_code_fn,
    model_registry: dict[str, tuple[str, str, str]],
    price_table: dict[str, tuple[float, float]],
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    results_path = run_dir / "results.jsonl"
    cases_path = run_dir / "cases.jsonl"
    budget_path = run_dir / "budget.jsonl"
    config_path = run_dir / "config.json"

    done_keys, api_error_keys = _load_completed_keys(results_path)
    if done_keys:
        msg = f"[{stage_name}] resume: {len(done_keys)} completed"
        if api_error_keys:
            msg += f", will retry {len(api_error_keys)} api_error trials"
        print(msg)

    trials = _make_trials(tasks, models, conditions)
    pending = [(t, row) for (t, row) in trials if t.trial_key not in done_keys]
    print(
        f"[{stage_name}] trials total={len(trials)} pending={len(pending)} "
        f"models={models} conditions={conditions}"
    )

    cfg: dict[str, Any] = {}
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg.update(
        {
            "manifest": str(Path(args.manifest).resolve()),
            "pool_root": str(Path(args.pool_root).resolve()),
            "python_executable": args.python_executable,
            "workers": args.workers,
            "gen_timeout": args.gen_timeout,
            "test_timeout": args.test_timeout,
            "score_pass_threshold": args.score_pass_threshold,
            "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )
    stages = cfg.get("stages")
    if not isinstance(stages, list):
        stages = []
    stages.append(
        {
            "stage": stage_name,
            "started_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "n_trials_total": len(trials),
            "n_trials_pending": len(pending),
            "models": models,
            "conditions": conditions,
        }
    )
    cfg["stages"] = stages[-20:]
    config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")

    if args.dry_run:
        for t, _ in pending[:20]:
            print(f"  [dry] {t.model:12s} {t.condition:12s} {t.task_id:8s}")
        if len(pending) > 20:
            print(f"  ... and {len(pending) - 20} more")
        return {
            "run_dir": run_dir,
            "results_path": results_path,
            "cases_path": cases_path,
            "budget_path": budget_path,
            "pending": len(pending),
            "done": 0,
            "ok": 0,
            "err": 0,
        }

    write_lock = threading.Lock()
    task_locks: dict[str, threading.Lock] = {}
    task_locks_guard = threading.Lock()
    counter = {"done": 0, "ok": 0, "err": 0, "total": len(pending)}
    t0 = time.time()

    def _task_lock(task_id: str) -> threading.Lock:
        with task_locks_guard:
            lock = task_locks.get(task_id)
            if lock is None:
                lock = threading.Lock()
                task_locks[task_id] = lock
            return lock

    def _do_trial(trial: Trial, row: dict[str, Any]) -> None:
        start = time.time()
        try:
            # v2.5 compatibility: skip known unrunnable curated tasks before API call.
            if (not args.no_skip_unrunnable) and trial.task_id in skip_task_ids:
                record = {
                    "trial": {**asdict(trial), "trial_key": trial.trial_key},
                    "eval": {
                        "mode": "none",
                        "passed": False,
                        "n_pass": 0,
                        "n_fail": 0,
                        "n_total": 0,
                        "pass_rate": 0.0,
                        "agg_score": None,
                        "scores": {},
                        "error_type": "skipped_unrunnable",
                        "reason": f"orig_id={trial.orig_id}",
                    },
                    "tokens": {"input": 0, "output": 0, "system_chars": 0},
                    "cost_usd": 0.0,
                    "elapsed_s": round(time.time() - start, 3),
                }
                _record_json_line(results_path, record, write_lock)
                with write_lock:
                    counter["done"] += 1
                    print(
                        f"  [{counter['done']}/{counter['total']}] "
                        f"{trial.model:12s} {trial.condition:12s} {trial.task_id:8s} SKIP[unrunnable]"
                    )
                return

            system_prompt, missing_gene = _build_system_prompt(
                trial.condition,
                row,
                Path(args.pool_root),
                genes_dir,
                args.skill_max_files,
                args.skill_max_chars,
            )
            if missing_gene:
                record = {
                    "trial": {**asdict(trial), "trial_key": trial.trial_key},
                    "eval": {
                        "mode": "none",
                        "passed": False,
                        "n_pass": 0,
                        "n_fail": 0,
                        "n_total": 0,
                        "pass_rate": 0.0,
                        "agg_score": None,
                        "scores": {},
                        "error_type": "missing_gene",
                        "reason": f"missing gene file for {trial.task_id}",
                    },
                    "tokens": {"input": 0, "output": 0, "system_chars": 0},
                    "cost_usd": 0.0,
                    "elapsed_s": round(time.time() - start, 3),
                }
                _record_json_line(results_path, record, write_lock)
                with write_lock:
                    counter["done"] += 1
                    print(
                        f"  [{counter['done']}/{counter['total']}] "
                        f"{trial.model:12s} {trial.condition:12s} {trial.task_id:8s} ERR[missing_gene]"
                    )
                return

            try:
                user_prompt = _build_user_prompt(row, Path(args.pool_root))
            except Exception as e:
                record = {
                    "trial": {**asdict(trial), "trial_key": trial.trial_key},
                    "eval": {
                        "mode": "none",
                        "passed": False,
                        "n_pass": 0,
                        "n_fail": 0,
                        "n_total": 0,
                        "pass_rate": 0.0,
                        "agg_score": None,
                        "scores": {},
                        "error_type": "prompt_build_error",
                        "reason": f"{type(e).__name__}: {e}",
                    },
                    "tokens": {"input": 0, "output": 0, "system_chars": 0},
                    "cost_usd": 0.0,
                    "elapsed_s": round(time.time() - start, 3),
                }
                _record_json_line(results_path, record, write_lock)
                with write_lock:
                    counter["done"] += 1
                    counter["err"] += 1
                    print(
                        f"  [{counter['done']}/{counter['total']}] "
                        f"{trial.model:12s} {trial.condition:12s} {trial.task_id:8s} "
                        f"ERR[prompt_build]"
                    )
                return

            api = call_llm_fn(
                trial.model,
                user_prompt,
                system_prompt,
                yunwu_key=keys["yunwu_key"],
                gemini_key=keys["gemini_key"],
                siliconflow_key=keys["siliconflow_key"],
                evomap_key=keys["evomap_key"],
                bedrock_key=keys["bedrock_key"],
                local_base_url=keys["local_base_url"],
            )
            raw_response = str(api.get("response") or "")
            in_tok = int(api.get("input_tokens", 0) or 0)
            out_tok = int(api.get("output_tokens", 0) or 0)
            model_id = model_registry[trial.model][0]
            cost = _compute_cost(model_id, in_tok, out_tok, price_table)
            _record_json_line(
                budget_path,
                {
                    "trial_key": trial.trial_key,
                    "model_id": model_id,
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "cost_usd": cost,
                },
                write_lock,
            )

            code = extract_python_code_fn(raw_response)
            _record_json_line(
                cases_path,
                {
                    "trial_key": trial.trial_key,
                    "raw_response": raw_response,
                    "extracted_code": code,
                },
                write_lock,
            )

            if not code.strip():
                eval_result = {
                    "mode": "none",
                    "passed": False,
                    "n_pass": 0,
                    "n_fail": 0,
                    "n_total": 0,
                    "pass_rate": 0.0,
                    "agg_score": None,
                    "scores": {},
                    "error_type": "no_code",
                    "reason": "extract_python_code returned empty",
                    "returncode": None,
                    "stdout_tail": "",
                    "stderr_tail": "",
                }
            else:
                lock = _task_lock(trial.task_id)
                with lock:
                    eval_result = _evaluate_generated_code(
                        row=row,
                        code=code,
                        pool_root=Path(args.pool_root),
                        python_exec=args.python_executable,
                        gen_timeout=args.gen_timeout,
                        test_timeout=args.test_timeout,
                        score_threshold=args.score_pass_threshold,
                    )

            record = {
                "trial": {**asdict(trial), "trial_key": trial.trial_key},
                "eval": eval_result,
                "tokens": {"input": in_tok, "output": out_tok, "system_chars": len(system_prompt)},
                "cost_usd": cost,
                "elapsed_s": round(time.time() - start, 3),
            }
            _record_json_line(results_path, record, write_lock)

            status = "PASS" if eval_result.get("passed") else f"FAIL[{eval_result.get('error_type', '?')}]"
            with write_lock:
                counter["done"] += 1
                if eval_result.get("passed"):
                    counter["ok"] += 1
                if eval_result.get("error_type") == "api_error":
                    counter["err"] += 1
                print(
                    f"  [{counter['done']}/{counter['total']}] "
                    f"{trial.model:12s} {trial.condition:12s} {trial.task_id:8s} "
                    f"{status:22s} ${cost:.4f} ({record['elapsed_s']}s)"
                )
        except Exception as e:
            err_record = {
                "trial": {**asdict(trial), "trial_key": trial.trial_key},
                "eval": {
                    "mode": "none",
                    "passed": False,
                    "n_pass": 0,
                    "n_fail": 0,
                    "n_total": 0,
                    "pass_rate": 0.0,
                    "agg_score": None,
                    "scores": {},
                    "error_type": "api_error",
                    "reason": f"{type(e).__name__}: {e}",
                    "traceback_tail": traceback.format_exc()[-1500:],
                },
                "tokens": {"input": 0, "output": 0, "system_chars": 0},
                "cost_usd": 0.0,
                "elapsed_s": round(time.time() - start, 3),
            }
            _record_json_line(results_path, err_record, write_lock)
            with write_lock:
                counter["done"] += 1
                counter["err"] += 1
                print(
                    f"  [{counter['done']}/{counter['total']}] "
                    f"{trial.model:12s} {trial.condition:12s} {trial.task_id:8s} "
                    f"ERR[{type(e).__name__}: {e}]"
                )

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(_do_trial, t, row) for (t, row) in pending]
        for _ in as_completed(futs):
            pass

    elapsed = round(time.time() - t0, 2)
    print(
        f"[{stage_name}] finished in {elapsed}s: "
        f"ok={counter['ok']} done={counter['done']} err={counter['err']}"
    )
    summary = _write_summary(
        run_dir,
        results_path,
        extra={
            "stage_name": stage_name,
            "models": models,
            "conditions": conditions,
            "n_tasks_selected": len(tasks),
            "elapsed_s": elapsed,
        },
    )
    print(f"[{stage_name}] summary -> {run_dir / 'summary.json'}")
    return {
        "run_dir": run_dir,
        "results_path": results_path,
        "cases_path": cases_path,
        "budget_path": budget_path,
        "pending": len(pending),
        "done": counter["done"],
        "ok": counter["ok"],
        "err": counter["err"],
        "summary": summary,
    }


def _import_v25_eval_api() -> tuple[dict[str, tuple[str, str, str]], dict[str, tuple[float, float]], Any, Any]:
    v3_eval = V3_ROOT / "eval"
    sys.path.insert(0, str(v3_eval))
    try:
        from api import MODEL_REGISTRY, PRICE_TABLE, call_llm, extract_python_code
    except Exception as e:
        raise SystemExit(
            "failed to import v3 eval API helpers from "
            f"{v3_eval}: {type(e).__name__}: {e}"
        )
    return MODEL_REGISTRY, PRICE_TABLE, call_llm, extract_python_code


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="Path to tasks_final manifest.json")
    ap.add_argument("--pool-root", default=str(DEFAULT_POOL_ROOT), help="Path to tasks_final root")
    ap.add_argument("--python-executable", default=DEFAULT_PYTHON, help="Python used to run oracles")
    ap.add_argument("--runs-root", default=str(DEFAULT_RUNS_ROOT), help="Directory to store run outputs")
    ap.add_argument("--run-id", default=None, help="Run directory name (default timestamp)")

    ap.add_argument("--models", required=True, help="Comma-separated model aliases (from v2.5 eval.api MODEL_REGISTRY)")
    ap.add_argument(
        "--conditions",
        default=",".join(CONDITIONS),
        help=f"Comma-separated subset of {CONDITIONS}",
    )
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0, help="Limit N tasks (0=all)")
    ap.add_argument("--shuffle", action="store_true", help="Shuffle tasks before limit")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--ids", default="", help="Comma-separated task_id filters, e.g. T0001,T0407")
    ap.add_argument("--families", default="", help="Comma-separated family filters")

    ap.add_argument("--gen-timeout", type=int, default=120, help="Timeout for generated.py execution")
    ap.add_argument("--test-timeout", type=int, default=180, help="Timeout for oracle execution")
    ap.add_argument("--score-pass-threshold", type=float, default=1.0, help="Pass threshold when SCORE:pass_rate exists")

    ap.add_argument("--summary-only", action="store_true", help="Only re-aggregate summary.json for existing run")
    ap.add_argument("--dry-run", action="store_true", help="No API calls; print pending trial sample only")
    ap.add_argument("--no-skip-unrunnable", action="store_true", help="Do not skip v2.5 unrunnable curated tasks")
    ap.add_argument("--v25-unrunnable-json", default=DEFAULT_UNRUNNABLE_JSON)

    # API keys / endpoints
    ap.add_argument("--yunwu-key", default="")
    ap.add_argument("--gemini-key", default="")
    ap.add_argument("--siliconflow-key", default="")
    ap.add_argument("--evomap-key", default="")
    ap.add_argument("--bedrock-key", default="")
    ap.add_argument("--local-base-url", default="")

    # Prompt shaping
    ap.add_argument("--skill-max-files", type=int, default=8)
    ap.add_argument("--skill-max-chars", type=int, default=12000)

    # Gene bootstrap controls
    ap.add_argument("--genes-dir", default=str(DEFAULT_GENES_DIR))
    ap.add_argument("--auto-gene", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--gene-only", action="store_true", help="Only generate genes then exit")
    ap.add_argument("--gene-source-model", default="gemini_pro", help="Model alias whose no_context outputs seed gene generation")
    ap.add_argument("--gene-generator-model", default="gemini_pro", help="Model alias used to distill gene JSON")
    ap.add_argument("--gene-from-run", default="", help="Existing run dir (or cases.jsonl path) to read no_context cases from")
    ap.add_argument("--gene-bootstrap-run-id", default="", help="Run-id for auto no_context bootstrap when needed")
    ap.add_argument("--gene-retries", type=int, default=2)
    ap.add_argument("--gene-max-task-chars", type=int, default=3500)
    ap.add_argument("--gene-max-code-chars", type=int, default=5000)

    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])

    model_registry, price_table, call_llm_fn, extract_python_code_fn = _import_v25_eval_api()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        raise SystemExit("--models cannot be empty")
    unknown_models = [m for m in models if m not in model_registry]
    if unknown_models:
        raise SystemExit(f"unknown model aliases: {sorted(unknown_models)}")

    conditions = [c.strip() for c in args.conditions.split(",") if c.strip()]
    if not conditions:
        raise SystemExit("--conditions cannot be empty")
    for c in conditions:
        if c not in CONDITIONS:
            raise SystemExit(f"unknown condition {c!r}; allowed={CONDITIONS}")

    manifest_path = Path(args.manifest).resolve()
    pool_root = Path(args.pool_root).resolve()
    if not manifest_path.exists():
        raise SystemExit(f"manifest not found: {manifest_path}")
    if not pool_root.exists():
        raise SystemExit(f"pool_root not found: {pool_root}")

    _, all_tasks = _load_manifest(manifest_path)
    tasks = _select_tasks(all_tasks, args)
    if not tasks:
        raise SystemExit("no tasks selected")
    print(f"[load] selected tasks: {len(tasks)} / {len(all_tasks)}")

    runs_root = Path(args.runs_root).resolve()
    runs_root.mkdir(parents=True, exist_ok=True)
    run_id = args.run_id or f"v3_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir = runs_root / run_id
    results_path = run_dir / "results.jsonl"
    if args.summary_only:
        if not results_path.exists():
            raise SystemExit(f"summary-only: results missing at {results_path}")
        summary = _write_summary(run_dir, results_path, extra={"stage_name": "summary_only"})
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        print(f"[summary] wrote {run_dir / 'summary.json'}")
        return 0

    keys = _resolve_keys(args)
    unrunnable_orig_ids = _load_v25_unrunnable(Path(args.v25_unrunnable_json).resolve())
    skip_task_ids = _resolve_unrunnable_task_ids(tasks, unrunnable_orig_ids)
    if skip_task_ids and not args.no_skip_unrunnable:
        print(f"[load] unrunnable curated tasks to skip: {len(skip_task_ids)}")

    # 1) Optional auto-gene pipeline when with_gene is requested.
    gene_summary: Optional[dict[str, Any]] = None
    genes_dir = Path(args.genes_dir).resolve()
    need_gene = "with_gene" in conditions
    if need_gene and args.auto_gene:
        if args.gene_source_model not in model_registry:
            raise SystemExit(f"unknown --gene-source-model: {args.gene_source_model}")
        if args.gene_generator_model not in model_registry:
            raise SystemExit(f"unknown --gene-generator-model: {args.gene_generator_model}")

        if args.gene_from_run:
            p = Path(args.gene_from_run).resolve()
            source_cases = p if p.is_file() else (p / "cases.jsonl")
            if not source_cases.exists():
                raise SystemExit(f"--gene-from-run cases not found: {source_cases}")
            print(f"[gene] using external no_context cases: {source_cases}")
        else:
            # Avoid duplicate expensive no_context calls when main run already contains
            # the source model + no_context condition.
            if args.gene_source_model in models and "no_context" in conditions:
                print("[gene] seeding from main run no_context trials ...")
                _execute_trials(
                    tasks=tasks,
                    models=[args.gene_source_model],
                    conditions=["no_context"],
                    run_dir=run_dir,
                    args=args,
                    keys=keys,
                    genes_dir=genes_dir,
                    skip_task_ids=skip_task_ids,
                    stage_name="gene_seed",
                    call_llm_fn=call_llm_fn,
                    extract_python_code_fn=extract_python_code_fn,
                    model_registry=model_registry,
                    price_table=price_table,
                )
                source_cases = run_dir / "cases.jsonl"
            else:
                seed_run_id = args.gene_bootstrap_run_id or f"{run_id}__gene_seed_{args.gene_source_model}"
                seed_run_dir = runs_root / seed_run_id
                print(f"[gene] bootstrap no_context run -> {seed_run_dir}")
                _execute_trials(
                    tasks=tasks,
                    models=[args.gene_source_model],
                    conditions=["no_context"],
                    run_dir=seed_run_dir,
                    args=args,
                    keys=keys,
                    genes_dir=genes_dir,
                    skip_task_ids=skip_task_ids,
                    stage_name="gene_seed",
                    call_llm_fn=call_llm_fn,
                    extract_python_code_fn=extract_python_code_fn,
                    model_registry=model_registry,
                    price_table=price_table,
                )
                source_cases = seed_run_dir / "cases.jsonl"

        gene_summary = _generate_genes_from_cases(
            tasks=tasks,
            pool_root=pool_root,
            cases_path=source_cases,
            source_model=args.gene_source_model,
            generator_model=args.gene_generator_model,
            genes_dir=genes_dir,
            keys=keys,
            call_llm_fn=call_llm_fn,
            model_registry=model_registry,
            price_table=price_table,
            extract_python_code_fn=extract_python_code_fn,
            max_task_chars=args.gene_max_task_chars,
            max_code_chars=args.gene_max_code_chars,
            retries=args.gene_retries,
            dry_run=args.dry_run,
            skip_task_ids=skip_task_ids if not args.no_skip_unrunnable else set(),
        )
        (run_dir / "gene_generation_summary.json").parent.mkdir(parents=True, exist_ok=True)
        (run_dir / "gene_generation_summary.json").write_text(
            json.dumps(gene_summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"[gene] summary -> {run_dir / 'gene_generation_summary.json'}")

    if args.gene_only:
        print("[done] gene-only mode completed")
        return 0

    # 2) Main evaluation run.
    result = _execute_trials(
        tasks=tasks,
        models=models,
        conditions=conditions,
        run_dir=run_dir,
        args=args,
        keys=keys,
        genes_dir=genes_dir,
        skip_task_ids=skip_task_ids,
        stage_name="main",
        call_llm_fn=call_llm_fn,
        extract_python_code_fn=extract_python_code_fn,
        model_registry=model_registry,
        price_table=price_table,
    )
    final_summary = _write_summary(
        run_dir,
        result["results_path"],
        extra={
            "stage_name": "final",
            "models": models,
            "conditions": conditions,
            "n_tasks_selected": len(tasks),
            "gene_summary": gene_summary,
        },
    )
    print("\n[done]")
    print(f"run_dir      : {run_dir}")
    print(f"results      : {result['results_path']}")
    print(f"cases        : {result['cases_path']}")
    print(f"budget       : {result['budget_path']}")
    print(f"summary      : {run_dir / 'summary.json'}")
    print(
        "latest trials: "
        f"{final_summary.get('n_trials_latest')} | "
        f"latest cost: ${final_summary.get('cost_usd_latest', 0.0):.4f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

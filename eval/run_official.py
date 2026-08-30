#!/usr/bin/env python3
"""Official TaskGenome Bench evaluation driver.

This driver consumes ``gene_bench_v3/tasks_final/manifest.json`` directly and
runs model trials across the flattened v3 task pool. It reuses the existing
v2.5 LLM API client so model aliases, retry behavior, token accounting, and
Gemini settings stay consistent with earlier runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any


HERE = Path(__file__).resolve().parent
V3_ROOT = HERE.parent
REPO_ROOT = V3_ROOT.parent
TOOLS_ROOT = V3_ROOT / "tools"
POOL_ROOT = V3_ROOT / "tasks_final"
DEFAULT_MANIFEST = POOL_ROOT / "manifest.json"
DEFAULT_RUNS_ROOT = V3_ROOT / "_runs"
DEFAULT_ASSET_POLICY = V3_ROOT / "release" / "asset_policy.v1.json"
DEFAULT_HARDENED_IO_CONTRACT = V3_ROOT / "release" / "hardened_io_contract.v1.json"
CANONICAL_ASSET_POLICY_SHA256 = (
    "f8616db30e0240e3ebfe7fb5804bc22509a99f4180f32377e448079ca34447f3"
)
CANONICAL_HARDENED_IO_CONTRACT_SHA256 = (
    "69d90e3737115b6070fd3937a934b5083d4068cc40e138cc8b6baba05a3ec5e6"
)
# Frozen legacy-v1 default. Public Agent Skill experiments must opt into a
# versioned directory explicitly instead of silently changing this path.
DEFAULT_AGENT_SKILLS_DIR = POOL_ROOT / "agent_skills"
_AGENT_SKILLS_DIR = DEFAULT_AGENT_SKILLS_DIR

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from api import (  # noqa: E402
    API_MAX_RETRIES,
    API_RETRY_BASE_DELAY,
    API_RETRY_MAX_DELAY,
    BEDROCK_REGION,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TIMEOUT,
    GEMINI_REASONING_EFFORT,
    GPT_REASONING_EFFORT,
    GEMINI_VERTEX_LOCATION,
    GEMINI_VERTEX_PROJECT_ID,
    LOCAL_ENABLE_THINKING,
    MODEL_REGISTRY,
    PRICE_TABLE,
    call_llm,
    extract_python_code,
    sanitize_api_error_text,
)
from reproducibility import (  # noqa: E402
    BUDGET_SCHEMA_VERSION,
    CASE_SCHEMA_VERSION,
    RESULT_SCHEMA_VERSION,
    RUN_CONFIG_SCHEMA_VERSION,
    collect_environment,
    credential_sources,
    digest_files,
    redact_text,
    redact_tree,
    redact_url,
    redacted_args,
    sha256_file,
    stable_json_sha256,
)
from runtime_policy import (  # noqa: E402
    HARDENED_PROTOCOL,
    LEGACY_PROTOCOL,
    PROTOCOLS,
    RuntimePolicy,
    docker_cli_env,
    docker_command,
    force_remove_container,
    from_args as runtime_policy_from_args,
    preflight as runtime_preflight,
)
from release_assets import (  # noqa: E402
    ReleaseError,
    classify_official_scenario_assets,
    copy_public_runtime_scenario_assets,
    load_validated_asset_policy,
)

_RUNTIME_POLICY = RuntimePolicy()
_ACTIVE_POOL_ROOT = POOL_ROOT
_HARDENED_ASSET_POLICY: dict[str, Any] | None = None
_HARDENED_IO_CONTRACT: dict[str, Any] | None = None
_RUNTIME_IMAGE_IDENTITY: dict[str, str] | None = None

HARDENED_IO_SCHEMA_VERSION = "taskgenome.hardened-io-contract.v1"
HARDENED_CONTROL_FILENAMES = {
    "conftest.py",
    "pytest.ini",
    "pyproject.toml",
    "tox.ini",
    "setup.cfg",
    "setup.py",
    "sitecustomize.py",
    "usercustomize.py",
    "test_script.py",
}
HARDENED_CONTROL_DIRNAMES = {
    "test",
    "tests",
    "oracle",
    "expected",
    "gold",
    "_gold",
    "groundtruth",
    "_fixtures",
}
HARDENED_EXECUTABLE_OUTPUT_SUFFIXES = {
    ".py",
    ".pyc",
    ".pyo",
    ".pth",
    ".so",
    ".dll",
    ".dylib",
    ".pkl",
    ".pickle",
    ".joblib",
    ".dill",
    ".pt",
    ".ckpt",
    ".npy",
    ".npz",
    ".sh",
    ".bat",
    ".cmd",
    ".exe",
}


CONDITIONS = (
    "no_context",
    "with_public_agent_skill",
    "with_agent_skill",
    "with_legacy_skill",
    "with_skill",
    "with_sanitized_skill",
    "with_gene",
    "with_gene_gemini",
    "with_gene_opus",
)
CONDITION_ALIASES = {
    "agent_skill": "with_agent_skill",
    "public_agent_skill": "with_public_agent_skill",
    "legacy_skill": "with_legacy_skill",
    "skill": "with_skill",
    "gene": "with_gene",
    "gene_gemini": "with_gene_gemini",
    "gene_opus": "with_gene_opus",
}
GENE_CONDITIONS = frozenset({"with_gene", "with_gene_gemini", "with_gene_opus"})
AGENT_SKILL_CONDITIONS = frozenset({"with_agent_skill", "with_public_agent_skill"})
DEFAULT_MODELS = "gemini_flash,gemini_pro"
DEFAULT_CONDITIONS = "no_context,with_legacy_skill"
DEFAULT_SKIP_IDS: tuple[str, ...] = ()
SCORE_PASS_THRESHOLD = float(os.environ.get("GENE_BENCH_SCORE_THRESHOLD", "1.0"))
TAIL_CHARS = 1800

CODE_INSTRUCTION = (
    "\n\nWrite a complete, self-contained Python solution. "
    "Output ONLY the code inside a single ```python code block. "
    "Do not include explanations outside the code block."
)

TEXT_INSTRUCTION = (
    "\n\nReturn exactly the requested `ANSWER:` and `ANALYSIS:` lines. "
    "Do not wrap the answer in a code block and do not add extra prose."
)

SANDBOX_NOTE = (
    "\n\n# Runtime environment\n"
    "Your solution runs in an isolated working directory containing the task's "
    "input data, helper modules, packages, and test assets. If the task gives "
    "a CLI contract, obey the provided paths exactly. Do not use absolute paths "
    "from the source checkout."
)

SKIP_COPY_NAMES = {
    "task.md",
    "SKILL.md",
    "scenario.yaml",
    "metadata.json",
    "reference_solution.py",
    "reference_solution.sh",
    "generated.py",
    "solution.py",
    "model_output.txt",
    "__pycache__",
    ".pytest_cache",
    "_bad_solutions",
    "_calibration",
}

DROP_RUNTIME_NAMES = {
    "output",
    "outputs",
    "results",
    "answer.json",
    "answer.txt",
    "report.json",
    "metrics.json",
    "control_log.json",
    "controller_params.json",
    "tuned_gains.json",
    "estimated_params.json",
    "calibration_log.json",
}

HARDENED_SKIP_COPY_NAMES = {
    "SKILL_oracle.md",
    "_traces",
    "_evolve_log.jsonl",
}

HARDENED_JUDGE_DIR_NAMES = {"_fixtures", "_gold", "groundtruth"}
HARDENED_JUDGE_FILE_NAMES = {"test_script.py", "verification_params.json"}


@dataclass(frozen=True)
class Trial:
    task_id: str
    legacy_task_id: str
    source: str
    family: str
    execution_mode: str
    model: str
    condition: str

    @property
    def trial_key(self) -> str:
        return f"{self.model}::{self.condition}::{self.task_id}"


def _load_manifest(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
        raise ValueError(f"manifest must be a dict with tasks[]: {path}")
    return payload, [x for x in payload["tasks"] if isinstance(x, dict)]


def _safe_hardened_output_path(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(
            re.fullmatch(r"[A-Za-z0-9._-]+", part) is None
            for part in path.parts
        )
    ):
        raise ValueError(f"unsafe {label}: {value!r}")
    lowered = tuple(part.lower() for part in path.parts)
    name = lowered[-1]
    if (
        name in HARDENED_CONTROL_FILENAMES
        or any(part in HARDENED_CONTROL_DIRNAMES for part in lowered[:-1])
        or PurePosixPath(name).suffix.lower() in HARDENED_EXECUTABLE_OUTPUT_SUFFIXES
    ):
        raise ValueError(f"executable or judge-control {label} is forbidden: {value!r}")
    return path.as_posix()


def _load_hardened_io_contract(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"missing or unsafe hardened I/O contract: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid hardened I/O contract {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("hardened I/O contract must be an object")
    expected_root_keys = {
        "schema_version",
        "expected_manifest_sha256",
        "execution_mode",
        "limits",
        "tasks",
    }
    if set(payload) != expected_root_keys:
        raise ValueError(
            "hardened I/O contract has invalid root keys: "
            f"missing={sorted(expected_root_keys - set(payload))} "
            f"unknown={sorted(set(payload) - expected_root_keys)}"
        )
    if payload["schema_version"] != HARDENED_IO_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported hardened I/O schema: {payload['schema_version']!r}"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", str(payload["expected_manifest_sha256"])):
        raise ValueError("hardened I/O contract manifest digest must be SHA-256")
    if payload["execution_mode"] != "subprocess_ref_runner":
        raise ValueError("hardened I/O contract execution_mode must be subprocess_ref_runner")

    limits = payload["limits"]
    expected_limit_keys = {"max_files", "max_file_bytes", "max_total_bytes"}
    if not isinstance(limits, dict) or set(limits) != expected_limit_keys:
        raise ValueError("hardened I/O contract limits have invalid keys")
    for key in expected_limit_keys:
        if type(limits[key]) is not int or limits[key] <= 0:
            raise ValueError(f"hardened I/O contract limits.{key} must be positive")
    if limits["max_file_bytes"] > limits["max_total_bytes"]:
        raise ValueError("hardened I/O max_file_bytes exceeds max_total_bytes")

    tasks = payload["tasks"]
    if not isinstance(tasks, dict) or not tasks:
        raise ValueError("hardened I/O contract tasks must be a non-empty object")
    for task_id, spec in tasks.items():
        if not re.fullmatch(r"T\d{4}", str(task_id)):
            raise ValueError(f"invalid hardened I/O task id: {task_id!r}")
        if not isinstance(spec, dict) or set(spec) != {"supported", "reason", "outputs"}:
            raise ValueError(f"invalid hardened I/O task contract: {task_id}")
        if type(spec["supported"]) is not bool:
            raise ValueError(f"hardened I/O {task_id}.supported must be boolean")
        if not isinstance(spec["reason"], str):
            raise ValueError(f"hardened I/O {task_id}.reason must be a string")
        outputs = spec["outputs"]
        if not isinstance(outputs, list):
            raise ValueError(f"hardened I/O {task_id}.outputs must be an array")
        if len(outputs) > limits["max_files"]:
            raise ValueError(
                f"hardened I/O {task_id}.outputs exceeds limits.max_files"
            )
        if spec["supported"] and not outputs:
            raise ValueError(f"supported hardened I/O task has no outputs: {task_id}")
        if not spec["supported"] and (outputs or not spec["reason"].strip()):
            raise ValueError(
                f"unsupported hardened I/O task needs a reason and no outputs: {task_id}"
            )
        generation_paths: set[str] = set()
        judge_paths: set[str] = set()
        for index, output in enumerate(outputs):
            if not isinstance(output, dict) or set(output) != {
                "generation_path",
                "judge_path",
                "required",
            }:
                raise ValueError(f"invalid hardened output {task_id}[{index}]")
            generation_path = _safe_hardened_output_path(
                output["generation_path"], f"{task_id} generation path"
            )
            judge_path = _safe_hardened_output_path(
                output["judge_path"], f"{task_id} judge path"
            )
            if type(output["required"]) is not bool:
                raise ValueError(f"{task_id} output required must be boolean")
            if generation_path in generation_paths or judge_path in judge_paths:
                raise ValueError(f"duplicate hardened I/O output path for {task_id}")
            generation_paths.add(generation_path)
            judge_paths.add(judge_path)
            output["generation_path"] = generation_path
            output["judge_path"] = judge_path
    return payload


def _canonical_hardened_boundary_paths(
    asset_policy_value: str,
    io_contract_value: str,
) -> tuple[Path, Path]:
    """Bind the formal hardened-v2 name to the reviewed repository boundary."""

    asset_policy_path = Path(asset_policy_value).absolute()
    io_contract_path = Path(io_contract_value).absolute()
    expected = (
        (asset_policy_path, DEFAULT_ASSET_POLICY, CANONICAL_ASSET_POLICY_SHA256),
        (
            io_contract_path,
            DEFAULT_HARDENED_IO_CONTRACT,
            CANONICAL_HARDENED_IO_CONTRACT_SHA256,
        ),
    )
    for selected, canonical, expected_sha256 in expected:
        if selected.is_symlink() or not selected.is_file():
            raise ValueError(f"missing or unsafe canonical hardened-v2 boundary: {selected}")
        if selected.resolve() != canonical.resolve():
            raise ValueError(
                "formal hardened-v2 only accepts the checked-in canonical asset "
                "policy and I/O contract; custom boundaries require a separately "
                "named development protocol"
            )
        actual_sha256 = sha256_file(selected)
        if actual_sha256 != expected_sha256:
            raise ValueError(
                f"canonical hardened-v2 boundary digest mismatch for {selected.name}: "
                f"expected={expected_sha256} actual={actual_sha256}"
            )
    return asset_policy_path, io_contract_path


def _validate_hardened_selection(
    rows: list[dict[str, Any]],
    manifest_sha256: str,
    *,
    manifest_rows: list[dict[str, Any]],
) -> None:
    if _HARDENED_IO_CONTRACT is None or _HARDENED_ASSET_POLICY is None:
        raise ValueError("hardened policy and I/O contract are not loaded")
    expected_manifest = _HARDENED_IO_CONTRACT["expected_manifest_sha256"]
    if expected_manifest != manifest_sha256:
        raise ValueError(
            "hardened I/O contract does not match the selected manifest: "
            f"expected={expected_manifest} actual={manifest_sha256}"
        )
    unsupported: list[str] = []
    contracts = _HARDENED_IO_CONTRACT["tasks"]
    expected_contract_ids = {
        str(row.get("task_id", ""))
        for row in manifest_rows
        if str(row.get("execution_mode", ""))
        == _HARDENED_IO_CONTRACT["execution_mode"]
    }
    if set(contracts) != expected_contract_ids:
        raise ValueError(
            "hardened I/O contract task coverage differs from the manifest: "
            f"missing={sorted(expected_contract_ids - set(contracts))} "
            f"extra={sorted(set(contracts) - expected_contract_ids)}"
        )
    for row in rows:
        if _is_text_task(row):
            continue
        task_id = str(row.get("task_id", ""))
        mode = str(row.get("execution_mode", ""))
        if mode != _HARDENED_IO_CONTRACT["execution_mode"]:
            unsupported.append(f"{task_id}({mode}: no isolated I/O contract)")
            continue
        spec = contracts.get(task_id)
        if not isinstance(spec, dict):
            unsupported.append(f"{task_id}({mode}: missing contract)")
        elif not spec.get("supported"):
            unsupported.append(f"{task_id}({spec.get('reason') or 'unsupported'})")
        else:
            classify_official_scenario_assets(
                _task_dir(row, _ACTIVE_POOL_ROOT),
                _ACTIVE_POOL_ROOT,
                row,
                _HARDENED_ASSET_POLICY,
            )
    if unsupported:
        preview = ", ".join(unsupported[:20])
        tail = "" if len(unsupported) <= 20 else f", ... (+{len(unsupported) - 20} more)"
        raise ValueError(
            "hardened-v2 refuses code tasks without a reviewed process-separated "
            f"I/O contract: {preview}{tail}. Use legacy-v1 only for trusted historical "
            "reproduction; do not relabel those results as hardened-v2."
        )


def _csv_arg(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def _condition_arg(value: str) -> list[str]:
    return [CONDITION_ALIASES.get(x, x) for x in _csv_arg(value)]


def _row_rel_dir(row: dict[str, Any]) -> str:
    rel = row.get("rel_dir")
    if isinstance(rel, str) and rel.strip():
        return rel.strip()
    return str(row.get("task_id", "")).strip()


def _task_dir(row: dict[str, Any], pool_root: Path) -> Path:
    return pool_root / _row_rel_dir(row)


def _load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip()


def _wrap_skill_prompt(content: str) -> str:
    if not content:
        return ""
    return (
        "You are given the following skill package for this task. "
        "Use it to guide your answer.\n\n"
        f"<skill-package>\n{content}\n</skill-package>"
    )


def _wrap_agent_skill_prompt(content: str) -> str:
    if not content:
        return ""
    return (
        "You are given the following static public Agent Skill for this task. "
        "It was authored from public task information only. Use it as a "
        "developer-style manual, but derive task-specific facts from the prompt.\n\n"
        f"<agent-skill-package>\n{content}\n</agent-skill-package>"
    )


_ANSWER_ONE_OF_RE = re.compile(r"ANSWER:\s*<one of:\s*([^>\n]+)>", re.IGNORECASE)
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_QUOTED_RE = re.compile(r"""['"]([^'"]{2,80})['"]""")
_FLAG_RE = re.compile(r"--[a-z][a-z0-9_-]*[a-z0-9]")
_SECTION_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")


def _is_trivial_number(tok: str) -> bool:
    try:
        return ("." not in tok) and (abs(int(tok)) < 10)
    except ValueError:
        return False


def _hard_literals(text: str) -> set[str]:
    out: set[str] = set()
    for n in _NUM_RE.findall(text or ""):
        if not _is_trivial_number(n):
            out.add(n)
    for q in _QUOTED_RE.findall(text or ""):
        q = q.strip()
        if len(re.findall(r"[A-Za-z0-9]", q)) >= 3:
            out.add(q)
    out.update(_FLAG_RE.findall((text or "").lower()))
    return out


def _public_answer_options(task_md: str) -> set[str]:
    opts: set[str] = set()
    for match in _ANSWER_ONE_OF_RE.finditer(task_md or ""):
        for item in re.split(r"[|/,]", match.group(1)):
            item = item.strip().strip("`'\" ")
            if item:
                opts.add(item)
    for line in (task_md or "").splitlines():
        if "which of the following" in line.lower():
            for item in re.findall(r"`([^`]+)`", line):
                item = item.strip()
                if item:
                    opts.add(item)
    return opts


def _read_optional_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _sanitize_skill_text(skill_md: str, task_md: str, task_dir: Path, row: dict[str, Any]) -> str:
    """Create a fairer skill baseline by mechanically removing private spec.

    The original SKILL.md files are useful as an oracle/full-spec upper bound,
    but many contain hidden rulebooks, thresholds, action matrices, and trap
    conventions. This sanitizer keeps high-level method/pitfall guidance while
    redacting answer labels and hidden-only literals. Public code contracts from
    task.md are preserved so code-generation skills can still mention allowed
    file names, CLI flags, columns, and output keys.
    """
    if str(row.get("family") or "") == "rule_following":
        return (
            "SANITIZED RULE-FOLLOWING SKILL BASELINE: the original skill's full "
            "private rulebook, hidden thresholds, action matrix, and override "
            "rules have been removed. Use only the public scenario and answer "
            "options from the prompt.\n\n"
            "## Method\n"
            "1. Extract every public fact from the scenario before choosing an answer.\n"
            "2. Avoid real-world assumptions; treat the task as a synthetic rule system.\n"
            "3. If the public prompt names modifiers, boundary words, or precedence cues, apply them in the stated order.\n"
            "4. Check equality and strict/inclusive boundary language carefully when it appears in the public prompt.\n"
            "5. Choose exactly one public answer option and format the ANSWER and ANALYSIS lines exactly as requested.\n\n"
            "## Common Pitfalls\n"
            "- Do not infer private thresholds or mappings from domain knowledge.\n"
            "- Do not choose an answer because its label sounds plausible; justify it from public facts.\n"
            "- Re-check whether any public cue indicates an override or precedence relationship."
        )

    files = row.get("files") if isinstance(row.get("files"), dict) else {}
    public_hard = _hard_literals(task_md)
    hidden_blob = "\n".join(
        _read_optional_text(task_dir / str(files.get(role) or default))
        for role, default in (
            ("scenario", "scenario.yaml"),
            ("ref", "reference_solution.py"),
            ("oracle", "test_script.py"),
        )
    )
    private_hard = _hard_literals(hidden_blob) - public_hard
    answer_options = _public_answer_options(task_md)

    def redact_token(text: str, token: str, replacement: str) -> str:
        if re.search(r"[A-Za-z_]", token):
            return re.sub(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])", replacement, text, flags=re.IGNORECASE)
        return re.sub(rf"(?<![0-9.]){re.escape(token)}(?![0-9.])", replacement, text)

    out: list[str] = []
    current_section = ""
    for raw_line in (skill_md or "").splitlines():
        line = raw_line.rstrip()
        section_match = _SECTION_RE.match(line)
        if section_match:
            current_section = section_match.group(1).strip().lower()
            out.append(line)
            continue

        low = line.lower()
        if "hidden convention" in low or "hidden behavior" in low:
            out.append("[SANITIZED: hidden convention removed]")
            continue

        if re.search(r"\*\*action\*\*\s*:", line, flags=re.IGNORECASE):
            out.append("  - **Action**: [SANITIZED: concrete action mapping removed; infer from public task only]")
            continue

        if current_section.startswith("decision procedure") and (
            "emit " in low or "output " in low or "return " in low
        ):
            out.append("[SANITIZED: final-output mapping removed]")
            continue

        redacted = line
        for opt in sorted(answer_options, key=len, reverse=True):
            redacted = redact_token(redacted, opt, "[ANSWER_OPTION]")
        for lit in sorted(private_hard, key=len, reverse=True):
            redacted = redact_token(redacted, lit, "[PRIVATE_LITERAL]")
        out.append(redacted)

    header = (
        "SANITIZED SKILL BASELINE: concrete answer labels, hidden-only literals, "
        "and explicit action mappings have been mechanically redacted. Use this "
        "only as general procedural guidance; infer task-specific facts from the "
        "public prompt.\n\n"
    )
    return header + "\n".join(out).strip()


def _wrap_gene_prompt(asset: dict[str, Any]) -> str:
    payload = asset.get("payload") if isinstance(asset.get("payload"), dict) else {}
    summary = str(payload.get("summary") or "").strip()
    signals = payload.get("signals_match") if isinstance(payload.get("signals_match"), list) else []
    strategy = payload.get("strategy") if isinstance(payload.get("strategy"), list) else []
    preconditions = payload.get("preconditions") if isinstance(payload.get("preconditions"), list) else []

    def to_bullets(items: list[Any]) -> str:
        lines: list[str] = []
        for item in items:
            text = str(item).strip()
            if text:
                lines.append(f"- {text}")
        return "\n".join(lines) if lines else "- (none)"

    return (
        "You are given an experiential Gene asset distilled from no-context attempts "
        "and verified failures for this task family. Use it as procedural guidance.\n\n"
        "<gene-asset>\n"
        f"summary:\n{summary or '(none)'}\n\n"
        f"signals_match:\n{to_bullets(signals)}\n\n"
        f"strategy:\n{to_bullets(strategy)}\n\n"
        f"preconditions:\n{to_bullets(preconditions)}\n"
        "</gene-asset>"
    )


def _resolve_gene_path(row: dict[str, Any], task_dir: Path, genes_dir: Path) -> Path | None:
    files = row.get("files") if isinstance(row.get("files"), dict) else {}
    candidates: list[Path] = []

    gene_rel = files.get("gene")
    if isinstance(gene_rel, str) and gene_rel.strip():
        rel = gene_rel.strip()
        p = Path(rel)
        if p.is_absolute():
            candidates.append(p)
        else:
            candidates.append(task_dir / rel)
            candidates.append(genes_dir / rel)
            candidates.append(genes_dir / p.name)

    task_id = str(row.get("task_id") or "").strip()
    if task_id:
        candidates.append(genes_dir / f"{task_id}.json")

    for c in candidates:
        if c.exists():
            return c
    return None


def _resolve_agent_skill_path(row: dict[str, Any], task_dir: Path, agent_skills_dir: Path) -> Path | None:
    files = row.get("files") if isinstance(row.get("files"), dict) else {}
    candidates: list[Path] = []

    rel_value = files.get("agent_skill")
    if isinstance(rel_value, str) and rel_value.strip():
        rel = rel_value.strip()
        path = Path(rel)
        if path.is_absolute():
            candidates.append(path)
        else:
            candidates.append(task_dir / rel)
            candidates.append(agent_skills_dir / rel)
            candidates.append(agent_skills_dir / path.name)

    task_id = str(row.get("task_id") or "").strip()
    if task_id:
        candidates.append(task_dir / "AGENT_SKILL.md")
        candidates.append(agent_skills_dir / f"{task_id}.md")

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _condition_gene_dir(condition: str, args: argparse.Namespace) -> Path:
    if condition == "with_gene_gemini":
        return Path(args.gene_gemini_dir).resolve()
    if condition == "with_gene_opus":
        return Path(args.gene_opus_dir).resolve()
    return Path(args.genes_dir).resolve()


def _build_system_prompt(row: dict[str, Any], task_dir: Path, condition: str, genes_dir: Path) -> str:
    if condition == "no_context":
        return ""
    if condition in {"with_agent_skill", "with_public_agent_skill"}:
        agent_skill_path = _resolve_agent_skill_path(row, task_dir, _AGENT_SKILLS_DIR)
        if agent_skill_path is None:
            raise FileNotFoundError(f"agent skill missing for task_id={row.get('task_id')}")
        return _wrap_agent_skill_prompt(_load_text(agent_skill_path))
    if condition in {"with_skill", "with_legacy_skill"}:
        skill_rel = ((row.get("files") or {}).get("skill")) or "SKILL.md"
        skill_path = task_dir / skill_rel
        if not skill_path.exists():
            return ""
        return _wrap_skill_prompt(_load_text(skill_path))
    if condition == "with_sanitized_skill":
        skill_rel = ((row.get("files") or {}).get("skill")) or "SKILL.md"
        task_rel = ((row.get("files") or {}).get("task")) or "task.md"
        skill_path = task_dir / skill_rel
        task_path = task_dir / task_rel
        if not skill_path.exists() or not task_path.exists():
            return ""
        sanitized = _sanitize_skill_text(_load_text(skill_path), _load_text(task_path), task_dir, row)
        return _wrap_skill_prompt(sanitized)
    if condition in GENE_CONDITIONS:
        gene_path = _resolve_gene_path(row, task_dir, genes_dir)
        if gene_path is None:
            raise FileNotFoundError(
                f"gene asset missing for task_id={row.get('task_id')} under genes_dir={genes_dir}"
            )
        try:
            asset = json.loads(gene_path.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid gene JSON for task_id={row.get('task_id')}: {gene_path}") from exc
        if not isinstance(asset, dict):
            raise ValueError(f"gene asset must be object: {gene_path}")
        return _wrap_gene_prompt(asset)
    raise ValueError(f"unsupported condition: {condition}")


def _is_text_task(row: dict[str, Any]) -> bool:
    return str(row.get("execution_mode")) == "text_short_answer"


def _build_user_prompt(row: dict[str, Any], task_dir: Path) -> str:
    task_rel = ((row.get("files") or {}).get("task")) or "task.md"
    task_path = task_dir / task_rel
    body = _load_text(task_path)
    if _is_text_task(row):
        return body + TEXT_INSTRUCTION
    return body + SANDBOX_NOTE + CODE_INSTRUCTION


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
        "sub2api_key": args.sub2api_key or first("SUB2API_API_KEY", "SUB2API_KEY"),
        "bedrock_key": args.bedrock_key or first("AWS_BEARER_TOKEN_BEDROCK", "BEDROCK_KEY", "BEDROCK_API_KEY"),
        "local_base_url": args.local_base_url or os.environ.get("LOCAL_BASE_URL", "http://localhost:8000/v1"),
        # The optional OpenAI-compatible channel has no baked-in endpoint.
        # Keeping the value empty until the caller configures it preserves the
        # exact run protocol while preventing an authoring-network default from
        # entering public code or new run configs.
        "sub2api_base_url": args.sub2api_base_url or os.environ.get("SUB2API_BASE_URL", ""),
    }


def _finite_nonnegative(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite non-negative number") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label} must be a finite non-negative number")
    return number


def _token_count(api_result: dict[str, Any], field: str) -> int:
    raw = api_result.get(field) or 0
    if isinstance(raw, bool):
        raise ValueError(f"{field} must be a non-negative integer")
    try:
        numeric = float(raw)
        value = int(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a non-negative integer") from exc
    if not math.isfinite(numeric) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _compute_cost(model_key: str, api_result: dict[str, Any]) -> float:
    model_id = MODEL_REGISTRY[model_key][0]
    inp, outp = PRICE_TABLE.get(model_id, (0.0, 0.0))
    input_price = _finite_nonnegative(inp, f"input price for {model_id}")
    output_price = _finite_nonnegative(outp, f"output price for {model_id}")
    in_tok = _token_count(api_result, "input_tokens")
    out_tok = _token_count(api_result, "output_tokens")
    return _finite_nonnegative(
        round(
            in_tok * input_price / 1_000_000
            + out_tok * output_price / 1_000_000,
            6,
        ),
        f"computed cost for {model_id}",
    )


def _load_budget_spent(path: Path) -> float:
    if not path.exists():
        return 0.0
    total = 0.0
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8", errors="strict").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"invalid budget.jsonl line {line_number}: {exc}"
            ) from exc
        if not isinstance(row, dict) or "cost_usd" not in row:
            raise SystemExit(
                f"invalid budget.jsonl line {line_number}: object with cost_usd required"
            )
        try:
            cost = _finite_nonnegative(
                row["cost_usd"], f"budget line {line_number} cost_usd"
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        total = round(total + cost, 6)
        if "cumulative_cost_usd" in row:
            try:
                cumulative = _finite_nonnegative(
                    row["cumulative_cost_usd"],
                    f"budget line {line_number} cumulative_cost_usd",
                )
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
            if not math.isclose(cumulative, total, rel_tol=0.0, abs_tol=5e-7):
                raise SystemExit(
                    f"budget line {line_number} cumulative_cost_usd does not match ledger"
                )
    return total


class BudgetTracker:
    def __init__(self, starting_spend: float, limit_usd: float, alert_step_usd: float) -> None:
        self._lock = threading.Lock()
        self.spent_usd = _finite_nonnegative(starting_spend, "starting budget spend")
        self.limit_usd = _finite_nonnegative(limit_usd, "budget limit")
        self.alert_step_usd = _finite_nonnegative(
            alert_step_usd, "budget alert step"
        )

    @property
    def enabled(self) -> bool:
        return self.limit_usd > 0

    def should_stop(self) -> bool:
        with self._lock:
            return self.enabled and self.spent_usd >= self.limit_usd

    def add(self, cost_usd: float, trial_key: str) -> float:
        cost = _finite_nonnegative(cost_usd, f"cost for {trial_key}")
        with self._lock:
            before = self.spent_usd
            self.spent_usd = _finite_nonnegative(
                round(self.spent_usd + cost, 6),
                "cumulative budget spend",
            )
            after = self.spent_usd
            limit = self.limit_usd
            step = self.alert_step_usd

        if step > 0:
            old_bucket = int(before // step)
            new_bucket = int(after // step)
            for bucket in range(old_bucket + 1, new_bucket + 1):
                threshold = bucket * step
                print(f"BUDGET ALERT: cumulative API spend crossed ${threshold:.2f} (now ${after:.2f})")

        if self.enabled and before < limit <= after:
            print(
                f"BUDGET CIRCUIT BREAKER: cumulative API spend reached ${after:.2f} "
                f"after {trial_key}; no new API calls will be started."
            )
        return after


def _is_hardened_authoring_path(relpath: Path) -> bool:
    lowered = tuple(part.lower() for part in relpath.parts)
    name = lowered[-1] if lowered else ""
    return (
        any(part in {"_traces", "__pycache__", ".pytest_cache"} for part in lowered)
        or name in {"skill_oracle.md", "_evolve_log.jsonl"}
    )


def _is_hardened_judge_path(relpath: Path, protected_relpaths: set[str]) -> bool:
    normalized = relpath.as_posix()
    lowered = tuple(part.lower() for part in relpath.parts)
    name = lowered[-1] if lowered else ""
    return (
        normalized in protected_relpaths
        or any(part in HARDENED_JUDGE_DIR_NAMES for part in lowered)
        or name in HARDENED_JUDGE_FILE_NAMES
        or "oracle" in name
        or name.startswith("expected")
    )


def _copy_scenario_assets(
    task_dir: Path,
    dst: Path,
    *,
    include_judge_assets: bool = True,
    protected_relpaths: tuple[str, ...] = (),
) -> None:
    if _RUNTIME_POLICY.is_legacy:
        # Frozen legacy-v1 copy behavior.
        skip_names = set(SKIP_COPY_NAMES)
        for child in task_dir.iterdir():
            if child.name in skip_names or child.name in DROP_RUNTIME_NAMES:
                continue
            target = dst / child.name
            try:
                if child.is_dir():
                    shutil.copytree(child, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(child, target)
            except Exception:
                pass

        data_root = dst / "data"
        if data_root.is_dir():
            for child in data_root.rglob("*"):
                if child.is_dir():
                    continue
                rel = child.relative_to(data_root)
                target = dst / rel
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(child, target)
                except Exception:
                    pass
        return

    skip_names = set(SKIP_COPY_NAMES) | set(HARDENED_SKIP_COPY_NAMES)
    protected = {Path(value).as_posix() for value in protected_relpaths}
    sources = sorted(
        task_dir.rglob("*"),
        key=lambda path: path.relative_to(task_dir).as_posix(),
    )
    for source in sources:
        relpath = source.relative_to(task_dir)
        if relpath.parts[0] in skip_names or relpath.parts[0] in DROP_RUNTIME_NAMES:
            continue
        if _is_hardened_authoring_path(relpath):
            continue
        if not include_judge_assets and _is_hardened_judge_path(relpath, protected):
            continue
        if source.is_symlink():
            raise RuntimeError(f"hardened-v2 refuses symlinked scenario asset: {relpath}")
        target = dst / relpath
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        else:
            raise RuntimeError(f"hardened-v2 refuses unsupported scenario asset: {relpath}")

    data_root = dst / "data"
    if data_root.is_dir():
        children = sorted(
            data_root.rglob("*"),
            key=lambda path: path.relative_to(data_root).as_posix(),
        )
        for child in children:
            if child.is_dir():
                continue
            rel = child.relative_to(data_root)
            target = dst / rel
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, target)


def _is_hardened_snapshot_noise(relpath: Path) -> bool:
    return (
        relpath.as_posix() in {"generated.py", "solution.py"}
        or any(part in {"__pycache__", ".pytest_cache"} for part in relpath.parts)
        or relpath.suffix.lower() in {".pyc", ".pyo"}
    )


def _snapshot_hardened_workspace(root: Path) -> dict[str, tuple[str, int]]:
    """Hash regular files and reject unsafe nodes in a candidate workspace."""

    snapshot: dict[str, tuple[str, int]] = {}
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        directory_names.sort()
        file_names.sort()
        kept_directories: list[str] = []
        for name in directory_names:
            path = current_path / name
            relpath = path.relative_to(root)
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise RuntimeError(
                    f"hardened-v2 refuses non-directory candidate node: {relpath}"
                )
            if _is_hardened_snapshot_noise(relpath):
                continue
            kept_directories.append(name)
        directory_names[:] = kept_directories
        for name in file_names:
            path = current_path / name
            relpath = path.relative_to(root)
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError(
                    f"hardened-v2 refuses non-regular candidate file: {relpath}"
                )
            if _is_hardened_snapshot_noise(relpath):
                continue
            snapshot[relpath.as_posix()] = (sha256_file(path), info.st_size)
    return snapshot


def _prepare_hardened_output_files(
    generation_dir: Path,
    task_contract: dict[str, Any],
    limits: dict[str, int],
) -> tuple[list[Path], int]:
    """Precreate only reviewed output files and derive a hard aggregate cap."""

    outputs = task_contract["outputs"]
    if not outputs or len(outputs) > limits["max_files"]:
        raise RuntimeError("invalid hardened-v2 output count")
    per_file_cap = min(
        limits["max_file_bytes"],
        limits["max_total_bytes"] // len(outputs),
    )
    if per_file_cap <= 0:
        raise RuntimeError("hardened-v2 output limits cannot bound declared outputs")

    writable: list[Path] = []
    for item in outputs:
        relative = PurePosixPath(item["generation_path"])
        target = generation_dir.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise RuntimeError(f"unsafe hardened-v2 output target: {relative.as_posix()}")
        target.touch(mode=0o600, exist_ok=True)
        info = target.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError(
                f"hardened-v2 output target is not a unique regular file: "
                f"{relative.as_posix()}"
            )
        os.chmod(target, 0o600)
        writable.append(target)
    return writable, per_file_cap


def _copy_hardened_candidate_outputs(
    generation_dir: Path,
    judge_dir: Path,
    before: dict[str, tuple[str, int]],
    task_contract: dict[str, Any],
    limits: dict[str, int],
) -> list[str]:
    """Transfer only exact, bounded candidate artifacts into the judge tree."""

    after = _snapshot_hardened_workspace(generation_dir)
    changed = {
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    }
    outputs = task_contract["outputs"]
    by_generation = {item["generation_path"]: item for item in outputs}
    # Unlisted intermediates remain inside the generation workspace and are
    # discarded with it. Only exact contract paths can cross into the judge.
    missing = sorted(
        item["generation_path"]
        for item in outputs
        if item["required"]
        and (
            item["generation_path"] not in changed
            or item["generation_path"] not in after
        )
    )
    if missing:
        raise RuntimeError(
            f"hardened-v2 candidate did not produce required outputs: {', '.join(missing)}"
        )

    produced = sorted(
        path for path in changed if path in after and path in by_generation
    )
    if len(produced) > limits["max_files"]:
        raise RuntimeError("hardened-v2 candidate output file limit exceeded")
    total_bytes = 0
    for path in produced:
        size = after[path][1]
        if size > limits["max_file_bytes"]:
            raise RuntimeError(
                f"hardened-v2 candidate output exceeds per-file limit: {path}"
            )
        total_bytes += size
    if total_bytes > limits["max_total_bytes"]:
        raise RuntimeError("hardened-v2 candidate total output limit exceeded")

    # Remove only the explicitly reviewed output targets from the freshly
    # copied canonical judge. This prevents stale pre-generated answers from
    # masking the candidate's artifacts.
    for item in outputs:
        target = judge_dir / item["judge_path"]
        if target.is_symlink():
            raise RuntimeError(f"unsafe symlinked judge output target: {target}")
        if target.exists():
            if not target.is_file():
                raise RuntimeError(f"judge output target is not a file: {target}")
            target.unlink()

    transferred: list[str] = []
    for generation_path in produced:
        item = by_generation[generation_path]
        source = generation_dir / generation_path
        target = judge_dir / item["judge_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        os.chmod(target, 0o644)
        expected_sha256, expected_size = after[generation_path]
        if (
            target.is_symlink()
            or not target.is_file()
            or target.stat().st_size != expected_size
            or sha256_file(target) != expected_sha256
        ):
            raise RuntimeError(
                f"candidate output changed during judge transfer: {generation_path}"
            )
        transferred.append(item["judge_path"])
    return transferred


def _write_code_candidates(directory: Path, code: str) -> None:
    for name in ("generated.py", "solution.py"):
        (directory / name).write_text(code, encoding="utf-8")


def _ensure_local_test_script(test_script: Path, directory: Path) -> None:
    if not (directory / "test_script.py").exists():
        shutil.copy2(test_script, directory / "test_script.py")


def _run_subprocess_ref_judge(
    test_script: Path,
    directory: Path,
    timeout: int,
) -> dict[str, Any]:
    if _looks_pytest(test_script):
        rc, stdout, stderr, err = _run(
            [
                sys.executable,
                "-m",
                "pytest",
                "test_script.py",
                "-q",
                "--tb=short",
                "--no-header",
                "--noconftest",
                "-p",
                "no:cacheprovider",
            ],
            cwd=directory,
            timeout=timeout,
        )
        return _result_from_pytest(rc, stdout, stderr, err)
    rc, stdout, stderr, err = _run(
        [sys.executable, "test_script.py"], cwd=directory, timeout=timeout
    )
    return _result_from_subprocess(
        rc, stdout, stderr, err, "subprocess_ref_runner"
    )


def _evaluate_hardened_subprocess_ref_runner(
    code: str,
    row: dict[str, Any],
    task_dir: Path,
    test_script: Path,
    root: Path,
    gen_timeout: int,
    test_timeout: int,
) -> dict[str, Any]:
    if _HARDENED_ASSET_POLICY is None or _HARDENED_IO_CONTRACT is None:
        raise RuntimeError("hardened-v2 policy and I/O contract are not loaded")
    task_id = str(row.get("task_id", ""))
    task_contract = _HARDENED_IO_CONTRACT["tasks"].get(task_id)
    if not isinstance(task_contract, dict) or not task_contract.get("supported"):
        raise RuntimeError(f"hardened-v2 task has no supported I/O contract: {task_id}")

    generation_dir = root / "generation"
    generation_dir.mkdir()
    try:
        copy_public_runtime_scenario_assets(
            task_dir,
            _ACTIVE_POOL_ROOT,
            row,
            _HARDENED_ASSET_POLICY,
            generation_dir,
        )
        _write_code_candidates(generation_dir, code)
        writable_files, per_file_cap = _prepare_hardened_output_files(
            generation_dir,
            task_contract,
            _HARDENED_IO_CONTRACT["limits"],
        )
        before = _snapshot_hardened_workspace(generation_dir)
    except (OSError, ReleaseError, RuntimeError) as exc:
        return {
            **_missing_test_result("subprocess_ref_runner"),
            "error_type": "hardened_policy_error",
            "reason": str(exc),
        }
    gen_rc, gen_stdout, gen_stderr, gen_err = _run(
        [sys.executable, "generated.py"],
        cwd=generation_dir,
        timeout=gen_timeout,
        writable_files=writable_files,
        max_file_bytes=per_file_cap,
    )
    if gen_err is not None or gen_rc != 0:
        result = _result_from_subprocess(
            gen_rc,
            gen_stdout,
            gen_stderr,
            gen_err,
            "subprocess_ref_runner",
        )
        result["error_type"] = (
            result["error_type"] if gen_err else _classify_stderr(gen_stderr)
        )
        return result

    # Build the judge only after candidate execution has ended. The generation
    # container mounted only generation_dir and cannot access this sibling.
    judge_dir = root / "judge"
    judge_dir.mkdir()
    _copy_scenario_assets(task_dir, judge_dir)
    _ensure_local_test_script(test_script, judge_dir)
    try:
        _copy_hardened_candidate_outputs(
            generation_dir,
            judge_dir,
            before,
            task_contract,
            _HARDENED_IO_CONTRACT["limits"],
        )
    except (OSError, RuntimeError) as exc:
        return {
            "mode": "subprocess_ref_runner",
            "passed": False,
            "n_pass": 0,
            "n_fail": 1,
            "n_total": 1,
            "pass_rate": 0.0,
            "agg_score": None,
            "scores": {},
            "error_type": "hardened_contract_violation",
            "reason": str(exc),
            "returncode": gen_rc,
            "stdout_tail": gen_stdout[-TAIL_CHARS:],
            "stderr_tail": gen_stderr[-TAIL_CHARS:],
        }
    return _run_subprocess_ref_judge(test_script, judge_dir, test_timeout)


def _tmp_root() -> str | None:
    if _RUNTIME_POLICY.is_hardened:
        raw = os.environ.get("TASKGENOME_TMPDIR", "system")
    else:
        # Scratch placement is not part of prompts, scoring, or provenance.
        # Use the host system temporary directory by default; an explicit
        # GENE_BENCH_V3_TMPDIR remains available for reproducible deployments.
        raw = os.environ.get("GENE_BENCH_V3_TMPDIR", "system")
    if not raw or raw.lower() == "system":
        return None
    try:
        os.makedirs(raw, exist_ok=True)
    except OSError:
        return None
    return raw


def _write_text_candidate(raw_response: str, tmpdir: Path) -> Path:
    candidate = tmpdir / "candidate_response.py"
    candidate.write_text(
        "import sys\n"
        f"sys.stdout.write({json.dumps(raw_response, ensure_ascii=False)})\n"
        "sys.stdout.write('\\n')\n",
        encoding="utf-8",
    )
    return candidate


_LEGACY_SAFE_ENV_NAMES = frozenset(
    {
        # The candidate needs a deterministic executable/search path and the
        # locale/terminal hints used by ordinary command-line tools.  Do not
        # broaden this set to "all non-secret" variables: provider SDKs and
        # authenticated proxies routinely use non-obvious variable names.
        "PATH",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "TERM",
        "TZ",
        "PYTHONNOUSERSITE",
        "PYTHONDONTWRITEBYTECODE",
        # Windows subprocesses need these variables to locate the system
        # runtime.  They are harmless on POSIX and are copied only if set.
        "SYSTEMROOT",
        "WINDIR",
        "SYSTEMDRIVE",
        "COMSPEC",
        "PATHEXT",
    }
)


def _subprocess_env(cwd: Path) -> dict[str, str]:
    """Build a credential-free environment for legacy host subprocesses.

    ``legacy-v1`` is intentionally still a host-execution protocol and is not
    a general sandbox.  It must nevertheless not hand provider credentials,
    proxy URLs, host ``PYTHONPATH``, or user configuration directories to
    candidate code.  An explicit allowlist is safer than trying to enumerate
    every secret-bearing environment-variable spelling.
    """

    parent_env = os.environ
    env = {
        name: parent_env[name]
        for name in _LEGACY_SAFE_ENV_NAMES
        if name in parent_env
    }
    python_bin = str(Path(sys.executable).resolve().parent)
    path_parts = [python_bin]
    if env.get("PATH"):
        path_parts.append(env["PATH"])
    env["PATH"] = os.pathsep.join(path_parts)

    # Keep user-level caches/configuration out of the host home directory. The
    # candidate workspace is already disposable and is the only location that
    # should receive legacy task-side temporary files.
    isolated_home = cwd / ".legacy-home"
    isolated_tmp = cwd / ".legacy-tmp"
    isolated_home.mkdir(parents=True, exist_ok=True)
    isolated_tmp.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(isolated_home)
    env["TMPDIR"] = str(isolated_tmp)
    env["TMP"] = str(isolated_tmp)
    env["TEMP"] = str(isolated_tmp)
    if os.name == "nt":
        env["USERPROFILE"] = str(isolated_home)

    pythonpath_parts = [str(cwd)]
    skill_root = cwd / "skill"
    if skill_root.is_dir():
        for skill_dir in sorted(p for p in skill_root.iterdir() if p.is_dir()):
            pythonpath_parts.append(str(skill_dir))
            scripts_dir = skill_dir / "scripts"
            if scripts_dir.is_dir():
                pythonpath_parts.append(str(scripts_dir))
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    return env


def _run_hardened(
    actual_cmd: list[str],
    cwd: Path,
    actual_env: dict[str, str],
    timeout: int,
    container_name: str,
) -> tuple[int, str, str, str | None]:
    """Run Docker with bounded capture; legacy-v1 deliberately does not use this."""

    limit = _RUNTIME_POLICY.output_limit_bytes
    buffers = [bytearray(), bytearray()]
    total = 0
    capture_lock = threading.Lock()
    overflow = threading.Event()
    proc: subprocess.Popen[bytes] | None = None

    def verified_cleanup() -> str | None:
        try:
            force_remove_container(container_name, verify=True)
        except Exception as exc:
            return f"sandbox_cleanup_error:{type(exc).__name__}:{exc}"
        return None

    def drain(stream: Any, index: int) -> None:
        nonlocal total
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            with capture_lock:
                remaining = max(0, limit - total)
                kept = chunk[:remaining]
                buffers[index].extend(kept)
                total += len(kept)
                if len(kept) != len(chunk):
                    overflow.set()

    try:
        proc = subprocess.Popen(
            actual_cmd,
            cwd=cwd,
            env=actual_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None
        readers = [
            threading.Thread(target=drain, args=(proc.stdout, 0), daemon=True),
            threading.Thread(target=drain, args=(proc.stderr, 1), daemon=True),
        ]
        for reader in readers:
            reader.start()

        reason: str | None = None
        deadline = time.monotonic() + timeout
        while proc.poll() is None:
            if overflow.is_set():
                reason = f"output_limit_{limit}_bytes"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = f"timeout_{timeout}s"
                break
            try:
                proc.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                continue

        if reason is not None:
            proc.kill()
            cleanup_error = verified_cleanup()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            if cleanup_error is not None:
                reason = cleanup_error
        for reader in readers:
            reader.join(timeout=5)
        if overflow.is_set() and reason is None:
            reason = f"output_limit_{limit}_bytes"

        stdout = bytes(buffers[0]).decode("utf-8", "replace")
        stderr = bytes(buffers[1]).decode("utf-8", "replace")
        cleanup_error = verified_cleanup()
        if cleanup_error is not None:
            reason = cleanup_error
        return (-1 if reason is not None else int(proc.returncode or 0), stdout, stderr, reason)
    except Exception as exc:
        if proc is not None and proc.poll() is None:
            proc.kill()
        cleanup_error = verified_cleanup()
        reason = f"exec_error:{type(exc).__name__}:{exc}"
        if cleanup_error is not None:
            reason = f"{reason};{cleanup_error}"
        return -1, "", "", reason
    except BaseException as exc:
        if proc is not None and proc.poll() is None:
            proc.kill()
        cleanup_error = verified_cleanup()
        if cleanup_error is not None:
            raise RuntimeError(cleanup_error) from exc
        raise


def _run(
    cmd: list[str],
    cwd: Path,
    timeout: int,
    *,
    writable_files: list[Path] | None = None,
    max_file_bytes: int | None = None,
) -> tuple[int, str, str, str | None]:
    container_name = ""
    actual_cmd = list(cmd)
    actual_env = docker_cli_env() if _RUNTIME_POLICY.is_hardened else _subprocess_env(cwd)
    if _RUNTIME_POLICY.is_hardened:
        container_name = f"taskgenome-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        actual_cmd = docker_command(
            _RUNTIME_POLICY,
            cmd,
            cwd,
            container_name=container_name,
            writable_files=writable_files,
            max_file_bytes=max_file_bytes,
        )
        return _run_hardened(actual_cmd, cwd, actual_env, timeout, container_name)
    try:
        proc = subprocess.run(
            actual_cmd,
            cwd=cwd,
            env=actual_env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or "", None
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        return -1, stdout, stderr, f"timeout_{timeout}s"
    except Exception as exc:
        return -1, "", "", f"exec_error:{type(exc).__name__}:{exc}"


SCORE_RE = re.compile(
    r"^SCORE:([A-Za-z_][A-Za-z0-9_]*)[:=]\s*(-?\d+(?:\.\d+)?)"
    r"|^(?:Final\s+)?[Ss]core[\s:=]+\s*(-?\d+(?:\.\d+)?)",
    re.MULTILINE,
)
PYTEST_SUMMARY_RE = re.compile(
    r"^(?:=+\s*)?(?P<body>[^=\n]+?)\s+in\s+[\d.]+s?[^=\n]*(?:\s*=+)?\s*$",
    re.MULTILINE,
)
PYTEST_TOKEN_RE = re.compile(r"(\d+)\s+(passed|failed|errors?|xfailed|xpassed|skipped|deselected)\b")
PYTEST_NAME_RE = re.compile(r"^\s*(?:def\s+test_[A-Za-z0-9_]+\s*\(|class\s+Test[A-Za-z0-9_]*\s*[\(:])", re.MULTILINE)
PYTEST_IMPORT_RE = re.compile(r"^\s*(?:import\s+pytest|from\s+pytest\b)", re.MULTILINE)


def _parse_scores(stdout: str) -> dict[str, float]:
    scores: dict[str, float] = {}
    bare_idx = 0
    for match in SCORE_RE.finditer(stdout):
        try:
            if match.group(1):
                scores[match.group(1)] = float(match.group(2))
            elif match.group(3):
                bare_idx += 1
                scores[f"score_{bare_idx}"] = float(match.group(3))
        except ValueError:
            pass
    return scores


def _aggregate_score(scores: dict[str, float]) -> float | None:
    if not scores:
        return None
    hints = ("overall", "final", "aggregate", "total", "mean", "weighted", "average", "pass_rate")
    aggs = [v for k, v in scores.items() if any(h in k.lower() for h in hints)]
    if aggs:
        return min(aggs)
    return sum(scores.values()) / len(scores)


def _count_pass_fail(stdout: str) -> tuple[int, int]:
    return stdout.count("PASS:"), stdout.count("FAIL:")


def _parse_pytest(stdout: str) -> tuple[int, int]:
    summary = None
    for match in PYTEST_SUMMARY_RE.finditer(stdout):
        summary = match
    if summary:
        passed = failed = 0
        for token in PYTEST_TOKEN_RE.finditer(summary.group("body")):
            n, kind = int(token.group(1)), token.group(2)
            if kind == "passed":
                passed += n
            elif kind in ("failed", "error", "errors"):
                failed += n
        if passed or failed:
            return passed, failed
    passed = len(re.findall(r"\bPASSED\b", stdout, flags=re.IGNORECASE))
    failed = len(re.findall(r"\bFAILED\b|\bERROR\b", stdout, flags=re.IGNORECASE))
    return passed, failed


def _classify_stderr(stderr: str) -> str:
    if "SyntaxError" in stderr or "IndentationError" in stderr:
        return "syntax_error"
    if "ModuleNotFoundError" in stderr or "ImportError" in stderr:
        return "import_error"
    if "Timeout" in stderr:
        return "timeout"
    last = stderr.strip().splitlines()[-1] if stderr.strip() else ""
    match = re.search(r"^([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception))\b", last)
    return match.group(1) if match else "runtime_error"


def _result_from_subprocess(rc: int, stdout: str, stderr: str, err: str | None, mode: str) -> dict[str, Any]:
    if err is not None:
        return {
            "mode": mode,
            "passed": False,
            "n_pass": 0,
            "n_fail": 1,
            "n_total": 1,
            "pass_rate": 0.0,
            "agg_score": None,
            "scores": {},
            "error_type": "timeout" if err.startswith("timeout_") else "exec_error",
            "reason": err,
            "returncode": rc,
            "stdout_tail": stdout[-TAIL_CHARS:],
            "stderr_tail": stderr[-TAIL_CHARS:],
        }

    passes, fails = _count_pass_fail(stdout)
    scores = _parse_scores(stdout)
    agg = _aggregate_score(scores)
    score_ok = agg is None or agg >= SCORE_PASS_THRESHOLD

    pass_rate_match = re.search(r"SCORE:pass_rate=([0-9]*\.?[0-9]+)", stdout)
    if pass_rate_match:
        try:
            score_ok = abs(float(pass_rate_match.group(1)) - 1.0) <= 1e-9
        except ValueError:
            score_ok = False

    structural_ok = (passes > 0 and fails == 0) or (passes == 0 and fails == 0 and rc == 0)
    passed = rc == 0 and structural_ok and score_ok
    total = passes + fails or 1
    if passed:
        error_type = "success"
    elif rc != 0 and passes == 0 and fails == 0:
        error_type = _classify_stderr(stderr)
    elif fails > 0:
        error_type = "test_failure"
    elif not score_ok:
        error_type = "low_score"
    else:
        error_type = "test_failure"

    return {
        "mode": mode,
        "passed": passed,
        "n_pass": passes,
        "n_fail": fails,
        "n_total": total,
        "pass_rate": passes / total,
        "agg_score": agg,
        "scores": scores,
        "error_type": error_type,
        "returncode": rc,
        "stdout_tail": stdout[-TAIL_CHARS:],
        "stderr_tail": stderr[-TAIL_CHARS:],
    }


def _result_from_pytest(rc: int, stdout: str, stderr: str, err: str | None) -> dict[str, Any]:
    if err is not None:
        return {
            "mode": "pytest",
            "passed": False,
            "n_pass": 0,
            "n_fail": 1,
            "n_total": 1,
            "pass_rate": 0.0,
            "agg_score": None,
            "scores": {},
            "error_type": "timeout" if err.startswith("timeout_") else "exec_error",
            "reason": err,
            "returncode": rc,
            "stdout_tail": stdout[-TAIL_CHARS:],
            "stderr_tail": stderr[-TAIL_CHARS:],
        }
    passed_count, failed_count = _parse_pytest(stdout + "\n" + stderr)
    passed = rc == 0 and passed_count > 0 and failed_count == 0
    total = passed_count + failed_count or 1
    return {
        "mode": "pytest",
        "passed": passed,
        "n_pass": passed_count,
        "n_fail": failed_count,
        "n_total": total,
        "pass_rate": passed_count / total,
        "agg_score": None,
        "scores": {},
        "error_type": "success" if passed else "test_failure",
        "returncode": rc,
        "stdout_tail": stdout[-TAIL_CHARS:],
        "stderr_tail": stderr[-TAIL_CHARS:],
    }


def _looks_pytest(test_script: Path) -> bool:
    text = test_script.read_text(encoding="utf-8", errors="replace")
    return bool(PYTEST_IMPORT_RE.search(text) or PYTEST_NAME_RE.search(text))


def _evaluate_text_task(raw_response: str, row: dict[str, Any], task_dir: Path, timeout: int) -> dict[str, Any]:
    test_script = task_dir / (((row.get("files") or {}).get("oracle")) or "test_script.py")
    if not test_script.exists():
        return _missing_test_result("text_short_answer")
    with tempfile.TemporaryDirectory(prefix="gbv3_text_", dir=_tmp_root()) as raw:
        tmpdir = Path(raw)
        candidate = _write_text_candidate(raw_response, tmpdir)
        if _RUNTIME_POLICY.is_legacy:
            cmd = [sys.executable, str(test_script), "--candidate", str(candidate)]
            run_cwd = task_dir
        else:
            _copy_scenario_assets(task_dir, tmpdir)
            local_test = tmpdir / "test_script.py"
            if not local_test.exists():
                shutil.copy2(test_script, local_test)
            cmd = [sys.executable, "test_script.py", "--candidate", candidate.name]
            run_cwd = tmpdir
        rc, stdout, stderr, err = _run(cmd, cwd=run_cwd, timeout=timeout)
        result = _result_from_subprocess(rc, stdout, stderr, err, "text_short_answer")
        result["passed"] = rc == 0 and "PASS:SCORE:1.0" in stdout
        if result["passed"]:
            result["error_type"] = "success"
            result["pass_rate"] = 1.0
            result["agg_score"] = 1.0
        return result


def _missing_test_result(mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "passed": False,
        "n_pass": 0,
        "n_fail": 0,
        "n_total": 0,
        "pass_rate": 0.0,
        "agg_score": None,
        "scores": {},
        "error_type": "no_test_script",
        "returncode": None,
        "stdout_tail": "",
        "stderr_tail": "",
    }


def _evaluate_code_task(code: str, row: dict[str, Any], task_dir: Path, gen_timeout: int, test_timeout: int) -> dict[str, Any]:
    if not code:
        return {
            "mode": str(row.get("execution_mode")),
            "passed": False,
            "n_pass": 0,
            "n_fail": 0,
            "n_total": 0,
            "pass_rate": 0.0,
            "agg_score": None,
            "scores": {},
            "error_type": "no_code",
            "returncode": None,
            "stdout_tail": "",
            "stderr_tail": "",
        }

    test_script = task_dir / (((row.get("files") or {}).get("oracle")) or "test_script.py")
    if not test_script.exists():
        return _missing_test_result(str(row.get("execution_mode")))

    mode = str(row.get("execution_mode"))
    with tempfile.TemporaryDirectory(prefix="gbv3_code_", dir=_tmp_root()) as raw:
        tmpdir = Path(raw)
        if _RUNTIME_POLICY.is_hardened and mode == "subprocess_ref_runner":
            return _evaluate_hardened_subprocess_ref_runner(
                code,
                row,
                task_dir,
                test_script,
                tmpdir,
                gen_timeout,
                test_timeout,
            )

        _copy_scenario_assets(task_dir, tmpdir)
        _write_code_candidates(tmpdir, code)
        _ensure_local_test_script(test_script, tmpdir)

        if mode == "pytest_pkg":
            rc, stdout, stderr, err = _run(
                [sys.executable, "-m", "pytest", "test_script.py", "-q", "--tb=short", "--no-header"],
                cwd=tmpdir,
                timeout=test_timeout,
            )
            return _result_from_pytest(rc, stdout, stderr, err)

        if mode == "subprocess_ref_runner":
            gen_rc, gen_stdout, gen_stderr, gen_err = _run([sys.executable, "generated.py"], cwd=tmpdir, timeout=gen_timeout)
            if gen_err is not None or gen_rc != 0:
                result = _result_from_subprocess(gen_rc, gen_stdout, gen_stderr, gen_err, mode)
                result["error_type"] = result["error_type"] if gen_err else _classify_stderr(gen_stderr)
                return result
            if _looks_pytest(test_script):
                rc, stdout, stderr, err = _run(
                    [sys.executable, "-m", "pytest", "test_script.py", "-q", "--tb=short", "--no-header"],
                    cwd=tmpdir,
                    timeout=test_timeout,
                )
                return _result_from_pytest(rc, stdout, stderr, err)
            rc, stdout, stderr, err = _run([sys.executable, "test_script.py"], cwd=tmpdir, timeout=test_timeout)
            return _result_from_subprocess(rc, stdout, stderr, err, mode)

        rc, stdout, stderr, err = _run([sys.executable, "test_script.py"], cwd=tmpdir, timeout=test_timeout)
        return _result_from_subprocess(rc, stdout, stderr, err, mode)


def _evaluate_response(raw_response: str, row: dict[str, Any], task_dir: Path, gen_timeout: int, test_timeout: int) -> tuple[dict[str, Any], str]:
    if _is_text_task(row):
        return _evaluate_text_task(raw_response, row, task_dir, test_timeout), ""
    code = extract_python_code(raw_response)
    return _evaluate_code_task(code, row, task_dir, gen_timeout, test_timeout), code


def _filter_rows(rows: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    out = list(rows)
    if args.families:
        wanted = set(_csv_arg(args.families))
        out = [r for r in out if str(r.get("family")) in wanted]
    if args.sources:
        wanted = set(_csv_arg(args.sources))
        out = [r for r in out if str(r.get("source")) in wanted]
    if args.execution_modes:
        wanted = set(_csv_arg(args.execution_modes))
        out = [r for r in out if str(r.get("execution_mode")) in wanted]
    if args.ids:
        wanted = set(_csv_arg(args.ids))
        out = [r for r in out if str(r.get("task_id")) in wanted or str(r.get("legacy_task_id")) in wanted or str(r.get("orig_id")) in wanted]
        found = {str(r.get("task_id")) for r in out} | {str(r.get("legacy_task_id")) for r in out} | {str(r.get("orig_id")) for r in out}
        missing = sorted(wanted - found)
        if missing:
            print(f"WARNING: requested ids not found: {missing}")
    skip_ids = set(_csv_arg(args.skip_ids))
    if not args.ids:
        skip_ids.update(DEFAULT_SKIP_IDS)
    if skip_ids:
        before = len(out)
        out = [r for r in out if str(r.get("task_id")) not in skip_ids and str(r.get("legacy_task_id")) not in skip_ids and str(r.get("orig_id")) not in skip_ids]
        skipped = before - len(out)
        if skipped:
            print(f"skipped {skipped} task(s) by id: {', '.join(sorted(skip_ids))}")
    if args.shuffle:
        random.seed(args.seed)
        random.shuffle(out)
    if args.limit:
        out = out[: args.limit]
    return out


def _make_trials(rows: list[dict[str, Any]], models: list[str], conditions: list[str]) -> list[tuple[Trial, dict[str, Any]]]:
    trials: list[tuple[Trial, dict[str, Any]]] = []
    for row in rows:
        for model in models:
            for condition in conditions:
                trial = Trial(
                    task_id=str(row.get("task_id")),
                    legacy_task_id=str(row.get("legacy_task_id") or row.get("task_id")),
                    source=str(row.get("source")),
                    family=str(row.get("family")),
                    execution_mode=str(row.get("execution_mode")),
                    model=model,
                    condition=condition,
                )
                trials.append((trial, row))
    return trials


def _read_jsonl_objects(path: Path, label: str) -> list[tuple[int, dict[str, Any]]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    except (OSError, UnicodeError) as exc:
        raise SystemExit(f"{label} is unreadable: {path}: {exc}") from exc
    rows: list[tuple[int, dict[str, Any]]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"invalid {label} line {line_number}: {exc}"
            ) from exc
        if not isinstance(row, dict):
            raise SystemExit(f"invalid {label} line {line_number}: object required")
        rows.append((line_number, row))
    return rows


def _load_completed(results_path: Path) -> tuple[set[str], set[str]]:
    completed: set[str] = set()
    api_errors: set[str] = set()
    for _line_number, row in _read_jsonl_objects(results_path, "results.jsonl"):
        key = (row.get("trial") or {}).get("trial_key")
        if not key:
            continue
        if (row.get("eval") or {}).get("error_type") == "api_error":
            api_errors.add(key)
        else:
            completed.add(key)
    return completed, api_errors


def _last_results(results_path: Path) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for _line_number, row in _read_jsonl_objects(results_path, "results.jsonl"):
        key = (row.get("trial") or {}).get("trial_key")
        if key:
            by_key[key] = row
    return list(by_key.values())


def _all_results(results_path: Path) -> list[dict[str, Any]]:
    return [row for _line_number, row in _read_jsonl_objects(results_path, "results.jsonl")]


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _read_summary_config(config_path: Path | None) -> dict[str, Any]:
    if config_path is None or not config_path.exists():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"run config is unreadable: {config_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"run config must be a JSON object: {config_path}")
    return payload


def _summary_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _single_summary_value(label: str, values: set[str]) -> str | None:
    if len(values) > 1:
        raise SystemExit(
            f"refusing to summarize mixed {label}: {', '.join(sorted(values))}"
        )
    return next(iter(values), None)


def _summary_contract(
    config: dict[str, Any], results: list[dict[str, Any]]
) -> dict[str, Any]:
    config_args = config.get("args") if isinstance(config.get("args"), dict) else {}
    specs = {
        "prompt_protocol": (
            (config.get("prompt_protocol"),),
            ("prompt_protocol",),
        ),
        "scoring_protocol": (
            (config.get("scoring_protocol"),),
            ("scoring_protocol",),
        ),
        "runtime_protocol": (
            (
                config.get("runtime_protocol")
                or config.get("protocol")
                or config_args.get("protocol"),
            ),
            ("runtime_protocol", "protocol"),
        ),
    }
    protocols: dict[str, str] = {}
    inferred_protocol_fields: list[str] = []
    for label, (config_values, result_fields) in specs.items():
        values = {
            value
            for raw in config_values
            if (value := _summary_text(raw)) is not None
        }
        for row in results:
            for field in result_fields:
                value = _summary_text(row.get(field))
                if value:
                    values.add(value)
                    break
        selected = _single_summary_value(label.replace("_", " ") + "s", values)
        if selected is None:
            selected = LEGACY_PROTOCOL
            inferred_protocol_fields.append(label)
        protocols[label] = selected

    fingerprint_values: set[str] = set()
    fingerprint_block = config.get("run_fingerprint")
    config_fingerprint = (
        _summary_text(fingerprint_block.get("digest"))
        if isinstance(fingerprint_block, dict)
        else _summary_text(fingerprint_block)
    )
    if config_fingerprint:
        fingerprint_values.add(config_fingerprint)
    fingerprint_result_count = 0
    for row in results:
        value = _summary_text(row.get("run_fingerprint"))
        if value:
            fingerprint_result_count += 1
            fingerprint_values.add(value)
    fingerprint = _single_summary_value("run fingerprints", fingerprint_values)

    manifest_values: set[str] = set()
    config_manifest = _summary_text(config.get("manifest_sha256"))
    if config_manifest:
        manifest_values.add(config_manifest)
    manifest_result_count = 0
    for row in results:
        value = _summary_text(row.get("manifest_sha256"))
        if value:
            manifest_result_count += 1
            manifest_values.add(value)
    manifest_sha256 = _single_summary_value("manifest digests", manifest_values)

    model_ids: dict[str, set[str]] = defaultdict(set)
    aliases: set[str] = set()
    registry = config.get("model_registry")
    if isinstance(registry, dict):
        for alias, spec in registry.items():
            aliases.add(str(alias))
            if isinstance(spec, (list, tuple)) and spec:
                model_id = _summary_text(spec[0])
            elif isinstance(spec, dict):
                model_id = _summary_text(spec.get("model_id"))
            else:
                model_id = None
            if model_id:
                model_ids[str(alias)].add(model_id)
    configured_models = config.get("models")
    if isinstance(configured_models, list):
        aliases.update(str(value) for value in configured_models)
    for row in results:
        trial = row.get("trial") if isinstance(row.get("trial"), dict) else {}
        alias = _summary_text(trial.get("model"))
        model_id = _summary_text(row.get("model_id"))
        if alias:
            aliases.add(alias)
            if model_id:
                model_ids[alias].add(model_id)
        elif model_id:
            aliases.add("<unlabeled>")
            model_ids["<unlabeled>"].add(model_id)

    return {
        **protocols,
        "inferred_protocol_fields": inferred_protocol_fields,
        "run_fingerprint": fingerprint,
        "fingerprint_result_count": fingerprint_result_count,
        "manifest_sha256": manifest_sha256,
        "manifest_result_count": manifest_result_count,
        "result_record_count": len(results),
        "model_ids": {
            alias: sorted(model_ids.get(alias, set())) for alias in sorted(aliases)
        },
    }


def _write_summary(
    results_path: Path,
    summary_path: Path,
    config_path: Path | None = None,
) -> None:
    results = _last_results(results_path)
    contract = _summary_contract(
        _read_summary_config(config_path), _all_results(results_path)
    )
    buckets: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(
        lambda: {"binary": [], "rate": [], "score": [], "families": defaultdict(list), "sources": defaultdict(list), "errors": defaultdict(int)}
    )
    for row in results:
        trial = row.get("trial") or {}
        ev = row.get("eval") or {}
        key = (str(trial.get("model")), str(trial.get("condition")), "all")
        b = buckets[key]
        passed = 1.0 if ev.get("passed") else 0.0
        b["binary"].append(passed)
        b["rate"].append(float(ev.get("pass_rate") or 0.0))
        if ev.get("agg_score") is not None:
            b["score"].append(float(ev.get("agg_score")))
        b["families"][str(trial.get("family"))].append(passed)
        b["sources"][str(trial.get("source"))].append(passed)
        b["errors"][str(ev.get("error_type") or "unknown")] += 1

    inferred = contract["inferred_protocol_fields"]
    provenance = (
        "inferred legacy-v1 for pre-v2 fields: " + ", ".join(inferred)
        if inferred
        else "persisted run metadata"
    )
    lines = [
        "# TaskGenome Bench Official Run Summary",
        "",
        f"- results: `{results_path}`",
        f"- records: {len(results)}",
        f"- prompt protocol: `{contract['prompt_protocol']}`",
        f"- scoring protocol: `{contract['scoring_protocol']}`",
        f"- runtime protocol: `{contract['runtime_protocol']}`",
        f"- protocol provenance: `{provenance}`",
        (
            f"- run fingerprint: `{contract['run_fingerprint']}` "
            f"({contract['fingerprint_result_count']}/{contract['result_record_count']} records)"
            if contract["run_fingerprint"]
            else "- run fingerprint: `unavailable in pre-v2 artifact`"
        ),
        (
            f"- manifest SHA-256: `{contract['manifest_sha256']}` "
            f"({contract['manifest_result_count']}/{contract['result_record_count']} records)"
            if contract["manifest_sha256"]
            else "- manifest SHA-256: `unavailable in pre-v2 artifact`"
        ),
        "- requested/configured model IDs (not provider-returned actual IDs):",
    ]
    if contract["model_ids"]:
        for alias, model_ids in contract["model_ids"].items():
            rendered = ", ".join(f"`{model_id}`" for model_id in model_ids)
            lines.append(
                f"  - `{alias}`: {rendered or '`unavailable in pre-v2 artifact`'}"
            )
    else:
        lines.append("  - `unavailable in pre-v2 artifact`")
    lines.extend([
        "",
        "| model | condition | n | pass% | pass_rate_avg | agg_score_avg |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for (model, condition, _), b in sorted(buckets.items()):
        score = _mean(b["score"]) if b["score"] else 0.0
        lines.append(
            f"| {model} | {condition} | {len(b['binary'])} | {_mean(b['binary']) * 100:.1f}% | "
            f"{_mean(b['rate']) * 100:.1f}% | {score:.4f} |"
        )

    lines.extend(["", "## By Family", ""])
    for (model, condition, _), b in sorted(buckets.items()):
        lines.append(f"### {model} / {condition}")
        lines.append("")
        lines.append("| family | n | pass% |")
        lines.append("|---|---:|---:|")
        for family, vals in sorted(b["families"].items()):
            lines.append(f"| {family} | {len(vals)} | {_mean(vals) * 100:.1f}% |")
        lines.append("")

    lines.extend(["## Error Types", ""])
    for (model, condition, _), b in sorted(buckets.items()):
        err = ", ".join(f"{k}={v}" for k, v in sorted(b["errors"].items()))
        lines.append(f"- `{model}` / `{condition}`: {err}")

    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(summary_path, 0o600)


def _resolve_run_directory(runs_root_value: str, run_id: str) -> Path:
    runs_root = Path(runs_root_value).resolve()
    run_dir = (runs_root / run_id).resolve()
    try:
        run_dir.relative_to(runs_root)
    except ValueError as exc:
        raise SystemExit(f"run-id escapes runs-root: {run_id!r}") from exc
    if run_dir == runs_root:
        raise SystemExit("run-id must name a child directory under runs-root")
    return run_dir


def _run_summary_only(args: argparse.Namespace) -> int:
    run_id = str(args.run_id or "").strip()
    if not run_id:
        raise SystemExit("--summary-only requires an explicit --run-id")
    run_dir = _resolve_run_directory(args.runs_root, run_id)
    if not run_dir.is_dir():
        raise SystemExit(f"run directory does not exist: {run_dir}")
    results_path = run_dir / "results.jsonl"
    if not results_path.is_file():
        raise SystemExit(f"run results do not exist: {results_path}")
    summary_path = run_dir / "summary.md"
    _write_summary(results_path, summary_path, run_dir / "config.json")
    print(f"summary: {summary_path}")
    return 0


def _is_repo_file(path: Path) -> bool:
    try:
        path.resolve().relative_to(V3_ROOT.resolve())
        return path.is_file()
    except (OSError, ValueError):
        return False


def _selected_asset_paths(
    rows: list[dict[str, Any]],
    pool_root: Path,
    conditions: list[str],
    args: argparse.Namespace,
) -> tuple[list[Path], int]:
    paths: list[Path] = []
    external_or_missing = 0
    full = args.repro_hash_mode == "full"
    for row in rows:
        task_dir = _task_dir(row, pool_root)
        if full:
            candidates = [
                path
                for path in task_dir.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and ".pytest_cache" not in path.parts
                and path.suffix != ".pyc"
            ]
        else:
            files = row.get("files") or {}
            candidates = [
                task_dir / str(files.get("task") or "task.md"),
                task_dir / str(files.get("skill") or "SKILL.md"),
                task_dir / str(files.get("oracle") or "test_script.py"),
            ]
        for condition in conditions:
            if condition in GENE_CONDITIONS:
                gene_path = _resolve_gene_path(
                    row,
                    task_dir,
                    _condition_gene_dir(condition, args),
                )
                if gene_path is not None:
                    candidates.append(gene_path)
            elif condition in AGENT_SKILL_CONDITIONS:
                skill_path = _resolve_agent_skill_path(row, task_dir, _AGENT_SKILLS_DIR)
                if skill_path is not None:
                    candidates.append(skill_path)
        for path in candidates:
            if path.resolve().is_file():
                paths.append(path)
                if not _is_repo_file(path):
                    external_or_missing += 1
            else:
                external_or_missing += 1
    return paths, external_or_missing


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _append_json_private(path: Path, payload: dict[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    os.chmod(path, 0o600)


def _validate_resume_config(
    path: Path,
    current: dict[str, Any],
    *,
    allow_unsafe_legacy_config: bool = False,
) -> bool:
    if not path.exists():
        return False
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"existing config is unreadable: {path}: {exc}") from exc
    if not isinstance(existing, dict):
        raise SystemExit(f"existing config must be a JSON object: {path}")
    fingerprint_block = existing.get("run_fingerprint")
    old_fingerprint = (
        fingerprint_block.get("digest")
        if isinstance(fingerprint_block, dict)
        else None
    )
    if not old_fingerprint and not allow_unsafe_legacy_config:
        raise SystemExit(
            "refusing to resume a pre-v2 config without an immutable run fingerprint; "
            "archive the run directory and start a new run_id, or pass "
            "--unsafe-resume-legacy-config only for an explicitly accepted migration"
        )
    old_protocol = str(existing.get("runtime_protocol") or existing.get("protocol") or LEGACY_PROTOCOL)
    if old_protocol != current["runtime_protocol"]:
        raise SystemExit(
            f"refusing to mix runtime protocols in one run_id: existing={old_protocol} "
            f"requested={current['runtime_protocol']}"
        )
    new_fingerprint = (current.get("run_fingerprint") or {}).get("digest")
    if old_fingerprint and new_fingerprint and old_fingerprint != new_fingerprint:
        raise SystemExit(
            "refusing to resume because the immutable run fingerprint changed "
            "(arguments, code, assets, model mapping, or environment differ)"
        )
    for key in ("models", "conditions", "selected_task_ids"):
        if key in existing and existing.get(key) != current.get(key):
            raise SystemExit(f"refusing to resume with changed {key}")
    old_digest = ((existing.get("reproducibility") or {}).get("asset_digest") or {}).get("digest")
    new_digest = ((current.get("reproducibility") or {}).get("asset_digest") or {}).get("digest")
    if old_digest and new_digest and old_digest != new_digest:
        raise SystemExit("refusing to resume because selected asset digest changed")
    for key in (
        "manifest_sha256",
        "score_pass_threshold",
        "runtime_policy",
        "model_registry",
        "api_protocol",
    ):
        if key in existing and existing.get(key) != current.get(key):
            raise SystemExit(f"refusing to resume with changed {key}")
    old_source = ((existing.get("reproducibility") or {}).get("source_digest") or {}).get("digest")
    new_source = ((current.get("reproducibility") or {}).get("source_digest") or {}).get("digest")
    if old_source and new_source and old_source != new_source:
        raise SystemExit("refusing to resume because evaluator source digest changed")
    return True


def _resume_record_error(line_number: int, message: str) -> None:
    raise SystemExit(
        f"refusing to resume with invalid results.jsonl line {line_number}: {message}"
    )


def _validate_result_record_shape(
    record: dict[str, Any],
    line_number: int,
    *,
    runtime_protocol: str,
    run_fingerprint: str,
    manifest_sha256: str,
    expected_trials: dict[str, Trial] | None,
    allow_missing_legacy_fields: bool,
) -> str:
    expected_metadata = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "prompt_protocol": LEGACY_PROTOCOL,
        "scoring_protocol": LEGACY_PROTOCOL,
        "runtime_protocol": runtime_protocol,
        "run_fingerprint": run_fingerprint,
        "manifest_sha256": manifest_sha256,
    }
    for field, wanted in expected_metadata.items():
        actual = record.get(field)
        if actual is None and allow_missing_legacy_fields:
            continue
        if actual != wanted:
            raise SystemExit(
                "refusing to resume with a mixed result contract at "
                f"line {line_number}: {field}={actual!r}, expected={wanted!r}"
            )

    trial = record.get("trial")
    if not isinstance(trial, dict):
        _resume_record_error(line_number, "trial must be an object")
    trial_key = trial.get("trial_key")
    if not isinstance(trial_key, str) or not trial_key:
        _resume_record_error(line_number, "trial_key must be a non-empty string")
    if expected_trials is not None:
        expected_trial = expected_trials.get(trial_key)
        if expected_trial is None:
            _resume_record_error(
                line_number, f"trial_key is not selected by this run: {trial_key}"
            )
        expected_trial_fields = {**asdict(expected_trial), "trial_key": trial_key}
        for field, wanted in expected_trial_fields.items():
            actual = trial.get(field)
            if actual is None and allow_missing_legacy_fields:
                continue
            if actual != wanted:
                _resume_record_error(
                    line_number,
                    f"trial.{field}={actual!r}, expected={wanted!r}",
                )
    else:
        components = (
            trial.get("model"),
            trial.get("condition"),
            trial.get("task_id"),
        )
        if all(isinstance(value, str) and value for value in components):
            derived_key = "::".join(components)
            if derived_key != trial_key:
                _resume_record_error(
                    line_number,
                    f"trial_key={trial_key!r} disagrees with trial fields",
                )

    evaluation = record.get("eval")
    required_eval = {
        "mode",
        "passed",
        "n_pass",
        "n_fail",
        "n_total",
        "pass_rate",
        "agg_score",
        "scores",
        "error_type",
    }
    if not isinstance(evaluation, dict) or not required_eval.issubset(evaluation):
        _resume_record_error(line_number, "eval is incomplete")
    if not isinstance(evaluation["mode"], str) or not evaluation["mode"]:
        _resume_record_error(line_number, "eval.mode must be a non-empty string")
    if type(evaluation["passed"]) is not bool:
        _resume_record_error(line_number, "eval.passed must be boolean")
    for field in ("n_pass", "n_fail", "n_total"):
        if type(evaluation[field]) is not int or evaluation[field] < 0:
            _resume_record_error(line_number, f"eval.{field} must be non-negative integer")
    try:
        pass_rate = _finite_nonnegative(evaluation["pass_rate"], "eval.pass_rate")
    except ValueError as exc:
        _resume_record_error(line_number, str(exc))
    if pass_rate > 1:
        _resume_record_error(line_number, "eval.pass_rate must not exceed 1")
    aggregate = evaluation["agg_score"]
    if aggregate is not None:
        try:
            if not math.isfinite(float(aggregate)):
                raise ValueError
        except (TypeError, ValueError):
            _resume_record_error(line_number, "eval.agg_score must be finite or null")
    scores = evaluation["scores"]
    if not isinstance(scores, dict) or not all(isinstance(key, str) for key in scores):
        _resume_record_error(line_number, "eval.scores must be an object")
    for value in scores.values():
        try:
            if not math.isfinite(float(value)):
                raise ValueError
        except (TypeError, ValueError):
            _resume_record_error(line_number, "eval.scores values must be finite")
    error_type = evaluation["error_type"]
    if not isinstance(error_type, str) or not error_type:
        _resume_record_error(line_number, "eval.error_type must be a non-empty string")

    if not allow_missing_legacy_fields or "model_id" in record:
        if not isinstance(record.get("model_id"), str) or not record["model_id"]:
            _resume_record_error(line_number, "model_id must be a non-empty string")
    tokens = record.get("tokens")
    required_token_fields = {"input", "output", "thoughts", "system_chars", "user_chars"}
    if tokens is None and allow_missing_legacy_fields:
        tokens = None
    elif not isinstance(tokens, dict) or set(tokens) != required_token_fields:
        _resume_record_error(line_number, "tokens has invalid fields")
    if isinstance(tokens, dict):
        for field, value in tokens.items():
            if type(value) is not int or value < 0:
                _resume_record_error(
                    line_number, f"tokens.{field} must be a non-negative integer"
                )
    for field in ("cost_usd", "elapsed_s"):
        if field not in record and allow_missing_legacy_fields:
            continue
        try:
            _finite_nonnegative(record.get(field), field)
        except ValueError as exc:
            _resume_record_error(line_number, str(exc))
    if "cumulative_cost_usd" in record:
        try:
            _finite_nonnegative(
                record["cumulative_cost_usd"], "cumulative_cost_usd"
            )
        except ValueError as exc:
            _resume_record_error(line_number, str(exc))

    prompt_hashes = record.get("prompt_sha256")
    if error_type != "api_error" and not allow_missing_legacy_fields:
        if not isinstance(prompt_hashes, dict) or set(prompt_hashes) != {"system", "user"}:
            _resume_record_error(line_number, "prompt_sha256 is incomplete")
        if not all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in prompt_hashes.values()
        ):
            _resume_record_error(line_number, "prompt_sha256 values must be SHA-256")
    return trial_key


def _validate_existing_result_contract(
    path: Path,
    *,
    runtime_protocol: str,
    run_fingerprint: str,
    manifest_sha256: str,
    expected_trials: dict[str, Trial] | None = None,
    cases_path: Path | None = None,
    budget_path: Path | None = None,
    allow_missing_legacy_fields: bool = False,
) -> None:
    """Reject mixed, partial, or tampered records before they skip trials."""

    committed: dict[str, tuple[int, dict[str, Any]]] = {}
    for line_number, record in _read_jsonl_objects(path, "results.jsonl"):
        trial_key = _validate_result_record_shape(
            record,
            line_number,
            runtime_protocol=runtime_protocol,
            run_fingerprint=run_fingerprint,
            manifest_sha256=manifest_sha256,
            expected_trials=expected_trials,
            allow_missing_legacy_fields=allow_missing_legacy_fields,
        )
        if (record["eval"] or {}).get("error_type") != "api_error":
            committed[trial_key] = (line_number, record)

    if allow_missing_legacy_fields or not committed:
        return
    if cases_path is None or budget_path is None:
        return

    cases: dict[str, dict[str, Any]] = {}
    for line_number, row in _read_jsonl_objects(cases_path, "cases.jsonl"):
        required = {
            "schema_version",
            "runtime_protocol",
            "run_fingerprint",
            "trial_key",
            "task_id",
            "raw_response",
            "extracted_code",
        }
        if set(row) != required:
            raise SystemExit(f"invalid cases.jsonl line {line_number}: invalid fields")
        if (
            row["schema_version"] != CASE_SCHEMA_VERSION
            or row["runtime_protocol"] != runtime_protocol
            or row["run_fingerprint"] != run_fingerprint
            or not isinstance(row["raw_response"], str)
            or not isinstance(row["extracted_code"], str)
        ):
            raise SystemExit(f"invalid cases.jsonl line {line_number}: contract mismatch")
        key = row["trial_key"]
        if not isinstance(key, str) or not key:
            raise SystemExit(f"invalid cases.jsonl line {line_number}: trial_key")
        cases[key] = row

    budgets: dict[str, dict[str, Any]] = {}
    for line_number, row in _read_jsonl_objects(budget_path, "budget.jsonl"):
        required = {
            "schema_version",
            "runtime_protocol",
            "run_fingerprint",
            "trial_key",
            "model_id",
            "input_tokens",
            "output_tokens",
            "thoughts_tokens",
            "cost_usd",
            "cumulative_cost_usd",
        }
        if set(row) != required:
            raise SystemExit(f"invalid budget.jsonl line {line_number}: invalid fields")
        if (
            row["schema_version"] != BUDGET_SCHEMA_VERSION
            or row["runtime_protocol"] != runtime_protocol
            or row["run_fingerprint"] != run_fingerprint
        ):
            raise SystemExit(f"invalid budget.jsonl line {line_number}: contract mismatch")
        for field in ("input_tokens", "output_tokens", "thoughts_tokens"):
            if type(row[field]) is not int or row[field] < 0:
                raise SystemExit(
                    f"invalid budget.jsonl line {line_number}: {field}"
                )
        key = row["trial_key"]
        if not isinstance(key, str) or not key:
            raise SystemExit(f"invalid budget.jsonl line {line_number}: trial_key")
        budgets[key] = row

    for trial_key, (line_number, record) in committed.items():
        case = cases.get(trial_key)
        budget = budgets.get(trial_key)
        if case is None or budget is None:
            _resume_record_error(
                line_number,
                f"committed trial {trial_key} has no matching case/budget record",
            )
        trial = record["trial"]
        tokens = record["tokens"]
        if case["task_id"] != trial["task_id"]:
            _resume_record_error(line_number, f"case task_id mismatch for {trial_key}")
        expected_budget = {
            "model_id": record["model_id"],
            "input_tokens": tokens["input"],
            "output_tokens": tokens["output"],
            "thoughts_tokens": tokens["thoughts"],
            "cost_usd": record["cost_usd"],
            "cumulative_cost_usd": record.get("cumulative_cost_usd"),
        }
        for field, wanted in expected_budget.items():
            if budget.get(field) != wanted:
                _resume_record_error(
                    line_number, f"budget {field} mismatch for {trial_key}"
                )


def _resolve_requested_protocol(args: argparse.Namespace) -> None:
    if args.protocol:
        return
    if args.dry_run or args.summary_only:
        args.protocol = LEGACY_PROTOCOL
        return
    raise SystemExit(
        "real execution requires an explicit --protocol: choose legacy-v1 only "
        "for trusted historical reproduction, or hardened-v2 for new untrusted runs"
    )


def run(args: argparse.Namespace) -> int:
    global _ACTIVE_POOL_ROOT, _AGENT_SKILLS_DIR, _HARDENED_ASSET_POLICY
    global _HARDENED_IO_CONTRACT, _RUNTIME_POLICY, _RUNTIME_IMAGE_IDENTITY

    if args.summary_only:
        return _run_summary_only(args)

    _resolve_requested_protocol(args)
    _RUNTIME_POLICY = runtime_policy_from_args(args)
    _RUNTIME_IMAGE_IDENTITY = None
    try:
        _finite_nonnegative(args.budget_limit_usd, "budget limit")
        _finite_nonnegative(args.budget_alert_step_usd, "budget alert step")
        _RUNTIME_POLICY.validate(require_execution=False)
        if not args.dry_run and not args.summary_only:
            _RUNTIME_IMAGE_IDENTITY = runtime_preflight(_RUNTIME_POLICY)
    except ValueError as exc:
        preflight_secrets = [
            getattr(args, name, "")
            for name in (
                "yunwu_key",
                "gemini_key",
                "siliconflow_key",
                "evomap_key",
                "sub2api_key",
                "bedrock_key",
            )
        ]
        preflight_secrets.append(os.environ.get("DOCKER_HOST", ""))
        safe_message = redact_text(str(exc), preflight_secrets)
        raise SystemExit(f"runtime policy error: {safe_message}") from exc

    manifest_path = Path(args.manifest).resolve()
    pool_root = Path(args.pool_root).resolve()
    _ACTIVE_POOL_ROOT = pool_root
    _AGENT_SKILLS_DIR = Path(args.agent_skills_dir).resolve()
    try:
        manifest, all_rows = _load_manifest(manifest_path)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot load manifest {manifest_path}: {exc}") from exc
    manifest_sha256 = sha256_file(manifest_path)
    asset_policy_path = Path(args.asset_policy).absolute()
    io_contract_path = Path(args.hardened_io_contract).absolute()
    if _RUNTIME_POLICY.is_hardened:
        try:
            asset_policy_path, io_contract_path = _canonical_hardened_boundary_paths(
                args.asset_policy,
                args.hardened_io_contract,
            )
            _HARDENED_ASSET_POLICY = load_validated_asset_policy(
                asset_policy_path
            )
            _HARDENED_IO_CONTRACT = _load_hardened_io_contract(
                io_contract_path
            )
            policy_manifest = _HARDENED_ASSET_POLICY.get(
                "expected_manifest_sha256"
            )
            if policy_manifest and policy_manifest != manifest_sha256:
                raise ValueError(
                    "asset policy does not match the selected manifest: "
                    f"expected={policy_manifest} actual={manifest_sha256}"
                )
            policy_task_count = _HARDENED_ASSET_POLICY.get("expected_task_count")
            if policy_task_count is not None and policy_task_count != len(all_rows):
                raise ValueError(
                    "asset policy task count does not match the selected manifest"
                )
        except (OSError, ReleaseError, ValueError) as exc:
            raise SystemExit(f"hardened-v2 asset boundary error: {exc}") from exc
    else:
        _HARDENED_ASSET_POLICY = None
        _HARDENED_IO_CONTRACT = None

    models = _csv_arg(args.models)
    for model in models:
        if model not in MODEL_REGISTRY:
            raise SystemExit(f"unknown model {model}; known: {sorted(MODEL_REGISTRY)}")
    conditions = _condition_arg(args.conditions)
    for condition in conditions:
        if condition not in CONDITIONS:
            raise SystemExit(f"unknown condition {condition}; allowed: {CONDITIONS}")
    genes_dir = Path(args.genes_dir).resolve()
    gene_conditions = [c for c in conditions if c in GENE_CONDITIONS]
    agent_skill_conditions = [c for c in conditions if c in AGENT_SKILL_CONDITIONS]

    rows = _filter_rows(all_rows, args)
    if agent_skill_conditions and args.filter_missing_agent_skill:
        before = len(rows)
        kept: list[dict[str, Any]] = []
        for row in rows:
            task_dir = _task_dir(row, pool_root)
            if _resolve_agent_skill_path(row, task_dir, _AGENT_SKILLS_DIR) is not None:
                kept.append(row)
        rows = kept
        dropped = before - len(rows)
        if dropped:
            print(f"filtered {dropped} task(s) missing public Agent Skill assets")

    if gene_conditions and args.filter_missing_gene:
        before = len(rows)
        kept: list[dict[str, Any]] = []
        dropped_by_condition: dict[str, int] = {c: 0 for c in gene_conditions}
        for row in rows:
            task_dir = _task_dir(row, pool_root)
            missing = [
                condition
                for condition in gene_conditions
                if _resolve_gene_path(row, task_dir, _condition_gene_dir(condition, args)) is None
            ]
            if missing:
                for condition in missing:
                    dropped_by_condition[condition] += 1
                continue
            kept.append(row)
        rows = kept
        dropped = before - len(rows)
        if dropped:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(dropped_by_condition.items()) if v)
            print(f"filtered {dropped} task(s) missing selected gene assets ({detail})")

    if gene_conditions and not args.allow_missing_gene:
        missing_by_condition: dict[str, list[str]] = {}
        for condition in gene_conditions:
            condition_genes_dir = _condition_gene_dir(condition, args)
            missing_gene_ids: list[str] = []
            for row in rows:
                task_dir = _task_dir(row, pool_root)
                gene_path = _resolve_gene_path(row, task_dir, condition_genes_dir)
                if gene_path is None:
                    missing_gene_ids.append(str(row.get("task_id")))
            if missing_gene_ids:
                missing_by_condition[condition] = missing_gene_ids
        if missing_by_condition:
            parts = []
            for condition, ids in sorted(missing_by_condition.items()):
                head = ", ".join(ids[:20])
                tail = "" if len(ids) <= 20 else f", ... (+{len(ids) - 20} more)"
                parts.append(f"{condition}: {head}{tail}")
            raise SystemExit(
                "with_gene requested but some gene files are missing. "
                f"missing task_ids by condition: {'; '.join(parts)}. "
                "Use --filter-missing-gene to keep only the common subset, or "
                "--allow-missing-gene to continue without strict precheck."
            )

    if agent_skill_conditions:
        missing_agent_skill_ids: list[str] = []
        for row in rows:
            task_dir = _task_dir(row, pool_root)
            if _resolve_agent_skill_path(row, task_dir, _AGENT_SKILLS_DIR) is None:
                missing_agent_skill_ids.append(str(row.get("task_id")))
        if missing_agent_skill_ids:
            head = ", ".join(missing_agent_skill_ids[:20])
            tail = "" if len(missing_agent_skill_ids) <= 20 else f", ... (+{len(missing_agent_skill_ids) - 20} more)"
            raise SystemExit(
                "with_public_agent_skill/with_agent_skill requested but some public Agent Skill files are missing. "
                f"missing task_ids: {head}{tail}. "
                "Run eval/generate_agent_skills_v3.py, or use --filter-missing-agent-skill "
                "to keep only tasks with generated assets."
            )

    if _RUNTIME_POLICY.is_hardened:
        try:
            _validate_hardened_selection(
                rows,
                manifest_sha256,
                manifest_rows=all_rows,
            )
        except (ReleaseError, ValueError) as exc:
            raise SystemExit(f"hardened-v2 selection rejected: {exc}") from exc

    run_id = args.run_id or f"v3_official_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir = _resolve_run_directory(args.runs_root, run_id)
    run_dir_existed = run_dir.exists()
    run_dir.mkdir(parents=True, exist_ok=True)
    if not run_dir_existed:
        os.chmod(run_dir, 0o700)
    results_path = run_dir / "results.jsonl"
    cases_path = run_dir / "cases.jsonl"
    budget_path = run_dir / "budget.jsonl"
    config_path = run_dir / "config.json"
    summary_path = run_dir / "summary.md"
    budget_tracker = BudgetTracker(
        starting_spend=_load_budget_spent(budget_path),
        limit_usd=args.budget_limit_usd,
        alert_step_usd=args.budget_alert_step_usd,
    )

    trials = _make_trials(rows, models, conditions)
    completed, api_errors = _load_completed(results_path)
    pending = [(t, r) for t, r in trials if t.trial_key not in completed]

    print(f"loaded {len(all_rows)} tasks from {manifest_path}")
    print(f"selected {len(rows)} tasks; trials total={len(trials)} pending={len(pending)}")
    print(
        f"protocol: prompt=legacy-v1 scoring=legacy-v1 runtime={_RUNTIME_POLICY.protocol} "
        f"backend={_RUNTIME_POLICY.backend}"
    )
    if _RUNTIME_POLICY.is_legacy and not args.dry_run and not args.summary_only:
        print(
            "WARNING: legacy-v1 executes candidate code on the host with no full "
            "security boundary. Candidate subprocesses receive a credential-free "
            "allowlisted environment, but must still run only on an expendable, "
            "isolated machine."
        )
    print("official policy: all sources are included by default, including v3_imported_curated_tasks_final")
    if budget_tracker.enabled:
        print(
            f"budget guard: starting=${budget_tracker.spent_usd:.2f}, "
            f"limit=${budget_tracker.limit_usd:.2f}, alert_step=${budget_tracker.alert_step_usd:.2f}"
        )
    bedrock_bypass_proxy = os.environ.get("GENE_BENCH_BEDROCK_BYPASS_PROXY", "0").strip().lower() in {
        "1", "true", "yes", "on"
    }
    bedrock_env_proxy = ""
    for proxy_env_name in ("GENE_BENCH_BEDROCK_PROXY", "https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        bedrock_env_proxy = os.environ.get(proxy_env_name, "").strip()
        if bedrock_env_proxy:
            break
    bedrock_proxy_mode = "direct" if bedrock_bypass_proxy else ("explicit_proxy" if bedrock_env_proxy else "requests_default")
    safe_bedrock_proxy = redact_url(bedrock_env_proxy)
    print(f"bedrock proxy: mode={bedrock_proxy_mode} proxy={safe_bedrock_proxy or '<none>'}")
    if api_errors:
        print(f"will retry {len(api_errors)} previous api_error trials")

    safe_args, cli_credentials_supplied = redacted_args(vars(args))
    asset_paths, external_or_missing_assets = _selected_asset_paths(rows, pool_root, conditions, args)
    source_paths = [
        HERE / "run_official.py",
        HERE / "api.py",
        HERE / "runtime_policy.py",
        HERE / "reproducibility.py",
    ]
    hardened_asset_boundary: dict[str, Any] | None = None
    if _RUNTIME_POLICY.is_hardened:
        source_paths.extend(
            [TOOLS_ROOT / "release_assets.py", asset_policy_path, io_contract_path]
        )
        hardened_asset_boundary = {
            "asset_policy_sha256": sha256_file(asset_policy_path),
            "io_contract_schema_version": HARDENED_IO_SCHEMA_VERSION,
            "io_contract_sha256": sha256_file(io_contract_path),
            "image_identity": _RUNTIME_IMAGE_IDENTITY,
        }
    effective_local_base_url = (
        args.local_base_url
        or os.environ.get("LOCAL_BASE_URL", "http://localhost:8000/v1")
    )
    effective_sub2api_base_url = (
        args.sub2api_base_url
        or os.environ.get("SUB2API_BASE_URL", "")
    )
    config_payload = {
        "schema_version": RUN_CONFIG_SCHEMA_VERSION,
        "prompt_protocol": LEGACY_PROTOCOL,
        "scoring_protocol": LEGACY_PROTOCOL,
        "runtime_protocol": _RUNTIME_POLICY.protocol,
        "runtime_policy": asdict(_RUNTIME_POLICY),
        "legacy_v1_reference": {
            "git_commit": "df31c643d3a8b21bf6b51aa3930fd6c20189d3dc",
            "run_official_sha256": "caeba49f0d3707f6e5341642bcab5e7b80acf7cfbec3eb2f5a7eec8d21e816bc",
            "api_sha256": "926f7f69a1fa1274b892dddcb5e06257c211f7869f6f87ee1e9819f34205ce3d",
            "manifest_sha256": "191587d6e35b794601c096e98133577ea497ab9e09936f6818d1b9e30a14264d",
        },
        "args": safe_args,
        "cli_credentials_supplied": cli_credentials_supplied,
        "credential_sources": credential_sources(vars(args)),
        "manifest_summary": manifest.get("summary"),
        "manifest_sha256": manifest_sha256,
        "official_policy": {
            "include_v3_imported_curated_tasks_final": True,
            "default_models": DEFAULT_MODELS,
            "default_conditions": DEFAULT_CONDITIONS,
        },
        "models": models,
        "model_registry": {model: list(MODEL_REGISTRY[model]) for model in models},
        "price_table": {
            MODEL_REGISTRY[model][0]: PRICE_TABLE.get(MODEL_REGISTRY[model][0])
            for model in models
        },
        "api_protocol": {
            "max_tokens": DEFAULT_MAX_TOKENS,
            "request_timeout_s": DEFAULT_TIMEOUT,
            "max_attempts": API_MAX_RETRIES,
            "retry_base_delay_s": API_RETRY_BASE_DELAY,
            "retry_max_delay_s": API_RETRY_MAX_DELAY,
            "openai_compatible_temperature": "provider_default",
            "gemini_temperature": 0.2,
            "local_temperature": 0.2,
            "local_base_url": redact_url(effective_local_base_url),
            "sub2api_base_url": redact_url(effective_sub2api_base_url),
            "gpt_reasoning_effort": args.gpt_reasoning_effort,
            "evomap_max_tokens_cap": 16000,
            "evomap_timeout_s": 400,
            "gemini_backend": "vertex_openai",
            "gemini_reasoning_effort": GEMINI_REASONING_EFFORT or "default",
            "gemini_vertex_project_id": GEMINI_VERTEX_PROJECT_ID,
            "gemini_vertex_location": GEMINI_VERTEX_LOCATION,
            "bedrock_region": BEDROCK_REGION,
            "local_enable_thinking": LOCAL_ENABLE_THINKING,
        },
        "conditions": conditions,
        "selected_task_ids": [str(row.get("task_id")) for row in rows],
        "genes_dir": str(genes_dir),
        "gene_dirs": {
            condition: str(_condition_gene_dir(condition, args))
            for condition in gene_conditions
        },
        "agent_skills_dir": str(_AGENT_SKILLS_DIR),
        "default_skip_ids": list(DEFAULT_SKIP_IDS),
        "effective_skip_ids": sorted(set(_csv_arg(args.skip_ids)) | (set(DEFAULT_SKIP_IDS) if not args.ids else set())),
        "n_tasks_selected": len(rows),
        "n_trials_total": len(trials),
        "n_trials_pending_at_start": len(pending),
        "score_pass_threshold": SCORE_PASS_THRESHOLD,
        "budget": {
            "starting_spend_usd": budget_tracker.spent_usd,
            "limit_usd": budget_tracker.limit_usd,
            "alert_step_usd": budget_tracker.alert_step_usd,
        },
        "bedrock_thinking": {
            "effort": args.bedrock_effort,
            "thinking_type": args.bedrock_thinking_type,
        },
        "bedrock_proxy": {
            "mode": bedrock_proxy_mode,
            "effective_proxy": safe_bedrock_proxy,
            "bypass_proxy": bedrock_bypass_proxy,
        },
        "gemini_thinking": {
            "reasoning_effort": os.environ.get("GENE_BENCH_GEMINI_REASONING_EFFORT", "low").strip().lower(),
        },
        "hardened_asset_boundary": hardened_asset_boundary,
        "reproducibility": {
            "hash_mode": args.repro_hash_mode,
            "asset_digest": digest_files(
                asset_paths,
                base=V3_ROOT,
                allow_external=True,
            ),
            "source_digest": digest_files(source_paths, base=V3_ROOT),
            "external_or_missing_asset_count": external_or_missing_assets,
            "environment": collect_environment(V3_ROOT),
        },
        "started_at": datetime.now().isoformat(),
    }
    fingerprint_args = {
        key: value
        for key, value in safe_args.items()
        if key
        not in {
            "run_id",
            "runs_root",
            "dry_run",
            "summary_only",
            "unsafe_resume_legacy_config",
            "yunwu_key",
            "gemini_key",
            "siliconflow_key",
            "evomap_key",
            "sub2api_key",
            "bedrock_key",
        }
    }
    run_contract = {
        "schema_version": RUN_CONFIG_SCHEMA_VERSION,
        "prompt_protocol": config_payload["prompt_protocol"],
        "scoring_protocol": config_payload["scoring_protocol"],
        "runtime_protocol": config_payload["runtime_protocol"],
        "runtime_policy": config_payload["runtime_policy"],
        "arguments": fingerprint_args,
        "manifest_sha256": config_payload["manifest_sha256"],
        "models": config_payload["models"],
        "model_registry": config_payload["model_registry"],
        "price_table": config_payload["price_table"],
        "api_protocol": config_payload["api_protocol"],
        "hardened_asset_boundary": config_payload["hardened_asset_boundary"],
        "conditions": config_payload["conditions"],
        "selected_task_ids": config_payload["selected_task_ids"],
        "score_pass_threshold": config_payload["score_pass_threshold"],
        "asset_digest": config_payload["reproducibility"]["asset_digest"],
        "source_digest": config_payload["reproducibility"]["source_digest"],
        "environment": {
            key: value
            for key, value in config_payload["reproducibility"]["environment"].items()
            if key != "sensitive_environment_names_present"
        },
    }
    config_payload["run_fingerprint"] = {
        "algorithm": "sha256",
        "digest": stable_json_sha256(run_contract),
    }
    resumed = _validate_resume_config(
        config_path,
        config_payload,
        allow_unsafe_legacy_config=args.unsafe_resume_legacy_config,
    )
    unsafe_legacy_resume = False
    if resumed:
        print(f"resume contract: {config_path} (kept immutable)")
        if args.unsafe_resume_legacy_config:
            persisted = _read_summary_config(config_path)
            persisted_fingerprint = persisted.get("run_fingerprint")
            unsafe_legacy_resume = not (
                isinstance(persisted_fingerprint, dict)
                and persisted_fingerprint.get("digest")
            )
        if unsafe_legacy_resume:
            print(
                "WARNING: unsafe legacy-config migration accepted; the archived "
                "config remains pre-v2 and has no immutable fingerprint"
            )
    _validate_existing_result_contract(
        results_path,
        runtime_protocol=_RUNTIME_POLICY.protocol,
        run_fingerprint=config_payload["run_fingerprint"]["digest"],
        manifest_sha256=config_payload["manifest_sha256"],
        expected_trials={trial.trial_key: trial for trial, _row in trials},
        cases_path=cases_path,
        budget_path=budget_path,
        allow_missing_legacy_fields=unsafe_legacy_resume,
    )
    completed, api_errors = _load_completed(results_path)
    pending = [(trial, row) for trial, row in trials if trial.trial_key not in completed]
    if not resumed:
        _write_json_atomic(config_path, config_payload)
    _append_json_private(
        run_dir / "run_events.jsonl",
        {
            "schema_version": "taskgenome.run-event.v1",
            "timestamp": datetime.now().astimezone().isoformat(),
            "event": "resumed" if resumed else "created",
            "runtime_protocol": _RUNTIME_POLICY.protocol,
            "run_fingerprint": config_payload["run_fingerprint"]["digest"],
            "credential_sources": credential_sources(vars(args)),
            "dry_run": bool(args.dry_run),
            "unsafe_legacy_config_resume": unsafe_legacy_resume,
        },
    )

    if args.dry_run:
        for trial, _ in pending[:30]:
            print(f"  [dry] {trial.model:12s} {trial.condition:10s} {trial.task_id:6s} {trial.family:16s} {trial.execution_mode}")
        if len(pending) > 30:
            print(f"  ... {len(pending) - 30} more")
        return 0

    keys = _resolve_keys(args)
    secret_values = tuple(
        value for value in (*keys.values(), bedrock_env_proxy) if value
    )
    lock = threading.RLock()
    counter = {"done": 0, "ok": 0, "err": 0, "skipped_budget": 0, "total": len(pending)}

    def record_json(path: Path, payload: dict[str, Any]) -> None:
        with lock:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            os.chmod(path, 0o600)

    def do_trial(trial: Trial, row: dict[str, Any]) -> None:
        t0 = time.time()
        task_dir = _task_dir(row, pool_root)
        model_id = MODEL_REGISTRY[trial.model][0]
        if budget_tracker.should_stop():
            with lock:
                counter["done"] += 1
                counter["skipped_budget"] += 1
                if counter["skipped_budget"] == 1:
                    print(
                        f"budget limit already reached (${budget_tracker.spent_usd:.2f}); "
                        "skipping remaining queued trials without API calls"
                    )
            return
        try:
            user_prompt = _build_user_prompt(row, task_dir)
            system_prompt = _build_system_prompt(
                row,
                task_dir,
                trial.condition,
                _condition_gene_dir(trial.condition, args),
            )
            api_result = call_llm(
                trial.model,
                user_prompt,
                system_prompt,
                yunwu_key=keys["yunwu_key"],
                gemini_key=keys["gemini_key"],
                siliconflow_key=keys["siliconflow_key"],
                evomap_key=keys["evomap_key"],
                sub2api_key=keys["sub2api_key"],
                bedrock_key=keys["bedrock_key"],
                local_base_url=keys["local_base_url"],
                sub2api_base_url=keys["sub2api_base_url"],
                effort=args.bedrock_effort,
                thinking_type=args.bedrock_thinking_type,
                gpt_reasoning_effort=args.gpt_reasoning_effort,
            )
            raw_response = str(api_result.get("response") or "")
            input_tokens = _token_count(api_result, "input_tokens")
            output_tokens = _token_count(api_result, "output_tokens")
            thoughts_tokens = _token_count(api_result, "thoughts_tokens")
            cost = _compute_cost(trial.model, api_result)
            with lock:
                cumulative_cost = budget_tracker.add(cost, trial.trial_key)
                record_json(
                    budget_path,
                    {
                        "schema_version": BUDGET_SCHEMA_VERSION,
                        "runtime_protocol": _RUNTIME_POLICY.protocol,
                        "run_fingerprint": config_payload["run_fingerprint"]["digest"],
                        "trial_key": trial.trial_key,
                        "model_id": model_id,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "thoughts_tokens": thoughts_tokens,
                        "cost_usd": cost,
                        "cumulative_cost_usd": cumulative_cost,
                    },
                )
            eval_result, extracted_code = _evaluate_response(
                raw_response,
                row,
                task_dir,
                gen_timeout=args.gen_timeout,
                test_timeout=args.test_timeout,
            )
            eval_result = redact_tree(eval_result, secret_values)
            record = {
                "schema_version": RESULT_SCHEMA_VERSION,
                "prompt_protocol": LEGACY_PROTOCOL,
                "scoring_protocol": LEGACY_PROTOCOL,
                "runtime_protocol": _RUNTIME_POLICY.protocol,
                "run_fingerprint": config_payload["run_fingerprint"]["digest"],
                "manifest_sha256": config_payload["manifest_sha256"],
                "model_id": model_id,
                "prompt_sha256": {
                    "system": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
                    "user": hashlib.sha256(user_prompt.encode("utf-8")).hexdigest(),
                },
                "trial": {**asdict(trial), "trial_key": trial.trial_key},
                "eval": eval_result,
                "tokens": {
                    "input": input_tokens,
                    "output": output_tokens,
                    "thoughts": thoughts_tokens,
                    "system_chars": len(system_prompt),
                    "user_chars": len(user_prompt),
                },
                "cost_usd": cost,
                "cumulative_cost_usd": cumulative_cost,
                "elapsed_s": round(time.time() - t0, 3),
            }
            record_json(
                cases_path,
                {
                    "schema_version": CASE_SCHEMA_VERSION,
                    "runtime_protocol": _RUNTIME_POLICY.protocol,
                    "run_fingerprint": config_payload["run_fingerprint"]["digest"],
                    "trial_key": trial.trial_key,
                    "task_id": trial.task_id,
                    "raw_response": redact_text(raw_response, secret_values),
                    "extracted_code": redact_text(extracted_code, secret_values),
                },
            )
            # results.jsonl is the per-trial commit marker. A crash before this
            # append leaves an orphan budget/case record but never a false
            # completed trial on resume.
            record_json(results_path, record)
            with lock:
                counter["done"] += 1
                if eval_result.get("passed"):
                    counter["ok"] += 1
                status = "PASS" if eval_result.get("passed") else f"FAIL[{eval_result.get('error_type')}]"
                print(
                    f"  [{counter['done']}/{counter['total']}] "
                    f"{trial.model:12s} {trial.condition:10s} {trial.task_id:6s} "
                    f"{status:22s} ${cost:.4f} ({record['elapsed_s']}s)"
                )
        except Exception as exc:
            safe_exception = sanitize_api_error_text(
                f"{type(exc).__name__}: {exc}",
                secret_values,
            )
            safe_traceback = sanitize_api_error_text(
                traceback.format_exc()[-1200:],
                secret_values,
            )
            record = {
                "schema_version": RESULT_SCHEMA_VERSION,
                "prompt_protocol": LEGACY_PROTOCOL,
                "scoring_protocol": LEGACY_PROTOCOL,
                "runtime_protocol": _RUNTIME_POLICY.protocol,
                "run_fingerprint": config_payload["run_fingerprint"]["digest"],
                "manifest_sha256": config_payload["manifest_sha256"],
                "model_id": model_id,
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
                    "exception": safe_exception,
                    "traceback_tail": safe_traceback,
                },
                "tokens": {"input": 0, "output": 0, "thoughts": 0, "system_chars": 0, "user_chars": 0},
                "cost_usd": 0.0,
                "elapsed_s": round(time.time() - t0, 3),
            }
            record_json(results_path, record)
            with lock:
                counter["done"] += 1
                counter["err"] += 1
                print(
                    f"  [{counter['done']}/{counter['total']}] "
                    f"{trial.model:12s} {trial.condition:10s} {trial.task_id:6s} "
                    f"ERR {safe_exception}"
                )

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(do_trial, trial, row) for trial, row in pending]
        for _ in as_completed(futures):
            pass

    _write_summary(results_path, summary_path, config_path)
    attempted = counter["done"] - counter["skipped_budget"]
    print(
        f"finished: {counter['ok']}/{attempted} attempted trials passed, "
        f"api_errors={counter['err']}, budget_skipped={counter['skipped_budget']}, "
        f"spent=${budget_tracker.spent_usd:.2f}"
    )
    print(f"summary: {summary_path}")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="Path to v3 tasks_final/manifest.json")
    parser.add_argument("--pool-root", default=str(POOL_ROOT), help="Path to v3 tasks_final")
    parser.add_argument("--runs-root", default=str(DEFAULT_RUNS_ROOT), help="Directory for run artifacts")
    parser.add_argument(
        "--asset-policy",
        default=str(DEFAULT_ASSET_POLICY),
        help="Release asset policy that defines the hardened-v2 public runtime view",
    )
    parser.add_argument(
        "--hardened-io-contract",
        default=str(DEFAULT_HARDENED_IO_CONTRACT),
        help="Versioned per-task output contract required by hardened-v2 code tasks",
    )
    parser.add_argument(
        "--protocol",
        choices=PROTOCOLS,
        default=None,
        help=(
            "Required for real execution. legacy-v1 preserves historical host execution; "
            "hardened-v2 keeps prompts/scoring unchanged and isolates candidate code."
        ),
    )
    parser.add_argument(
        "--execution-backend",
        choices=("auto", "host", "docker"),
        default="auto",
        help="Execution backend; auto selects host for legacy-v1 and Docker for hardened-v2",
    )
    parser.add_argument(
        "--sandbox-image",
        default=os.environ.get("TASKGENOME_SANDBOX_IMAGE", ""),
        help="Digest-pinned Docker image required by hardened-v2",
    )
    parser.add_argument("--sandbox-memory", default="4g")
    parser.add_argument("--sandbox-cpus", type=float, default=2.0)
    parser.add_argument("--sandbox-pids-limit", type=int, default=256)
    parser.add_argument("--sandbox-tmpfs-size", default="1g")
    parser.add_argument(
        "--sandbox-output-limit-bytes",
        type=int,
        default=8 * 1024 * 1024,
        help="Combined stdout/stderr capture limit for each hardened-v2 process",
    )
    parser.add_argument(
        "--allow-unpinned-sandbox-image",
        action="store_true",
        help=(
            "Deprecated compatibility flag; hardened-v2 rejects unpinned images "
            "even when this flag is present"
        ),
    )
    parser.add_argument(
        "--repro-hash-mode",
        choices=("core", "full"),
        default="full",
        help=(
            "Hash every selected scenario asset (default) or only core prompt/test "
            "files for a faster, explicitly partial check"
        ),
    )
    parser.add_argument("--models", default=DEFAULT_MODELS, help="Comma-separated model aliases")
    parser.add_argument("--conditions", default=DEFAULT_CONDITIONS, help=f"Comma-separated subset of {CONDITIONS}")
    parser.add_argument(
        "--genes-dir",
        default=str(POOL_ROOT / "genes_gemini31pro"),
        help="Directory containing experiential gene assets for condition=with_gene (<task_id>.json)",
    )
    parser.add_argument(
        "--gene-gemini-dir",
        default=str(POOL_ROOT / "genes_gemini31pro"),
        help="Directory containing Gemini Gene assets for condition=gene_gemini/with_gene_gemini",
    )
    parser.add_argument(
        "--gene-opus-dir",
        default=str(POOL_ROOT / "genes_opus48"),
        help="Directory containing Opus Gene assets for condition=gene_opus/with_gene_opus",
    )
    parser.add_argument(
        "--agent-skills-dir",
        default=str(DEFAULT_AGENT_SKILLS_DIR),
        help="Directory containing public Agent Skill assets for condition=with_public_agent_skill/with_agent_skill (<task_id>.md)",
    )
    parser.add_argument(
        "--filter-missing-gene",
        action="store_true",
        help="Drop tasks missing any gene asset required by the selected gene conditions",
    )
    parser.add_argument(
        "--filter-missing-agent-skill",
        action="store_true",
        help="Drop tasks missing public Agent Skill assets required by public Agent Skill conditions",
    )
    parser.add_argument(
        "--allow-missing-gene",
        action="store_true",
        help="Do not fail fast when condition=with_gene and some gene files are missing",
    )
    parser.add_argument("--families", default="", help="Optional comma-separated family filter")
    parser.add_argument("--sources", default="", help="Optional comma-separated source filter")
    parser.add_argument("--execution-modes", default="", help="Optional comma-separated execution_mode filter")
    parser.add_argument("--ids", default="", help="Comma-separated task_id, legacy_task_id, or orig_id filter")
    parser.add_argument(
        "--skip-ids",
        default="",
        help=(
            "Comma-separated task_id, legacy_task_id, or orig_id values to exclude. "
            f"Full runs also skip defaults: {','.join(DEFAULT_SKIP_IDS)}. "
            "Explicit --ids disables the default skip list."
        ),
    )
    parser.add_argument("--limit", type=int, default=0, help="Limit selected tasks after filters/shuffle")
    parser.add_argument("--shuffle", action="store_true", help="Shuffle selected tasks before --limit")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--gen-timeout", type=int, default=120)
    parser.add_argument("--test-timeout", type=int, default=180)
    parser.add_argument(
        "--budget-limit-usd",
        type=float,
        default=float(os.environ.get("GENE_BENCH_BUDGET_LIMIT_USD", "400")),
        help="Stop starting new API calls once cumulative budget.jsonl spend reaches this USD amount; 0 disables",
    )
    parser.add_argument(
        "--budget-alert-step-usd",
        type=float,
        default=float(os.environ.get("GENE_BENCH_BUDGET_ALERT_STEP_USD", "50")),
        help="Print a budget alert whenever cumulative API spend crosses this USD interval; 0 disables alerts",
    )
    parser.add_argument(
        "--bedrock-effort",
        default=os.environ.get("GENE_BENCH_BEDROCK_EFFORT", "low").strip().lower(),
        help="Bedrock Claude thinking effort: low, medium, high, or off",
    )
    parser.add_argument(
        "--bedrock-thinking-type",
        default=os.environ.get("GENE_BENCH_BEDROCK_THINKING_TYPE", "adaptive").strip().lower(),
        help="Bedrock Claude thinking type, default adaptive",
    )
    parser.add_argument(
        "--gpt-reasoning-effort",
        default=os.environ.get(
            "GENE_BENCH_GPT_REASONING_EFFORT", GPT_REASONING_EFFORT
        ).strip().lower(),
        help="GPT reasoning effort for compatible gateways: low, medium, high, or provider default",
    )
    parser.add_argument("--run-id", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument(
        "--unsafe-resume-legacy-config",
        action="store_true",
        help=(
            "DANGEROUS: permit resume when an archived pre-v2 config has no "
            "immutable run fingerprint; copy/archive the original run first"
        ),
    )
    parser.add_argument("--yunwu-key", default="", help="Provider credential (prefer the documented environment variable)")
    parser.add_argument("--gemini-key", default="", help="Provider credential (prefer the documented environment variable)")
    parser.add_argument("--siliconflow-key", default="", help="Provider credential (prefer the documented environment variable)")
    parser.add_argument("--evomap-key", default="", help="Provider credential (prefer the documented environment variable)")
    parser.add_argument("--sub2api-key", default="", help="Provider credential (prefer SUB2API_API_KEY)")
    parser.add_argument("--bedrock-key", default="", help="Provider credential (prefer the documented environment variable)")
    parser.add_argument("--local-base-url", default="")
    parser.add_argument(
        "--sub2api-base-url",
        default="",
        help="Explicit OpenAI-compatible endpoint for the optional sub2api channel",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv or sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())

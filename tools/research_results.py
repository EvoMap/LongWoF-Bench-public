#!/usr/bin/env python3
"""Build and render the TaskGenome Bench research-v1 evidence package.

This module is deliberately separate from ``eval/``.  It never calls a model,
executes a candidate answer, rebuilds a prompt, or changes a verdict.  Private
build mode reads already-recorded result JSONL and emits sanitized per-task
metrics.  Public render mode rebuilds aggregate tables, statistics, and SVGs
from those sanitized metrics only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import os
import random
import subprocess
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Iterable, Sequence


ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
MANIFEST = ROOT / "tasks_final" / "manifest.json"
RELEASE_ID = "73d08c560817d745c8415927"
RESEARCH_VERSION = "research-v1"
HISTORICAL_COMMIT = "df31c643d3a8b21bf6b51aa3930fd6c20189d3dc"
HISTORICAL_RUNNER_SHA256 = "caeba49f0d3707f6e5341642bcab5e7b80acf7cfbec3eb2f5a7eec8d21e816bc"
HISTORICAL_API_SHA256 = "926f7f69a1fa1274b892dddcb5e06257c211f7869f6f87ee1e9819f34205ce3d"
MANIFEST_SHA256 = "191587d6e35b794601c096e98133577ea497ab9e09936f6818d1b9e30a14264d"
PRIVATE_SOURCES_ENV = "TASKGENOME_PRIVATE_RESEARCH_SOURCES"
PRIVATE_SOURCE_ROOT_ENV = "TASKGENOME_PRIVATE_SOURCE_ROOT"
# This inventory is metadata-only and is deliberately not part of the public
# code export.  A private authoring checkout may override it with
# --private-sources or TASKGENOME_PRIVATE_RESEARCH_SOURCES.
DEFAULT_PRIVATE_SOURCES = ROOT / "release" / "research_sources.v1.json"
LEGACY_PRIVATE_SOURCES = ROOT / "release" / "historical_runs.v1.json"
BOOTSTRAP_ITERATIONS = 5000
BOOTSTRAP_SEED = 20260718

CONDITIONS = (
    "no_context",
    "with_skill",
    "with_gene_gemini",
    "with_gene_opus",
)
REPORT_CONDITIONS = ("no_context", "with_skill", "with_gene_opus")
FAMILIES = (
    "agent_env_synth",
    "code_generation",
    "math_reasoning",
    "rule_following",
)

MODEL_META: dict[str, dict[str, str]] = {
    "bedrock_opus48": {
        "display_name": "Anthropic Claude Opus 4.8",
        "model_id": "global.anthropic.claude-opus-4-8",
        "provider": "AWS Bedrock Converse API",
    },
    "bedrock_sonnet46": {
        "display_name": "Anthropic Claude Sonnet 4.6",
        "model_id": "global.anthropic.claude-sonnet-4-6",
        "provider": "AWS Bedrock Converse API",
    },
    "gemini_flash": {
        "display_name": "Google Gemini 3.1 Flash-Lite Preview",
        "model_id": "gemini-3.1-flash-lite-preview",
        "provider": "Google Gemini API",
    },
    "gemini_pro": {
        "display_name": "Google Gemini 3.1 Pro Preview",
        "model_id": "gemini-3.1-pro-preview",
        "provider": "Google Gemini API",
    },
    "minimax_m3": {
        "display_name": "MiniMax M3",
        "model_id": "MiniMax-M3",
        "provider": "MiniMax API",
    },
    "sf_qwen_coder30b": {
        "display_name": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        "model_id": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        "provider": "SiliconFlow",
    },
    "sf_qwen_moe": {
        "display_name": "Qwen/Qwen3.5-397B-A17B",
        "model_id": "Qwen/Qwen3.5-397B-A17B",
        "provider": "SiliconFlow",
    },
}
MODEL_ORDER = tuple(MODEL_META)

RUN_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "run_id": "v3_final_common778_claude_thinklow",
        "git_commit": HISTORICAL_COMMIT,
        "models": ("bedrock_opus48", "bedrock_sonnet46"),
        "provenance": "pre-v2 private Git artifact",
        "settings": "Bedrock adaptive thinking, effort=low",
    },
    {
        "run_id": "v3_final_common778_gemini_thinklow",
        "git_commit": HISTORICAL_COMMIT,
        "models": ("gemini_flash", "gemini_pro"),
        "provenance": "pre-v2 private Git artifact",
        "settings": "reasoning_effort=low",
    },
    {
        "run_id": "v3_final_common778_minimaxm3_think",
        "git_commit": HISTORICAL_COMMIT,
        "models": ("minimax_m3",),
        "provenance": "pre-v2 private Git artifact",
        "settings": "adaptive thinking; original adapter no longer present",
    },
    {
        "run_id": "v3_sf_nothink_qwen_778_4cond_20260618_2346",
        "git_commit": None,
        "models": ("sf_qwen_coder30b", "sf_qwen_moe"),
        "provenance": "777-task archive plus separately fingerprinted T0466 supplement",
        "settings": "historical run labeled nothink; exact provider sampling seed not persisted",
    },
)

METRIC_FIELDS = (
    "release_id",
    "task_id",
    "family",
    "source",
    "execution_mode",
    "model_alias",
    "model_display_name",
    "requested_model_id",
    "provider",
    "condition",
    "passed",
    "input_tokens",
    "output_tokens",
    "thoughts_tokens",
    "total_tokens",
    "opus_gene_source",
    "subset",
    "subset_memberships",
    "source_run_id",
    "source_segment",
    "protocol",
    "runner_seed",
    "provider_sampling_seed",
    "trial_count",
    "manifest_sha256",
    "skill_asset_set_sha256",
    "opus_gene_asset_set_sha256",
    "gemini_gene_asset_set_sha256",
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_csv(path: Path, fields: Sequence[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(fields), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def source_bytes(
    path: str, git_commit: str | None, source_root: Path | None = None
) -> bytes:
    """Read a private source from the checkout or its pinned Git object.

    ``path`` is supplied by the private source inventory at runtime rather
    than embedded in this public module.  That keeps the aggregation logic
    reproducible without publishing an authoring checkout layout.
    """
    requested = Path(path)
    local = requested if requested.is_absolute() else (source_root or ROOT) / requested
    if local.is_file():
        return local.read_bytes()
    if not git_commit:
        raise SystemExit(f"missing private research source: {path}")
    try:
        return subprocess.check_output(
            ["git", "show", f"{git_commit}:{path}"], cwd=ROOT
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"cannot recover private research source {path} from {git_commit}"
        ) from exc


def load_private_sources(path: Path | None = None) -> dict[str, Any]:
    """Load and validate the metadata-only private source inventory.

    The inventory contains repository-relative paths, byte sizes, and hashes;
    it contains no result payloads.  Older inventories (including
    ``historical-runs.v1``) are accepted as-is.  The final Qwen archive is
    resolved from the private checkout when it is not represented by that
    historical inventory.
    """
    requested = path
    if requested is None:
        configured = os.environ.get(PRIVATE_SOURCES_ENV, "").strip()
        if configured:
            requested = Path(configured)
        else:
            requested = (
                DEFAULT_PRIVATE_SOURCES
                if DEFAULT_PRIVATE_SOURCES.is_file()
                else LEGACY_PRIVATE_SOURCES
            )
    if not requested.is_absolute():
        requested = ROOT / requested
    if not requested.is_file():
        return {
            "version": "filesystem-discovery",
            "source_git_commit": None,
            "source_root": ROOT,
            "files": [],
        }
    try:
        payload = json.loads(requested.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid private source inventory {requested}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"private source inventory must be an object: {requested}")
    records = payload.get("files", [])
    if not isinstance(records, list):
        raise SystemExit(f"private source inventory files must be an array: {requested}")
    normalized: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise SystemExit(f"invalid private source record in {requested}")
        candidate = PurePosixPath(record["path"].replace("\\", "/"))
        if candidate.is_absolute() or ".." in candidate.parts:
            raise SystemExit(f"unsafe private source path in {requested}: {record['path']!r}")
        normalized.append(dict(record, path=candidate.as_posix()))
    return {
        "version": str(payload.get("version") or "private-sources.v1"),
        "source_git_commit": payload.get("source_git_commit"),
        "source_root": _source_root(payload, requested.parent),
        "files": normalized,
    }


def _source_root(payload: dict[str, Any], inventory_parent: Path) -> Path:
    configured = os.environ.get(PRIVATE_SOURCE_ROOT_ENV, "").strip()
    raw = configured or payload.get("source_root")
    if not raw:
        return ROOT
    candidate = Path(str(raw)).expanduser()
    if not candidate.is_absolute():
        candidate = inventory_parent / candidate
    return candidate.resolve()


def _source_record(inventory: dict[str, Any], path: str) -> dict[str, Any] | None:
    for record in inventory.get("files", []):
        if isinstance(record, dict) and record.get("path") == path:
            return record
    return None


def _private_source_candidates(group: dict[str, Any], filename: str,
                               inventory: dict[str, Any]) -> list[str]:
    """Return deterministic candidate paths for one run artifact.

    Historical files are first resolved through the inventory so a missing
    checkout can be recovered from the pinned Git commit.  A private archive
    that is intentionally outside that inventory is discovered by its stable
    run identifier and filename; its bytes are still hashed in the generated
    registry.
    """
    run_id = str(group["run_id"])
    candidates = []
    for record in inventory.get("files", []):
        if not isinstance(record, dict):
            continue
        raw_path = record.get("path")
        if not isinstance(raw_path, str):
            continue
        parsed = PurePosixPath(raw_path)
        if parsed.name == filename and run_id in parsed.parts:
            candidates.append(parsed.as_posix())
    if candidates:
        return sorted(set(candidates))

    discovered: list[str] = []
    source_root = inventory.get("source_root")
    roots = [ROOT]
    if isinstance(source_root, Path) and source_root != ROOT:
        roots.append(source_root)
    for root in roots:
        for candidate in root.rglob(filename):
            try:
                relative = candidate.relative_to(root)
            except ValueError:
                continue
            if run_id in relative.parts and candidate.is_file():
                discovered.append(relative.as_posix())
    # Prefer the archive root.  Supplement directories may contain another
    # config/results pair for one task and are intentionally separate inputs.
    root_candidates = [
        value for value in discovered if PurePosixPath(value).parent.name == run_id
    ]
    return sorted(set(root_candidates or discovered))


def resolve_private_sources(group: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    """Resolve and hash-check the result/config pair for one private run."""
    resolved: dict[str, Any] = {}
    git_commit = group.get("git_commit") or inventory.get("source_git_commit")
    for kind, filename in (("results", "results.jsonl"), ("config", "config.json")):
        candidates = _private_source_candidates(group, filename, inventory)
        if not candidates:
            raise SystemExit(
                f"missing private {kind} source for {group['run_id']}; "
                f"provide it through {PRIVATE_SOURCES_ENV}"
            )
        if len(candidates) > 1:
            raise SystemExit(
                f"ambiguous private {kind} source for {group['run_id']}: {candidates}"
            )
        source_path = candidates[0]
        data = source_bytes(source_path, git_commit, inventory.get("source_root"))
        record = _source_record(inventory, source_path)
        if record is not None:
            expected_size = record.get("size")
            expected_sha = record.get("sha256")
            if isinstance(expected_size, int) and len(data) != expected_size:
                raise SystemExit(
                    f"private source size mismatch for {source_path}: "
                    f"expected {expected_size}, got {len(data)}"
                )
            if isinstance(expected_sha, str) and sha256_bytes(data) != expected_sha:
                raise SystemExit(f"private source hash mismatch for {source_path}")
        resolved[kind] = {
            "path": source_path,
            "bytes": data,
            "record": record,
        }
    return resolved


def jsonl_rows(data: bytes, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(data.decode("utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise SystemExit(f"object required at {label}:{line_number}")
        rows.append(value)
    return rows


def latest_results(rows: Sequence[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = ((row.get("trial") or {}).get("trial_key"))
        if not isinstance(key, str) or not key:
            raise SystemExit(f"missing trial_key in {label}")
        by_key[key] = row
    return list(by_key.values())


def asset_set_digest(paths: Iterable[Path]) -> dict[str, Any]:
    selected = sorted({path.resolve() for path in paths})
    digest = hashlib.sha256()
    for path in selected:
        relative = path.relative_to(ROOT).as_posix()
        file_digest = sha256_file(path)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_digest.encode("ascii"))
        digest.update(b"\n")
    return {
        "algorithm": "sha256(relative_path NUL file_sha256 LF)",
        "file_count": len(selected),
        "sha256": digest.hexdigest(),
    }


def load_assets() -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, Any]]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    tasks = {row["task_id"]: row for row in manifest["tasks"]}
    if len(tasks) != 778 or sha256_file(MANIFEST) != MANIFEST_SHA256:
        raise SystemExit("manifest does not match research-v1")

    skill_paths: list[Path] = []
    for row in tasks.values():
        files = row.get("files") or {}
        skill_paths.append(ROOT / "tasks_final" / row["rel_dir"] / files.get("skill", "SKILL.md"))
    opus_paths = sorted((ROOT / "tasks_final" / "genes_opus48").glob("T*.json"))
    gemini_paths = sorted((ROOT / "tasks_final" / "genes_gemini31pro").glob("T*.json"))
    if len(skill_paths) != 778 or len(opus_paths) != 778 or len(gemini_paths) != 778:
        raise SystemExit("research-v1 expects 778 Skill, Opus Gene, and Gemini Gene assets")

    opus: dict[str, dict[str, Any]] = {}
    gemini: dict[str, dict[str, Any]] = {}
    for path in opus_paths:
        opus[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    for path in gemini_paths:
        gemini[path.stem] = json.loads(path.read_text(encoding="utf-8"))

    asset_sets = {
        "manifest": {"file_count": 1, "sha256": MANIFEST_SHA256},
        "skill": asset_set_digest(skill_paths),
        "opus_gene": asset_set_digest(opus_paths),
        "gemini_gene": asset_set_digest(gemini_paths),
    }
    return tasks, {"opus": opus, "gemini": gemini}, asset_sets


def build_subsets(
    tasks: dict[str, dict[str, Any]], genes: dict[str, dict[str, dict[str, Any]]]
) -> dict[str, Any]:
    all_ids = set(tasks)
    opus_sources: dict[str, set[str]] = defaultdict(set)
    for task_id, asset in genes["opus"].items():
        opus_sources[str(asset.get("generation_source") or "unknown")].add(task_id)
    gemini_evolved = {
        task_id
        for task_id, asset in genes["gemini"].items()
        if asset.get("generation_source") == "evolved"
    }
    definitions = {
        "full778": {
            "description": "All tasks in the frozen 778-task manifest.",
            "selection_rule": "task_id in research-v1 manifest",
            "task_ids": sorted(all_ids),
            "selection_bias": "None beyond benchmark construction and filtering.",
        },
        "opus_evolved252": {
            "description": "Tasks whose Opus Gene came from a verified successful rollout trajectory.",
            "selection_rule": "Opus Gene generation_source == evolved",
            "task_ids": sorted(opus_sources["evolved"]),
            "selection_bias": (
                "Selected on successful Opus exploration within the rollout budget; "
                "this is an easier, author-model-conditioned subset and is not representative of full778."
            ),
        },
        "opus_reference_distilled526": {
            "description": (
                "Non-evolved Opus Gene complement: reference-distilled tasks plus the "
                "legacy skill-distilled fallback task."
            ),
            "selection_rule": (
                "Opus Gene generation_source in {reference_distilled, skill_distilled}"
            ),
            "task_ids": sorted(
                opus_sources["reference_distilled"] | opus_sources["skill_distilled"]
            ),
            "selection_bias": (
                "Complementary non-evolved source class, not a randomized treatment group; "
                "comparisons against evolved tasks are not same-task causal estimates."
            ),
        },
        "common_evolved180": {
            "description": "Tasks evolved successfully by both Opus and Gemini Gene authors.",
            "selection_rule": "Opus evolved intersection Gemini evolved",
            "task_ids": sorted(opus_sources["evolved"] & gemini_evolved),
            "selection_bias": (
                "Conditioned on successful evolution by two author models; used only for paired Gene-author comparison."
            ),
        },
    }
    expected = {
        "full778": 778,
        "opus_evolved252": 252,
        "opus_reference_distilled526": 526,
        "common_evolved180": 180,
    }
    for name, count in expected.items():
        definitions[name]["task_count"] = len(definitions[name]["task_ids"])
        if definitions[name]["task_count"] != count:
            raise SystemExit(f"unexpected subset size for {name}")
    return {
        "schema_version": "taskgenome.subset-definitions.v1",
        "research_version": RESEARCH_VERSION,
        "release_id": RELEASE_ID,
        "definitions": definitions,
    }


def membership(task_id: str, subsets: dict[str, Any]) -> str:
    names = [
        name
        for name, definition in subsets["definitions"].items()
        if task_id in set(definition["task_ids"])
    ]
    return ";".join(names)


def build_metrics(
    tasks: dict[str, dict[str, Any]],
    genes: dict[str, dict[str, dict[str, Any]]],
    subsets: dict[str, Any],
    asset_sets: dict[str, Any],
    private_sources: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    output: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    seen_models: set[str] = set()
    inventory = load_private_sources(private_sources)
    for group in RUN_GROUPS:
        sources = resolve_private_sources(group, inventory)
        raw = sources["results"]["bytes"]
        config_raw = sources["config"]["bytes"]
        # Use the stable run identifier in diagnostics instead of exposing
        # the authoring checkout path in generated public artifacts.
        source_label = f"{group['run_id']}/results.jsonl"
        all_rows = jsonl_rows(raw, source_label)
        rows = latest_results(all_rows, source_label)
        artifacts.append(
            {
                "run_id": group["run_id"],
                "results_sha256": sha256_bytes(raw),
                "results_size": len(raw),
                "config_sha256": sha256_bytes(config_raw),
                "config_size": len(config_raw),
                "git_commit": group["git_commit"],
                "raw_record_count": len(all_rows),
                "logical_record_count": len(rows),
                "duplicate_records_resolved_by_latest_trial_key": len(all_rows) - len(rows),
                "provenance": group["provenance"],
            }
        )
        models = {str((row.get("trial") or {}).get("model")) for row in rows}
        if models != set(group["models"]):
            raise SystemExit(f"model mismatch in {group['run_id']}: {models}")
        seen_models.update(models)
        by_model_condition: Counter[tuple[str, str]] = Counter()
        for row in rows:
            trial = row.get("trial") or {}
            task_id = str(trial.get("task_id"))
            model = str(trial.get("model"))
            condition = str(trial.get("condition"))
            if task_id not in tasks or condition not in CONDITIONS or model not in MODEL_META:
                raise SystemExit(f"unexpected trial identity in {group['run_id']}")
            by_model_condition[(model, condition)] += 1
            tokens = row.get("tokens") or {}
            input_tokens = int(tokens.get("input") or 0)
            output_tokens = int(tokens.get("output") or 0)
            thoughts_tokens = int(tokens.get("thoughts") or 0)
            source_segment = "historical"
            source_run_id = group["run_id"]
            if group["run_id"].startswith("v3_sf_nothink"):
                if task_id == "T0466":
                    source_segment = "t0466_supplement_20260718"
                    source_run_id = "t0466_siliconflow_legacy_v1_20260718"
                else:
                    source_segment = "historical_777"
            meta = MODEL_META[model]
            task = tasks[task_id]
            output.append(
                {
                    "release_id": RELEASE_ID,
                    "task_id": task_id,
                    "family": task["family"],
                    "source": task["source"],
                    "execution_mode": task["execution_mode"],
                    "model_alias": model,
                    "model_display_name": meta["display_name"],
                    "requested_model_id": meta["model_id"],
                    "provider": meta["provider"],
                    "condition": condition,
                    "passed": 1 if (row.get("eval") or {}).get("passed") else 0,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "thoughts_tokens": thoughts_tokens,
                    "total_tokens": input_tokens + output_tokens + thoughts_tokens,
                    "opus_gene_source": str(genes["opus"][task_id].get("generation_source")),
                    "subset": {
                        "evolved": "opus_evolved252",
                        "reference_distilled": "opus_reference_distilled526",
                        "skill_distilled": "opus_reference_distilled526",
                    }[str(genes["opus"][task_id].get("generation_source"))],
                    "subset_memberships": membership(task_id, subsets),
                    "source_run_id": source_run_id,
                    "source_segment": source_segment,
                    "protocol": "legacy-v1",
                    "runner_seed": 42,
                    "provider_sampling_seed": "not_recorded",
                    "trial_count": 1,
                    "manifest_sha256": MANIFEST_SHA256,
                    "skill_asset_set_sha256": asset_sets["skill"]["sha256"],
                    "opus_gene_asset_set_sha256": asset_sets["opus_gene"]["sha256"],
                    "gemini_gene_asset_set_sha256": asset_sets["gemini_gene"]["sha256"],
                }
            )
        for model in group["models"]:
            for condition in CONDITIONS:
                if by_model_condition[(model, condition)] != 778:
                    raise SystemExit(
                        f"incomplete logical run: {group['run_id']} {model} {condition}"
                    )
    if seen_models != set(MODEL_META) or len(output) != 7 * 4 * 778:
        raise SystemExit("research-v1 requires seven complete four-condition model runs")
    output.sort(
        key=lambda row: (
            MODEL_ORDER.index(row["model_alias"]),
            CONDITIONS.index(row["condition"]),
            row["task_id"],
        )
    )
    return output, artifacts


def build_registry(
    artifacts: list[dict[str, Any]], asset_sets: dict[str, Any]
) -> dict[str, Any]:
    artifact_by_run = {row["run_id"]: row for row in artifacts}
    experiments: list[dict[str, Any]] = []
    for group in RUN_GROUPS:
        for model in group["models"]:
            meta = MODEL_META[model]
            caveats = [
                "Single recorded trial per task-condition; provider sampling seed was not persisted.",
                "Requested/configured model ID is not a provider-returned immutable weight revision.",
                "Artifact aggregation is reproducible; exact hosted-model rerun parity is not claimed.",
            ]
            if model == "minimax_m3":
                caveats.append("Original MiniMax M3 adapter is absent from the current runner.")
            if model.startswith("sf_qwen"):
                caveats.append(
                    "T0466 was recorded on 2026-07-18 as a separately fingerprinted legacy-v1 supplement; all four conditions failed."
                )
            experiments.append(
                {
                    "experiment_id": f"{RESEARCH_VERSION}:{model}:full778",
                    "model_alias": model,
                    "model_display_name": meta["display_name"],
                    "requested_model_id": meta["model_id"],
                    "provider": meta["provider"],
                    "run_id": group["run_id"],
                    "conditions": list(CONDITIONS),
                    "subset": "full778",
                    "task_count": 778,
                    "logical_trial_count": 3112,
                    "trials_per_task_condition": 1,
                    "runner_seed": 42,
                    "provider_sampling_seed": "not_recorded",
                    "prompt_protocol": "legacy-v1",
                    "scoring_protocol": "legacy-v1",
                    "runtime_protocol": "legacy-v1",
                    "settings": group["settings"],
                    "manifest_sha256": MANIFEST_SHA256,
                    "skill_asset_set_sha256": asset_sets["skill"]["sha256"],
                    "opus_gene_asset_set_sha256": asset_sets["opus_gene"]["sha256"],
                    "gemini_gene_asset_set_sha256": asset_sets["gemini_gene"]["sha256"],
                    "raw_artifact": artifact_by_run[group["run_id"]],
                    "report_status": "included_with_caveats",
                    "caveats": caveats,
                }
            )
    return {
        "schema_version": "taskgenome.experiment-registry.v1",
        "research_version": RESEARCH_VERSION,
        "release_id": RELEASE_ID,
        "historical_protocol_source": {
            "git_commit": HISTORICAL_COMMIT,
            "run_official_sha256": HISTORICAL_RUNNER_SHA256,
            "api_sha256": HISTORICAL_API_SHA256,
            "manifest_sha256": MANIFEST_SHA256,
        },
        "aggregation_policy": (
            "Latest JSONL record per trial_key; all report experiments require exactly one "
            "logical record for every model/condition/task cell."
        ),
        "experiments": experiments,
    }


def parse_metric_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in read_csv(path):
        row: dict[str, Any] = dict(raw)
        for field in (
            "passed",
            "input_tokens",
            "output_tokens",
            "thoughts_tokens",
            "total_tokens",
            "runner_seed",
            "trial_count",
        ):
            row[field] = int(raw[field])
        rows.append(row)
    return rows


def ids_for(subsets: dict[str, Any], name: str) -> set[str]:
    return set(subsets["definitions"][name]["task_ids"])


def wilson(passed: int, n: int) -> tuple[float, float]:
    if not n:
        return 0.0, 0.0
    z = 1.959963984540054
    p = passed / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return 100 * (center - margin), 100 * (center + margin)


def historical_display_percent(passed: int, n: int) -> str:
    """Preserve the one-decimal display convention of historical summaries."""
    return f"{round(passed / n, 4) * 100:.1f}"


def aggregate_rows(
    rows: Sequence[dict[str, Any]], subsets: dict[str, Any]
) -> list[dict[str, Any]]:
    scopes = (
        "full778",
        "opus_evolved252",
        "opus_reference_distilled526",
        "common_evolved180",
    )
    output: list[dict[str, Any]] = []
    for scope in scopes:
        selected_ids = ids_for(subsets, scope)
        for model in MODEL_ORDER:
            for condition in CONDITIONS:
                selected = [
                    row
                    for row in rows
                    if row["model_alias"] == model
                    and row["condition"] == condition
                    and row["task_id"] in selected_ids
                ]
                passed = sum(row["passed"] for row in selected)
                n = len(selected)
                low, high = wilson(passed, n)
                tokens = sum(row["total_tokens"] for row in selected)
                output.append(
                    {
                        "research_version": RESEARCH_VERSION,
                        "subset": scope,
                        "model_alias": model,
                        "model_display_name": MODEL_META[model]["display_name"],
                        "condition": condition,
                        "n": n,
                        "passed": passed,
                        "pass_rate": f"{passed / n:.6f}",
                        "pass_rate_percent": historical_display_percent(passed, n),
                        "wilson_95_low_percent": f"{low:.2f}",
                        "wilson_95_high_percent": f"{high:.2f}",
                        "total_tokens": tokens,
                        "avg_tokens_per_task": f"{tokens / n:.2f}",
                        "tokens_per_passed_task": f"{tokens / passed:.2f}" if passed else "",
                        "protocol": "legacy-v1",
                        "trials_per_task_condition": 1,
                    }
                )
    return output


def track_rows(
    rows: Sequence[dict[str, Any]], subsets: dict[str, Any]
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for scope in ("full778", "opus_evolved252"):
        selected_ids = ids_for(subsets, scope)
        for family in FAMILIES:
            for model in MODEL_ORDER:
                for condition in CONDITIONS:
                    selected = [
                        row
                        for row in rows
                        if row["task_id"] in selected_ids
                        and row["family"] == family
                        and row["model_alias"] == model
                        and row["condition"] == condition
                    ]
                    passed = sum(row["passed"] for row in selected)
                    output.append(
                        {
                            "research_version": RESEARCH_VERSION,
                            "subset": scope,
                            "family": family,
                            "model_alias": model,
                            "model_display_name": MODEL_META[model]["display_name"],
                            "condition": condition,
                            "n": len(selected),
                            "passed": passed,
                            "pass_rate_percent": historical_display_percent(
                                passed, len(selected)
                            ),
                        }
                    )
    return output


def exact_mcnemar_p(gene_only: int, comparator_only: int) -> float:
    discordant = gene_only + comparator_only
    if discordant == 0:
        return 1.0
    tail = min(gene_only, comparator_only)
    probability = sum(math.comb(discordant, k) for k in range(tail + 1)) / (2**discordant)
    return min(1.0, 2 * probability)


def percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def stable_seed(*parts: str) -> int:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).digest()
    return BOOTSTRAP_SEED + int.from_bytes(digest[:4], "big")


def paired_stat_rows(
    rows: Sequence[dict[str, Any]], subsets: dict[str, Any]
) -> list[dict[str, Any]]:
    comparisons = (
        ("full778", "with_gene_opus", "with_skill"),
        ("opus_evolved252", "with_gene_opus", "with_skill"),
        ("opus_reference_distilled526", "with_gene_opus", "with_skill"),
        ("common_evolved180", "with_gene_opus", "with_gene_gemini"),
    )
    output: list[dict[str, Any]] = []
    for scope, treatment, comparator in comparisons:
        selected_ids = ids_for(subsets, scope)
        for model in MODEL_ORDER:
            maps: dict[str, dict[str, int]] = {}
            for condition in (treatment, comparator):
                maps[condition] = {
                    row["task_id"]: row["passed"]
                    for row in rows
                    if row["model_alias"] == model
                    and row["condition"] == condition
                    and row["task_id"] in selected_ids
                }
            if set(maps[treatment]) != selected_ids or set(maps[comparator]) != selected_ids:
                raise SystemExit(f"incomplete paired comparison {scope} {model}")
            differences = [
                maps[treatment][task_id] - maps[comparator][task_id]
                for task_id in sorted(selected_ids)
            ]
            n = len(differences)
            treatment_passed = sum(maps[treatment].values())
            comparator_passed = sum(maps[comparator].values())
            treatment_only = differences.count(1)
            comparator_only = differences.count(-1)
            rng_seed = stable_seed(scope, model, treatment, comparator)
            rng = random.Random(rng_seed)
            bootstrap = [
                100 * sum(rng.choices(differences, k=n)) / n
                for _ in range(BOOTSTRAP_ITERATIONS)
            ]
            output.append(
                {
                    "research_version": RESEARCH_VERSION,
                    "subset": scope,
                    "model_alias": model,
                    "model_display_name": MODEL_META[model]["display_name"],
                    "treatment": treatment,
                    "comparator": comparator,
                    "n": n,
                    "treatment_passed": treatment_passed,
                    "comparator_passed": comparator_passed,
                    "treatment_pass_rate_percent": historical_display_percent(
                        treatment_passed, n
                    ),
                    "comparator_pass_rate_percent": historical_display_percent(
                        comparator_passed, n
                    ),
                    "delta_pp": f"{100 * (treatment_passed - comparator_passed) / n:.2f}",
                    "paired_bootstrap_95_low_pp": f"{percentile(bootstrap, 0.025):.2f}",
                    "paired_bootstrap_95_high_pp": f"{percentile(bootstrap, 0.975):.2f}",
                    "treatment_only_pass": treatment_only,
                    "comparator_only_pass": comparator_only,
                    "mcnemar_exact_p": f"{exact_mcnemar_p(treatment_only, comparator_only):.8g}",
                    "bootstrap_iterations": BOOTSTRAP_ITERATIONS,
                    "bootstrap_seed": rng_seed,
                    "trials_per_task_condition": 1,
                    "uncertainty_scope": "task bootstrap only; excludes hosted-model rerun variance",
                }
            )
    return output


def evolution_metrics(
    tasks: dict[str, dict[str, Any]],
    genes: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    evolved = [
        (task_id, asset)
        for task_id, asset in genes["opus"].items()
        if asset.get("generation_source") == "evolved"
    ]
    calls = [
        call
        for _, asset in evolved
        for call in ((asset.get("evolve") or {}).get("calls") or [])
        if call.get("api_call")
    ]
    final_rounds = Counter(
        max(int(call.get("rollout") or 0) for call in ((asset.get("evolve") or {}).get("calls") or []))
        for _, asset in evolved
    )
    by_family: dict[str, dict[str, Any]] = {}
    for family in FAMILIES:
        family_assets = [
            asset for task_id, asset in evolved if tasks[task_id]["family"] == family
        ]
        family_calls = [
            call
            for asset in family_assets
            for call in ((asset.get("evolve") or {}).get("calls") or [])
            if call.get("api_call")
        ]
        input_tokens = sum(int(call.get("input_tokens") or 0) for call in family_calls)
        completion_tokens = sum(
            int(call.get("output_tokens") or 0) + int(call.get("thoughts_tokens") or 0)
            for call in family_calls
        )
        total_tokens = sum(int(call.get("total_tokens") or 0) for call in family_calls)
        by_family[family] = {
            "tasks": len(family_assets),
            "solve_mutate_calls": len(family_calls),
            "input_tokens": input_tokens,
            "completion_plus_thoughts_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "avg_tokens_per_call": f"{total_tokens / len(family_calls):.1f}",
            "avg_tokens_per_task": f"{total_tokens / len(family_assets):.1f}",
        }
    return {
        "schema_version": "taskgenome.gene-evolution-metrics.v1",
        "research_version": RESEARCH_VERSION,
        "gene_author_model": "global.anthropic.claude-opus-4-8",
        "task_subset": "opus_evolved252",
        "tasks": len(evolved),
        "verified_successful_trajectories": len(evolved),
        "solve_mutate_calls": len(calls),
        "total_tokens": sum(int(call.get("total_tokens") or 0) for call in calls),
        "tasks_by_success_round": {str(key): final_rounds[key] for key in sorted(final_rounds)},
        "by_family": by_family,
        "token_definition": "input_tokens + output_tokens + thoughts_tokens for solve/mutate rollout calls",
        "gene_distillation_tokens_included": False,
        "warning": (
            "This is a selected-on-success exploration baseline, not a no-context pass rate. "
            "Gene construction cost is excluded."
        ),
    }


def token_efficiency_rows(
    metrics: Sequence[dict[str, Any]], subsets: dict[str, Any], evolution: dict[str, Any]
) -> list[dict[str, Any]]:
    ids = ids_for(subsets, "opus_evolved252")
    output = [
        {
            "research_version": RESEARCH_VERSION,
            "mode": "opus_multi_round_exploration",
            "calls": evolution["solve_mutate_calls"],
            "tasks": evolution["tasks"],
            "passed": evolution["verified_successful_trajectories"],
            "pass_rate_percent": "100.0",
            "total_tokens": evolution["total_tokens"],
            "avg_tokens_per_task": f"{evolution['total_tokens'] / evolution['tasks']:.1f}",
            "tokens_per_passed_task": f"{evolution['total_tokens'] / evolution['verified_successful_trajectories']:.1f}",
            "cost_scope": "solve/mutate rollout only; Gene distillation excluded",
        }
    ]
    labels = {
        "no_context": "opus_no_context_single_call",
        "with_skill": "opus_with_skill_single_call",
        "with_gene_opus": "opus_with_opus_gene_single_call",
    }
    for condition in REPORT_CONDITIONS:
        selected = [
            row
            for row in metrics
            if row["model_alias"] == "bedrock_opus48"
            and row["condition"] == condition
            and row["task_id"] in ids
        ]
        passed = sum(row["passed"] for row in selected)
        tokens = sum(row["total_tokens"] for row in selected)
        output.append(
            {
                "research_version": RESEARCH_VERSION,
                "mode": labels[condition],
                "calls": len(selected),
                "tasks": len(selected),
                "passed": passed,
                "pass_rate_percent": historical_display_percent(passed, len(selected)),
                "total_tokens": tokens,
                "avg_tokens_per_task": f"{tokens / len(selected):.1f}",
                "tokens_per_passed_task": f"{tokens / passed:.1f}",
                "cost_scope": "single evaluation call; upstream Skill/Gene construction excluded",
            }
        )
    return output



def token_efficiency_by_track_rows(
    metrics: Sequence[dict[str, Any]],
    subsets: dict[str, Any],
    evolution: dict[str, Any],
) -> list[dict[str, Any]]:
    selected_ids = ids_for(subsets, "opus_evolved252")
    if "by_family" not in evolution:
        raise SystemExit("gene_evolution_metrics.json is missing by_family token detail")
    output: list[dict[str, Any]] = []
    for family in FAMILIES:
        selected = [
            row
            for row in metrics
            if row["task_id"] in selected_ids
            and row["family"] == family
            and row["model_alias"] == "bedrock_opus48"
            and row["condition"] == "with_gene_opus"
        ]
        explore = evolution["by_family"][family]
        gene_total = sum(row["total_tokens"] for row in selected)
        gene_avg = gene_total / len(selected)
        explore_avg = int(explore["total_tokens"]) / int(explore["tasks"])
        output.append(
            {
                "research_version": RESEARCH_VERSION,
                "family": family,
                "tasks": len(selected),
                "explore_calls": int(explore["solve_mutate_calls"]),
                "explore_total_tokens": int(explore["total_tokens"]),
                "explore_avg_tokens_per_task": f"{explore_avg:.1f}",
                "gene_passed": sum(row["passed"] for row in selected),
                "gene_total_tokens": gene_total,
                "gene_avg_tokens_per_task": f"{gene_avg:.1f}",
                "avg_token_change_percent": f"{100 * (gene_avg / explore_avg - 1):.1f}",
            }
        )
    explore_total = sum(int(row["explore_total_tokens"]) for row in output)
    gene_total = sum(int(row["gene_total_tokens"]) for row in output)
    task_total = sum(int(row["tasks"]) for row in output)
    output.append(
        {
            "research_version": RESEARCH_VERSION,
            "family": "all",
            "tasks": task_total,
            "explore_calls": sum(int(row["explore_calls"]) for row in output),
            "explore_total_tokens": explore_total,
            "explore_avg_tokens_per_task": f"{explore_total / task_total:.1f}",
            "gene_passed": sum(int(row["gene_passed"]) for row in output),
            "gene_total_tokens": gene_total,
            "gene_avg_tokens_per_task": f"{gene_total / task_total:.1f}",
            "avg_token_change_percent": f"{100 * (gene_total / explore_total - 1):.1f}",
        }
    )
    return output


def markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines.extend("| " + " | ".join(str(value) for value in row) + " |" for row in rows)
    return "\n".join(lines) + "\n"


def lookup_headline(
    headline: Sequence[dict[str, Any]], subset: str, model: str, condition: str
) -> dict[str, Any]:
    return next(
        row
        for row in headline
        if row["subset"] == subset
        and row["model_alias"] == model
        and row["condition"] == condition
    )


def result_table(headline: Sequence[dict[str, Any]], subset: str, conditions: Sequence[str]) -> str:
    rows = []
    for model in MODEL_ORDER:
        values = [MODEL_META[model]["display_name"]]
        for condition in conditions:
            row = lookup_headline(headline, subset, model, condition)
            values.append(f"{row['pass_rate_percent']}% ({row['passed']}/{row['n']})")
        rows.append(values)
    labels = {
        "no_context": "No context",
        "with_skill": "Skill",
        "with_gene_gemini": "Gemini Gene",
        "with_gene_opus": "Opus Gene",
    }
    return markdown_table(["Model", *[labels[value] for value in conditions]], rows)


def svg_evolved(headline: Sequence[dict[str, Any]]) -> str:
    width, height = 1180, 600
    left, top, chart_w, chart_h = 250, 80, 880, 430
    colors = {"no_context": "#94a3b8", "with_skill": "#3b82f6", "with_gene_opus": "#f97316"}
    labels = {"no_context": "No context", "with_skill": "Skill", "with_gene_opus": "Opus Gene"}
    group_h = chart_h / len(MODEL_ORDER)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<style>text{font-family:Inter,Arial,sans-serif;fill:#172033}.title{font-size:24px;font-weight:700}.small{font-size:13px}.model{font-size:14px}.axis{stroke:#cbd5e1;stroke-width:1}</style>',
        '<text x="40" y="38" class="title">Opus-evolved subset (252 tasks)</text>',
        '<text x="40" y="62" class="small">Strict task pass rate; one recorded trial per model-condition-task</text>',
    ]
    for tick in range(0, 101, 20):
        x = left + chart_w * tick / 100
        parts.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top+chart_h}" class="axis"/>')
        parts.append(f'<text x="{x:.1f}" y="{top+chart_h+24}" text-anchor="middle" class="small">{tick}%</text>')
    for index, model in enumerate(MODEL_ORDER):
        y0 = top + index * group_h
        parts.append(f'<text x="{left-12}" y="{y0+group_h/2+5:.1f}" text-anchor="end" class="model">{html.escape(MODEL_META[model]["display_name"])}</text>')
        for ci, condition in enumerate(REPORT_CONDITIONS):
            value = float(lookup_headline(headline, "opus_evolved252", model, condition)["pass_rate_percent"])
            y = y0 + 7 + ci * 14
            bar_w = chart_w * value / 100
            parts.append(f'<rect x="{left}" y="{y:.1f}" width="{bar_w:.1f}" height="11" rx="2" fill="{colors[condition]}"/>')
            parts.append(f'<text x="{left+bar_w+5:.1f}" y="{y+10:.1f}" class="small">{value:.1f}</text>')
    lx = left
    for condition in REPORT_CONDITIONS:
        parts.append(f'<rect x="{lx}" y="{height-36}" width="14" height="14" fill="{colors[condition]}"/>')
        parts.append(f'<text x="{lx+20}" y="{height-24}" class="small">{labels[condition]}</text>')
        lx += 150
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def svg_tokens(token_rows: Sequence[dict[str, Any]]) -> str:
    width, height = 1050, 390
    left, top, chart_w = 285, 75, 690
    max_tokens = max(int(row["total_tokens"]) for row in token_rows)
    colors = ("#64748b", "#94a3b8", "#3b82f6", "#f97316")
    labels = {
        "opus_multi_round_exploration": "Multi-round exploration",
        "opus_no_context_single_call": "No context, single call",
        "opus_with_skill_single_call": "Skill, single call",
        "opus_with_opus_gene_single_call": "Opus Gene, single call",
    }
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<style>text{font-family:Inter,Arial,sans-serif;fill:#172033}.title{font-size:24px;font-weight:700}.small{font-size:13px}.label{font-size:15px}</style>',
        '<text x="36" y="36" class="title">Opus token efficiency on 252 evolved tasks</text>',
        '<text x="36" y="59" class="small">Input + output + thoughts; Gene distillation cost is excluded</text>',
    ]
    for index, row in enumerate(token_rows):
        y = top + index * 68
        value = int(row["total_tokens"])
        bar_w = chart_w * value / max_tokens
        parts.append(f'<text x="{left-12}" y="{y+24}" text-anchor="end" class="label">{labels[row["mode"]]}</text>')
        parts.append(f'<rect x="{left}" y="{y+6}" width="{bar_w:.1f}" height="28" rx="4" fill="{colors[index]}"/>')
        parts.append(f'<text x="{left+bar_w+8:.1f}" y="{y+25}" class="small">{value:,} tokens · {row["passed"]}/252 passed</text>')
    parts.append("</svg>")
    return "\n".join(parts) + "\n"



def png_evolved(path: Path, headline: Sequence[dict[str, Any]]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    y = list(range(len(MODEL_ORDER)))
    offsets = (-0.24, 0.0, 0.24)
    colors = ("#94a3b8", "#3b82f6", "#f97316")
    labels = ("No context", "Skill", "Opus Gene")
    fig, ax = plt.subplots(figsize=(11.8, 6.2))
    for offset, color, label, condition in zip(offsets, colors, labels, REPORT_CONDITIONS):
        values = [
            float(lookup_headline(headline, "opus_evolved252", model, condition)["pass_rate_percent"])
            for model in MODEL_ORDER
        ]
        positions = [value + offset for value in y]
        bars = ax.barh(positions, values, height=0.21, color=color, label=label)
        ax.bar_label(bars, fmt="%.1f", padding=3, fontsize=8)
    ax.set_yticks(y, [MODEL_META[model]["display_name"] for model in MODEL_ORDER])
    ax.invert_yaxis()
    ax.set_xlim(0, 100)
    ax.set_xlabel("Strict task pass rate (%)")
    ax.set_title("Opus-evolved subset (252 tasks)", loc="left", weight="bold")
    ax.grid(axis="x", color="#e2e8f0", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.legend(ncols=3, frameon=False, loc="lower center", bbox_to_anchor=(0.5, -0.2))
    for spine in ax.spines.values():
        spine.set_visible(False)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=180, bbox_inches="tight", metadata={"Software": "TaskGenome Bench research-v1"})
    plt.close(fig)


def png_tokens(path: Path, token_rows: Sequence[dict[str, Any]]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    labels = {
        "opus_multi_round_exploration": "Multi-round exploration",
        "opus_no_context_single_call": "No context, single call",
        "opus_with_skill_single_call": "Skill, single call",
        "opus_with_opus_gene_single_call": "Opus Gene, single call",
    }
    names = [labels[row["mode"]] for row in token_rows]
    values = [int(row["total_tokens"]) for row in token_rows]
    annotations = [f"{value:,} tokens · {row['passed']}/252 passed" for value, row in zip(values, token_rows)]
    colors = ("#64748b", "#94a3b8", "#3b82f6", "#f97316")
    fig, ax = plt.subplots(figsize=(10.5, 4.8))
    bars = ax.barh(list(range(len(names))), values, color=colors, height=0.55)
    ax.set_yticks(list(range(len(names))), names)
    ax.invert_yaxis()
    ax.set_xlabel("Input + output + recorded thinking tokens")
    ax.set_title("Opus token efficiency on 252 evolved tasks", loc="left", weight="bold")
    ax.grid(axis="x", color="#e2e8f0", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.ticklabel_format(axis="x", style="plain")
    for bar, annotation in zip(bars, annotations):
        ax.text(bar.get_width() + max(values) * 0.015, bar.get_y() + bar.get_height() / 2, annotation, va="center", fontsize=9)
    ax.set_xlim(0, max(values) * 1.35)
    for spine in ax.spines.values():
        spine.set_visible(False)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=180, bbox_inches="tight", metadata={"Software": "TaskGenome Bench research-v1"})
    plt.close(fig)



def png_evolved_tracks(path: Path, tracks: Sequence[dict[str, Any]]) -> None:
    # Render the evolved-252 Skill/Gene comparison as a four-panel blog figure.
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    family_labels = {
        "agent_env_synth": "Agent environment (64 tasks)",
        "code_generation": "Code generation (62 tasks)",
        "math_reasoning": "Math reasoning (30 tasks)",
        "rule_following": "Rule following (96 tasks)",
    }
    model_labels = {
        "bedrock_opus48": "Claude Opus 4.8",
        "bedrock_sonnet46": "Claude Sonnet 4.6",
        "gemini_flash": "Gemini Flash-Lite",
        "gemini_pro": "Gemini Pro",
        "minimax_m3": "MiniMax M3",
        "sf_qwen_coder30b": "Qwen3-Coder 30B",
        "sf_qwen_moe": "Qwen3.5-397B",
    }
    colors = {"with_skill": "#3b82f6", "with_gene_opus": "#f97316"}
    positions = list(range(len(MODEL_ORDER)))

    fig, axes = plt.subplots(2, 2, figsize=(16, 10.2), sharex=True)
    for ax, family in zip(axes.flat, FAMILIES):
        skill = [
            float(
                lookup_track(
                    tracks, "opus_evolved252", family, model, "with_skill"
                )["pass_rate_percent"]
            )
            for model in MODEL_ORDER
        ]
        gene = [
            float(
                lookup_track(
                    tracks, "opus_evolved252", family, model, "with_gene_opus"
                )["pass_rate_percent"]
            )
            for model in MODEL_ORDER
        ]
        skill_positions = [value - 0.18 for value in positions]
        gene_positions = [value + 0.18 for value in positions]
        skill_bars = ax.barh(
            skill_positions,
            skill,
            height=0.32,
            color=colors["with_skill"],
            label="Skill",
        )
        gene_bars = ax.barh(
            gene_positions,
            gene,
            height=0.32,
            color=colors["with_gene_opus"],
            label="Opus Gene",
        )
        ax.bar_label(skill_bars, fmt="%.1f", padding=3, fontsize=8, color="#334155")
        ax.bar_label(gene_bars, fmt="%.1f", padding=3, fontsize=8, color="#334155")
        ax.set_yticks(positions, [model_labels[model] for model in MODEL_ORDER])
        ax.invert_yaxis()
        ax.set_xlim(0, 100)
        ax.set_title(family_labels[family], loc="left", weight="bold", fontsize=13)
        ax.grid(axis="x", color="#e2e8f0", linewidth=0.8)
        ax.set_axisbelow(True)
        ax.tick_params(axis="x", colors="#64748b")
        ax.tick_params(axis="y", labelsize=9, colors="#172033")
        for spine in ax.spines.values():
            spine.set_visible(False)

    fig.suptitle(
        "Fig.3.3 · Opus Gene vs Skill by task type",
        x=0.055,
        y=0.985,
        ha="left",
        fontsize=20,
        fontweight="bold",
        color="#172033",
    )
    fig.text(
        0.055,
        0.948,
        "Opus-evolved subset (252 tasks) · strict task pass rate (%)",
        ha="left",
        fontsize=11,
        color="#64748b",
    )
    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.legend(
        handles,
        labels,
        ncols=2,
        frameon=False,
        loc="upper right",
        bbox_to_anchor=(0.955, 0.975),
        fontsize=10,
    )
    fig.supxlabel("Strict task pass rate (%)", y=0.025, fontsize=11, color="#334155")
    fig.subplots_adjust(
        left=0.13,
        right=0.985,
        top=0.89,
        bottom=0.085,
        wspace=0.24,
        hspace=0.28,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        path,
        dpi=180,
        bbox_inches="tight",
        facecolor="white",
        metadata={"Software": "TaskGenome Bench research-v1"},
    )
    plt.close(fig)

def latex_escape(value: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in value)


def latex_command(name: str, lines: Sequence[str]) -> str:
    return "\n".join([f"\\newcommand{{\\{name}}}{{%", *lines, "}", ""])


def latex_result_tabular(
    headline: Sequence[dict[str, Any]],
    subset: str,
    conditions: Sequence[str],
    include_author_delta: bool = False,
) -> list[str]:
    labels = {
        "no_context": "No context",
        "with_skill": "Skill",
        "with_gene_gemini": "Gemini Gene",
        "with_gene_opus": "Opus Gene",
    }
    columns = "l" + "r" * (len(conditions) + int(include_author_delta))
    headers = ["Model", *[labels[value] for value in conditions]]
    if include_author_delta:
        headers.append(r"$\Delta$ Opus--Gemini")
    lines = [f"\\begin{{tabular}}{{{columns}}}", r"\toprule", " & ".join(headers) + r" \\", r"\midrule"]
    for model in MODEL_ORDER:
        rows = [lookup_headline(headline, subset, model, condition) for condition in conditions]
        rates = [float(row["pass_rate_percent"]) for row in rows]
        best = max(rates)
        cells = [latex_escape(MODEL_META[model]["display_name"])]
        for row, rate in zip(rows, rates):
            value = f"{row['pass_rate_percent']}\\%"
            cells.append(f"\\textbf{{{value}}}" if rate == best else value)
        if include_author_delta:
            opus = lookup_headline(headline, subset, model, "with_gene_opus")
            gemini = lookup_headline(headline, subset, model, "with_gene_gemini")
            delta = 100 * (int(opus["passed"]) - int(gemini["passed"])) / int(opus["n"])
            cells.append(f"{delta:+.1f}")
        lines.append(" & ".join(cells) + r" \\")
    lines.extend([r"\bottomrule", r"\end{tabular}"])
    return lines


def lookup_track(
    tracks: Sequence[dict[str, Any]], subset: str, family: str, model: str, condition: str
) -> dict[str, Any]:
    return next(
        row
        for row in tracks
        if row["subset"] == subset
        and row["family"] == family
        and row["model_alias"] == model
        and row["condition"] == condition
    )


def track_markdown_table(
    tracks: Sequence[dict[str, Any]], subset: str, conditions: Sequence[str]
) -> str:
    labels = {"no_context": "No context", "with_skill": "Skill", "with_gene_opus": "Opus Gene"}
    rows: list[list[Any]] = []
    for family in FAMILIES:
        for model in MODEL_ORDER:
            values = [
                lookup_track(tracks, subset, family, model, condition)["pass_rate_percent"]
                for condition in conditions
            ]
            rows.append([family, MODEL_META[model]["display_name"], *[f"{value}%" for value in values]])
    return markdown_table(["Track", "Model", *[labels[value] for value in conditions]], rows)


def latex_track_tabular(
    tracks: Sequence[dict[str, Any]], subset: str
) -> list[str]:
    family_labels = {
        "agent_env_synth": "Agent environment",
        "code_generation": "Code generation",
        "math_reasoning": "Math reasoning",
        "rule_following": "Rule following",
    }
    lines = [r"\begin{tabular}{llrrr}", r"\toprule", r"Track & Model & No context & Skill & Opus Gene \\", r"\midrule"]
    for family_index, family in enumerate(FAMILIES):
        if family_index:
            lines.append(r"\midrule")
        for model_index, model in enumerate(MODEL_ORDER):
            rows = [lookup_track(tracks, subset, family, model, condition) for condition in REPORT_CONDITIONS]
            rates = [float(row["pass_rate_percent"]) for row in rows]
            best = max(rates)
            cells = [family_labels[family] if model_index == 0 else "", latex_escape(MODEL_META[model]["display_name"])]
            for row, rate in zip(rows, rates):
                value = f"{row['pass_rate_percent']}\\%"
                cells.append(f"\\textbf{{{value}}}" if rate == best else value)
            lines.append(" & ".join(cells) + r" \\")
    lines.extend([r"\bottomrule", r"\end{tabular}"])
    return lines


def tech_report_tex(
    headline: Sequence[dict[str, Any]],
    tracks: Sequence[dict[str, Any]],
    stats: Sequence[dict[str, Any]],
    token_rows: Sequence[dict[str, Any]],
    token_track_rows: Sequence[dict[str, Any]],
    evolution: dict[str, Any],
) -> str:
    parts = ["% Generated by tools/research_results.py; do not edit manually.", ""]
    parts.append(latex_command("FullResultsTabular", latex_result_tabular(headline, "full778", REPORT_CONDITIONS)))
    parts.append(latex_command("EvolvedResultsTabular", latex_result_tabular(headline, "opus_evolved252", REPORT_CONDITIONS)))
    parts.append(latex_command("ReferenceResultsTabular", latex_result_tabular(headline, "opus_reference_distilled526", REPORT_CONDITIONS)))
    parts.append(latex_command("CommonResultsTabular", latex_result_tabular(headline, "common_evolved180", CONDITIONS, True)))

    evolved_stats = [row for row in stats if row["subset"] == "opus_evolved252"]
    stat_lines = [r"\begin{tabular}{lrrr}", r"\toprule", r"Model & $\Delta$ (pp) & Paired bootstrap 95\% CI & McNemar $p$ \\", r"\midrule"]
    for row in evolved_stats:
        stat_lines.append(
            f"{latex_escape(row['model_display_name'])} & {row['delta_pp']} & "
            f"[{row['paired_bootstrap_95_low_pp']}, {row['paired_bootstrap_95_high_pp']}] & "
            f"{row['mcnemar_exact_p']}" + r" \\"
        )
    stat_lines.extend([r"\bottomrule", r"\end{tabular}"])
    parts.append(latex_command("EvolvedStatsTabular", stat_lines))
    parts.append(latex_command("EvolvedTracksTabular", latex_track_tabular(tracks, "opus_evolved252")))
    parts.append(latex_command("FullTracksTabular", latex_track_tabular(tracks, "full778")))

    mode_labels = {
        "opus_multi_round_exploration": "Multi-round exploration",
        "opus_no_context_single_call": "No context, single call",
        "opus_with_skill_single_call": "Skill, single call",
        "opus_with_opus_gene_single_call": "Opus Gene, single call",
    }
    token_lines = [r"\begin{tabular}{lrrrrrr}", r"\toprule", r"Mode & Calls & Passed & Pass rate & Total tokens & Avg./task & Tokens/pass \\", r"\midrule"]
    for row in token_rows:
        token_lines.append(
            f"{mode_labels[row['mode']]} & {row['calls']} & {row['passed']} & {row['pass_rate_percent']}\\% & "
            f"{int(row['total_tokens']):,} & {row['avg_tokens_per_task']} & {row['tokens_per_passed_task']}" + r" \\"
        )
    token_lines.extend([r"\bottomrule", r"\end{tabular}"])
    parts.append(latex_command("TokenEfficiencyTabular", token_lines))

    round_lines = [r"\begin{tabular}{lrrrr}", r"\toprule", r"Passing round & Thinking & Tasks & Share & Additional calls \\", r"\midrule"]
    round_modes = {"1": "off", "2": "low", "3": "high"}
    for round_number, count in evolution["tasks_by_success_round"].items():
        share = 100 * int(count) / int(evolution["tasks"])
        additional = int(count) * (int(round_number) - 1)
        round_lines.append(f"{round_number} & {round_modes[round_number]} & {count} & {share:.1f}\\% & {additional}" + r" \\")
    round_lines.extend([r"\midrule", f"Total & off/low/high & {evolution['tasks']} & 100.0\\% & {int(evolution['solve_mutate_calls']) - int(evolution['tasks'])}" + r" \\", r"\bottomrule", r"\end{tabular}"])
    parts.append(latex_command("EvolutionDepthTabular", round_lines))

    family_labels = {
        "agent_env_synth": "Agent environment",
        "code_generation": "Code generation",
        "math_reasoning": "Math reasoning",
        "rule_following": "Rule following",
        "all": "Total",
    }
    track_token_lines = [r"\begin{tabular}{lrrrrrrrr}", r"\toprule", r"Track & Tasks & Explore calls & Explore total & Explore avg. & Gene passed & Gene total & Gene avg. & Change \\", r"\midrule"]
    for row in token_track_rows:
        if row["family"] == "all":
            track_token_lines.append(r"\midrule")
        track_token_lines.append(
            f"{family_labels[row['family']]} & {row['tasks']} & {row['explore_calls']} & {int(row['explore_total_tokens']):,} & "
            f"{row['explore_avg_tokens_per_task']} & {row['gene_passed']} & {int(row['gene_total_tokens']):,} & "
            f"{row['gene_avg_tokens_per_task']} & {row['avg_token_change_percent']}\\%" + r" \\"
        )
    track_token_lines.extend([r"\bottomrule", r"\end{tabular}"])
    parts.append(latex_command("TokenByTrackTabular", track_token_lines))
    return "\n".join(parts).rstrip() + "\n"


def write_tables(
    output: Path,
    headline: list[dict[str, Any]],
    tracks: list[dict[str, Any]],
    stats: list[dict[str, Any]],
    token_rows: list[dict[str, Any]],
    token_track_rows: list[dict[str, Any]],
    evolution: dict[str, Any],
) -> None:
    table_dir = output / "tables"
    table_dir.mkdir(parents=True, exist_ok=True)
    (table_dir / "full778.md").write_text(result_table(headline, "full778", REPORT_CONDITIONS), encoding="utf-8")
    (table_dir / "evolved252.md").write_text(result_table(headline, "opus_evolved252", REPORT_CONDITIONS), encoding="utf-8")
    (table_dir / "reference_distilled526.md").write_text(result_table(headline, "opus_reference_distilled526", REPORT_CONDITIONS), encoding="utf-8")
    (table_dir / "common180.md").write_text(result_table(headline, "common_evolved180", CONDITIONS), encoding="utf-8")
    (table_dir / "tracks_evolved252.md").write_text(
        track_markdown_table(tracks, "opus_evolved252", ("with_skill", "with_gene_opus")),
        encoding="utf-8",
    )
    (table_dir / "tracks_full778.md").write_text(
        track_markdown_table(tracks, "full778", REPORT_CONDITIONS),
        encoding="utf-8",
    )

    evolved_stats = [row for row in stats if row["subset"] == "opus_evolved252"]
    (table_dir / "statistical_tests.md").write_text(
        markdown_table(
            ["Model", "Delta pp", "Paired bootstrap 95% CI", "McNemar p"],
            [
                [
                    row["model_display_name"],
                    row["delta_pp"],
                    f"[{row['paired_bootstrap_95_low_pp']}, {row['paired_bootstrap_95_high_pp']}]",
                    row["mcnemar_exact_p"],
                ]
                for row in evolved_stats
            ],
        ),
        encoding="utf-8",
    )
    (table_dir / "token_efficiency.md").write_text(
        markdown_table(
            ["Mode", "Calls", "Passed", "Pass rate", "Total tokens", "Tokens / passed"],
            [
                [
                    row["mode"],
                    row["calls"],
                    row["passed"],
                    f"{row['pass_rate_percent']}%",
                    row["total_tokens"],
                    row["tokens_per_passed_task"],
                ]
                for row in token_rows
            ],
        ),
        encoding="utf-8",
    )
    (table_dir / "evolution_depth.md").write_text(
        markdown_table(
            ["Passing round", "Thinking", "Tasks", "Share", "Additional calls"],
            [
                [
                    round_number,
                    {"1": "off", "2": "low", "3": "high"}[round_number],
                    count,
                    f"{100 * int(count) / int(evolution['tasks']):.1f}%",
                    int(count) * (int(round_number) - 1),
                ]
                for round_number, count in evolution["tasks_by_success_round"].items()
            ],
        ),
        encoding="utf-8",
    )
    (table_dir / "token_efficiency_by_track.md").write_text(
        markdown_table(
            ["Track", "Tasks", "Explore calls", "Explore total", "Explore avg/task", "Gene passed", "Gene total", "Gene avg/task", "Change"],
            [
                [
                    row["family"], row["tasks"], row["explore_calls"], row["explore_total_tokens"],
                    row["explore_avg_tokens_per_task"], row["gene_passed"], row["gene_total_tokens"],
                    row["gene_avg_tokens_per_task"], f"{row['avg_token_change_percent']}%",
                ]
                for row in token_track_rows
            ],
        ),
        encoding="utf-8",
    )
    (table_dir / "tech_report_tables.tex").write_text(
        tech_report_tex(headline, tracks, stats, token_rows, token_track_rows, evolution),
        encoding="utf-8",
    )


def render_public(source: Path, output: Path) -> list[Path]:
    metrics = parse_metric_rows(source / "task_metrics.csv")
    subsets = json.loads((source / "subset_definitions.json").read_text(encoding="utf-8"))
    evolution = json.loads((source / "gene_evolution_metrics.json").read_text(encoding="utf-8"))
    headline = aggregate_rows(metrics, subsets)
    tracks = track_rows(metrics, subsets)
    stats = paired_stat_rows(metrics, subsets)
    token_rows = token_efficiency_rows(metrics, subsets, evolution)
    token_track_rows = token_efficiency_by_track_rows(metrics, subsets, evolution)

    headline_fields = tuple(headline[0])
    track_fields = tuple(tracks[0])
    stats_fields = tuple(stats[0])
    token_fields = tuple(token_rows[0])
    token_track_fields = tuple(token_track_rows[0])
    write_csv(output / "headline_results.csv", headline_fields, headline)
    write_csv(output / "track_results.csv", track_fields, tracks)
    write_csv(output / "statistical_tests.csv", stats_fields, stats)
    write_csv(output / "token_efficiency.csv", token_fields, token_rows)
    write_csv(output / "token_efficiency_by_track.csv", token_track_fields, token_track_rows)
    write_tables(output, headline, tracks, stats, token_rows, token_track_rows, evolution)
    figures = output / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    (figures / "evolved_results.svg").write_text(svg_evolved(headline), encoding="utf-8")
    (figures / "token_efficiency.svg").write_text(svg_tokens(token_rows), encoding="utf-8")
    png_evolved(figures / "evolved_results.png", headline)
    png_tokens(figures / "token_efficiency.png", token_rows)
    png_evolved_tracks(figures / "fig-3-3-opus-gene-vs-skill-by-task-type.png", tracks)
    return [
        output / "headline_results.csv",
        output / "track_results.csv",
        output / "statistical_tests.csv",
        output / "token_efficiency.csv",
        output / "token_efficiency_by_track.csv",
        figures / "evolved_results.svg",
        figures / "token_efficiency.svg",
        figures / "evolved_results.png",
        figures / "token_efficiency.png",
        figures / "fig-3-3-opus-gene-vs-skill-by-task-type.png",
        *sorted((output / "tables").glob("*")),
    ]


def research_readme() -> str:
    return """# TaskGenome Bench research-v1 results

This directory contains sanitized derived metrics only. It excludes raw model
responses, prompts, verifier stdout/stderr, private judges, gold outputs, and
reference solutions.

## Rebuild

Public/offline rendering from the checked-in sanitized task metrics:

```bash
python tools/research_results.py render
```

Private authoring rebuild, which additionally verifies the archived JSONL
sources and recreates `task_metrics.csv`. The source inventory is read from
`release/research_sources.v1.json` (falling back to the legacy historical
inventory) when present; a private checkout may pass a different
metadata-only inventory with `--private-sources`. If the inventory is kept
outside the checkout, set `TASKGENOME_PRIVATE_SOURCE_ROOT` (or its
`source_root` field) to the archive root:

```bash
python tools/research_results.py build
```

The public code export intentionally omits the private inventory and raw
archives. Run `build`/`verify` only in the private authoring checkout; the
public tree supports the offline `render` command from checked-in metrics.

Verify that a fresh private rebuild is byte-identical:

```bash
python tools/research_results.py verify
```

## Statistical scope

Pass-rate intervals in `headline_results.csv` are Wilson 95% intervals over
tasks. Paired comparisons use a deterministic paired task bootstrap and exact
McNemar test. They quantify task-sampling uncertainty only. Every reported
model/condition has one recorded trial per task, so hosted-model rerun variance
is not estimated.

`pass_rate` is the exact fraction rounded to six decimals. The display-only
`pass_rate_percent` preserves the historical run-summary convention: first
round the fraction to four decimals, then format it as a one-decimal percent.
The integer `passed` and `n` columns are authoritative.

## Interpretation limits

- `opus_evolved252` is selected on successful Opus exploration and is not a
  representative sample of the full benchmark.
- Evolved and reference-distilled subsets contain different tasks; their
  difference is not a same-task causal estimate of Gene construction method.
- Full-778 Gene results mix evolved and non-evolved (reference- or
  skill-distilled) assets.
- Token totals are input + output + recorded thoughts. Provider accounting is
  not perfectly homogeneous. Gene-generation token cost is excluded from
  single-call evaluation totals; the exploration baseline excludes the later
  Gene distillation call.
- Requested model IDs are not provider-returned immutable weight revisions.
- Historical pre-v2 runs support artifact-level re-aggregation but do not
  preserve a complete original package/environment fingerprint.
"""


def research_doc(asset_sets: dict[str, Any]) -> str:
    return f"""# Research v1 freeze

`research-v1` binds the reportable TaskGenome Bench results to Release ID
`{RELEASE_ID}` and manifest SHA-256 `{MANIFEST_SHA256}`.

## Frozen inputs

- Historical source commit: `{HISTORICAL_COMMIT}`
- Historical `run_official.py`: `{HISTORICAL_RUNNER_SHA256}`
- Historical `api.py`: `{HISTORICAL_API_SHA256}`
- Skill asset-set digest: `{asset_sets['skill']['sha256']}`
- Opus Gene asset-set digest: `{asset_sets['opus_gene']['sha256']}`
- Gemini Gene asset-set digest: `{asset_sets['gemini_gene']['sha256']}`
- Protocol: `legacy-v1` prompt, scoring, and runtime

The seven report experiments are listed in `results/experiment_registry.json`.
All are single recorded trials per task-condition and therefore carry the
registry's caveats. No result from a different protocol may be pooled into
these tables.

## Frozen subsets

- Full benchmark: 778 tasks
- Opus-evolved: 252 tasks
- Opus reference-distilled complement: 526 tasks (525 reference-distilled
  plus one legacy skill-distilled fallback)
- Common Opus/Gemini evolved overlap: 180 tasks

Exact task IDs and selection rules are in `results/subset_definitions.json`.

## Qwen completion

The original SiliconFlow Qwen archive covered 777 tasks and skipped T0466.
Eight T0466 trials were recorded on 2026-07-18 under `legacy-v1`; all completed
without API errors and all failed the strict verifier. The original archive and
supplement remain separate provenance segments inside the private run, while
the sanitized metrics expose a complete 778-task matrix.

## Known reproducibility limits

1. Hosted APIs are nondeterministic and provider model aliases are not immutable
   weight revisions.
2. Historical pre-v2 runs lack complete original environment and run
   fingerprints. MiniMax M3's original adapter is not in the current runner.
3. The evolved subset is selected on successful author-model exploration.
4. Evolved and reference-distilled subsets are different tasks, not randomized
   same-task treatments.
5. All report cells have one recorded trial; confidence intervals resample
   tasks and do not estimate model rerun variance.
6. Token comparisons exclude Gene distillation cost and inherit provider token
   accounting differences.

## Logic boundary

Stage A is a read-only research layer. It does not modify or invoke evaluation
logic, model adapters, prompt construction, scoring, task verifiers, the
manifest, Skills, or Genes. `tools/research_results.py` reads frozen verdicts
and emits only sanitized derived artifacts.
"""


def protected_hashes() -> dict[str, str]:
    return {
        "eval/run_official.py": sha256_file(ROOT / "eval" / "run_official.py"),
        "eval/api.py": sha256_file(ROOT / "eval" / "api.py"),
        "tasks_final/manifest.json": sha256_file(MANIFEST),
    }


def build_private(output: Path, private_sources: Path | None = None) -> None:
    tasks, genes, asset_sets = load_assets()
    subsets = build_subsets(tasks, genes)
    metrics, artifacts = build_metrics(
        tasks, genes, subsets, asset_sets, private_sources=private_sources
    )
    registry = build_registry(artifacts, asset_sets)
    evolution = evolution_metrics(tasks, genes)

    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "experiment_registry.json", registry)
    write_json(output / "subset_definitions.json", subsets)
    write_json(output / "gene_evolution_metrics.json", evolution)
    write_csv(output / "task_metrics.csv", METRIC_FIELDS, metrics)
    render_public(output, output)
    (output / "README.md").write_text(research_readme(), encoding="utf-8")

    generated = [
        path
        for path in output.rglob("*")
        if path.is_file() and path.name != "research_v1.json"
    ]
    freeze = {
        "schema_version": "taskgenome.research-freeze.v1",
        "research_version": RESEARCH_VERSION,
        "release_id": RELEASE_ID,
        "manifest_sha256": MANIFEST_SHA256,
        "historical_protocol_source": registry["historical_protocol_source"],
        "asset_sets": asset_sets,
        "report_experiment_ids": [row["experiment_id"] for row in registry["experiments"]],
        "protected_logic_files_at_freeze": protected_hashes(),
        "analysis_tool": {
            "path": "tools/research_results.py",
            "sha256": sha256_file(ROOT / "tools" / "research_results.py"),
        },
        "derived_artifacts": [
            {
                "path": f"results/{path.relative_to(output).as_posix()}",
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in sorted(generated)
        ],
        "known_limitations_document": "docs/RESEARCH_V1.md",
    }
    write_json(output / "research_v1.json", freeze)
    if output.resolve() == RESULTS.resolve():
        (ROOT / "docs" / "RESEARCH_V1.md").write_text(
            research_doc(asset_sets), encoding="utf-8"
        )


def verify_private(private_sources: Path | None = None) -> None:
    with tempfile.TemporaryDirectory(prefix="taskgenome-research-v1-") as directory:
        target = Path(directory) / "results"
        build_private(target, private_sources=private_sources)
        expected = sorted(
            path.relative_to(RESULTS)
            for path in RESULTS.rglob("*")
            if path.is_file()
        )
        actual = sorted(
            path.relative_to(target)
            for path in target.rglob("*")
            if path.is_file()
        )
        if expected != actual:
            raise SystemExit(f"artifact set mismatch: expected={expected} actual={actual}")
        for relative in expected:
            if (RESULTS / relative).read_bytes() != (target / relative).read_bytes():
                raise SystemExit(f"research artifact differs: {relative}")
    print("research-v1 private rebuild verified")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "render", "verify"))
    parser.add_argument("--source", type=Path, default=RESULTS)
    parser.add_argument("--output", type=Path, default=RESULTS)
    parser.add_argument(
        "--private-sources",
        type=Path,
        default=None,
        help=(
            "metadata-only private source inventory; defaults to "
            f"${PRIVATE_SOURCES_ENV} or the authoring checkout inventory"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "build":
        build_private(args.output.resolve(), private_sources=args.private_sources)
        print(f"research-v1 built at {args.output.resolve()}")
    elif args.command == "render":
        render_public(args.source.resolve(), args.output.resolve())
        print(f"research-v1 public tables and figures rendered at {args.output.resolve()}")
    else:
        verify_private(private_sources=args.private_sources)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

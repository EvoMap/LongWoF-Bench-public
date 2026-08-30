#!/usr/bin/env python3
"""Build deterministic public/private TaskGenome Bench asset bundles.

This tool is deliberately independent from the evaluation runner.  It treats
the canonical task manifest as immutable input and uses a fail-closed policy:
every discovered asset must be classified as public, private, or excluded.
"""

from __future__ import annotations

import argparse
import codecs
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


SCHEMA_VERSION = "1.0.0"
TASK_ID_RE = re.compile(r"^T\d{4}$")

POLICY_ROOT_KEYS = {
    "schema_version",
    "policy_name",
    "expected_task_count",
    "expected_manifest_sha256",
    "expected_ordered_task_ids_sha256",
    "expected_public_contexts",
    "sources",
    "public_safety",
    "rules",
}
POLICY_SOURCE_KEYS = {"scenario_root", "scan_non_official_scenarios", "collections"}
POLICY_COLLECTION_KEYS = {"name", "path", "task_file_regex"}
POLICY_SAFETY_KEYS = {
    "max_text_scan_bytes",
    "forbidden_asset_patterns",
    "forbidden_content_regexes",
}
POLICY_RULE_KEYS = {"id", "priority", "match", "classification"}
POLICY_MATCH_KEYS = {
    "scope",
    "collection",
    "collections",
    "family",
    "families",
    "execution_mode",
    "execution_modes",
    "task_id",
    "task_ids",
    "official",
    "has_task_id",
    "relpaths",
    "source_relpaths",
}
POLICY_CLASSIFICATION_KEYS = {
    "distribution",
    "role",
    "destination",
    "materialize",
    "transform",
}
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_MAX_TEXT_SCAN_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 100_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_OOXML_TRANSFORM_SOURCE_BYTES = 64 * 1024 * 1024
MAX_OOXML_TRANSFORM_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
ARCHIVE_SUFFIXES = {".zip", ".xlsx", ".docx", ".pptx", ".whl", ".jar"}
SUPPORTED_RELEASE_TRANSFORMS = {
    "strip_ooxml_abs_path",
    "sanitize_gene_provenance",
}
SANITIZED_GENE_PUBLIC_ROLES = {
    "context.gene_gemini31pro",
    "context.gene_opus48",
}
SANITIZED_OOXML_PUBLIC_ROLES = {
    "runtime.sanitized_ooxml_input",
    "environment.sanitized_ooxml_input",
}
PRIVATE_HOST_PATH_ROLE = "privacy.host_path_metadata"
OOXML_ABS_PATH_BLOCK_RE = re.compile(
    rb"<mc:AlternateContent\b[^>]*>\s*"
    rb"<mc:Choice\b[^>]*Requires=[\"']x15[\"'][^>]*>\s*"
    rb"<x15ac:absPath\b[^>]*/>\s*"
    rb"</mc:Choice>\s*</mc:AlternateContent>"
)
RELEASE_ASSET_RECORD_KEYS = {
    "bundle_path",
    "family",
    "execution_mode",
    "materialize",
    "mode",
    "role",
    "sha256",
    "size",
    "source_relpath",
    "task_id",
}
RELEASE_MANIFEST_KEYS = {
    "schema_version",
    "release_id",
    "bundle",
    "canonical",
    "policy_sha256",
    "asset_count",
    "asset_merkle_root",
    "bundle_roots",
    "assets",
}
RELEASE_CANONICAL_KEYS = {
    "manifest_sha256",
    "ordered_task_ids_sha256",
    "task_count",
    "first_task_id",
    "last_task_id",
    "summary",
}
RELEASE_SUMMARY_KEYS = {
    "total_tasks",
    "by_family",
    "by_execution_mode",
    "by_source",
}

DEFAULT_FORBIDDEN_ASSET_PATTERNS = (
    "*/test_script.py",
    "*/SKILL_oracle.md",
    "*/reference_solution.py",
    "*/reference_solution.sh",
    "*/_gold",
    "*/_gold/**",
    "*/_gold.json",
    "*/_fixtures",
    "*/_fixtures/**",
    "*/_run_record.json",
    "*/_variants.json",
    "*/_trace*",
    "*/_evolve_log.jsonl",
    "*/_rewrite_log.jsonl",
    "*/_agent_skill_log.jsonl",
    "*/expected*",
    "*/oracle*",
    "*/groundtruth",
    "*/groundtruth/**",
    "*/verification_params.json",
    "*/T0494/data/solve.py",
    "*/T0494/data/validate.py",
    "*/T0494/environment/solve.py",
    "*/T0494/environment/validate.py",
    "*/skill/SKILL.md",
    "*/skill/**/SKILL.md",
)

# These patterns target host-local paths and credential material.  The /data
# rule is intentionally user-name agnostic while requiring the benchmark's
# scratch/repository marker, so ordinary scientific URLs such as /data/MROM
# remain valid public task content.  Container contract paths such as
# /root/data or /tests are intentionally not rejected.
DEFAULT_FORBIDDEN_CONTENT_REGEXES = (
    r"/data/[A-Za-z0-9._-]+/(?:_[A-Za-z0-9.-]*tmp|gene[_-]?bench(?:[_/]|$)|longwof(?:[_/]|$))",
    r"/home/[A-Za-z0-9._-]+/",
    r"/Users/[A-Za-z0-9._-]+/",
    r"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+\\",
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    r'"private_key"\s*:',
    r'"client_email"\s*:\s*"[^"\n]+@[^"\n]+"',
    r"\bAIza[0-9A-Za-z_-]{20,}\b",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\b(?:sk|rk)-[A-Za-z0-9_-]{20,}\b",
    r"gen-lang-client-[0-9-]+-[0-9a-f]+\.json",
)


class ReleaseError(RuntimeError):
    """User-facing release construction error."""


@dataclass(frozen=True)
class Asset:
    source_path: Path
    source_relpath: str
    scope: str
    collection: str | None
    task_id: str | None
    official: bool | None
    family: str | None
    execution_mode: str | None
    relpath: str
    kind: str
    size: int
    sha256: str
    mode: str


@dataclass(frozen=True)
class ClassifiedAsset:
    asset: Asset
    rule_id: str
    distribution: str
    role: str
    bundle_path: str | None
    materialize: bool
    transform: str | None = None


@dataclass
class AuditOutcome:
    manifest: dict[str, Any]
    policy: dict[str, Any]
    manifest_sha256: str
    policy_sha256: str
    ordered_task_ids_sha256: str
    task_rows: list[dict[str, Any]]
    assets: list[Asset]
    classified: list[ClassifiedAsset]
    report: dict[str, Any]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ordered_ids_sha256(task_ids: Iterable[str]) -> str:
    payload = "".join(f"{task_id}\n" for task_id in task_ids).encode("utf-8")
    return _sha256_bytes(payload)


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(payload))


def _print_json(payload: Any, *, stream: Any | None = None) -> None:
    (stream or sys.stdout).write(_json_bytes(payload).decode("utf-8"))


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReleaseError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"invalid JSON in {label}: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReleaseError(f"{label} must be a JSON object: {path}")
    return payload


def load_validated_asset_policy(path: Path) -> dict[str, Any]:
    """Load the release policy used as the hardened runtime trust boundary."""

    policy = _load_json(path, "asset policy")
    _validate_policy(policy)
    return policy


def _check_object_keys(
    value: dict[str, Any],
    allowed: set[str],
    label: str,
    *,
    required: set[str] | None = None,
) -> None:
    unknown = sorted(set(value) - allowed)
    missing = sorted((required or set()) - set(value))
    if unknown:
        raise ReleaseError(f"{label} has unknown keys: {', '.join(unknown)}")
    if missing:
        raise ReleaseError(f"{label} is missing keys: {', '.join(missing)}")


def _string_or_string_list(value: Any, label: str) -> list[str]:
    values = value if isinstance(value, list) else [value]
    if not values or not all(isinstance(item, str) and item for item in values):
        raise ReleaseError(f"{label} must be a non-empty string or array of non-empty strings")
    return list(values)


def _validate_policy(policy: dict[str, Any]) -> None:
    """Validate the executable policy contract without permissive coercions."""

    _check_object_keys(
        policy,
        POLICY_ROOT_KEYS,
        "asset policy",
        required={"schema_version", "sources", "rules"},
    )
    if policy.get("schema_version") != SCHEMA_VERSION:
        raise ReleaseError(
            f"unsupported policy schema_version={policy.get('schema_version')!r}; expected {SCHEMA_VERSION}"
        )
    if "policy_name" in policy and not isinstance(policy["policy_name"], str):
        raise ReleaseError("asset policy policy_name must be a string")
    if "expected_task_count" in policy and (
        type(policy["expected_task_count"]) is not int or policy["expected_task_count"] < 0
    ):
        raise ReleaseError("asset policy expected_task_count must be a non-negative integer")
    for key in ("expected_manifest_sha256", "expected_ordered_task_ids_sha256"):
        if key in policy and not (
            isinstance(policy[key], str) and HEX64_RE.fullmatch(policy[key])
        ):
            raise ReleaseError(f"asset policy {key} must be a lowercase SHA-256")
    if "expected_public_contexts" in policy:
        contexts = policy["expected_public_contexts"]
        if not isinstance(contexts, dict):
            raise ReleaseError("asset policy expected_public_contexts must be an object")
        for role, count in contexts.items():
            if not isinstance(role, str) or not role or type(count) is not int or count < 0:
                raise ReleaseError(
                    "asset policy expected_public_contexts must map non-empty strings to non-negative integers"
                )

    sources = policy["sources"]
    if not isinstance(sources, dict):
        raise ReleaseError("asset policy sources must be an object")
    _check_object_keys(sources, POLICY_SOURCE_KEYS, "asset policy sources")
    if "scenario_root" in sources and not (
        isinstance(sources["scenario_root"], str) and sources["scenario_root"]
    ):
        raise ReleaseError("asset policy sources.scenario_root must be a non-empty string")
    if "scan_non_official_scenarios" in sources and type(
        sources["scan_non_official_scenarios"]
    ) is not bool:
        raise ReleaseError("asset policy sources.scan_non_official_scenarios must be a boolean")
    collections = sources.get("collections", [])
    if not isinstance(collections, list):
        raise ReleaseError("asset policy sources.collections must be an array")
    collection_names: set[str] = set()
    for index, spec in enumerate(collections):
        label = f"asset policy sources.collections[{index}]"
        if not isinstance(spec, dict):
            raise ReleaseError(f"{label} must be an object")
        _check_object_keys(
            spec,
            POLICY_COLLECTION_KEYS,
            label,
            required=POLICY_COLLECTION_KEYS,
        )
        if not all(isinstance(spec[key], str) and spec[key] for key in POLICY_COLLECTION_KEYS):
            raise ReleaseError(f"{label} fields must be non-empty strings")
        if spec["name"] in collection_names:
            raise ReleaseError(f"duplicate collection name: {spec['name']}")
        collection_names.add(spec["name"])
        try:
            compiled = re.compile(spec["task_file_regex"])
        except re.error as exc:
            raise ReleaseError(f"invalid {label}.task_file_regex: {exc}") from exc
        if compiled.groups < 1:
            raise ReleaseError(f"{label}.task_file_regex must contain a task-id capture group")

    safety = policy.get("public_safety", {})
    if not isinstance(safety, dict):
        raise ReleaseError("asset policy public_safety must be an object")
    _check_object_keys(safety, POLICY_SAFETY_KEYS, "asset policy public_safety")
    if "max_text_scan_bytes" in safety and (
        type(safety["max_text_scan_bytes"]) is not int or safety["max_text_scan_bytes"] <= 0
    ):
        raise ReleaseError("asset policy public_safety.max_text_scan_bytes must be a positive integer")
    for key in ("forbidden_asset_patterns", "forbidden_content_regexes"):
        values = safety.get(key, [])
        if not isinstance(values, list) or not all(
            isinstance(item, str) and item for item in values
        ):
            raise ReleaseError(f"asset policy public_safety.{key} must be an array of strings")
        if key == "forbidden_content_regexes":
            for pattern in values:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ReleaseError(f"invalid public safety regex {pattern!r}: {exc}") from exc

    rules = policy["rules"]
    if not isinstance(rules, list) or not rules:
        raise ReleaseError("asset policy rules must be a non-empty array")
    rule_ids: set[str] = set()
    string_match_keys = POLICY_MATCH_KEYS - {"official", "has_task_id"}
    for index, rule in enumerate(rules):
        label = f"asset policy rules[{index}]"
        if not isinstance(rule, dict):
            raise ReleaseError(f"{label} must be an object")
        _check_object_keys(
            rule,
            POLICY_RULE_KEYS,
            label,
            required=POLICY_RULE_KEYS,
        )
        rule_id = rule["id"]
        if not isinstance(rule_id, str) or not rule_id:
            raise ReleaseError(f"{label}.id must be a non-empty string")
        if rule_id in rule_ids:
            raise ReleaseError(f"duplicate asset policy rule id: {rule_id}")
        rule_ids.add(rule_id)
        if type(rule["priority"]) is not int:
            raise ReleaseError(f"{label}.priority must be an integer")
        match = rule["match"]
        if not isinstance(match, dict) or not match:
            raise ReleaseError(f"{label}.match must be a non-empty object")
        _check_object_keys(match, POLICY_MATCH_KEYS, f"{label}.match")
        for key in string_match_keys & set(match):
            values = _string_or_string_list(match[key], f"{label}.match.{key}")
            if key == "scope" and any(
                value not in {"canonical", "scenario", "collection"} for value in values
            ):
                raise ReleaseError(f"{label}.match.scope has an invalid value")
        for key in ("official", "has_task_id"):
            if key in match and type(match[key]) is not bool:
                raise ReleaseError(f"{label}.match.{key} must be a boolean")
        classification = rule["classification"]
        if not isinstance(classification, dict):
            raise ReleaseError(f"{label}.classification must be an object")
        _check_object_keys(
            classification,
            POLICY_CLASSIFICATION_KEYS,
            f"{label}.classification",
            required={"distribution", "role"},
        )
        distribution = classification["distribution"]
        if distribution not in {"public", "private", "excluded"}:
            raise ReleaseError(f"{label}.classification.distribution is invalid")
        if not isinstance(classification["role"], str) or not classification["role"]:
            raise ReleaseError(f"{label}.classification.role must be a non-empty string")
        if distribution != "excluded" and not (
            isinstance(classification.get("destination"), str)
            and classification["destination"]
        ):
            raise ReleaseError(f"{label}.classification.destination is required")
        if "materialize" in classification and type(classification["materialize"]) is not bool:
            raise ReleaseError(f"{label}.classification.materialize must be a boolean")
        transform = classification.get("transform")
        if transform is not None:
            if (
                not isinstance(transform, str)
                or transform not in SUPPORTED_RELEASE_TRANSFORMS
            ):
                raise ReleaseError(f"{label}.classification.transform is invalid")
            if distribution != "public":
                raise ReleaseError(
                    f"{label}.classification.transform is allowed only for public assets"
                )
            if classification.get("materialize") is not False:
                raise ReleaseError(
                    f"{label}.classification.transform requires materialize=false"
                )
            scope_values = _string_or_string_list(
                match.get("scope"), f"{label}.match.scope"
            )
            if transform == "strip_ooxml_abs_path":
                if scope_values != ["scenario"] or match.get("official") is not True:
                    raise ReleaseError(
                        f"{label}.classification.transform requires an official scenario match"
                    )
            elif transform == "sanitize_gene_provenance":
                if scope_values != ["collection"] or match.get("official") is not True:
                    raise ReleaseError(
                        f"{label}.classification.transform requires an official collection match"
                    )
                if classification.get("role") not in SANITIZED_GENE_PUBLIC_ROLES:
                    raise ReleaseError(
                        f"{label}.classification.transform has an invalid Gene public role"
                    )


def _safe_relative(value: str, label: str) -> str:
    value = value.replace("\\", "/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseError(f"unsafe {label}: {value!r}")
    return path.as_posix()


def _relative_to(path: Path, root: Path, label: str) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ReleaseError(f"{label} must be under pool root: {path}") from exc


def _lexical_relative_to(path: Path, root: Path, label: str) -> str:
    """Return an in-tree path without following its final symlink."""
    try:
        return path.absolute().relative_to(root.absolute()).as_posix()
    except ValueError as exc:
        raise ReleaseError(f"{label} must be under pool root: {path}") from exc


def _asset_from_path(
    path: Path,
    pool_root: Path,
    *,
    scope: str,
    collection: str | None,
    task_id: str | None,
    official: bool | None,
    family: str | None,
    execution_mode: str | None,
    relpath: str,
) -> Asset:
    source_relpath = _lexical_relative_to(path, pool_root, "asset")
    if path.is_symlink():
        info = path.lstat()
        return Asset(
            path,
            source_relpath,
            scope,
            collection,
            task_id,
            official,
            family,
            execution_mode,
            relpath,
            "symlink",
            info.st_size,
            "",
            "0000",
        )
    if not path.is_file():
        return Asset(
            path,
            source_relpath,
            scope,
            collection,
            task_id,
            official,
            family,
            execution_mode,
            relpath,
            "unsupported",
            0,
            "",
            "0000",
        )
    info = path.stat()
    executable = bool(info.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    return Asset(
        path,
        source_relpath,
        scope,
        collection,
        task_id,
        official,
        family,
        execution_mode,
        relpath,
        "file",
        info.st_size,
        _sha256_file(path),
        "0755" if executable else "0644",
    )


def _walk_files(root: Path) -> list[Path]:
    if not root.is_dir():
        raise ReleaseError(f"asset directory not found: {root}")
    found: list[Path] = []
    for path in root.rglob("*"):
        relpath = path.relative_to(root)
        if (
            any(part in {"__pycache__", ".pytest_cache"} for part in relpath.parts)
            or path.suffix.lower() in {".pyc", ".pyo"}
        ):
            continue
        if path.is_symlink() or path.is_file():
            found.append(path)
    return sorted(found, key=lambda item: item.relative_to(root).as_posix())


def _load_inputs(
    manifest_path: Path,
    pool_root: Path,
    policy_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], str, str, str]:
    # Validate the manifest target before reading it. This rejects a manifest
    # symlink that escapes the declared pool root.
    _relative_to(manifest_path, pool_root, "manifest")
    manifest = _load_json(manifest_path, "manifest")
    policy = _load_json(policy_path, "asset policy")
    _validate_policy(policy)
    rows = manifest.get("tasks")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ReleaseError("manifest must contain a tasks[] array of objects")
    task_rows = list(rows)
    manifest_sha = _sha256_file(manifest_path)
    policy_sha = _sha256_file(policy_path)
    task_ids = [str(row.get("task_id", "")) for row in task_rows]
    order_sha = _ordered_ids_sha256(task_ids)
    return manifest, policy, task_rows, manifest_sha, policy_sha, order_sha


def _inventory_assets(
    manifest_path: Path,
    pool_root: Path,
    policy: dict[str, Any],
    task_rows: list[dict[str, Any]],
) -> tuple[list[Asset], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    rows_by_id: dict[str, dict[str, Any]] = {}
    task_dirs: dict[str, Path] = {}

    for row in task_rows:
        task_id = str(row.get("task_id", ""))
        if not TASK_ID_RE.fullmatch(task_id):
            errors.append({"code": "invalid_task_id", "task_id": task_id})
            continue
        if task_id in rows_by_id:
            errors.append({"code": "duplicate_task_id", "task_id": task_id})
            continue
        rows_by_id[task_id] = row
        try:
            rel_dir = _safe_relative(str(row.get("rel_dir") or task_id), "task rel_dir")
        except ReleaseError as exc:
            errors.append({"code": "invalid_rel_dir", "task_id": task_id, "message": str(exc)})
            continue
        task_dir = pool_root / rel_dir
        if not task_dir.is_dir():
            errors.append({"code": "missing_task_dir", "task_id": task_id, "source_relpath": rel_dir})
            continue
        if task_dir.resolve() != task_dir.absolute():
            errors.append(
                {"code": "symlinked_task_dir", "task_id": task_id, "source_relpath": rel_dir}
            )
            continue
        task_dirs[task_id] = task_dir

    sources = policy.get("sources") or {}
    scenario_root_rel = _safe_relative(str(sources.get("scenario_root", "scenarios")), "scenario_root")
    scenario_root = pool_root / scenario_root_rel
    scan_non_official = bool(sources.get("scan_non_official_scenarios", True))

    scenario_dirs: dict[str, tuple[Path, bool]] = {
        task_id: (task_dir, True) for task_id, task_dir in task_dirs.items()
    }
    if scan_non_official:
        if not scenario_root.is_dir():
            errors.append({"code": "missing_scenario_root", "source_relpath": scenario_root_rel})
        elif scenario_root.resolve() != scenario_root.absolute():
            errors.append(
                {"code": "symlinked_scenario_root", "source_relpath": scenario_root_rel}
            )
        else:
            for child in sorted(scenario_root.iterdir(), key=lambda item: item.name):
                if not TASK_ID_RE.fullmatch(child.name) or child.name in scenario_dirs:
                    continue
                if child.is_symlink():
                    errors.append(
                        {
                            "code": "symlinked_non_official_task_dir",
                            "task_id": child.name,
                            "source_relpath": _lexical_relative_to(
                                child, pool_root, "non-official scenario"
                            ),
                        }
                    )
                    continue
                if child.is_dir():
                    scenario_dirs[child.name] = (child, False)

    assets: list[Asset] = []
    for task_id, (task_dir, official) in sorted(scenario_dirs.items()):
        row = rows_by_id.get(task_id, {})
        family = str(row.get("family")) if row.get("family") is not None else None
        execution_mode = (
            str(row.get("execution_mode")) if row.get("execution_mode") is not None else None
        )
        for path in _walk_files(task_dir):
            relpath = path.relative_to(task_dir).as_posix()
            assets.append(
                _asset_from_path(
                    path,
                    pool_root,
                    scope="scenario",
                    collection=None,
                    task_id=task_id,
                    official=official,
                    family=family,
                    execution_mode=execution_mode,
                    relpath=relpath,
                )
            )

    # Keep the original directory entries so `_asset_from_path` can identify
    # symlinks and fail closed. Resolving here would turn a symlink into an
    # apparently ordinary file and could move the sort key outside the pool.
    canonical_paths = [
        path for path in pool_root.iterdir() if path.is_file() or path.is_symlink()
    ]
    manifest_resolved = manifest_path.resolve()
    if not any(path.resolve() == manifest_resolved for path in canonical_paths):
        canonical_paths.append(manifest_path)
    for path in sorted(canonical_paths, key=lambda item: item.relative_to(pool_root).as_posix()):
        relpath = _lexical_relative_to(path, pool_root, "canonical file")
        assets.append(
            _asset_from_path(
                path,
                pool_root,
                scope="canonical",
                collection=None,
                task_id=None,
                official=None,
                family=None,
                execution_mode=None,
                relpath=relpath,
            )
        )

    collections = sources.get("collections") or []
    if not isinstance(collections, list):
        raise ReleaseError("policy sources.collections must be an array")
    for spec in collections:
        if not isinstance(spec, dict):
            raise ReleaseError("each collection source must be an object")
        name = str(spec.get("name", ""))
        if not name:
            raise ReleaseError("collection source is missing name")
        collection_rel = _safe_relative(str(spec.get("path", "")), f"collection {name} path")
        collection_root = pool_root / collection_rel
        if not collection_root.is_dir():
            errors.append(
                {
                    "code": "missing_collection_root",
                    "collection": name,
                    "source_relpath": collection_rel,
                }
            )
            continue
        if collection_root.resolve() != collection_root.absolute():
            errors.append(
                {
                    "code": "symlinked_collection_root",
                    "collection": name,
                    "source_relpath": collection_rel,
                }
            )
            continue
        task_file_regex = re.compile(str(spec.get("task_file_regex", r"^(T\d{4})\..+$")))
        for path in _walk_files(collection_root):
            relpath = path.relative_to(collection_root).as_posix()
            match = task_file_regex.fullmatch(relpath)
            task_id = match.group(1) if match and match.lastindex else None
            official: bool | None = None
            row: dict[str, Any] = {}
            if task_id is not None:
                official = task_id in rows_by_id
                row = rows_by_id.get(task_id, {})
            assets.append(
                _asset_from_path(
                    path,
                    pool_root,
                    scope="collection",
                    collection=name,
                    task_id=task_id,
                    official=official,
                    family=str(row.get("family")) if row.get("family") is not None else None,
                    execution_mode=(
                        str(row.get("execution_mode"))
                        if row.get("execution_mode") is not None
                        else None
                    ),
                    relpath=relpath,
                )
            )

    seen: dict[str, int] = Counter(asset.source_relpath for asset in assets)
    # The configured sources must cover the entire canonical pool. Without
    # this check, a newly added top-level directory could be silently omitted
    # from both release bundles and from the unclassified-file count.
    for path in _walk_files(pool_root):
        source_relpath = _lexical_relative_to(path, pool_root, "discovered asset")
        if source_relpath not in seen:
            errors.append(
                {
                    "code": "unscanned_asset_path",
                    "source_relpath": source_relpath,
                }
            )
    for source_relpath, count in sorted(seen.items()):
        if count > 1:
            errors.append(
                {"code": "duplicate_inventory_path", "source_relpath": source_relpath, "count": count}
            )
    assets.sort(key=lambda asset: asset.source_relpath)
    return assets, errors


def _one_or_many_matches(actual: str | None, expected: Any) -> bool:
    values = expected if isinstance(expected, list) else [expected]
    return actual in {str(value) for value in values}


def _matches_rule(asset: Asset, match: dict[str, Any]) -> bool:
    scalar_fields = {
        "scope": asset.scope,
        "collection": asset.collection,
        "family": asset.family,
        "execution_mode": asset.execution_mode,
        "task_id": asset.task_id,
    }
    for key, actual in scalar_fields.items():
        if key in match and not _one_or_many_matches(actual, match[key]):
            return False
    plural_fields = {
        "collections": asset.collection,
        "families": asset.family,
        "execution_modes": asset.execution_mode,
        "task_ids": asset.task_id,
    }
    for key, actual in plural_fields.items():
        if key in match and not _one_or_many_matches(actual, match[key]):
            return False
    if "official" in match and asset.official is not bool(match["official"]):
        return False
    if "has_task_id" in match and (asset.task_id is not None) is not bool(match["has_task_id"]):
        return False
    for key, value in (("relpaths", asset.relpath), ("source_relpaths", asset.source_relpath)):
        if key in match:
            patterns = match[key] if isinstance(match[key], list) else [match[key]]
            if not any(fnmatch.fnmatchcase(value, str(pattern)) for pattern in patterns):
                return False
    return True


def _render_destination(template: str, asset: Asset) -> str:
    values = {
        "task_id": asset.task_id or "",
        "relpath": asset.relpath,
        "collection": asset.collection or "",
        "name": PurePosixPath(asset.relpath).name,
        "source_relpath": asset.source_relpath,
    }
    try:
        rendered = template.format_map(values)
    except (KeyError, ValueError) as exc:
        raise ReleaseError(f"invalid destination template {template!r}: {exc}") from exc
    return _safe_relative(rendered, "bundle destination")


def _classify_assets(
    assets: list[Asset], policy: dict[str, Any]
) -> tuple[list[ClassifiedAsset], list[dict[str, Any]], list[dict[str, Any]]]:
    rules = policy.get("rules")
    if not isinstance(rules, list):
        raise ReleaseError("asset policy must contain rules[]")
    classified: list[ClassifiedAsset] = []
    unclassified: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    for asset in assets:
        matches: list[tuple[int, dict[str, Any]]] = []
        for rule in rules:
            if not isinstance(rule, dict) or not isinstance(rule.get("match"), dict):
                raise ReleaseError("every policy rule must be an object containing match{}")
            if _matches_rule(asset, rule["match"]):
                matches.append((int(rule.get("priority", 0)), rule))
        if not matches:
            unclassified.append(
                {
                    "source_relpath": asset.source_relpath,
                    "task_id": asset.task_id,
                    "scope": asset.scope,
                }
            )
            continue
        highest = max(priority for priority, _rule in matches)
        winners = [rule for priority, rule in matches if priority == highest]
        if len(winners) != 1:
            conflicts.append(
                {
                    "source_relpath": asset.source_relpath,
                    "priority": highest,
                    "rule_ids": sorted(str(rule.get("id", "")) for rule in winners),
                }
            )
            continue
        rule = winners[0]
        classification = rule.get("classification")
        if not isinstance(classification, dict):
            raise ReleaseError(f"rule {rule.get('id')!r} is missing classification{{}}")
        distribution = str(classification.get("distribution", ""))
        if distribution not in {"public", "private", "excluded"}:
            raise ReleaseError(
                f"rule {rule.get('id')!r} has invalid distribution={distribution!r}"
            )
        role = str(classification.get("role", ""))
        if not role:
            raise ReleaseError(f"rule {rule.get('id')!r} is missing classification.role")
        bundle_path: str | None = None
        if distribution != "excluded":
            destination = classification.get("destination")
            if not isinstance(destination, str) or not destination:
                raise ReleaseError(
                    f"rule {rule.get('id')!r} must define classification.destination"
                )
            bundle_path = _render_destination(destination, asset)
        default_materialize = distribution != "excluded" and (
            asset.official is True or asset.scope == "canonical"
        )
        materialize = bool(classification.get("materialize", default_materialize))
        transform = classification.get("transform")
        item = ClassifiedAsset(
            asset=asset,
            rule_id=str(rule.get("id", "")),
            distribution=distribution,
            role=role,
            bundle_path=bundle_path,
            materialize=materialize,
            transform=str(transform) if transform is not None else None,
        )
        classified.append(item)
        if item.transform is not None:
            original_destination = (
                "provenance/{collection}/{relpath}"
                if asset.scope == "collection"
                else "scenarios/{task_id}/{relpath}"
            )
            classified.append(
                ClassifiedAsset(
                    asset=asset,
                    rule_id=f"{item.rule_id}__private_original",
                    distribution="private",
                    role=PRIVATE_HOST_PATH_ROLE,
                    bundle_path=_render_destination(original_destination, asset),
                    materialize=True,
                    transform=None,
                )
            )
    classified.sort(
        key=lambda item: (
            item.asset.source_relpath,
            item.distribution,
            item.bundle_path or "",
        )
    )
    return classified, unclassified, conflicts


def _verified_source_bytes(asset: Asset) -> bytes:
    if asset.source_path.is_symlink() or not asset.source_path.is_file():
        raise ReleaseError(f"unsafe transform source: {asset.source_path}")
    if asset.size > MAX_OOXML_TRANSFORM_SOURCE_BYTES:
        raise ReleaseError(
            "release transform source exceeds bounded size limit: "
            f"{asset.source_relpath}: {asset.size} > {MAX_OOXML_TRANSFORM_SOURCE_BYTES}"
        )
    data = asset.source_path.read_bytes()
    if len(data) != asset.size or _sha256_bytes(data) != asset.sha256:
        raise ReleaseError(f"source changed after audit: {asset.source_path}")
    return data


def _strip_ooxml_abs_path(data: bytes, source_relpath: str) -> bytes:
    """Remove only Excel's host-local last-save path metadata."""

    source_buffer = io.BytesIO(data)
    output_buffer = io.BytesIO()
    try:
        with zipfile.ZipFile(source_buffer, "r") as source_archive:
            infos = source_archive.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise ReleaseError(
                    f"OOXML transform archive has too many members: {source_relpath}"
                )
            uncompressed_size = sum(info.file_size for info in infos)
            if uncompressed_size > MAX_OOXML_TRANSFORM_UNCOMPRESSED_BYTES:
                raise ReleaseError(
                    "OOXML transform archive exceeds bounded uncompressed size: "
                    f"{source_relpath}: {uncompressed_size} > "
                    f"{MAX_OOXML_TRANSFORM_UNCOMPRESSED_BYTES}"
                )
            workbook_infos = [
                info for info in infos if info.filename == "xl/workbook.xml"
            ]
            if len(workbook_infos) != 1:
                raise ReleaseError(
                    f"OOXML transform requires exactly one xl/workbook.xml: {source_relpath}"
                )
            if len({info.filename for info in infos}) != len(infos):
                raise ReleaseError(
                    f"OOXML transform refuses duplicate archive members: {source_relpath}"
                )
            transformed_entries: list[tuple[zipfile.ZipInfo, bytes]] = []
            removal_count = 0
            for info in infos:
                normalized_name = info.filename.replace("\\", "/")
                member = PurePosixPath(normalized_name)
                if (
                    not normalized_name
                    or member.is_absolute()
                    or ".." in member.parts
                    or stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK
                ):
                    raise ReleaseError(
                        f"OOXML transform refuses unsafe archive member: "
                        f"{source_relpath}!/{normalized_name}"
                    )
                if info.flag_bits & 0x1:
                    raise ReleaseError(
                        f"OOXML transform refuses encrypted archive members: {source_relpath}"
                    )
                entry_data = source_archive.read(info)
                if info.filename == "xl/workbook.xml":
                    entry_data, removal_count = OOXML_ABS_PATH_BLOCK_RE.subn(
                        b"", entry_data
                    )
                    if b"x15ac:absPath" in entry_data:
                        raise ReleaseError(
                            f"OOXML transform left absPath metadata behind: {source_relpath}"
                        )
                transformed_entries.append((info, entry_data))
            if removal_count < 1:
                raise ReleaseError(
                    f"OOXML transform found no absPath metadata: {source_relpath}"
                )
            with zipfile.ZipFile(output_buffer, "w") as output_archive:
                output_archive.comment = source_archive.comment
                for info, entry_data in transformed_entries:
                    # ZIP_STORED avoids zlib-version-dependent deflate bytes.
                    # OOXML readers accept stored entries, and the logical
                    # workbook content remains unchanged.
                    output_archive.writestr(
                        info,
                        entry_data,
                        compress_type=zipfile.ZIP_STORED,
                    )
    except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ReleaseError(
            f"failed OOXML transform for {source_relpath}: {type(exc).__name__}: {exc}"
        ) from exc
    return output_buffer.getvalue()


def _sanitize_gene_provenance(data: bytes, source_relpath: str) -> bytes:
    """Remove exploratory generation traces from a public final Gene asset.

    The final Gene payload is sufficient for benchmark replay.  The nested
    ``evolve`` object contains intermediate rollout metadata, token counts,
    mutation logs, and pointers to private trace files; none of that is part
    of the public benchmark contract.  Keep the stable Gene fields and emit
    canonical JSON so the transform is deterministic.
    """

    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(
            f"Gene provenance transform requires UTF-8 JSON: {source_relpath}"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("payload"), dict):
        raise ReleaseError(
            f"Gene provenance transform requires a Gene object with payload: {source_relpath}"
        )
    payload.pop("evolve", None)
    return _json_bytes(payload)


def _transformed_asset_bytes(item: ClassifiedAsset) -> bytes:
    if item.transform is None:
        raise ReleaseError("internal error: transformed bytes requested without a transform")
    source = _verified_source_bytes(item.asset)
    if item.transform == "strip_ooxml_abs_path":
        return _strip_ooxml_abs_path(source, item.asset.source_relpath)
    if item.transform == "sanitize_gene_provenance":
        return _sanitize_gene_provenance(source, item.asset.source_relpath)
    raise ReleaseError(f"unsupported release transform: {item.transform}")


def _effective_asset_sha_size(item: ClassifiedAsset) -> tuple[str, int]:
    if item.transform is None:
        return item.asset.sha256, item.asset.size
    data = _transformed_asset_bytes(item)
    return _sha256_bytes(data), len(data)


def _looks_textual_prefix(prefix: bytes) -> bool:
    return b"\x00" not in prefix


def _stream_regex_hits(
    handle: Any,
    prefix: bytes,
    compiled: list[tuple[str, re.Pattern[str]]],
    *,
    max_bytes: int,
) -> list[str]:
    """Scan decoded text without loading large public assets into memory."""

    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    hits: set[str] = set()
    tail = ""
    total = 0

    def consume(data: bytes, *, final: bool = False) -> None:
        nonlocal tail, total
        total += len(data)
        if total > max_bytes:
            raise OverflowError(f"text scan exceeds {max_bytes} bytes")
        text = tail + decoder.decode(data, final=final)
        for pattern, regex in compiled:
            if pattern not in hits and regex.search(text):
                hits.add(pattern)
        tail = text[-8192:]

    consume(prefix)
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        consume(chunk)
    consume(b"", final=True)
    return sorted(hits)


def _public_content_violations(
    path: Path,
    *,
    source_relpath: str | None,
    bundle_path: str,
    asset_patterns: list[str],
    compiled: list[tuple[str, re.Pattern[str]]],
    max_bytes: int,
) -> list[dict[str, Any]]:
    """Scan regular text and bounded archive members fail-closed."""

    violations: list[dict[str, Any]] = []

    def base(code: str, **extra: Any) -> dict[str, Any]:
        row: dict[str, Any] = {"code": code, "bundle_path": bundle_path, **extra}
        if source_relpath is not None:
            row["source_relpath"] = source_relpath
        return row

    if path.suffix.lower() in ARCHIVE_SUFFIXES:
        try:
            with zipfile.ZipFile(path) as archive:
                infos = sorted(archive.infolist(), key=lambda info: info.filename)
                if len(infos) > MAX_ARCHIVE_ENTRIES:
                    return [
                        base(
                            "public_archive_scan_limit_exceeded",
                            detail=f"entries={len(infos)} limit={MAX_ARCHIVE_ENTRIES}",
                        )
                    ]
                total_size = sum(info.file_size for info in infos)
                if total_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                    return [
                        base(
                            "public_archive_scan_limit_exceeded",
                            detail=(
                                f"uncompressed_bytes={total_size} "
                                f"limit={MAX_ARCHIVE_UNCOMPRESSED_BYTES}"
                            ),
                        )
                    ]
                for info in infos:
                    normalized_name = info.filename.replace("\\", "/")
                    member = PurePosixPath(normalized_name)
                    if (
                        not normalized_name
                        or member.is_absolute()
                        or ".." in member.parts
                        or stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK
                    ):
                        violations.append(
                            base(
                                "public_unsafe_archive_member",
                                archive_member=normalized_name,
                            )
                        )
                        continue
                    if info.is_dir():
                        continue
                    if info.flag_bits & 0x1:
                        violations.append(
                            base(
                                "public_encrypted_archive_member",
                                archive_member=normalized_name,
                            )
                        )
                        continue
                    virtual_paths = [f"{bundle_path}!/{member.as_posix()}"]
                    if source_relpath is not None:
                        virtual_paths.append(f"{source_relpath}!/{member.as_posix()}")
                    matched = next(
                        (
                            pattern
                            for pattern in asset_patterns
                            if any(
                                fnmatch.fnmatchcase(candidate, pattern)
                                for candidate in virtual_paths
                            )
                        ),
                        None,
                    )
                    if matched:
                        violations.append(
                            base(
                                "public_forbidden_archive_asset",
                                archive_member=normalized_name,
                                pattern=matched,
                            )
                        )
                    with archive.open(info) as handle:
                        prefix = handle.read(8192)
                        if not _looks_textual_prefix(prefix):
                            continue
                        if info.file_size > max_bytes:
                            violations.append(
                                base(
                                    "public_text_scan_limit_exceeded",
                                    archive_member=normalized_name,
                                    size=info.file_size,
                                    limit=max_bytes,
                                )
                            )
                            continue
                        for pattern in _stream_regex_hits(
                            handle, prefix, compiled, max_bytes=max_bytes
                        ):
                            violations.append(
                                base(
                                    "public_forbidden_archive_content",
                                    archive_member=normalized_name,
                                    pattern=pattern,
                                )
                            )
        except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            violations.append(
                base(
                    "public_archive_scan_error",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
        return violations

    try:
        with path.open("rb") as handle:
            prefix = handle.read(8192)
            if not _looks_textual_prefix(prefix):
                return violations
            size = path.stat().st_size
            if size > max_bytes:
                return [
                    base(
                        "public_text_scan_limit_exceeded",
                        size=size,
                        limit=max_bytes,
                    )
                ]
            for pattern in _stream_regex_hits(
                handle, prefix, compiled, max_bytes=max_bytes
            ):
                violations.append(base("public_forbidden_content", pattern=pattern))
    except (OSError, OverflowError) as exc:
        violations.append(
            base("public_content_scan_error", detail=f"{type(exc).__name__}: {exc}")
        )
    return violations


def _public_safety_violations(
    classified: list[ClassifiedAsset], policy: dict[str, Any]
) -> list[dict[str, Any]]:
    safety = policy.get("public_safety") or {}
    asset_patterns = list(DEFAULT_FORBIDDEN_ASSET_PATTERNS)
    asset_patterns.extend(str(item) for item in safety.get("forbidden_asset_patterns", []))
    content_patterns = list(DEFAULT_FORBIDDEN_CONTENT_REGEXES)
    content_patterns.extend(str(item) for item in safety.get("forbidden_content_regexes", []))
    compiled = [(pattern, re.compile(pattern, re.MULTILINE)) for pattern in content_patterns]
    max_bytes = int(safety.get("max_text_scan_bytes", DEFAULT_MAX_TEXT_SCAN_BYTES))
    violations: list[dict[str, Any]] = []

    for item in classified:
        if item.distribution != "public":
            continue
        asset = item.asset
        for candidate in (asset.source_relpath, item.bundle_path or ""):
            matched = next(
                (pattern for pattern in asset_patterns if fnmatch.fnmatchcase(candidate, pattern)),
                None,
            )
            if matched:
                violations.append(
                    {
                        "code": "public_forbidden_asset",
                        "source_relpath": asset.source_relpath,
                        "bundle_path": item.bundle_path,
                        "pattern": matched,
                    }
                )
                break
        if asset.kind == "file":
            if item.transform is None:
                violations.extend(
                    _public_content_violations(
                        asset.source_path,
                        source_relpath=asset.source_relpath,
                        bundle_path=item.bundle_path or "",
                        asset_patterns=asset_patterns,
                        compiled=compiled,
                        max_bytes=max_bytes,
                    )
                )
            else:
                transformed = _transformed_asset_bytes(item)
                with tempfile.NamedTemporaryFile(
                    prefix="taskgenome-public-scan-",
                    suffix=asset.source_path.suffix,
                ) as handle:
                    handle.write(transformed)
                    handle.flush()
                    violations.extend(
                        _public_content_violations(
                            Path(handle.name),
                            source_relpath=asset.source_relpath,
                            bundle_path=item.bundle_path or "",
                            asset_patterns=asset_patterns,
                            compiled=compiled,
                            max_bytes=max_bytes,
                        )
                    )
    return violations


def _sensitive_hash_overlaps(
    classified: list[ClassifiedAsset],
) -> list[dict[str, Any]]:
    """Find byte-identical public copies of private judge/reference material."""

    sensitive_prefixes = (
        "judge.",
        "reference.",
        "legacy.",
        "authoring.oracle",
        "privacy.",
    )
    public_by_sha: dict[str, list[ClassifiedAsset]] = defaultdict(list)
    private_by_sha: dict[str, list[ClassifiedAsset]] = defaultdict(list)
    for item in classified:
        if item.asset.kind != "file" or item.asset.size <= 0 or not item.asset.sha256:
            continue
        effective_sha256, effective_size = _effective_asset_sha_size(item)
        if effective_size <= 0:
            continue
        if item.distribution == "public":
            public_by_sha[effective_sha256].append(item)
        elif item.distribution == "private" and item.role.startswith(sensitive_prefixes):
            private_by_sha[effective_sha256].append(item)
    overlaps: list[dict[str, Any]] = []
    for digest in sorted(set(public_by_sha) & set(private_by_sha)):
        overlaps.append(
            {
                "sha256": digest,
                "public_assets": [
                    {"source_relpath": item.asset.source_relpath, "role": item.role}
                    for item in sorted(
                        public_by_sha[digest], key=lambda value: value.asset.source_relpath
                    )
                ],
                "private_assets": [
                    {"source_relpath": item.asset.source_relpath, "role": item.role}
                    for item in sorted(
                        private_by_sha[digest], key=lambda value: value.asset.source_relpath
                    )
                ],
            }
        )
    return overlaps


def _summary_errors(manifest: dict[str, Any], task_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary = manifest.get("summary")
    if not isinstance(summary, dict):
        return []
    errors: list[dict[str, Any]] = []
    by_family = dict(sorted(Counter(str(row.get("family")) for row in task_rows).items()))
    by_mode = dict(
        sorted(Counter(str(row.get("execution_mode")) for row in task_rows).items())
    )
    checks = {
        "total_tasks": len(task_rows),
        "by_family": by_family,
        "by_execution_mode": by_mode,
    }
    for key, computed in checks.items():
        if key in summary and summary[key] != computed:
            errors.append(
                {
                    "code": "manifest_summary_mismatch",
                    "field": key,
                    "declared": summary[key],
                    "computed": computed,
                }
            )
    return errors


def _merkle_root(records: Iterable[dict[str, Any]]) -> str:
    lines = [
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for record in records
    ]
    return _sha256_bytes("".join(sorted(lines)).encode("utf-8"))


def _content_bound_release_id(
    manifest_sha256: str,
    ordered_task_ids_sha256: str,
    policy_sha256: str,
    bundle_roots: dict[str, str],
) -> str:
    return _sha256_bytes(
        (
            "taskgenome-release-content-v1\0"
            f"{manifest_sha256}\0{ordered_task_ids_sha256}\0{policy_sha256}\0"
            f"{bundle_roots['public']}\0{bundle_roots['private']}"
        ).encode("utf-8")
    )[:24]


def audit_repository(
    manifest_path: Path,
    pool_root: Path,
    policy_path: Path,
) -> AuditOutcome:
    manifest, policy, task_rows, manifest_sha, policy_sha, order_sha = _load_inputs(
        manifest_path, pool_root, policy_path
    )
    errors: list[dict[str, Any]] = []
    expected_count = policy.get("expected_task_count")
    if expected_count is not None and len(task_rows) != int(expected_count):
        errors.append(
            {
                "code": "task_count_mismatch",
                "expected": int(expected_count),
                "actual": len(task_rows),
            }
        )
    expected_manifest_sha = policy.get("expected_manifest_sha256")
    if expected_manifest_sha and manifest_sha != expected_manifest_sha:
        errors.append(
            {
                "code": "manifest_sha256_mismatch",
                "expected": expected_manifest_sha,
                "actual": manifest_sha,
            }
        )
    expected_order_sha = policy.get("expected_ordered_task_ids_sha256")
    if expected_order_sha and order_sha != expected_order_sha:
        errors.append(
            {
                "code": "ordered_task_ids_sha256_mismatch",
                "expected": expected_order_sha,
                "actual": order_sha,
            }
        )
    errors.extend(_summary_errors(manifest, task_rows))

    assets, inventory_errors = _inventory_assets(manifest_path, pool_root, policy, task_rows)
    errors.extend(inventory_errors)
    for asset in assets:
        if asset.kind != "file":
            errors.append(
                {
                    "code": "unsupported_asset_type",
                    "source_relpath": asset.source_relpath,
                    "kind": asset.kind,
                }
            )

    classified, unclassified, conflicts = _classify_assets(assets, policy)
    safety_violations = _public_safety_violations(classified, policy)
    sensitive_hash_overlaps = _sensitive_hash_overlaps(classified)

    destinations: dict[tuple[str, str], list[str]] = defaultdict(list)
    for item in classified:
        if item.bundle_path is not None:
            destinations[(item.distribution, item.bundle_path)].append(item.asset.source_relpath)
    duplicate_destinations = [
        {
            "code": "duplicate_bundle_destination",
            "distribution": distribution,
            "bundle_path": bundle_path,
            "source_relpaths": source_relpaths,
        }
        for (distribution, bundle_path), source_relpaths in sorted(destinations.items())
        if len(source_relpaths) > 1
    ]
    errors.extend(duplicate_destinations)

    context_tasks: dict[str, set[str]] = defaultdict(set)
    for item in classified:
        if (
            item.distribution == "public"
            and item.role.startswith("context.")
            and item.asset.task_id
            and item.asset.official is True
        ):
            context_tasks[item.role].add(item.asset.task_id)
    expected_contexts = policy.get("expected_public_contexts") or {}
    context_coverage: dict[str, dict[str, Any]] = {}
    for role in sorted(set(context_tasks) | set(expected_contexts)):
        actual_ids = context_tasks.get(role, set())
        expected = int(expected_contexts.get(role, len(actual_ids)))
        missing_ids = sorted(
            {str(row.get("task_id")) for row in task_rows} - actual_ids
        ) if expected == len(task_rows) else []
        context_coverage[role] = {
            "expected": expected,
            "actual": len(actual_ids),
            "missing_task_ids": missing_ids,
        }
        if len(actual_ids) != expected:
            errors.append(
                {
                    "code": "context_coverage_mismatch",
                    "role": role,
                    "expected": expected,
                    "actual": len(actual_ids),
                }
            )

    distribution_counts = Counter(item.distribution for item in classified)
    distribution_bytes: Counter[str] = Counter()
    for item in classified:
        _digest, effective_size = _effective_asset_sha_size(item)
        distribution_bytes[item.distribution] += effective_size
    role_counts = Counter(f"{item.distribution}:{item.role}" for item in classified)
    non_official_scenarios = sorted(
        {
            asset.task_id
            for asset in assets
            if asset.scope == "scenario" and asset.official is False and asset.task_id
        }
    )
    bundle_summaries: dict[str, dict[str, Any]] = {}
    for distribution in ("public", "private"):
        records: list[dict[str, Any]] = []
        for item in classified:
            if item.distribution != distribution or item.bundle_path is None:
                continue
            digest, size = _effective_asset_sha_size(item)
            records.append(_asset_record(item, sha256=digest, size=size))
        records.sort(key=lambda record: str(record["bundle_path"]))
        bundle_summaries[distribution] = {
            "asset_count": len(records),
            "asset_merkle_root": _merkle_root(records),
        }
    bundle_roots = {
        distribution: summary["asset_merkle_root"]
        for distribution, summary in bundle_summaries.items()
    }
    release_id = _content_bound_release_id(
        manifest_sha,
        order_sha,
        policy_sha,
        bundle_roots,
    )
    all_errors = list(errors)
    all_errors.extend({"code": "unclassified_asset", **item} for item in unclassified)
    all_errors.extend({"code": "classification_conflict", **item} for item in conflicts)
    all_errors.extend(safety_violations)
    all_errors.extend(
        {"code": "sensitive_content_cross_distribution", **item}
        for item in sensitive_hash_overlaps
    )

    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "passed" if not all_errors else "failed",
        "release_id": release_id,
        "canonical": {
            "manifest_sha256": manifest_sha,
            "ordered_task_ids_sha256": order_sha,
            "task_count": len(task_rows),
            "first_task_id": str(task_rows[0].get("task_id")) if task_rows else None,
            "last_task_id": str(task_rows[-1].get("task_id")) if task_rows else None,
        },
        "policy_sha256": policy_sha,
        "bundles": bundle_summaries,
        "inventory": {
            "total_files": len(assets),
            "classified_files": len(
                {item.asset.source_relpath for item in classified}
            ),
            "bundle_asset_records": len(classified),
            "unclassified_files": len(unclassified),
            "by_distribution": dict(sorted(distribution_counts.items())),
            "bytes_by_distribution": dict(sorted(distribution_bytes.items())),
            "by_role": dict(sorted(role_counts.items())),
            "non_official_scenario_task_ids": non_official_scenarios,
        },
        "context_coverage": context_coverage,
        "unclassified_assets": unclassified,
        "classification_conflicts": conflicts,
        "public_safety_violations": safety_violations,
        "sensitive_hash_overlaps": sensitive_hash_overlaps,
        "errors": all_errors,
    }
    return AuditOutcome(
        manifest=manifest,
        policy=policy,
        manifest_sha256=manifest_sha,
        policy_sha256=policy_sha,
        ordered_task_ids_sha256=order_sha,
        task_rows=task_rows,
        assets=assets,
        classified=classified,
        report=report,
    )


def _asset_record(
    item: ClassifiedAsset,
    *,
    sha256: str | None = None,
    size: int | None = None,
) -> dict[str, Any]:
    asset = item.asset
    return {
        "bundle_path": item.bundle_path,
        "family": asset.family,
        "execution_mode": asset.execution_mode,
        "materialize": item.materialize,
        "mode": asset.mode,
        "role": item.role,
        "sha256": sha256 if sha256 is not None else asset.sha256,
        "size": size if size is not None else asset.size,
        "source_relpath": asset.source_relpath,
        "task_id": asset.task_id,
    }


def _ensure_empty_output(path: Path) -> None:
    if path.is_symlink():
        raise ReleaseError(f"output path must not be a symlink: {path}")
    if path.exists():
        if not path.is_dir():
            raise ReleaseError(f"output path exists and is not a directory: {path}")
        if any(path.iterdir()):
            raise ReleaseError(f"output directory must be empty: {path}")
    else:
        for parent in path.parents:
            if parent.is_symlink():
                raise ReleaseError(
                    f"output path has a symlinked parent: {parent}"
                )
            if parent.exists():
                if not parent.is_dir():
                    raise ReleaseError(
                        f"output path parent is not a directory: {parent}"
                    )
                break
        path.mkdir(parents=True)


def _copy_deterministic(
    source: Path,
    destination: Path,
    mode: str,
    *,
    expected_sha256: str,
    expected_size: int,
) -> str:
    if mode not in {"0644", "0755"}:
        raise ReleaseError(f"invalid deterministic copy mode {mode!r}: {destination}")
    if source.is_symlink() or not source.is_file():
        raise ReleaseError(f"unsafe deterministic copy source: {source}")
    if source.stat().st_size != expected_size or _sha256_file(source) != expected_sha256:
        raise ReleaseError(f"source changed after audit: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    os.chmod(destination, int(mode, 8))
    if destination.is_symlink() or not destination.is_file():
        raise ReleaseError(f"unsafe deterministic copy destination: {destination}")
    info = destination.stat()
    actual_sha256 = _sha256_file(destination)
    actual_mode = stat.S_IMODE(info.st_mode)
    if (
        info.st_size != expected_size
        or actual_sha256 != expected_sha256
        or actual_mode != int(mode, 8)
    ):
        raise ReleaseError(f"copied asset size/sha256/mode mismatch: {destination}")
    return actual_sha256


def _write_deterministic_bytes(
    data: bytes,
    destination: Path,
    mode: str,
) -> tuple[str, int]:
    if mode not in {"0644", "0755"}:
        raise ReleaseError(f"invalid deterministic write mode {mode!r}: {destination}")
    expected_sha256 = _sha256_bytes(data)
    expected_size = len(data)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    os.chmod(destination, int(mode, 8))
    if destination.is_symlink() or not destination.is_file():
        raise ReleaseError(f"unsafe deterministic write destination: {destination}")
    info = destination.stat()
    if (
        info.st_size != expected_size
        or _sha256_file(destination) != expected_sha256
        or stat.S_IMODE(info.st_mode) != int(mode, 8)
    ):
        raise ReleaseError(f"written asset size/sha256/mode mismatch: {destination}")
    return expected_sha256, expected_size


PUBLIC_RUNTIME_ROLE_PREFIXES = ("runtime.", "environment.")


def classify_official_scenario_assets(
    task_dir: Path,
    pool_root: Path,
    task_row: dict[str, Any],
    policy: dict[str, Any],
) -> list[ClassifiedAsset]:
    """Classify one official scenario with the exact release-policy engine.

    This is intentionally fail-closed. Hardened execution must not fall back
    to filename heuristics when the policy cannot classify an asset uniquely.
    """

    _validate_policy(policy)
    task_id = str(task_row.get("task_id", ""))
    if not TASK_ID_RE.fullmatch(task_id):
        raise ReleaseError(f"invalid official task id: {task_id!r}")
    rel_dir = _safe_relative(
        str(task_row.get("rel_dir") or task_id), "task rel_dir"
    )
    expected_task_dir = (pool_root / rel_dir).absolute()
    if task_dir.absolute() != expected_task_dir:
        raise ReleaseError(
            f"task directory does not match manifest rel_dir for {task_id}: {task_dir}"
        )
    if not task_dir.is_dir() or task_dir.resolve() != task_dir.absolute():
        raise ReleaseError(f"missing or symlinked official task directory: {task_dir}")

    family = task_row.get("family")
    execution_mode = task_row.get("execution_mode")
    if not isinstance(family, str) or not family:
        raise ReleaseError(f"official task {task_id} has no family")
    if not isinstance(execution_mode, str) or not execution_mode:
        raise ReleaseError(f"official task {task_id} has no execution_mode")

    assets = [
        _asset_from_path(
            path,
            pool_root,
            scope="scenario",
            collection=None,
            task_id=task_id,
            official=True,
            family=family,
            execution_mode=execution_mode,
            relpath=path.relative_to(task_dir).as_posix(),
        )
        for path in _walk_files(task_dir)
    ]
    unsafe = [asset.source_relpath for asset in assets if asset.kind != "file"]
    if unsafe:
        raise ReleaseError(
            f"official task {task_id} contains unsafe assets: {', '.join(unsafe)}"
        )
    classified, unclassified, conflicts = _classify_assets(assets, policy)
    if unclassified:
        paths = ", ".join(item["source_relpath"] for item in unclassified)
        raise ReleaseError(
            f"official task {task_id} has unclassified assets: {paths}"
        )
    if conflicts:
        paths = ", ".join(item["source_relpath"] for item in conflicts)
        raise ReleaseError(
            f"official task {task_id} has classification conflicts: {paths}"
        )
    return classified


def copy_public_runtime_scenario_assets(
    task_dir: Path,
    pool_root: Path,
    task_row: dict[str, Any],
    policy: dict[str, Any],
    destination: Path,
) -> list[ClassifiedAsset]:
    """Build a hardened generation view from policy-public runtime assets.

    Files retain their canonical task-relative paths. Public ``data/`` files
    are also flattened exactly as the historical runner did, but the source of
    that compatibility copy is the already policy-filtered (and, where needed,
    sanitized) view.
    """

    if destination.is_symlink() or not destination.is_dir():
        raise ReleaseError(f"unsafe hardened runtime destination: {destination}")
    if any(destination.iterdir()):
        raise ReleaseError(
            f"hardened runtime destination must start empty: {destination}"
        )

    classified = classify_official_scenario_assets(
        task_dir, pool_root, task_row, policy
    )
    selected = [
        item
        for item in classified
        if item.distribution == "public"
        and item.role.startswith(PUBLIC_RUNTIME_ROLE_PREFIXES)
    ]
    seen_relpaths: set[str] = set()
    for item in selected:
        relpath = _safe_relative(item.asset.relpath, "runtime asset path")
        if relpath in seen_relpaths:
            raise ReleaseError(
                f"duplicate public runtime path for {item.asset.task_id}: {relpath}"
            )
        seen_relpaths.add(relpath)
        target = destination / relpath
        if item.transform is None:
            _copy_deterministic(
                item.asset.source_path,
                target,
                item.asset.mode,
                expected_sha256=item.asset.sha256,
                expected_size=item.asset.size,
            )
        else:
            _write_deterministic_bytes(
                _transformed_asset_bytes(item), target, item.asset.mode
            )

    for item in selected:
        source_relpath = PurePosixPath(item.asset.relpath)
        if not source_relpath.parts or source_relpath.parts[0] != "data":
            continue
        flattened = PurePosixPath(*source_relpath.parts[1:])
        if not flattened.parts:
            continue
        source = destination / source_relpath.as_posix()
        target = destination / flattened.as_posix()
        if target.exists() or target.is_symlink():
            continue
        info = source.stat()
        _copy_deterministic(
            source,
            target,
            item.asset.mode,
            expected_sha256=_sha256_file(source),
            expected_size=info.st_size,
        )
    return selected


def _write_checksums(bundle_root: Path) -> None:
    lines: list[str] = []
    for path in sorted(bundle_root.rglob("*"), key=lambda item: item.relative_to(bundle_root).as_posix()):
        if not path.is_file() or path.name == "SHA256SUMS":
            continue
        relpath = path.relative_to(bundle_root).as_posix()
        lines.append(f"{_sha256_file(path)}  {relpath}\n")
    (bundle_root / "SHA256SUMS").write_text("".join(lines), encoding="utf-8")
    os.chmod(bundle_root / "SHA256SUMS", 0o644)


def build_release(
    outcome: AuditOutcome,
    output_root: Path,
    *,
    distributions: tuple[str, ...] = ("public", "private"),
) -> dict[str, Any]:
    if outcome.report["status"] != "passed":
        raise ReleaseError("audit failed; release was not built")
    if not distributions or any(
        distribution not in {"public", "private"}
        for distribution in distributions
    ):
        raise ReleaseError("build distributions must be public and/or private")
    if len(set(distributions)) != len(distributions):
        raise ReleaseError("build distributions must not contain duplicates")
    _ensure_empty_output(output_root)
    quality_root = output_root / "quality"
    quality_root.mkdir()

    releases: dict[str, dict[str, Any]] = {}
    for distribution in distributions:
        bundle_root = output_root / distribution
        bundle_root.mkdir()
        selected = [item for item in outcome.classified if item.distribution == distribution]
        records: list[dict[str, Any]] = []
        for item in selected:
            assert item.bundle_path is not None
            if item.transform is None:
                _copy_deterministic(
                    item.asset.source_path,
                    bundle_root / item.bundle_path,
                    item.asset.mode,
                    expected_sha256=item.asset.sha256,
                    expected_size=item.asset.size,
                )
                records.append(_asset_record(item))
            else:
                transformed = _transformed_asset_bytes(item)
                digest, size = _write_deterministic_bytes(
                    transformed,
                    bundle_root / item.bundle_path,
                    item.asset.mode,
                )
                records.append(_asset_record(item, sha256=digest, size=size))
        records.sort(key=lambda record: str(record["bundle_path"]))
        payload = {
            "schema_version": SCHEMA_VERSION,
            "release_id": outcome.report["release_id"],
            "bundle": distribution,
            "canonical": {
                **outcome.report["canonical"],
                "summary": outcome.manifest.get("summary"),
            },
            "policy_sha256": outcome.policy_sha256,
            "asset_count": len(records),
            "asset_merkle_root": _merkle_root(records),
            "bundle_roots": {
                name: outcome.report["bundles"][name]["asset_merkle_root"]
                for name in ("public", "private")
            },
            "assets": records,
        }
        if (
            payload["asset_count"]
            != outcome.report["bundles"][distribution]["asset_count"]
            or payload["asset_merkle_root"]
            != outcome.report["bundles"][distribution]["asset_merkle_root"]
        ):
            raise ReleaseError(
                f"{distribution} bundle content changed after audit"
            )
        _write_json(bundle_root / "release.json", payload)
        _write_checksums(bundle_root)
        releases[distribution] = payload

    quality_report = dict(outcome.report)
    _write_json(quality_root / "quality_report.json", quality_report)
    _write_checksums(quality_root)
    result = {
        "status": "built",
        "release_id": outcome.report["release_id"],
        "bundles": list(distributions),
        "excluded_assets": outcome.report["inventory"]["by_distribution"].get("excluded", 0),
    }
    for distribution in distributions:
        result[f"{distribution}_assets"] = releases[distribution]["asset_count"]
    return result


def _parse_checksums(path: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise ReleaseError(f"missing checksums file: {path}") from exc
    for line in lines:
        if not line.strip():
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            raise ReleaseError(f"invalid checksum line in {path}: {line!r}")
        relpath = _safe_relative(parts[1], "checksum path")
        if relpath in checksums:
            raise ReleaseError(f"duplicate checksum path in {path}: {relpath}")
        checksums[relpath] = parts[0]
    return checksums


def _scan_public_bundle_file(path: Path, relpath: str) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for pattern in DEFAULT_FORBIDDEN_ASSET_PATTERNS:
        if fnmatch.fnmatchcase(f"bundle/{relpath}", pattern):
            violations.append({"code": "public_forbidden_asset", "bundle_path": relpath, "pattern": pattern})
            break
    if path.name == "SHA256SUMS":
        return violations
    compiled = [
        (pattern, re.compile(pattern, re.MULTILINE))
        for pattern in DEFAULT_FORBIDDEN_CONTENT_REGEXES
    ]
    violations.extend(
        _public_content_violations(
            path,
            source_relpath=None,
            bundle_path=relpath,
            asset_patterns=list(DEFAULT_FORBIDDEN_ASSET_PATTERNS),
            compiled=compiled,
            max_bytes=DEFAULT_MAX_TEXT_SCAN_BYTES,
        )
    )
    return violations


def verify_bundle(bundle_root: Path, expected_bundle: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    for metadata_name in ("release.json", "SHA256SUMS"):
        metadata_path = bundle_root / metadata_name
        if metadata_path.is_symlink() or not metadata_path.is_file():
            raise ReleaseError(f"missing or unsafe bundle metadata: {metadata_path}")
    payload = _load_json(bundle_root / "release.json", "release manifest")
    bundle = str(payload.get("bundle", ""))
    if expected_bundle and bundle != expected_bundle:
        raise ReleaseError(f"expected {expected_bundle} bundle, found {bundle!r}: {bundle_root}")
    if bundle not in {"public", "private"}:
        raise ReleaseError(f"invalid bundle type in {bundle_root}/release.json: {bundle!r}")
    records = payload.get("assets")
    if not isinstance(records, list):
        raise ReleaseError(f"release manifest assets must be an array: {bundle_root}")
    errors: list[dict[str, Any]] = []
    if set(payload) != RELEASE_MANIFEST_KEYS:
        errors.append(
            {
                "code": "invalid_release_manifest_keys",
                "missing": sorted(RELEASE_MANIFEST_KEYS - set(payload)),
                "unknown": sorted(set(payload) - RELEASE_MANIFEST_KEYS),
            }
        )
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append({"code": "invalid_release_schema_version"})
    if not (isinstance(payload.get("release_id"), str) and re.fullmatch(r"[0-9a-f]{24}", payload["release_id"])):
        errors.append({"code": "invalid_release_id"})
    if not (
        isinstance(payload.get("policy_sha256"), str)
        and HEX64_RE.fullmatch(payload["policy_sha256"])
    ):
        errors.append({"code": "invalid_policy_sha256"})
    if not (
        isinstance(payload.get("asset_merkle_root"), str)
        and HEX64_RE.fullmatch(payload["asset_merkle_root"])
    ):
        errors.append({"code": "invalid_declared_asset_merkle_root"})
    bundle_roots = payload.get("bundle_roots")
    valid_bundle_roots = (
        isinstance(bundle_roots, dict)
        and set(bundle_roots) == {"public", "private"}
        and all(
            isinstance(bundle_roots.get(name), str)
            and HEX64_RE.fullmatch(bundle_roots[name])
            for name in ("public", "private")
        )
    )
    if not valid_bundle_roots:
        errors.append({"code": "invalid_bundle_roots"})
    if type(payload.get("asset_count")) is not int or payload["asset_count"] < 0:
        errors.append({"code": "invalid_asset_count"})
    canonical = payload.get("canonical")
    if not isinstance(canonical, dict):
        errors.append({"code": "invalid_release_canonical"})
    else:
        if set(canonical) != RELEASE_CANONICAL_KEYS:
            errors.append(
                {
                    "code": "invalid_release_canonical_keys",
                    "missing": sorted(RELEASE_CANONICAL_KEYS - set(canonical)),
                    "unknown": sorted(set(canonical) - RELEASE_CANONICAL_KEYS),
                }
            )
        for field in ("manifest_sha256", "ordered_task_ids_sha256"):
            if not (
                isinstance(canonical.get(field), str)
                and HEX64_RE.fullmatch(canonical[field])
            ):
                errors.append({"code": "invalid_release_canonical_field", "field": field})
        if type(canonical.get("task_count")) is not int or canonical["task_count"] < 0:
            errors.append(
                {"code": "invalid_release_canonical_field", "field": "task_count"}
            )
        for field in ("first_task_id", "last_task_id"):
            value = canonical.get(field)
            if value is not None and not (
                isinstance(value, str) and TASK_ID_RE.fullmatch(value)
            ):
                errors.append({"code": "invalid_release_canonical_field", "field": field})
        summary = canonical.get("summary")
        if summary is not None:
            if not isinstance(summary, dict) or set(summary) != RELEASE_SUMMARY_KEYS:
                errors.append({"code": "invalid_release_canonical_summary"})
            else:
                if type(summary.get("total_tasks")) is not int or summary["total_tasks"] < 0:
                    errors.append(
                        {
                            "code": "invalid_release_canonical_summary_field",
                            "field": "total_tasks",
                        }
                    )
                for field in ("by_family", "by_execution_mode", "by_source"):
                    value = summary.get(field)
                    if not isinstance(value, dict) or any(
                        not isinstance(key, str)
                        or not key
                        or type(count) is not int
                        or count < 0
                        for key, count in value.items()
                    ):
                        errors.append(
                            {
                                "code": "invalid_release_canonical_summary_field",
                                "field": field,
                            }
                        )
    expected_files = {"release.json", "SHA256SUMS"}
    normalized_records: list[dict[str, Any]] = []
    seen_bundle_paths: set[str] = set()
    seen_source_paths: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            errors.append({"code": "invalid_asset_record", "index": index})
            continue
        if set(record) != RELEASE_ASSET_RECORD_KEYS:
            errors.append(
                {
                    "code": "invalid_asset_record_keys",
                    "index": index,
                    "missing": sorted(RELEASE_ASSET_RECORD_KEYS - set(record)),
                    "unknown": sorted(set(record) - RELEASE_ASSET_RECORD_KEYS),
                }
            )
            continue
        invalid = False
        if not isinstance(record["bundle_path"], str) or not isinstance(
            record["source_relpath"], str
        ):
            errors.append({"code": "invalid_asset_path_type", "index": index})
            invalid = True
        if record["family"] is not None and not isinstance(record["family"], str):
            errors.append({"code": "invalid_asset_family", "index": index})
            invalid = True
        if record["execution_mode"] is not None and not isinstance(
            record["execution_mode"], str
        ):
            errors.append({"code": "invalid_asset_execution_mode", "index": index})
            invalid = True
        if type(record["materialize"]) is not bool:
            errors.append({"code": "invalid_asset_materialize", "index": index})
            invalid = True
        if record["mode"] not in {"0644", "0755"}:
            errors.append({"code": "invalid_asset_mode", "index": index})
            invalid = True
        if not isinstance(record["role"], str) or not record["role"]:
            errors.append({"code": "invalid_asset_role", "index": index})
            invalid = True
        if not isinstance(record["sha256"], str) or not HEX64_RE.fullmatch(
            record["sha256"]
        ):
            errors.append({"code": "invalid_asset_sha256", "index": index})
            invalid = True
        if type(record["size"]) is not int or record["size"] < 0:
            errors.append({"code": "invalid_asset_size", "index": index})
            invalid = True
        if record["task_id"] is not None and not (
            isinstance(record["task_id"], str)
            and TASK_ID_RE.fullmatch(record["task_id"])
        ):
            errors.append({"code": "invalid_asset_task_id", "index": index})
            invalid = True
        if invalid:
            continue
        try:
            relpath = _safe_relative(record["bundle_path"], "bundle path")
            source_relpath = _safe_relative(record["source_relpath"], "source relative path")
        except ReleaseError as exc:
            errors.append({"code": "invalid_asset_path", "index": index, "message": str(exc)})
            continue
        if relpath in seen_bundle_paths:
            errors.append({"code": "duplicate_bundle_path", "index": index, "bundle_path": relpath})
        seen_bundle_paths.add(relpath)
        if source_relpath in seen_source_paths:
            errors.append(
                {
                    "code": "duplicate_source_relpath",
                    "index": index,
                    "source_relpath": source_relpath,
                }
            )
        seen_source_paths.add(source_relpath)
        expected_files.add(relpath)
        path = bundle_root / relpath
        if not path.is_file() or path.is_symlink():
            errors.append({"code": "missing_or_unsafe_bundle_asset", "bundle_path": relpath})
            continue
        digest = _sha256_file(path)
        if digest != record.get("sha256"):
            errors.append(
                {
                    "code": "asset_sha256_mismatch",
                    "bundle_path": relpath,
                    "expected": record.get("sha256"),
                    "actual": digest,
                }
            )
        info = path.stat()
        if info.st_size != record.get("size"):
            errors.append({"code": "asset_size_mismatch", "bundle_path": relpath})
        expected_mode = int(record["mode"], 8)
        actual_mode = stat.S_IMODE(info.st_mode)
        if actual_mode != expected_mode:
            errors.append(
                {
                    "code": "asset_mode_mismatch",
                    "bundle_path": relpath,
                    "expected": f"{expected_mode:04o}",
                    "actual": f"{actual_mode:04o}",
                }
            )
        normalized = dict(record)
        normalized["bundle_path"] = relpath
        normalized["source_relpath"] = source_relpath
        normalized_records.append(normalized)
        if bundle == "public":
            errors.extend(_scan_public_bundle_file(path, relpath))

    discovered_nodes = list(bundle_root.rglob("*"))
    unsafe_nodes = sorted(
        path.relative_to(bundle_root).as_posix()
        for path in discovered_nodes
        if path.is_symlink() or not (path.is_file() or path.is_dir())
    )
    if unsafe_nodes:
        errors.append({"code": "unsafe_bundle_nodes", "paths": unsafe_nodes})
    actual_files = {
        path.relative_to(bundle_root).as_posix()
        for path in discovered_nodes
        if path.is_file() and not path.is_symlink()
    }
    if actual_files != expected_files:
        errors.append(
            {
                "code": "bundle_file_set_mismatch",
                "missing": sorted(expected_files - actual_files),
                "extra": sorted(actual_files - expected_files),
            }
        )
    checksums = _parse_checksums(bundle_root / "SHA256SUMS")
    checksum_expected_files = actual_files - {"SHA256SUMS"}
    if set(checksums) != checksum_expected_files:
        errors.append(
            {
                "code": "checksum_file_set_mismatch",
                "missing": sorted(checksum_expected_files - set(checksums)),
                "extra": sorted(set(checksums) - checksum_expected_files),
            }
        )
    for relpath, expected_digest in checksums.items():
        path = bundle_root / relpath
        if path.is_symlink() or not path.is_file():
            errors.append({"code": "missing_or_unsafe_checksum_asset", "bundle_path": relpath})
        elif _sha256_file(path) != expected_digest:
            errors.append({"code": "checksum_mismatch", "bundle_path": relpath})

    computed_merkle = _merkle_root(normalized_records)
    if computed_merkle != payload.get("asset_merkle_root"):
        errors.append(
            {
                "code": "asset_merkle_root_mismatch",
                "expected": payload.get("asset_merkle_root"),
                "actual": computed_merkle,
            }
        )
    if valid_bundle_roots and bundle_roots[bundle] != computed_merkle:
        errors.append(
            {
                "code": "bundle_roots_content_mismatch",
                "bundle": bundle,
                "expected": bundle_roots[bundle],
                "actual": computed_merkle,
            }
        )
    if (
        valid_bundle_roots
        and isinstance(canonical, dict)
        and isinstance(canonical.get("manifest_sha256"), str)
        and HEX64_RE.fullmatch(canonical["manifest_sha256"])
        and isinstance(canonical.get("ordered_task_ids_sha256"), str)
        and HEX64_RE.fullmatch(canonical["ordered_task_ids_sha256"])
        and isinstance(payload.get("policy_sha256"), str)
        and HEX64_RE.fullmatch(payload["policy_sha256"])
    ):
        computed_release_id = _content_bound_release_id(
            canonical["manifest_sha256"],
            canonical["ordered_task_ids_sha256"],
            payload["policy_sha256"],
            bundle_roots,
        )
        if payload.get("release_id") != computed_release_id:
            errors.append(
                {
                    "code": "release_id_content_mismatch",
                    "expected": computed_release_id,
                    "actual": payload.get("release_id"),
                }
            )
    if len(records) != payload.get("asset_count"):
        errors.append({"code": "asset_count_mismatch"})
    return payload, errors


def verify_release(public_root: Path | None, private_root: Path | None) -> dict[str, Any]:
    if public_root is None and private_root is None:
        raise ReleaseError("verify requires --public and/or --private")
    payloads: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    if public_root is not None:
        payloads["public"], public_errors = verify_bundle(public_root, "public")
        errors.extend({"bundle": "public", **error} for error in public_errors)
    if private_root is not None:
        payloads["private"], private_errors = verify_bundle(private_root, "private")
        errors.extend({"bundle": "private", **error} for error in private_errors)
    if len(payloads) == 2:
        for field in ("release_id", "policy_sha256", "canonical", "bundle_roots"):
            if payloads["public"].get(field) != payloads["private"].get(field):
                errors.append({"code": "cross_bundle_mismatch", "field": field})
        public_records = [
            record
            for record in payloads["public"].get("assets", [])
            if isinstance(record, dict)
        ]
        private_records = [
            record
            for record in payloads["private"].get("assets", [])
            if isinstance(record, dict)
        ]
        public_by_source = {
            record["source_relpath"]: record
            for record in public_records
            if isinstance(record.get("source_relpath"), str)
        }
        private_by_source = {
            record["source_relpath"]: record
            for record in private_records
            if isinstance(record.get("source_relpath"), str)
        }
        disallowed_source_overlaps: list[str] = []
        for source_relpath in sorted(set(public_by_source) & set(private_by_source)):
            public_record = public_by_source[source_relpath]
            private_record = private_by_source[source_relpath]
            allowed_sanitized_pair = (
                public_record.get("role") in SANITIZED_OOXML_PUBLIC_ROLES
                and public_record.get("materialize") is False
                and private_record.get("role") == PRIVATE_HOST_PATH_ROLE
                and private_record.get("materialize") is True
            )
            allowed_sanitized_pair = allowed_sanitized_pair or (
                public_record.get("role") in SANITIZED_GENE_PUBLIC_ROLES
                and public_record.get("materialize") is False
                and private_record.get("role") == PRIVATE_HOST_PATH_ROLE
                and private_record.get("materialize") is True
            )
            if not allowed_sanitized_pair:
                disallowed_source_overlaps.append(source_relpath)
        if disallowed_source_overlaps:
            errors.append(
                {
                    "code": "public_private_source_overlap",
                    "source_relpaths": disallowed_source_overlaps,
                }
            )
        public_by_sha: dict[str, list[dict[str, Any]]] = defaultdict(list)
        private_by_sha: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in public_records:
            digest = record.get("sha256")
            size = record.get("size")
            if (
                isinstance(digest, str)
                and HEX64_RE.fullmatch(digest)
                and type(size) is int
                and size > 0
            ):
                public_by_sha[digest].append(record)
        for record in private_records:
            digest = record.get("sha256")
            role = record.get("role")
            size = record.get("size")
            if (
                isinstance(digest, str)
                and HEX64_RE.fullmatch(digest)
                and isinstance(role, str)
                and role.startswith(
                    ("judge.", "reference.", "legacy.", "authoring.oracle", "privacy.")
                )
                and type(size) is int
                and size > 0
            ):
                private_by_sha[digest].append(record)
        for digest in sorted(set(public_by_sha) & set(private_by_sha)):
            errors.append(
                {
                    "code": "cross_bundle_sensitive_hash_overlap",
                    "sha256": digest,
                    "public_source_relpaths": sorted(
                        str(record.get("source_relpath")) for record in public_by_sha[digest]
                    ),
                    "private_source_relpaths": sorted(
                        str(record.get("source_relpath")) for record in private_by_sha[digest]
                    ),
                }
            )
    return {
        "status": "passed" if not errors else "failed",
        "release_id": next((payload.get("release_id") for payload in payloads.values()), None),
        "bundles": sorted(payloads),
        "errors": errors,
    }


def materialize_legacy(public_root: Path, private_root: Path, output_root: Path) -> dict[str, Any]:
    verification = verify_release(public_root, private_root)
    if verification["status"] != "passed":
        raise ReleaseError("bundle verification failed; legacy tree was not materialized")
    public_payload = _load_json(public_root / "release.json", "public release manifest")
    private_payload = _load_json(private_root / "release.json", "private release manifest")
    _ensure_empty_output(output_root)
    copied: dict[str, str] = {}
    for bundle_root, payload in ((public_root, public_payload), (private_root, private_payload)):
        for record in payload["assets"]:
            if not record.get("materialize"):
                continue
            source_relpath = _safe_relative(record["source_relpath"], "materialized source path")
            digest = str(record["sha256"])
            if source_relpath in copied:
                if copied[source_relpath] != digest:
                    raise ReleaseError(f"materialization collision: {source_relpath}")
                continue
            actual_digest = _copy_deterministic(
                bundle_root / record["bundle_path"],
                output_root / source_relpath,
                str(record.get("mode", "0644")),
                expected_sha256=digest,
                expected_size=int(record["size"]),
            )
            copied[source_relpath] = actual_digest
    manifest_path = output_root / "manifest.json"
    expected_manifest_sha = public_payload["canonical"]["manifest_sha256"]
    if not manifest_path.is_file() or _sha256_file(manifest_path) != expected_manifest_sha:
        raise ReleaseError("materialized canonical manifest is missing or changed")
    tree_root = _sha256_bytes(
        "".join(f"{digest}  {path}\n" for path, digest in sorted(copied.items())).encode("utf-8")
    )
    return {
        "status": "materialized",
        "release_id": public_payload["release_id"],
        "file_count": len(copied),
        "tree_merkle_root": tree_root,
    }


def _common_audit_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", default="tasks_final/manifest.json")
    parser.add_argument("--pool-root", default=None)
    parser.add_argument("--policy", default="release/asset_policy.v1.json")


def _resolve_audit_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    manifest_path = Path(args.manifest).resolve()
    pool_root = Path(args.pool_root).resolve() if args.pool_root else manifest_path.parent
    policy_path = Path(args.policy).resolve()
    return manifest_path, pool_root, policy_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit_parser = subparsers.add_parser("audit", help="classify and validate release assets")
    _common_audit_arguments(audit_parser)
    audit_parser.add_argument("--report", default=None)

    build_parser = subparsers.add_parser("build", help="build public/private bundles")
    _common_audit_arguments(build_parser)
    build_parser.add_argument("--out", required=True)

    public_parser = subparsers.add_parser(
        "build-public",
        help="build only the publishable public bundle and quality report",
    )
    _common_audit_arguments(public_parser)
    public_parser.add_argument("--out", required=True)

    private_parser = subparsers.add_parser(
        "build-private",
        help="build only the access-controlled private bundle and quality report",
    )
    _common_audit_arguments(private_parser)
    private_parser.add_argument("--out", required=True)

    verify_parser = subparsers.add_parser("verify", help="verify bundle hashes and safety")
    verify_parser.add_argument("--public", default=None)
    verify_parser.add_argument("--private", default=None)

    materialize_parser = subparsers.add_parser(
        "materialize-legacy", help="reconstruct the canonical legacy pool layout"
    )
    materialize_parser.add_argument("--public", required=True)
    materialize_parser.add_argument("--private", required=True)
    materialize_parser.add_argument("--out", required=True)

    explain_parser = subparsers.add_parser("explain", help="explain policy matches for a task")
    _common_audit_arguments(explain_parser)
    explain_parser.add_argument("--task-id", required=True)
    explain_parser.add_argument("--path", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command in {
            "audit",
            "build",
            "build-public",
            "build-private",
            "explain",
        }:
            manifest_path, pool_root, policy_path = _resolve_audit_paths(args)
            outcome = audit_repository(manifest_path, pool_root, policy_path)
            if args.command == "audit":
                if args.report:
                    _write_json(Path(args.report), outcome.report)
                _print_json(outcome.report)
                return 0 if outcome.report["status"] == "passed" else 1
            if args.command in {"build", "build-public", "build-private"}:
                distributions = {
                    "build": ("public", "private"),
                    "build-public": ("public",),
                    "build-private": ("private",),
                }[args.command]
                result = build_release(
                    outcome,
                    Path(args.out).absolute(),
                    distributions=distributions,
                )
                _print_json(result)
                return 0
            selected = [
                item
                for item in outcome.classified
                if item.asset.task_id == args.task_id
                and (not args.path or args.path in item.asset.source_relpath)
            ]
            unclassified = [
                item
                for item in outcome.report["unclassified_assets"]
                if item.get("task_id") == args.task_id
                and (not args.path or args.path in item.get("source_relpath", ""))
            ]
            payload = {
                "task_id": args.task_id,
                "assets": [
                    {
                        "source_relpath": item.asset.source_relpath,
                        "distribution": item.distribution,
                        "role": item.role,
                        "bundle_path": item.bundle_path,
                        "materialize": item.materialize,
                        "rule_id": item.rule_id,
                    }
                    for item in selected
                ],
                "unclassified_assets": unclassified,
            }
            _print_json(payload)
            return 0 if selected or unclassified else 1

        if args.command == "verify":
            result = verify_release(
                Path(args.public).resolve() if args.public else None,
                Path(args.private).resolve() if args.private else None,
            )
            _print_json(result)
            return 0 if result["status"] == "passed" else 1

        if args.command == "materialize-legacy":
            result = materialize_legacy(
                Path(args.public).resolve(),
                Path(args.private).resolve(),
                Path(args.out).absolute(),
            )
            _print_json(result)
            return 0
    except ReleaseError as exc:
        _print_json({"status": "error", "error": str(exc)}, stream=sys.stderr)
        return 2
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())

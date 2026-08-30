"""Reproducibility metadata and secret-safe serialization helpers."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit


RUN_CONFIG_SCHEMA_VERSION = "taskgenome.run-config.v2"
RESULT_SCHEMA_VERSION = "taskgenome.result.v2"
BUDGET_SCHEMA_VERSION = "taskgenome.budget.v1"
CASE_SCHEMA_VERSION = "taskgenome.case.v1"

SECRET_ARG_NAMES = frozenset(
    {
        "yunwu_key",
        "gemini_key",
        "siliconflow_key",
        "evomap_key",
        "sub2api_key",
        "bedrock_key",
    }
)

CREDENTIAL_ENV_CANDIDATES = {
    "yunwu_key": ("YUNWU_KEY", "YUNWU_API_KEY"),
    "gemini_key": ("GEMINI_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "siliconflow_key": ("SILICONFLOW_KEY", "SILICONFLOW_API_KEY", "SF_API_KEY"),
    "evomap_key": ("EVOMAP_KEY", "EVOMAP_API_KEY"),
    "sub2api_key": ("SUB2API_API_KEY", "SUB2API_KEY"),
    "bedrock_key": (
        "AWS_BEARER_TOKEN_BEDROCK",
        "BEDROCK_KEY",
        "BEDROCK_API_KEY",
    ),
}

_SECRET_ENV_NAME = re.compile(
    r"(?:api[_-]?key|token|secret|password|passwd|credential|private[_-]?key)",
    re.IGNORECASE,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def digest_files(
    paths: Iterable[Path],
    *,
    base: Path,
    allow_external: bool = False,
) -> dict[str, Any]:
    digest = hashlib.sha256()
    entries: list[tuple[str, str]] = []
    seen: set[Path] = set()
    for raw in paths:
        path = raw.resolve()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            name = path.relative_to(base.resolve()).as_posix()
        except ValueError:
            if not allow_external:
                raise ValueError(
                    f"refusing to hash a file outside repository root: {path}"
                )
            location = hashlib.sha256(
                str(path).encode("utf-8", "surrogateescape")
            ).hexdigest()
            name = f"external/{location}/{path.name}"
        file_hash = sha256_file(path)
        entries.append((name, file_hash))
    for name, file_hash in sorted(entries):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return {
        "algorithm": "sha256",
        "digest": digest.hexdigest(),
        "file_count": len(entries),
    }


def redact_url(value: str) -> str:
    if not value:
        return value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<redacted-url>"
    if not parsed.scheme or not parsed.netloc:
        return value
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        return "<redacted-url>"
    if port is not None:
        host = f"{host}:{port}"
    safe_path = parsed.path if re.fullmatch(r"/(?:v\d+)?/?", parsed.path or "/") else ""
    return urlunsplit((parsed.scheme, host, safe_path, "", ""))


def redacted_args(values: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, bool]]:
    safe = dict(values)
    supplied: dict[str, bool] = {}
    for name in SECRET_ARG_NAMES:
        present = bool(safe.get(name))
        supplied[name] = present
        safe[name] = "<redacted>" if present else ""
    if safe.get("local_base_url"):
        safe["local_base_url"] = redact_url(str(safe["local_base_url"]))
    if safe.get("sub2api_base_url"):
        safe["sub2api_base_url"] = redact_url(str(safe["sub2api_base_url"]))
    return safe, supplied


def credential_sources(values: Mapping[str, Any]) -> dict[str, str]:
    sources: dict[str, str] = {}
    for arg_name, env_names in CREDENTIAL_ENV_CANDIDATES.items():
        if values.get(arg_name):
            sources[arg_name] = "cli"
            continue
        sources[arg_name] = next(
            (f"env:{name}" for name in env_names if os.environ.get(name)),
            "unset",
        )
    sources["gemini_vertex_service_account"] = next(
        (
            f"env:{name}"
            for name in (
                "GEMINI_VERTEX_SERVICE_ACCOUNT_FILE",
                "GOOGLE_APPLICATION_CREDENTIALS",
            )
            if os.environ.get(name)
        ),
        "unset",
    )
    return sources


def redact_text(text: str, secrets: Iterable[str]) -> str:
    safe = str(text)
    for value in sorted({str(x) for x in secrets if x}, key=len, reverse=True):
        safe = safe.replace(value, "<redacted>")
    return safe


def redact_tree(value: Any, secrets: Iterable[str]) -> Any:
    secret_values = tuple(str(x) for x in secrets if x)
    if isinstance(value, str):
        return redact_text(value, secret_values)
    if isinstance(value, list):
        return [redact_tree(item, secret_values) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_tree(item, secret_values) for item in value)
    if isinstance(value, dict):
        return {key: redact_tree(item, secret_values) for key, item in value.items()}
    return value


def sensitive_environment_names() -> list[str]:
    return sorted(name for name in os.environ if _SECRET_ENV_NAME.search(name))


def _git_metadata(repo_root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return (proc.stdout or "").strip() if proc.returncode == 0 else ""

    commit = run("rev-parse", "HEAD")
    branch = run("rev-parse", "--abbrev-ref", "HEAD")
    dirty = bool(run("status", "--porcelain", "--untracked-files=normal"))
    return {"commit": commit or None, "branch": branch or None, "dirty": dirty}


def _package_versions() -> dict[str, str | None]:
    names = (
        "anthropic",
        "openai",
        "google-generativeai",
        "boto3",
        "numpy",
        "pandas",
        "scikit-learn",
        "scipy",
        "pytest",
        "pydantic",
    )
    versions: dict[str, str | None] = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _installed_distributions() -> dict[str, Any]:
    rows = sorted(
        {
            f"{dist.metadata.get('Name') or dist.metadata.get('Summary') or 'unknown'}=={dist.version}"
            for dist in importlib.metadata.distributions()
        },
        key=str.lower,
    )
    payload = "\n".join(rows) + ("\n" if rows else "")
    return {
        "count": len(rows),
        "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "freeze": rows,
    }


def _protocol_environment() -> dict[str, str | None]:
    names = (
        "GENE_BENCH_API_TIMEOUT",
        "GENE_BENCH_MAX_TOKENS",
        "GENE_BENCH_GEMINI_REASONING_EFFORT",
        "GENE_BENCH_GPT_REASONING_EFFORT",
        "GENE_BENCH_BEDROCK_EFFORT",
        "GENE_BENCH_BEDROCK_THINKING_TYPE",
        "GENE_BENCH_BEDROCK_BYPASS_PROXY",
        "GENE_BENCH_API_RETRIES",
        "GENE_BENCH_API_RETRY_BASE",
        "GENE_BENCH_API_RETRY_MAX",
        "GENE_BENCH_SCORE_THRESHOLD",
        "QWEN_ENABLE_THINKING",
        "AWS_BEDROCK_REGION",
        "BEDROCK_REGION",
        "GEMINI_VERTEX_PROJECT_ID",
        "GEMINI_VERTEX_LOCATION",
    )
    return {name: os.environ.get(name) for name in names}


def collect_environment(repo_root: Path) -> dict[str, Any]:
    return {
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "git": _git_metadata(repo_root),
        "packages": _package_versions(),
        "installed_distributions": _installed_distributions(),
        "protocol_environment": _protocol_environment(),
        "sensitive_environment_names_present": sensitive_environment_names(),
    }

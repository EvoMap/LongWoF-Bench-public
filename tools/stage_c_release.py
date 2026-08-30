#!/usr/bin/env python3
"""Build and verify the three Stage C release boundaries.

The public code export is an explicit whitelist copy and never copies a Git
object database. The public data packager accepts only a bundle that already
passes the fail-closed public release verifier. It writes deterministic,
versioned channel metadata alongside the archive record; this metadata
describes pending URLs without claiming that an external upload occurred.
Private bundles use the separate ``release_assets.py build-private`` command
and are never an input to this module's packaging command.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "release" / "stage_c_release.v1.json"
CODE_MANIFEST = "PUBLIC_CODE_MANIFEST.json"
DEFAULT_RELEASE_AUTHOR_NAME = "Codex Release Builder"
DEFAULT_RELEASE_AUTHOR_EMAIL = "codex-release@users.noreply.github.com"
DEFAULT_RELEASE_COMMIT_DATE = "2000-01-01T00:00:00+0000"
DEFAULT_RELEASE_BRANCH = "main"
TEXT_SUFFIXES = {
    "", ".cff", ".cfg", ".csv", ".gitignore", ".ini", ".json", ".md",
    ".py", ".sh", ".tex", ".txt", ".yaml", ".yml",
}

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_assets  # noqa: E402


class StageCError(RuntimeError):
    """A release boundary or integrity check failed."""


def _git_output(
    root: Path,
    args: list[str],
    *,
    text: bool = True,
    env: dict[str, str] | None = None,
) -> str | bytes:
    """Run Git without inheriting an authoring repository's working tree.

    The release commands intentionally use ``git -C`` and never copy or
    inspect the source checkout's object database.  Capturing stderr here
    keeps failures actionable while preventing a subprocess traceback from
    becoming part of a release report.
    """

    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=text,
            env=env,
        )
    except FileNotFoundError as exc:
        raise StageCError("git is required to create or verify a clean history") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() if isinstance(exc.stderr, str) else ""
        suffix = f": {detail}" if detail else ""
        raise StageCError(f"git {' '.join(args)} failed{suffix}") from exc
    return completed.stdout


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _publication_metadata(
    policy: dict[str, Any],
    *,
    archive_name: str,
    sha256_sidecar: str,
    sigstore_bundle: str,
) -> dict[str, Any]:
    """Return deterministic, channel-specific metadata for a data artifact.

    The URLs are derived from the repository, immutable version tag, and asset
    names rather than from a local checkout.  A pending status is intentional:
    constructing a URL must never be mistaken for having uploaded the bytes,
    and uploading bytes to a repository that is not public yet must never be
    mistaken for an anonymously downloadable artifact.  A channel the policy
    does not declare is omitted rather than emitted as nulls, so an absent
    key means "not a channel for this release", never "pending".
    """

    project = policy.get("project", {})
    data_policy = policy.get("public_data", {})
    repository_url = str(project.get("code_repository_url", "")).rstrip("/")
    repository_slug = str(project.get("repository_slug", ""))
    version = str(policy.get("versions", {}).get("public_data", ""))
    tag = f"v{version}"
    release_url = f"{repository_url}/releases/tag/{tag}"
    download_root = f"{repository_url}/releases/download/{tag}"
    publication = data_policy.get("publication", {})
    if not isinstance(publication, dict):
        publication = {}
    zenodo = publication.get("zenodo", {})
    if not isinstance(zenodo, dict):
        zenodo = {}
    status = str(publication.get("status", "pending_publication"))
    anonymous_download = status == "published"
    result: dict[str, Any] = {
        "status": status,
        "anonymous_download": anonymous_download,
        "version": version,
        "github_release": {
            "repository": repository_slug,
            "tag": tag,
            "release_url": release_url,
            "archive_url": f"{download_root}/{archive_name}",
            "sha256_url": f"{download_root}/{sha256_sidecar}",
            "sigstore_bundle_url": f"{download_root}/{sigstore_bundle}",
        },
    }
    if zenodo:
        result["zenodo"] = {
            "version_doi": zenodo.get("version_doi"),
            "concept_doi": zenodo.get("concept_doi"),
            "archive_url": zenodo.get("archive_url"),
        }
    return result


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StageCError(f"invalid {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise StageCError(f"{label} must be a JSON object: {path}")
    return payload


def _safe_relative(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise StageCError(f"{label} must be a non-empty relative path")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise StageCError(f"unsafe {label}: {value!r}")
    return path.as_posix()


def _load_policy(path: Path) -> dict[str, Any]:
    payload = _load_json(path, "Stage C policy")
    if payload.get("schema_version") != "taskgenome.stage-c-release.v1":
        raise StageCError("unsupported Stage C policy schema")
    code_export = payload.get("code_export")
    if not isinstance(code_export, dict):
        raise StageCError("Stage C policy is missing code_export")
    for key in (
        "forbidden_path_components",
        "forbidden_basenames",
        "forbidden_content_regexes",
        "copy_files",
        "copy_globs",
    ):
        if not isinstance(code_export.get(key), list):
            raise StageCError(f"Stage C policy code_export.{key} must be an array")
    exemptions = code_export.get("content_scan_exemptions", {})
    if not isinstance(exemptions, dict) or any(
        not isinstance(path, str)
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        for path, digest in exemptions.items()
    ):
        raise StageCError(
            "Stage C policy code_export.content_scan_exemptions must map paths to SHA-256"
        )
    return payload


def _source_revision(root: Path) -> tuple[str | None, bool | None]:
    try:
        revision = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status_output = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None, None
    return revision, bool(status_output.strip())


def _destination_violation(path: str, policy: dict[str, Any]) -> str | None:
    code_export = policy["code_export"]
    parts = tuple(part.lower() for part in PurePosixPath(path).parts)
    forbidden_parts = {str(value).lower() for value in code_export["forbidden_path_components"]}
    forbidden_names = {str(value).lower() for value in code_export["forbidden_basenames"]}
    overlap = sorted(set(parts) & forbidden_parts)
    if overlap:
        return f"forbidden path component {overlap[0]!r}"
    if parts and parts[-1] in forbidden_names:
        return f"forbidden basename {parts[-1]!r}"
    return None


def _scan_text(path: Path, destination: str, policy: dict[str, Any]) -> list[str]:
    if path.suffix.lower() not in TEXT_SUFFIXES and path.name != ".gitignore":
        return []
    exemptions = policy["code_export"].get("content_scan_exemptions", {})
    if exemptions.get(destination) == _sha256_file(path):
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"cannot scan {destination}: {exc}"]
    violations: list[str] = []
    for pattern in policy["code_export"]["forbidden_content_regexes"]:
        try:
            if re.search(str(pattern), text):
                violations.append(f"forbidden content in {destination}: {pattern}")
        except re.error as exc:
            raise StageCError(f"invalid forbidden-content regex {pattern!r}: {exc}") from exc
    return violations


def _selected_code_files(
    root: Path,
    policy: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    code_export = policy["code_export"]
    selected: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    def add(source_value: str, destination_value: str) -> None:
        try:
            source_rel = _safe_relative(source_value, "code source path")
            destination = _safe_relative(destination_value, "code destination path")
        except StageCError as exc:
            errors.append(str(exc))
            return
        source = root / source_rel
        if source.is_symlink() or not source.is_file():
            errors.append(f"missing or unsafe code source: {source_rel}")
            return
        violation = _destination_violation(destination, policy)
        if violation:
            errors.append(f"{destination}: {violation}")
            return
        if destination in selected:
            errors.append(
                f"duplicate code destination {destination}: "
                f"{selected[destination]['source_relpath']} and {source_rel}"
            )
            return
        mode = "0755" if source.stat().st_mode & stat.S_IXUSR else "0644"
        record = {
            "source_relpath": source_rel,
            "path": destination,
            "sha256": _sha256_file(source),
            "size": source.stat().st_size,
            "mode": mode,
            "source": source,
        }
        selected[destination] = record
        errors.extend(_scan_text(source, destination, policy))

    for mapping in code_export["copy_files"]:
        if not isinstance(mapping, dict):
            errors.append("code_export.copy_files entries must be objects")
            continue
        add(str(mapping.get("source") or ""), str(mapping.get("destination") or ""))

    for rule in code_export["copy_globs"]:
        if not isinstance(rule, dict):
            errors.append("code_export.copy_globs entries must be objects")
            continue
        pattern = str(rule.get("pattern") or "")
        try:
            strip_prefix = _safe_relative(
                str(rule.get("strip_prefix") or "placeholder"),
                "glob strip_prefix",
            )
            if not rule.get("strip_prefix"):
                strip_prefix = ""
            destination_root = str(rule.get("destination_root") or "")
            if destination_root:
                destination_root = _safe_relative(destination_root, "glob destination_root")
        except StageCError as exc:
            errors.append(str(exc))
            continue
        matches = sorted(path for path in root.glob(pattern) if path.is_file())
        if not matches:
            errors.append(f"code glob matched no files: {pattern}")
            continue
        for source in matches:
            source_rel = source.relative_to(root).as_posix()
            if strip_prefix:
                prefix = PurePosixPath(strip_prefix)
                source_path = PurePosixPath(source_rel)
                try:
                    relative = source_path.relative_to(prefix)
                except ValueError:
                    errors.append(
                        f"glob source {source_rel} is outside strip_prefix {strip_prefix}"
                    )
                    continue
            else:
                relative = PurePosixPath(source_rel)
            destination = (
                PurePosixPath(destination_root) / relative
                if destination_root
                else relative
            ).as_posix()
            add(source_rel, destination)

    records = [selected[path] for path in sorted(selected)]
    return records, sorted(set(errors))


def audit_code(
    root: Path,
    policy: dict[str, Any],
    *,
    require_clean: bool = False,
) -> dict[str, Any]:
    records, errors = _selected_code_files(root, policy)
    revision, dirty = _source_revision(root)
    if require_clean and dirty is not False:
        errors.append("final code export requires a clean Git worktree")
    return {
        "status": "passed" if not errors else "failed",
        "version": policy["versions"]["code"],
        "file_count": len(records),
        "bytes": sum(int(record["size"]) for record in records),
        "source_revision": revision,
        "source_dirty": dirty,
        "errors": sorted(errors),
        "files": [
            {key: value for key, value in record.items() if key != "source"}
            for record in records
        ],
    }


def _ensure_empty_directory(path: Path) -> None:
    if path.is_symlink():
        raise StageCError(f"output directory must not be a symlink: {path}")
    if path.exists():
        if not path.is_dir():
            raise StageCError(f"output path is not a directory: {path}")
        if any(path.iterdir()):
            raise StageCError(f"output directory must be empty: {path}")
    else:
        path.mkdir(parents=True)


def export_code(
    root: Path,
    output: Path,
    policy: dict[str, Any],
    *,
    require_clean: bool = False,
) -> dict[str, Any]:
    audit = audit_code(root, policy, require_clean=require_clean)
    if audit["status"] != "passed":
        raise StageCError("code export audit failed: " + "; ".join(audit["errors"]))
    records, errors = _selected_code_files(root, policy)
    if errors:
        raise StageCError("code export changed after audit: " + "; ".join(errors))
    _ensure_empty_directory(output)
    for record in records:
        destination = output / record["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(record["source"], destination)
        os.chmod(destination, int(record["mode"], 8))
        if _sha256_file(destination) != record["sha256"]:
            raise StageCError(f"code export changed while copying {record['path']}")
    manifest = {
        "schema_version": "taskgenome.public-code-export.v1",
        "project": policy["project"],
        "version": policy["versions"]["code"],
        "source_revision": audit["source_revision"],
        "source_dirty": audit["source_dirty"],
        "history_requirement": "initialize a new Git repository from this tree",
        "file_count": len(audit["files"]),
        "files": audit["files"],
    }
    manifest_path = output / CODE_MANIFEST
    manifest_path.write_bytes(_json_bytes(manifest))
    os.chmod(manifest_path, 0o644)
    return {
        "status": "built",
        "version": manifest["version"],
        "file_count": manifest["file_count"],
        "output": str(output),
        "manifest_sha256": _sha256_file(manifest_path),
    }


def _iter_tree_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        # Python and pytest create these local caches while running the
        # verifier itself. They are not release assets and must not make a
        # clean checkout fail its own manifest check.
        if (
            ".git" in relative.parts
            or "__pycache__" in relative.parts
            or ".pytest_cache" in relative.parts
        ):
            continue
        if path.suffix == ".pyc":
            continue
        if path.is_file() or path.is_symlink():
            yield path


def verify_code(root: Path, policy: dict[str, Any]) -> dict[str, Any]:
    manifest = _load_json(root / CODE_MANIFEST, "public code manifest")
    if manifest.get("schema_version") != "taskgenome.public-code-export.v1":
        raise StageCError("unsupported public code manifest")
    records = manifest.get("files")
    if not isinstance(records, list):
        raise StageCError("public code manifest files must be an array")
    errors: list[str] = []
    expected: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            errors.append("public code manifest contains a non-object record")
            continue
        try:
            path = _safe_relative(str(record.get("path") or ""), "manifest path")
        except StageCError as exc:
            errors.append(str(exc))
            continue
        if path in expected:
            errors.append(f"duplicate public code manifest path: {path}")
        expected[path] = record
    actual = {
        path.relative_to(root).as_posix(): path
        for path in _iter_tree_files(root)
        if path.relative_to(root).as_posix() != CODE_MANIFEST
    }
    for path in sorted(set(expected) - set(actual)):
        errors.append(f"missing public code file: {path}")
    for path in sorted(set(actual) - set(expected)):
        errors.append(f"unexpected public code file: {path}")
    for path in sorted(set(expected) & set(actual)):
        source = actual[path]
        if source.is_symlink() or not source.is_file():
            errors.append(f"unsafe public code file: {path}")
            continue
        violation = _destination_violation(path, policy)
        if violation:
            errors.append(f"{path}: {violation}")
        if _sha256_file(source) != expected[path].get("sha256"):
            errors.append(f"public code hash mismatch: {path}")
        if source.stat().st_size != expected[path].get("size"):
            errors.append(f"public code size mismatch: {path}")
        errors.extend(_scan_text(source, path, policy))
    return {
        "status": "passed" if not errors else "failed",
        "version": manifest.get("version"),
        "file_count": len(actual),
        "errors": sorted(set(errors)),
    }


def _clean_history_policy(policy: dict[str, Any]) -> dict[str, Any]:
    """Return validated, path-independent defaults for a fresh public repo."""

    raw = policy.get("clean_history", {})
    if not isinstance(raw, dict):
        raise StageCError("clean_history must be an object")
    identity = raw.get("commit_identity", {})
    if not isinstance(identity, dict):
        raise StageCError("clean_history.commit_identity must be an object")

    def non_empty(key: str, default: str) -> str:
        value = identity.get(key, default)
        if not isinstance(value, str) or not value.strip():
            raise StageCError(f"clean_history.commit_identity.{key} must be a non-empty string")
        return value.strip()

    branch = raw.get("branch", DEFAULT_RELEASE_BRANCH)
    if not isinstance(branch, str) or not branch.strip():
        raise StageCError("clean_history.branch must be a non-empty string")
    branch = branch.strip()
    tag = raw.get("tag")
    if tag is not None:
        if not isinstance(tag, str) or not tag.strip():
            raise StageCError("clean_history.tag must be a non-empty string when provided")
        tag = tag.strip()
    message = raw.get("initial_commit_message")
    if message is None:
        message = f"{policy['project']['name']} public release v{policy['versions']['code']}"
    if not isinstance(message, str) or not message.strip():
        raise StageCError("clean_history.initial_commit_message must be a non-empty string")
    commit_date = raw.get("commit_date", DEFAULT_RELEASE_COMMIT_DATE)
    if not isinstance(commit_date, str) or not commit_date.strip():
        raise StageCError("clean_history.commit_date must be a non-empty string")
    normalized_date = commit_date.strip().replace("Z", "+00:00")
    try:
        parsed_date = _datetime.datetime.fromisoformat(normalized_date)
    except ValueError as exc:
        raise StageCError("clean_history.commit_date must be ISO-8601") from exc
    if parsed_date.tzinfo is None:
        raise StageCError("clean_history.commit_date must include a timezone")

    path_patterns = raw.get(
        "forbidden_history_path_regexes",
        [r"(?:^|/)docs/(?:tech_report|blog)(?:/|$)", r"(?:^|/)_runs(?:/|$)"],
    )
    content_patterns = raw.get(
        "forbidden_history_content_regexes",
        [
            # Generic by design: a denylist that spells out the identifier
            # it keeps out of the release would publish that identifier.
            r"(?i)[a-z0-9._%+-]+@(?:gmail|googlemail|qq|163|126|outlook|hotmail|yahoo)[.]com",
            r"(?i)huggingface[.]co/(?:datasets|models)/(?!EvoMapAI/)[A-Za-z0-9._-]+",
            r"(?i)huggingface[.]co/(?!datasets/|models/|EvoMapAI/)[A-Za-z0-9._-]+/",
            r"https://github[.]com/EvoMap/TaskGenome[-]Bench(?:[/\\?#)]|$)",
        ],
    )
    for key, values in (
        ("forbidden_history_path_regexes", path_patterns),
        ("forbidden_history_content_regexes", content_patterns),
    ):
        if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
            raise StageCError(f"clean_history.{key} must be an array of non-empty strings")
        for value in values:
            try:
                re.compile(value)
            except re.error as exc:
                raise StageCError(f"invalid clean_history regex {value!r}: {exc}") from exc
    return {
        "author_name": non_empty("author_name", DEFAULT_RELEASE_AUTHOR_NAME),
        "author_email": non_empty("author_email", DEFAULT_RELEASE_AUTHOR_EMAIL),
        "branch": branch,
        "tag": tag,
        "message": message.strip(),
        "commit_date": commit_date.strip(),
        "parsed_date": parsed_date,
        "path_patterns": list(path_patterns),
        "content_patterns": list(content_patterns),
    }


def _copy_export_tree(source: Path, output: Path) -> None:
    """Copy an already verified export without copying any Git metadata."""

    if source.is_symlink() or not source.is_dir():
        raise StageCError(f"public code export root is missing or unsafe: {source}")
    git_metadata = source / ".git"
    if git_metadata.exists() or git_metadata.is_symlink():
        raise StageCError("public code export must not contain a .git directory")
    _ensure_empty_directory(output)
    for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
        relative = path.relative_to(source)
        if ".git" in relative.parts:
            raise StageCError(f"public code export contains Git metadata: {relative.as_posix()}")
        if path.is_symlink():
            raise StageCError(f"public code export contains a symlink: {relative.as_posix()}")
        if path.is_dir():
            (output / relative).mkdir(parents=True, exist_ok=True)
            continue
        if not path.is_file():
            raise StageCError(f"public code export contains unsupported entry: {relative.as_posix()}")
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        os.chmod(destination, stat.S_IMODE(path.stat().st_mode))


def _history_object_paths(root: Path) -> tuple[list[str], list[str]]:
    """Return object IDs and their optional paths from all reachable refs."""

    raw = _git_output(root, ["rev-list", "--all", "--objects"])
    assert isinstance(raw, str)
    object_ids: list[str] = []
    paths: list[str] = []
    for line in raw.splitlines():
        fields = line.split(" ", 1)
        object_id = fields[0].strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", object_id):
            raise StageCError(f"invalid object ID in git rev-list output: {line!r}")
        object_ids.append(object_id)
        if len(fields) == 2 and fields[1].strip():
            paths.append(fields[1].strip())
    return object_ids, paths


def _scan_history_objects(
    root: Path,
    object_ids: Iterable[str],
    patterns: Iterable[str],
) -> list[str]:
    compiled = [(value, re.compile(value)) for value in patterns]
    violations: list[str] = []
    seen: set[str] = set()
    for object_id in object_ids:
        if object_id in seen:
            continue
        seen.add(object_id)
        kind = _git_output(root, ["cat-file", "-t", object_id])
        assert isinstance(kind, str)
        if kind.strip() not in {"blob", "commit", "tag"}:
            continue
        payload = _git_output(root, ["cat-file", "-p", object_id], text=False)
        assert isinstance(payload, bytes)
        decoded = payload.decode("utf-8", errors="replace")
        for pattern, expression in compiled:
            if expression.search(decoded):
                violations.append(f"forbidden history content in object {object_id}: {pattern}")
    return violations


def verify_clean_history(root: Path, policy: dict[str, Any]) -> dict[str, Any]:
    """Verify the candidate is a one-commit, whitelist-only Git repository."""

    errors: list[str] = []
    code = verify_code(root, policy)
    if code["status"] != "passed":
        errors.extend(f"code export: {error}" for error in code["errors"])
    history = _clean_history_policy(policy)

    try:
        refs_raw = _git_output(root, ["for-each-ref", "--format=%(refname)"])
        assert isinstance(refs_raw, str)
        refs = sorted(line.strip() for line in refs_raw.splitlines() if line.strip())
        expected_refs = [f"refs/heads/{history['branch']}"]
        if history["tag"]:
            expected_refs.append(f"refs/tags/{history['tag']}")
        if refs != sorted(expected_refs):
            errors.append(f"unexpected refs: expected {sorted(expected_refs)}, found {refs}")

        branch = _git_output(root, ["symbolic-ref", "--short", "HEAD"])
        assert isinstance(branch, str)
        if branch.strip() != history["branch"]:
            errors.append(f"candidate HEAD is not on {history['branch']!r}")
        count = _git_output(root, ["rev-list", "--count", "--all"])
        assert isinstance(count, str)
        commit_count = int(count.strip())
        if commit_count != 1:
            errors.append(f"candidate history must contain exactly one commit, found {commit_count}")
        head = _git_output(root, ["rev-parse", "HEAD"])
        assert isinstance(head, str)
        head = head.strip()
        parents = _git_output(root, ["rev-list", "--parents", "-n", "1", "HEAD"])
        assert isinstance(parents, str)
        if len(parents.split()) != 1:
            errors.append("candidate initial commit unexpectedly has a parent")
        tree = _git_output(root, ["rev-parse", "HEAD^{tree}"])
        assert isinstance(tree, str)
        tree = tree.strip()

        commit_identity = _git_output(root, ["show", "-s", "--format=%an%n%ae%n%ad%n%cn%n%ce%n%cd", "HEAD"])
        assert isinstance(commit_identity, str)
        identity_lines = commit_identity.splitlines()
        if len(identity_lines) >= 2:
            if identity_lines[0] != history["author_name"] or identity_lines[1] != history["author_email"]:
                errors.append("initial commit author does not match clean_history.commit_identity")
        if len(identity_lines) >= 5:
            if identity_lines[3] != history["author_name"] or identity_lines[4] != history["author_email"]:
                errors.append("initial commit committer does not match clean_history.commit_identity")
        message = _git_output(root, ["show", "-s", "--format=%B", "HEAD"])
        assert isinstance(message, str)
        if message.strip() != history["message"]:
            errors.append("initial commit message does not match clean_history.initial_commit_message")
        dates = _git_output(root, ["show", "-s", "--format=%aI%n%cI", "HEAD"])
        assert isinstance(dates, str)
        date_lines = dates.splitlines()
        if len(date_lines) >= 2:
            try:
                actual_author_date = _datetime.datetime.fromisoformat(date_lines[0])
                actual_committer_date = _datetime.datetime.fromisoformat(date_lines[1])
            except ValueError:
                errors.append("initial commit dates are not ISO-8601")
            else:
                expected_date = history["parsed_date"]
                if actual_author_date != expected_date or actual_committer_date != expected_date:
                    errors.append("initial commit date does not match clean_history.commit_date")
        if history["tag"]:
            tag_target = _git_output(root, ["rev-parse", f"refs/tags/{history['tag']}^{{commit}}"])
            assert isinstance(tag_target, str)
            if tag_target.strip() != head:
                errors.append(f"tag {history['tag']!r} does not point at HEAD")

        manifest = _load_json(root / CODE_MANIFEST, "public code manifest")
        expected_files = {CODE_MANIFEST}
        expected_files.update(str(record.get("path")) for record in manifest.get("files", []) if isinstance(record, dict))
        actual_files_raw = _git_output(root, ["ls-tree", "-r", "--name-only", "HEAD"])
        assert isinstance(actual_files_raw, str)
        actual_files = {line.strip() for line in actual_files_raw.splitlines() if line.strip()}
        if actual_files != expected_files:
            missing = sorted(expected_files - actual_files)
            extra = sorted(actual_files - expected_files)
            if missing:
                errors.append(f"manifest files missing from Git tree: {missing}")
            if extra:
                errors.append(f"Git tree contains files outside manifest: {extra}")

        object_ids, object_paths = _history_object_paths(root)
        ancestors: set[str] = set()
        for path in expected_files:
            current = PurePosixPath(path)
            while current.as_posix() not in {".", ""}:
                ancestors.add(current.as_posix())
                current = current.parent
        extra_object_paths = sorted(set(object_paths) - ancestors)
        if extra_object_paths:
            errors.append(f"Git history contains object paths outside manifest: {extra_object_paths}")
        for pattern in history["path_patterns"]:
            expression = re.compile(pattern)
            for path in sorted(set(object_paths)):
                if expression.search(path):
                    errors.append(f"forbidden history path {path!r}: {pattern}")
        errors.extend(_scan_history_objects(root, object_ids, history["content_patterns"]))
        fsck = _git_output(root, ["fsck", "--full", "--no-reflogs", "--unreachable"])
        assert isinstance(fsck, str)
        if fsck.strip():
            errors.append("candidate Git repository has unreachable objects")
    except (StageCError, ValueError, AssertionError) as exc:
        errors.append(str(exc))

    return {
        "status": "passed" if not errors else "failed",
        "branch": history["branch"],
        "tag": history["tag"],
        "commit": locals().get("head"),
        "tree": locals().get("tree"),
        "commit_count": locals().get("commit_count"),
        "manifest_sha256": (
            _sha256_file(root / CODE_MANIFEST) if (root / CODE_MANIFEST).is_file() else None
        ),
        "object_count": len(locals().get("object_ids", [])),
        "object_path_count": len(set(locals().get("object_paths", []))),
        "errors": sorted(set(error for error in errors if error)),
    }


def init_code_repo(
    root: Path,
    output: Path,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Create and verify a fresh single-commit repository from an export."""

    verification = verify_code(root, policy)
    if verification["status"] != "passed":
        raise StageCError("cannot initialize Git history from an unverified export")
    history = _clean_history_policy(policy)
    source_resolved = root.resolve()
    output_resolved = output.resolve()
    if output_resolved == source_resolved or source_resolved in output_resolved.parents:
        raise StageCError("fresh Git repository output must be outside the export root")
    _copy_export_tree(root, output)

    git_env = os.environ.copy()
    # Ignore a maintainer's global/system identity and date configuration.  A
    # fixed policy identity/date makes the commit hash reproducible for the
    # same exported bytes, while the source checkout path never enters it.
    git_env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": history["author_name"],
            "GIT_AUTHOR_EMAIL": history["author_email"],
            "GIT_COMMITTER_NAME": history["author_name"],
            "GIT_COMMITTER_EMAIL": history["author_email"],
            "GIT_AUTHOR_DATE": history["commit_date"],
            "GIT_COMMITTER_DATE": history["commit_date"],
        }
    )
    _git_output(
        output,
        ["init", "--initial-branch", history["branch"], "--object-format", "sha1"],
        env=git_env,
    )
    _git_output(output, ["config", "user.name", history["author_name"]], env=git_env)
    _git_output(output, ["config", "user.email", history["author_email"]], env=git_env)
    _git_output(output, ["config", "core.autocrlf", "false"], env=git_env)
    _git_output(output, ["config", "core.filemode", "true"], env=git_env)
    _git_output(output, ["config", "commit.gpgSign", "false"], env=git_env)
    _git_output(output, ["add", "--all"], env=git_env)
    _git_output(output, ["commit", "--no-gpg-sign", "--message", history["message"]], env=git_env)
    if history["tag"]:
        _git_output(output, ["check-ref-format", f"refs/tags/{history['tag']}"], env=git_env)
        _git_output(output, ["tag", history["tag"]], env=git_env)
    result = verify_clean_history(output, policy)
    if result["status"] != "passed":
        raise StageCError("fresh Git history verification failed: " + "; ".join(result["errors"]))
    return {"output": str(output), **result, "status": "built"}


def _normalized_tarinfo(name: str, *, size: int = 0, mode: int = 0o644, is_dir: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name + ("/" if is_dir and not name.endswith("/") else ""))
    info.type = tarfile.DIRTYPE if is_dir else tarfile.REGTYPE
    info.size = 0 if is_dir else size
    info.mode = 0o755 if is_dir else mode
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    return info


def _add_bytes(archive: tarfile.TarFile, name: str, payload: bytes, mode: int = 0o644) -> None:
    archive.addfile(_normalized_tarinfo(name, size=len(payload), mode=mode), io.BytesIO(payload))


def package_data(
    public_root: Path,
    output_dir: Path,
    policy: dict[str, Any],
    *,
    record_path: Path | None = None,
) -> dict[str, Any]:
    verification = release_assets.verify_release(public_root, None)
    if verification.get("status") != "passed":
        raise StageCError("public bundle verification failed before packaging")
    release = _load_json(public_root / "release.json", "public release manifest")
    data_policy = policy["public_data"]
    if release.get("release_id") != data_policy["release_id"]:
        raise StageCError("public bundle release ID does not match Stage C policy")
    if release.get("asset_count") != data_policy["expected_asset_count"]:
        raise StageCError("public bundle asset count does not match Stage C policy")
    asset_bytes = sum(int(record.get("size", -1)) for record in release.get("assets", []))
    if asset_bytes != data_policy["expected_unpacked_asset_bytes"]:
        raise StageCError("public bundle byte count does not match Stage C policy")

    output_dir.mkdir(parents=True, exist_ok=True)
    basename = data_policy["artifact_basename"]
    archive_path = output_dir / f"{basename}.tar.gz"
    sidecar_path = output_dir / f"{archive_path.name}.sha256"
    for path in (archive_path, sidecar_path):
        if path.exists() or path.is_symlink():
            raise StageCError(f"refusing to overwrite release artifact: {path}")

    license_path = ROOT / "release" / "public_code" / "DATA_LICENSE.md"
    if not license_path.is_file():
        license_path = ROOT / "DATA_LICENSE.md"
    if not license_path.is_file():
        raise StageCError("public data license template is missing")

    artifact_manifest = {
        "schema_version": "taskgenome.public-data-artifact.v1",
        "project": policy["project"]["name"],
        "version": policy["versions"]["public_data"],
        "release_id": release["release_id"],
        "license": policy["licenses"]["public_data"]["spdx"],
        "asset_count": release["asset_count"],
        "asset_bytes": asset_bytes,
        "asset_merkle_root": release["asset_merkle_root"],
        "public_release_manifest_sha256": _sha256_file(public_root / "release.json"),
        "archive_root": basename,
    }
    readme = (
        f"# {policy['project']['name']} public data v{policy['versions']['public_data']}\n\n"
        "The verified public bundle is under `data/`. Run the release verifier "
        "against that directory before use. Private judges and authoring assets "
        "are not included.\n"
    ).encode("utf-8")
    virtual = {
        f"{basename}/ARTIFACT.json": _json_bytes(artifact_manifest),
        f"{basename}/LICENSE-DATA.md": license_path.read_bytes(),
        f"{basename}/README.md": readme,
    }
    public_files = list(_iter_tree_files(public_root))
    for path in public_files:
        if path.is_symlink() or not path.is_file():
            raise StageCError(f"public bundle contains a symlink or non-file: {path}")

    member_names = list(virtual)
    member_names.extend(
        f"{basename}/data/{path.relative_to(public_root).as_posix()}"
        for path in public_files
    )
    directories = {basename, f"{basename}/data"}
    for name in member_names:
        current = PurePosixPath(name).parent
        while current.as_posix() not in {".", ""}:
            directories.add(current.as_posix())
            current = current.parent

    with archive_path.open("xb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as zipped:
            with tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for directory in sorted(directories):
                    archive.addfile(_normalized_tarinfo(directory, is_dir=True))
                for name, payload in sorted(virtual.items()):
                    _add_bytes(archive, name, payload)
                for source in public_files:
                    relative = source.relative_to(public_root).as_posix()
                    name = f"{basename}/data/{relative}"
                    mode = 0o755 if source.stat().st_mode & stat.S_IXUSR else 0o644
                    with source.open("rb") as handle:
                        archive.addfile(
                            _normalized_tarinfo(name, size=source.stat().st_size, mode=mode),
                            handle,
                        )
    os.chmod(archive_path, 0o644)
    archive_sha256 = _sha256_file(archive_path)
    sidecar_path.write_text(f"{archive_sha256}  {archive_path.name}\n", encoding="utf-8")
    os.chmod(sidecar_path, 0o644)
    record = {
        **artifact_manifest,
        "archive": archive_path.name,
        "archive_sha256": archive_sha256,
        "archive_bytes": archive_path.stat().st_size,
        "sha256_sidecar": sidecar_path.name,
        "sigstore_bundle": f"{archive_path.name}.sigstore.json",
    }
    record["publication"] = _publication_metadata(
        policy,
        archive_name=record["archive"],
        sha256_sidecar=record["sha256_sidecar"],
        sigstore_bundle=record["sigstore_bundle"],
    )
    if record_path is not None:
        if record_path.exists() or record_path.is_symlink():
            raise StageCError(f"refusing to overwrite artifact record: {record_path}")
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_bytes(_json_bytes(record))
        os.chmod(record_path, 0o644)
    return {"status": "built", **record}


def verify_package(
    archive_path: Path,
    sidecar_path: Path | None = None,
    record_path: Path | None = None,
) -> dict[str, Any]:
    if archive_path.is_symlink() or not archive_path.is_file():
        raise StageCError(f"missing or unsafe data archive: {archive_path}")
    digest = _sha256_file(archive_path)
    errors: list[str] = []
    if sidecar_path is not None:
        try:
            line = sidecar_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise StageCError(f"cannot read SHA-256 sidecar: {exc}") from exc
        expected = f"{digest}  {archive_path.name}"
        if line != expected:
            errors.append("archive SHA-256 sidecar mismatch")
    artifact: dict[str, Any] | None = None
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            members = archive.getmembers()
            roots: set[str] = set()
            for member in members:
                path = PurePosixPath(member.name)
                if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
                    errors.append(f"unsafe archive member: {member.name}")
                    continue
                roots.add(path.parts[0])
                if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                    errors.append(f"unsupported archive member type: {member.name}")
            if len(roots) != 1:
                errors.append("data archive must contain exactly one top-level directory")
            else:
                artifact_name = f"{next(iter(roots))}/ARTIFACT.json"
                try:
                    extracted = archive.extractfile(artifact_name)
                    if extracted is None:
                        raise KeyError(artifact_name)
                    loaded = json.loads(extracted.read().decode("utf-8"))
                    if not isinstance(loaded, dict):
                        raise ValueError("ARTIFACT.json is not an object")
                    artifact = loaded
                except (KeyError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    errors.append(f"invalid ARTIFACT.json: {exc}")
    except (OSError, tarfile.TarError) as exc:
        errors.append(f"invalid data archive: {exc}")
    result = {
        "status": "passed" if not errors else "failed",
        "archive": archive_path.name,
        "archive_sha256": digest,
        "archive_bytes": archive_path.stat().st_size,
        "artifact": artifact,
        "errors": sorted(set(errors)),
    }
    if record_path is not None:
        record = _load_json(record_path, "public data artifact record")
        result["record"] = record
        for key, actual in (
            ("archive", archive_path.name),
            ("archive_sha256", digest),
            ("archive_bytes", archive_path.stat().st_size),
        ):
            if record.get(key) != actual:
                errors.append(f"artifact record {key} mismatch")
        if sidecar_path is not None and record.get("sha256_sidecar") != sidecar_path.name:
            errors.append("artifact record sha256_sidecar mismatch")
        if artifact is not None:
            for key in (
                "archive_root",
                "asset_bytes",
                "asset_count",
                "asset_merkle_root",
                "license",
                "project",
                "public_release_manifest_sha256",
                "release_id",
                "schema_version",
                "version",
            ):
                if record.get(key) != artifact.get(key):
                    errors.append(f"archive ARTIFACT.json {key} mismatch")
        publication = record.get("publication")
        if not isinstance(publication, dict):
            errors.append("artifact record publication metadata is missing")
        else:
            version = str(record.get("version", ""))
            status = publication.get("status")
            if status not in {"pending_publication", "published"}:
                errors.append("artifact record publication status is invalid")
            elif publication.get("anonymous_download") is not (status == "published"):
                errors.append("publication anonymous_download flag disagrees with status")
            github = publication.get("github_release")
            if not isinstance(github, dict):
                errors.append("artifact record github_release metadata is missing")
            else:
                if publication.get("version") != version:
                    errors.append("publication version differs from artifact version")
                if github.get("tag") != f"v{version}":
                    errors.append("GitHub Release tag differs from artifact version")
                expected_suffixes = {
                    "archive_url": archive_path.name,
                    "sha256_url": str(record.get("sha256_sidecar", "")),
                    "sigstore_bundle_url": str(record.get("sigstore_bundle", "")),
                }
                for field, suffix in expected_suffixes.items():
                    value = github.get(field)
                    if not isinstance(value, str) or not value.endswith(suffix):
                        errors.append(f"GitHub Release {field} does not name the pinned asset")
        result["errors"] = sorted(set(errors))
        result["status"] = "passed" if not result["errors"] else "failed"
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit = subparsers.add_parser("audit-code", help="audit the clean code whitelist")
    audit.add_argument("--policy", default=str(DEFAULT_POLICY))
    audit.add_argument("--require-clean", action="store_true")

    export = subparsers.add_parser("export-code", help="build a fresh public code tree")
    export.add_argument("--policy", default=str(DEFAULT_POLICY))
    export.add_argument("--out", required=True)
    export.add_argument("--require-clean", action="store_true")

    verify = subparsers.add_parser("verify-code", help="verify an exported code tree")
    verify.add_argument("--root", required=True)
    verify.add_argument("--policy", default=None)

    init_repo = subparsers.add_parser(
        "init-code-repo",
        help="copy a verified export into a new one-commit Git repository",
    )
    init_repo.add_argument("--root", required=True, help="verified export without .git")
    init_repo.add_argument("--out", required=True, help="empty destination for the new repository")
    init_repo.add_argument("--policy", default=None)

    history = subparsers.add_parser(
        "verify-history",
        help="verify a fresh public repository has one clean, whitelist-only history",
    )
    history.add_argument("--root", required=True)
    history.add_argument("--policy", default=None)

    package = subparsers.add_parser("package-data", help="package a verified public data bundle")
    package.add_argument("--public", required=True)
    package.add_argument("--out", required=True)
    package.add_argument("--policy", default=str(DEFAULT_POLICY))
    package.add_argument("--record", default=None)

    package_verify = subparsers.add_parser("verify-package", help="verify a public data archive")
    package_verify.add_argument("--archive", required=True)
    package_verify.add_argument("--sha256-file", default=None)
    package_verify.add_argument("--record", default=None)
    return parser


def _print(payload: object, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), file=stream)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "audit-code":
            policy = _load_policy(Path(args.policy).resolve())
            result = audit_code(ROOT, policy, require_clean=args.require_clean)
        elif args.command == "export-code":
            policy = _load_policy(Path(args.policy).resolve())
            result = export_code(
                ROOT,
                Path(args.out).absolute(),
                policy,
                require_clean=args.require_clean,
            )
        elif args.command == "verify-code":
            code_root = Path(args.root).resolve()
            policy_path = (
                Path(args.policy).resolve()
                if args.policy
                else code_root / "release" / "stage_c_release.v1.json"
            )
            result = verify_code(code_root, _load_policy(policy_path))
        elif args.command == "init-code-repo":
            export_root = Path(args.root).resolve()
            policy_path = (
                Path(args.policy).resolve()
                if args.policy
                else export_root / "release" / "stage_c_release.v1.json"
            )
            result = init_code_repo(
                export_root,
                Path(args.out).absolute(),
                _load_policy(policy_path),
            )
        elif args.command == "verify-history":
            repo_root = Path(args.root).resolve()
            policy_path = (
                Path(args.policy).resolve()
                if args.policy
                else repo_root / "release" / "stage_c_release.v1.json"
            )
            result = verify_clean_history(repo_root, _load_policy(policy_path))
        elif args.command == "package-data":
            policy = _load_policy(Path(args.policy).resolve())
            result = package_data(
                Path(args.public).resolve(),
                Path(args.out).absolute(),
                policy,
                record_path=Path(args.record).absolute() if args.record else None,
            )
        else:
            result = verify_package(
                Path(args.archive).resolve(),
                Path(args.sha256_file).resolve() if args.sha256_file else None,
                Path(args.record).resolve() if args.record else None,
            )
        _print(result)
        return 0 if result.get("status") in {"passed", "built"} else 1
    except (OSError, StageCError, release_assets.ReleaseError) as exc:
        _print({"status": "error", "error": str(exc)}, stream=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

from jsonschema import Draft202012Validator


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS = REPO_ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import release_assets  # noqa: E402
import public_data_smoke  # noqa: E402
import stage_c_release  # noqa: E402


def _code_policy() -> dict:
    return {
        "schema_version": "taskgenome.stage-c-release.v1",
        "project": {
            "name": "Fixture",
            "repository_slug": "example/fixture",
            "code_repository_url": "https://example.invalid/fixture",
        },
        "versions": {"code": "1.0.0", "public_data": "1.0.0", "private_assets": "1.0.0"},
        "licenses": {"public_data": {"spdx": "CC-BY-4.0"}},
        "public_data": {
            "artifact_basename": "fixture-public-data-v1.0.0",
            "release_id": "1" * 24,
            "expected_asset_count": 1,
            "expected_unpacked_asset_bytes": len(b"Public task\n"),
        },
        "code_export": {
            "forbidden_path_components": [".git", "_runs", "tasks_final", "judge", "gold", "oracle", "trace"],
            "forbidden_basenames": ["reference_solution.py", "test_script.py"],
            "forbidden_content_regexes": ["-----BEGIN PRIVATE KEY-----"],
            "copy_files": [
                {"source": "runner.py", "destination": "runner.py"},
                {"source": "docs/README.md", "destination": "README.md"},
            ],
            "copy_globs": [],
        },
    }


def test_checked_in_stage_c_policy_validates_and_metrics_header_is_allowlisted() -> None:
    schema = json.loads(
        (REPO_ROOT / "schemas/stage-c-release.schema.json").read_text(encoding="utf-8")
    )
    policy = json.loads(
        (REPO_ROOT / "release/stage_c_release.v1.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(policy)
    artifact_schema = json.loads(
        (REPO_ROOT / "schemas/public-data-artifact.schema.json").read_text(encoding="utf-8")
    )
    artifact = json.loads(
        (REPO_ROOT / "release/public_data_artifact.v1.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(artifact_schema).validate(artifact)
    assert artifact["version"] == policy["versions"]["public_data"]
    assert artifact["release_id"] == policy["public_data"]["release_id"]
    assert artifact["asset_count"] == policy["public_data"]["expected_asset_count"]
    assert artifact["asset_bytes"] == policy["public_data"]["expected_unpacked_asset_bytes"]
    with (REPO_ROOT / "results/task_metrics.csv").open(encoding="utf-8", newline="") as handle:
        header = next(csv.reader(handle))
    assert header == policy["per_task_metrics"]["allowed_fields"]
    assert policy["evaluation"]["hidden_test"]["task_count"] == 778
    assert policy["evaluation"]["public_dev"]["scored"] is False
    publication = artifact["publication"]
    assert publication["version"] == artifact["version"] == policy["versions"]["public_data"]
    assert publication["github_release"]["tag"] == "v1.0.1"
    assert publication["github_release"]["archive_url"].endswith(artifact["archive"])
    assert publication["github_release"]["sha256_url"].endswith(artifact["sha256_sidecar"])
    # anonymous_download is derived, never asserted on its own: bytes can be
    # uploaded while the repository is still private.
    assert publication["anonymous_download"] is (publication["status"] == "published")
    policy_publication = policy["public_data"]["publication"]
    assert policy_publication["status"] == publication["status"]
    assert policy_publication["github_release"]["repository"] == publication["github_release"]["repository"]
    assert policy_publication["github_release"]["tag"] == publication["github_release"]["tag"]


def test_code_export_is_whitelisted_hash_bound_and_has_no_inherited_git(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "docs").mkdir()
    (source / "runner.py").write_text("print('safe')\n", encoding="utf-8")
    (source / "docs/README.md").write_text("# Fixture\n", encoding="utf-8")
    policy = _code_policy()

    audit = stage_c_release.audit_code(source, policy)
    assert audit["status"] == "passed"
    assert audit["file_count"] == 2

    output = tmp_path / "public-code"
    built = stage_c_release.export_code(source, output, policy)
    assert built["status"] == "built"
    assert not (output / ".git").exists()
    assert stage_c_release.verify_code(output, policy)["status"] == "passed"

    (output / "judge").mkdir()
    (output / "judge/secret.txt").write_text("hidden\n", encoding="utf-8")
    verified = stage_c_release.verify_code(output, policy)
    assert verified["status"] == "failed"
    assert any("unexpected public code file" in error for error in verified["errors"])


def test_fresh_history_is_single_commit_manifest_bound_and_reproducible(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "docs").mkdir()
    (source / "runner.py").write_text("print('safe')\n", encoding="utf-8")
    (source / "docs/README.md").write_text("# Fixture\n", encoding="utf-8")
    policy = _code_policy()
    export = tmp_path / "export"
    stage_c_release.export_code(source, export, policy)

    first = tmp_path / "candidate-a"
    second = tmp_path / "candidate-b"
    built_a = stage_c_release.init_code_repo(export, first, policy)
    built_b = stage_c_release.init_code_repo(export, second, policy)

    assert built_a["status"] == "built"
    assert built_a["commit"] == built_b["commit"]
    assert built_a["commit_count"] == 1
    assert stage_c_release.verify_clean_history(first, policy)["status"] == "passed"
    assert subprocess.run(
        ["git", "-C", str(first), "rev-list", "--parents", "-n", "1", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split() == [built_a["commit"]]
    assert subprocess.run(
        ["git", "-C", str(first), "show", "-s", "--format=%an <%ae>", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == "Codex Release Builder <codex-release@users.noreply.github.com>"


def test_fresh_history_refuses_inherited_git_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "docs").mkdir()
    (source / "runner.py").write_text("print('safe')\n", encoding="utf-8")
    (source / "docs/README.md").write_text("# Fixture\n", encoding="utf-8")
    policy = _code_policy()
    export = tmp_path / "export"
    stage_c_release.export_code(source, export, policy)
    (export / ".git").mkdir()
    try:
        stage_c_release.init_code_repo(export, tmp_path / "candidate", policy)
    except stage_c_release.StageCError as exc:
        assert "must not contain a .git" in str(exc)
    else:
        raise AssertionError("inherited .git metadata was accepted")


def test_code_audit_rejects_forbidden_destination_and_private_key(tmp_path: Path) -> None:
    (tmp_path / "runner.py").write_text(
        "SECRET = '-----BEGIN PRIVATE KEY-----'\n", encoding="utf-8"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/README.md").write_text("safe\n", encoding="utf-8")
    policy = _code_policy()
    policy["code_export"]["copy_files"][0]["destination"] = "judge/runner.py"
    audit = stage_c_release.audit_code(tmp_path, policy)
    assert audit["status"] == "failed"
    assert any("forbidden path component" in error for error in audit["errors"])


def _public_bundle(root: Path) -> str:
    task = root / "tasks/T0001/task.md"
    task.parent.mkdir(parents=True)
    task.write_bytes(b"Public task\n")
    task.chmod(0o644)
    record = {
        "bundle_path": "tasks/T0001/task.md",
        "family": "code_generation",
        "execution_mode": "subprocess_cli",
        "materialize": True,
        "mode": "0644",
        "role": "prompt.task",
        "sha256": release_assets._sha256_file(task),
        "size": task.stat().st_size,
        "source_relpath": "scenarios/T0001/task.md",
        "task_id": "T0001",
    }
    public_root = release_assets._merkle_root([record])
    release_id = release_assets._content_bound_release_id(
        "2" * 64,
        "3" * 64,
        "4" * 64,
        {"public": public_root, "private": "5" * 64},
    )
    release = {
        "schema_version": "1.0.0",
        "release_id": release_id,
        "bundle": "public",
        "canonical": {
            "manifest_sha256": "2" * 64,
            "ordered_task_ids_sha256": "3" * 64,
            "task_count": 1,
            "first_task_id": "T0001",
            "last_task_id": "T0001",
            "summary": {
                "total_tasks": 1,
                "by_family": {"code_generation": 1},
                "by_execution_mode": {"subprocess_cli": 1},
                "by_source": {"fixture": 1},
            },
        },
        "policy_sha256": "4" * 64,
        "asset_count": 1,
        "asset_merkle_root": public_root,
        "bundle_roots": {"public": public_root, "private": "5" * 64},
        "assets": [record],
    }
    (root / "release.json").write_text(
        json.dumps(release, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    release_assets._write_checksums(root)
    return release_id


def test_public_data_package_is_standalone_verified_and_deterministic(tmp_path: Path) -> None:
    public = tmp_path / "public"
    public.mkdir()
    release_id = _public_bundle(public)
    policy = _code_policy()
    policy["public_data"]["release_id"] = release_id

    first = tmp_path / "first"
    second = tmp_path / "second"
    record_path = tmp_path / "artifact-record.json"
    built_a = stage_c_release.package_data(public, first, policy, record_path=record_path)
    built_b = stage_c_release.package_data(public, second, policy)
    assert built_a["archive_sha256"] == built_b["archive_sha256"]
    assert json.loads(record_path.read_text(encoding="utf-8"))["archive_sha256"] == built_a["archive_sha256"]
    assert built_a["publication"]["github_release"]["tag"] == "v1.0.0"
    assert built_a["publication"]["anonymous_download"] is False

    archive = first / built_a["archive"]
    sidecar = first / built_a["sha256_sidecar"]
    verified = stage_c_release.verify_package(archive, sidecar)
    assert verified["status"] == "passed"
    assert verified["artifact"]["release_id"] == release_id
    verified_record = stage_c_release.verify_package(archive, sidecar, record_path)
    assert verified_record["status"] == "passed"


def test_public_data_smoke_manifest_is_sanitized_and_covers_778_tasks() -> None:
    release = {
        "canonical": {"task_count": 778},
        "assets": [
            {
                "role": "prompt.task",
                "task_id": f"T{task_number:04d}",
                "family": "math_reasoning",
                "execution_mode": "text_short_answer",
            }
            for task_number in range(1, 779)
        ],
    }
    manifest = public_data_smoke._manifest_from_public_release(release)
    assert manifest["summary"] == {"total_tasks": 778}
    assert manifest["tasks"][0]["task_id"] == "T0001"
    assert manifest["tasks"][-1]["task_id"] == "T0778"
    assert all(row["source"] == "public_bundle" for row in manifest["tasks"])
    assert all("source_paths" not in row for row in manifest["tasks"])

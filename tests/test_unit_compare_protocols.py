from __future__ import annotations

import json
from pathlib import Path

import pytest

import compare_runs


def _config(run_dir: Path, protocol: str | None) -> None:
    run_dir.mkdir()
    payload = {} if protocol is None else {"runtime_protocol": protocol}
    (run_dir / "config.json").write_text(json.dumps(payload), encoding="utf-8")


def test_historical_config_without_protocol_is_legacy(tmp_path: Path) -> None:
    run_dir = tmp_path / "historical"
    _config(run_dir, None)
    assert compare_runs.runtime_protocol(run_dir) == "legacy-v1"


def test_cross_protocol_comparison_requires_explicit_override(tmp_path: Path) -> None:
    base = tmp_path / "base"
    gene = tmp_path / "gene"
    _config(base, "legacy-v1")
    _config(gene, "hardened-v2")

    with pytest.raises(SystemExit, match="refusing cross-protocol comparison"):
        compare_runs.require_comparable_protocols(
            base,
            gene,
            allow_cross_protocol=False,
        )
    assert compare_runs.require_comparable_protocols(
        base,
        gene,
        allow_cross_protocol=True,
    ) == ("legacy-v1", "hardened-v2")


def test_config_cannot_hide_mixed_or_mismatched_result_protocols(tmp_path: Path) -> None:
    mismatch = tmp_path / "mismatch"
    _config(mismatch, "legacy-v1")
    (mismatch / "results.jsonl").write_text(
        json.dumps({"runtime_protocol": "hardened-v2"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="config/results protocol mismatch"):
        compare_runs.runtime_protocol(mismatch)

    mixed = tmp_path / "mixed"
    _config(mixed, "legacy-v1")
    (mixed / "results.jsonl").write_text(
        "\n".join(
            json.dumps({"runtime_protocol": protocol})
            for protocol in ("legacy-v1", "hardened-v2")
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="mixed runtime protocols"):
        compare_runs.runtime_protocol(mixed)


IMAGE_A = f"taskgenome/runtime@sha256:{'1' * 64}"
IMAGE_B = f"taskgenome/runtime@sha256:{'2' * 64}"
USER_A = "a" * 64
USER_B = "b" * 64
SYSTEM_A = "c" * 64
SYSTEM_B = "d" * 64
_DEFAULT = object()


def _policy(protocol: str, *, image: str | None = None) -> dict:
    hardened = protocol == "hardened-v2"
    return {
        "protocol": protocol,
        "backend": "docker" if hardened else "host",
        "sandbox_image": (image or IMAGE_A) if hardened else "",
        "memory": "4g",
        "cpus": 2.0,
        "pids_limit": 256,
        "tmpfs_size": "1g",
        "output_limit_bytes": 8 * 1024 * 1024,
        "allow_unpinned_image": False,
    }


def _boundary(*, image_id: str = "sha256:" + "3" * 64) -> dict:
    return {
        "asset_policy_sha256": "4" * 64,
        "io_contract_schema_version": "taskgenome.hardened-io-contract.v1",
        "io_contract_sha256": "5" * 64,
        "image_identity": {
            "image_id": image_id,
            "os": "linux",
            "architecture": "amd64",
        },
    }


def _identity_run(
    run_dir: Path,
    *,
    manifest: str,
    protocol: str = "legacy-v1",
    configured_model_id: str = "provider/model-v1",
    requested_model_id: str | None = "provider/model-v1",
    condition: str = "with_gene",
    user_hash: str | None = USER_A,
    system_hash: str | None = SYSTEM_A,
    score_pass_threshold: float = 1.0,
    runtime_policy: dict | None = None,
    hardened_boundary: object = _DEFAULT,
) -> None:
    run_dir.mkdir()
    if runtime_policy is None:
        runtime_policy = _policy(protocol)
    if hardened_boundary is _DEFAULT:
        hardened_boundary = _boundary() if protocol == "hardened-v2" else None
    config = {
        "runtime_protocol": protocol,
        "prompt_protocol": "legacy-v1",
        "scoring_protocol": "legacy-v1",
        "manifest_sha256": manifest,
        "score_pass_threshold": score_pass_threshold,
        "runtime_policy": runtime_policy,
        "hardened_asset_boundary": hardened_boundary,
        "model_registry": {
            "gemini_flash": [configured_model_id, "gemini", "free"]
        },
    }
    (run_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    record = {
        "runtime_protocol": protocol,
        "prompt_protocol": "legacy-v1",
        "scoring_protocol": "legacy-v1",
        "manifest_sha256": manifest,
        "trial": {
            "trial_key": f"gemini_flash::{condition}::T0001",
            "model": "gemini_flash",
            "condition": condition,
            "task_id": "T0001",
        },
        "eval": {"passed": True},
    }
    if requested_model_id is not None:
        record["model_id"] = requested_model_id
    if user_hash is not None and system_hash is not None:
        record["prompt_sha256"] = {"user": user_hash, "system": system_hash}
    (run_dir / "results.jsonl").write_text(
        json.dumps(record) + "\n",
        encoding="utf-8",
    )


def test_matching_run_identities_are_comparable(tmp_path: Path) -> None:
    base = tmp_path / "base"
    gene = tmp_path / "gene"
    _identity_run(base, manifest="a" * 64)
    _identity_run(gene, manifest="a" * 64)

    base_identity, gene_identity, issues = (
        compare_runs.require_comparable_identities(
            base,
            gene,
            models=["gemini_flash"],
            allow_identity_mismatch=False,
        )
    )
    assert issues == []
    assert base_identity == gene_identity


def test_manifest_or_model_identity_mismatch_requires_explicit_override(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    gene = tmp_path / "gene"
    _identity_run(base, manifest="a" * 64)
    _identity_run(
        gene,
        manifest="b" * 64,
        configured_model_id="provider/model-v2",
        requested_model_id="provider/model-v2",
    )

    with pytest.raises(SystemExit, match="unverified or different identities"):
        compare_runs.require_comparable_identities(
            base,
            gene,
            models=["gemini_flash"],
            allow_identity_mismatch=False,
        )

    _base, _gene, issues = compare_runs.require_comparable_identities(
        base,
        gene,
        models=["gemini_flash"],
        allow_identity_mismatch=True,
    )
    assert any("manifest_sha256 differs" in issue for issue in issues)
    assert any("configured model registry" in issue for issue in issues)
    assert any("requested/configured model id" in issue for issue in issues)


def test_missing_requested_model_identity_is_not_silently_accepted(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    gene = tmp_path / "gene"
    _identity_run(base, manifest="a" * 64, requested_model_id=None)
    _identity_run(gene, manifest="a" * 64)

    with pytest.raises(SystemExit, match="requested/configured model id.*is missing"):
        compare_runs.require_comparable_identities(
            base,
            gene,
            models=["gemini_flash"],
            allow_identity_mismatch=False,
        )


def test_config_cannot_hide_a_different_result_manifest(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _identity_run(run_dir, manifest="a" * 64)
    results_path = run_dir / "results.jsonl"
    record = json.loads(results_path.read_text(encoding="utf-8"))
    record["manifest_sha256"] = "b" * 64
    results_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="config/results manifest_sha256 mismatch"):
        compare_runs.run_identity(run_dir, ["gemini_flash"])


def test_score_threshold_and_runtime_policy_mismatch_are_reported(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    gene = tmp_path / "gene"
    _identity_run(base, manifest="a" * 64)
    changed_policy = _policy("legacy-v1")
    changed_policy["memory"] = "8g"
    _identity_run(
        gene,
        manifest="a" * 64,
        score_pass_threshold=0.75,
        runtime_policy=changed_policy,
    )

    with pytest.raises(SystemExit, match="score_pass_threshold differs"):
        compare_runs.require_comparable_identities(
            base,
            gene,
            models=["gemini_flash"],
            allow_identity_mismatch=False,
        )
    _base, _gene, issues = compare_runs.require_comparable_identities(
        base,
        gene,
        models=["gemini_flash"],
        allow_identity_mismatch=True,
    )
    assert any("score_pass_threshold differs" in issue for issue in issues)
    assert any("runtime_policy.memory differs" in issue for issue in issues)


def test_hardened_container_and_asset_identities_must_match(tmp_path: Path) -> None:
    base = tmp_path / "base"
    gene = tmp_path / "gene"
    _identity_run(base, manifest="a" * 64, protocol="hardened-v2")
    changed_boundary = _boundary(image_id="sha256:" + "6" * 64)
    changed_boundary["asset_policy_sha256"] = "7" * 64
    changed_boundary["io_contract_sha256"] = "8" * 64
    _identity_run(
        gene,
        manifest="a" * 64,
        protocol="hardened-v2",
        runtime_policy=_policy("hardened-v2", image=IMAGE_B),
        hardened_boundary=changed_boundary,
    )

    with pytest.raises(SystemExit, match="unverified or different identities"):
        compare_runs.require_comparable_identities(
            base,
            gene,
            models=["gemini_flash"],
            allow_identity_mismatch=False,
        )
    _base, _gene, issues = compare_runs.require_comparable_identities(
        base,
        gene,
        models=["gemini_flash"],
        allow_identity_mismatch=True,
    )
    assert any("runtime_policy.sandbox_image differs" in issue for issue in issues)
    assert any(
        "hardened_asset_boundary.asset_policy_sha256 differs" in issue
        for issue in issues
    )
    assert any(
        "hardened_asset_boundary.io_contract_sha256 differs" in issue
        for issue in issues
    )
    assert any(
        "hardened_asset_boundary.image_identity differs" in issue
        for issue in issues
    )


def test_user_prompt_hash_mismatch_is_rejected_even_for_gene_treatments(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    gene = tmp_path / "gene"
    _identity_run(base, manifest="a" * 64, user_hash=USER_A)
    _identity_run(gene, manifest="a" * 64, user_hash=USER_B)

    with pytest.raises(SystemExit, match="user prompt hash.*differs"):
        compare_runs.require_comparable_identities(
            base,
            gene,
            models=["gemini_flash"],
            allow_identity_mismatch=False,
        )


def test_non_gene_system_prompt_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    base = tmp_path / "base"
    gene = tmp_path / "gene"
    _identity_run(
        base,
        manifest="a" * 64,
        condition="with_skill",
        system_hash=SYSTEM_A,
    )
    _identity_run(
        gene,
        manifest="a" * 64,
        condition="with_skill",
        system_hash=SYSTEM_B,
    )

    with pytest.raises(SystemExit, match="system prompt hash.*differs"):
        compare_runs.require_comparable_identities(
            base,
            gene,
            models=["gemini_flash"],
            allow_identity_mismatch=False,
        )


def test_gene_system_prompt_hash_may_differ_by_experimental_design(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    gene = tmp_path / "gene"
    _identity_run(base, manifest="a" * 64, system_hash=SYSTEM_A)
    _identity_run(gene, manifest="a" * 64, system_hash=SYSTEM_B)

    _base, _gene, issues = compare_runs.require_comparable_identities(
        base,
        gene,
        models=["gemini_flash"],
        allow_identity_mismatch=False,
    )
    assert issues == []


def test_missing_prompt_hash_is_unverified(tmp_path: Path) -> None:
    base = tmp_path / "base"
    gene = tmp_path / "gene"
    _identity_run(base, manifest="a" * 64, user_hash=None, system_hash=None)
    _identity_run(gene, manifest="a" * 64)

    with pytest.raises(SystemExit, match="prompt hash.*is missing"):
        compare_runs.require_comparable_identities(
            base,
            gene,
            models=["gemini_flash"],
            allow_identity_mismatch=False,
        )

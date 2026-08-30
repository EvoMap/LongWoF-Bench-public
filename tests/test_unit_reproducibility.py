from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import reproducibility
import runtime_policy


def test_schema_versions_are_explicit() -> None:
    assert reproducibility.RUN_CONFIG_SCHEMA_VERSION == "taskgenome.run-config.v2"
    assert reproducibility.RESULT_SCHEMA_VERSION == "taskgenome.result.v2"


def test_sha256_and_tree_digest_are_stable_and_order_independent(tmp_path: Path) -> None:
    first = tmp_path / "a.txt"
    second = tmp_path / "nested" / "b.txt"
    second.parent.mkdir()
    first.write_text("alpha\n", encoding="utf-8")
    second.write_text("beta\n", encoding="utf-8")

    assert reproducibility.sha256_file(first) == (
        "b6a98d9ce9a2d9149288fa3df42d377c3e42737afdcdaf714e33c0a100b51060"
    )
    forward = reproducibility.digest_files([first, second], base=tmp_path)
    reverse = reproducibility.digest_files([second, first, first], base=tmp_path)
    assert forward == reverse
    assert forward["file_count"] == 2


def test_tree_digest_rejects_paths_outside_base(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("not part of the run", encoding="utf-8")
    with pytest.raises(ValueError, match="outside repository root"):
        reproducibility.digest_files([outside], base=base)


def test_tree_digest_can_fingerprint_explicit_external_assets(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "external" / "gene.json"
    outside.parent.mkdir()
    outside.write_text('{"version": 1}\n', encoding="utf-8")

    first = reproducibility.digest_files(
        [outside],
        base=base,
        allow_external=True,
    )
    outside.write_text('{"version": 2}\n', encoding="utf-8")
    second = reproducibility.digest_files(
        [outside],
        base=base,
        allow_external=True,
    )
    assert first["file_count"] == 1
    assert second["file_count"] == 1
    assert first["digest"] != second["digest"]
    assert str(outside) not in json.dumps(first)


def test_tree_digest_rejects_symlinks_that_escape_base(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = base / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(ValueError, match="outside repository root"):
        reproducibility.digest_files([link], base=base)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://alice:secret@example.com:8443/v1?token=secret#fragment",
            "https://example.com:8443/v1",
        ),
        ("https://example.com/v1/secret-token", "https://example.com"),
        ("https://example.com:invalid/v1", "<redacted-url>"),
        ("http://localhost:8000/v1", "http://localhost:8000/v1"),
        ("", ""),
    ],
)
def test_url_redaction_removes_credentials_and_untrusted_components(
    raw: str, expected: str
) -> None:
    assert reproducibility.redact_url(raw) == expected


def test_argument_and_text_redaction_never_serializes_secret_values() -> None:
    values = {
        "yunwu_key": "yunwu-secret",
        "gemini_key": "gemini-secret",
        "siliconflow_key": "sf-secret",
        "evomap_key": "evomap-secret",
        "sub2api_key": "sub2api-secret",
        "bedrock_key": "bedrock-secret",
        "local_base_url": "https://user:pass@example.com/v1?token=abc",
        "sub2api_base_url": "https://user:pass@sub2api.example/v1?token=def",
        "models": "gemini_flash",
    }
    safe, supplied = reproducibility.redacted_args(values)
    serialized = json.dumps(safe, sort_keys=True)
    secrets = (
        "yunwu-secret",
        "gemini-secret",
        "sf-secret",
        "evomap-secret",
        "sub2api-secret",
        "bedrock-secret",
        "pass",
        "abc",
        "def",
    )
    assert all(secret not in serialized for secret in secrets)
    assert safe["local_base_url"] == "https://example.com/v1"
    assert safe["sub2api_base_url"] == "https://sub2api.example/v1"
    assert all(supplied.values())
    assert reproducibility.redact_text(
        "request failed for bedrock-secret", ["bedrock-secret"]
    ) == "request failed for <redacted>"


def test_credential_sources_record_names_not_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "never-serialize-this")
    monkeypatch.setenv("GEMINI_VERTEX_SERVICE_ACCOUNT_FILE", "/secret/account.json")
    sources = reproducibility.credential_sources(
        {"bedrock_key": "", "yunwu_key": "provided-on-cli"}
    )
    serialized = json.dumps(sources, sort_keys=True)
    assert sources["bedrock_key"] == "env:AWS_BEARER_TOKEN_BEDROCK"
    assert sources["yunwu_key"] == "cli"
    assert sources["gemini_vertex_service_account"] == (
        "env:GEMINI_VERTEX_SERVICE_ACCOUNT_FILE"
    )
    assert "never-serialize-this" not in serialized
    assert "/secret/account.json" not in serialized


def test_environment_metadata_records_secret_names_but_not_values(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path
) -> None:
    monkeypatch.setenv("UNIT_TEST_API_KEY", "top-secret-value")
    metadata = reproducibility.collect_environment(repo_root)
    serialized = json.dumps(metadata, sort_keys=True)
    assert "UNIT_TEST_API_KEY" in metadata["sensitive_environment_names_present"]
    assert "top-secret-value" not in serialized
    assert metadata["python"]["version"]
    assert metadata["platform"]["system"]


def test_runtime_policy_defaults_to_legacy_host_execution() -> None:
    policy = runtime_policy.RuntimePolicy()
    policy.validate(require_execution=False)
    assert policy.protocol == runtime_policy.LEGACY_PROTOCOL
    assert policy.backend == "host"
    assert policy.is_legacy
    assert not policy.is_hardened


def test_runtime_policy_auto_backend_is_versioned() -> None:
    legacy = runtime_policy.from_args(SimpleNamespace(protocol="legacy-v1"))
    hardened = runtime_policy.from_args(SimpleNamespace(protocol="hardened-v2"))
    assert legacy.backend == "host"
    assert hardened.backend == "docker"


def test_runtime_policy_rejects_crossed_protocol_backends() -> None:
    with pytest.raises(ValueError, match="legacy-v1 requires backend=host"):
        runtime_policy.RuntimePolicy(protocol="legacy-v1", backend="docker").validate(
            require_execution=False
        )
    with pytest.raises(ValueError, match="hardened-v2 requires backend=docker"):
        runtime_policy.RuntimePolicy(protocol="hardened-v2", backend="host").validate(
            require_execution=False
        )


def test_hardened_policy_rejects_unpinned_images_before_execution() -> None:
    policy = runtime_policy.RuntimePolicy(
        protocol="hardened-v2",
        backend="docker",
        sandbox_image="taskgenome-runner:latest",
    )
    with pytest.raises(ValueError, match="digest-pinned"):
        policy.validate(require_execution=True)


def test_hardened_policy_does_not_relabel_an_unpinned_dev_image() -> None:
    policy = runtime_policy.RuntimePolicy(
        protocol="hardened-v2",
        backend="docker",
        sandbox_image="taskgenome-runner:dev",
        allow_unpinned_image=True,
    )
    with pytest.raises(ValueError, match="cannot be labelled hardened-v2"):
        policy.validate(require_execution=False)


def test_hardened_policy_rejects_malformed_digest_and_root_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed = runtime_policy.RuntimePolicy(
        protocol="hardened-v2",
        backend="docker",
        sandbox_image="taskgenome-runner@sha256:abc",
    )
    with pytest.raises(ValueError, match="digest-pinned"):
        malformed.validate(require_execution=False)

    pinned = runtime_policy.RuntimePolicy(
        protocol="hardened-v2",
        backend="docker",
        sandbox_image="taskgenome-runner@sha256:" + "0" * 64,
    )
    monkeypatch.setattr(runtime_policy.os, "getuid", lambda: 0)
    with pytest.raises(ValueError, match="non-root"):
        pinned.validate(require_execution=True)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("memory", "0", "sandbox memory"),
        ("memory", "4g,exec", "sandbox memory"),
        ("memory", "65g", "sandbox memory"),
        ("tmpfs_size", "0", "sandbox tmpfs size"),
        ("tmpfs_size", "1g,exec", "sandbox tmpfs size"),
        ("cpus", float("nan"), "finite and positive"),
        ("cpus", float("inf"), "finite and positive"),
        ("cpus", 65.0, "maximum 64"),
        ("pids_limit", 4097, "maximum 4096"),
        (
            "output_limit_bytes",
            256 * 1024 * 1024 + 1,
            "output limit exceeds hardened-v2 maximum",
        ),
    ],
)
def test_hardened_policy_rejects_fail_open_resource_values(
    field: str,
    value: object,
    message: str,
) -> None:
    values = {
        "protocol": "hardened-v2",
        "backend": "docker",
        "sandbox_image": "taskgenome-runner@sha256:" + "0" * 64,
        field: value,
    }
    with pytest.raises(ValueError, match=message):
        runtime_policy.RuntimePolicy(**values).validate(require_execution=False)


def test_hardened_docker_command_locks_down_the_only_mount(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    image = "taskgenome-runner@sha256:" + "0" * 64
    policy = runtime_policy.RuntimePolicy(
        protocol="hardened-v2",
        backend="docker",
        sandbox_image=image,
    )
    monkeypatch.setattr(runtime_policy.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(runtime_policy.os, "getuid", lambda: 1000)
    monkeypatch.setattr(runtime_policy.os, "getgid", lambda: 1000)

    command = runtime_policy.docker_command(
        policy,
        [sys.executable, "generated.py"],
        trial,
        container_name="taskgenome-unit-test",
    )

    def option(name: str) -> str:
        return command[command.index(name) + 1]

    assert option("--network") == "none"
    assert "--read-only" in command
    assert option("--cap-drop") == "ALL"
    assert option("--security-opt") == "no-new-privileges"
    assert option("--pull") == "never"
    assert option("--user") == "1000:1000"
    assert option("--workdir") == "/workspace"
    assert option("--log-driver") == "none"
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1" in command
    assert "core=0:0" in command
    assert command.count("--volume") == 1
    assert option("--volume") == f"{trial.resolve()}:/workspace:ro"
    assert command.count("--mount") == 0
    assert command[-3:] == [image, "python", "generated.py"]


def test_hardened_docker_command_mounts_only_precreated_contract_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    trial = tmp_path / "trial"
    output = trial / "output" / "answer.json"
    output.parent.mkdir(parents=True)
    output.touch()
    monkeypatch.setattr(runtime_policy.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(runtime_policy.os, "getuid", lambda: 1000)
    monkeypatch.setattr(runtime_policy.os, "getgid", lambda: 1000)

    command = runtime_policy.docker_command(
        _pinned_policy(),
        [sys.executable, "generated.py"],
        trial,
        container_name="taskgenome-unit-test",
        writable_files=[output],
        max_file_bytes=4096,
    )

    assert command[command.index("--volume") + 1] == f"{trial.resolve()}:/workspace:ro"
    assert command.count("--mount") == 1
    assert command[command.index("--mount") + 1] == (
        f"type=bind,src={output.resolve()},dst=/workspace/output/answer.json"
    )
    assert "fsize=4096:4096" in command


def test_hardened_docker_command_rejects_mount_path_injection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    trial = tmp_path / "trial"
    output = trial / "bad,name.json"
    trial.mkdir()
    output.touch()
    monkeypatch.setattr(runtime_policy.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(runtime_policy.os, "getuid", lambda: 1000)
    monkeypatch.setattr(runtime_policy.os, "getgid", lambda: 1000)
    with pytest.raises(ValueError, match="conservative POSIX"):
        runtime_policy.docker_command(
            _pinned_policy(),
            [sys.executable, "generated.py"],
            trial,
            container_name="taskgenome-unit-test",
            writable_files=[output],
            max_file_bytes=4096,
        )


def test_docker_client_environment_does_not_forward_provider_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-secret")
    monkeypatch.setenv("GEMINI_KEY", "gemini-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    docker_env = runtime_policy.docker_cli_env()
    assert docker_env["PATH"] == "/usr/bin"
    assert "AWS_BEARER_TOKEN_BEDROCK" not in docker_env
    assert "GEMINI_KEY" not in docker_env
    assert "bedrock-secret" not in json.dumps(docker_env)


def _pinned_policy() -> runtime_policy.RuntimePolicy:
    return runtime_policy.RuntimePolicy(
        protocol="hardened-v2",
        backend="docker",
        sandbox_image="taskgenome-runner@sha256:" + "0" * 64,
    )


def _inspect_metadata(volumes: object = None) -> str:
    return json.dumps(
        {
            "image_id": "sha256:" + "1" * 64,
            "os": "linux",
            "architecture": "amd64",
            "volumes": volumes,
        }
    )


def test_hardened_preflight_runs_a_locked_down_smoke_and_verifies_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        command = list(command)
        commands.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=_inspect_metadata(), stderr="")
        if command[:2] == ["docker", "run"]:
            return SimpleNamespace(returncode=0, stdout="3.11", stderr="")
        if command[:3] == ["docker", "rm", "-f"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:3] == ["docker", "ps", "-a"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(runtime_policy.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(runtime_policy.os, "getuid", lambda: 1000)
    monkeypatch.setattr(runtime_policy.os, "getgid", lambda: 1000)
    monkeypatch.setattr(runtime_policy.subprocess, "run", fake_run)

    identity = runtime_policy.preflight(_pinned_policy())
    assert identity == {
        "image_id": "sha256:" + "1" * 64,
        "os": "linux",
        "architecture": "amd64",
    }

    smoke = next(command for command in commands if command[:2] == ["docker", "run"])

    def option(name: str) -> str:
        return smoke[smoke.index(name) + 1]

    assert option("--network") == "none"
    assert "--read-only" in smoke
    assert option("--cap-drop") == "ALL"
    assert option("--security-opt") == "no-new-privileges"
    assert option("--pull") == "never"
    assert option("--user") == "1000:1000"
    assert smoke.count("--volume") == 1
    assert "/workspace:ro" in option("--volume")
    assert smoke.count("--mount") == 1
    assert "dst=/workspace/.taskgenome-preflight-output" in option("--mount")
    assert option("--log-driver") == "none"
    assert "core=0:0" in smoke
    assert "fsize=4096:4096" in smoke
    assert "workspace write/read verification" in smoke[-1]
    assert any(command[:3] == ["docker", "rm", "-f"] for command in commands)
    assert any(command[:3] == ["docker", "ps", "-a"] for command in commands)


def test_hardened_preflight_cleans_up_after_smoke_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        command = list(command)
        commands.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=_inspect_metadata(), stderr="")
        if command[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(command, 30)
        if command[:3] == ["docker", "rm", "-f"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:3] == ["docker", "ps", "-a"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(runtime_policy.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(runtime_policy.os, "getuid", lambda: 1000)
    monkeypatch.setattr(runtime_policy.os, "getgid", lambda: 1000)
    monkeypatch.setattr(runtime_policy.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="sandbox smoke preflight failed: TimeoutExpired"):
        runtime_policy.preflight(_pinned_policy())

    assert any(command[:3] == ["docker", "rm", "-f"] for command in commands)
    assert any(command[:3] == ["docker", "ps", "-a"] for command in commands)


def test_hardened_preflight_fails_closed_when_cleanup_cannot_be_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        command = list(command)
        commands.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=_inspect_metadata(), stderr="")
        if command[:2] == ["docker", "run"]:
            return SimpleNamespace(returncode=0, stdout="3.11", stderr="")
        if command[:3] == ["docker", "rm", "-f"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:3] == ["docker", "ps", "-a"]:
            name_filter = command[command.index("--filter") + 1]
            name = name_filter.removeprefix("name=^/").removesuffix("$")
            return SimpleNamespace(returncode=0, stdout=name + "\n", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(runtime_policy.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(runtime_policy.os, "getuid", lambda: 1000)
    monkeypatch.setattr(runtime_policy.os, "getgid", lambda: 1000)
    monkeypatch.setattr(runtime_policy.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="could not verify removal"):
        runtime_policy.preflight(_pinned_policy())

    assert sum(command[:3] == ["docker", "rm", "-f"] for command in commands) == 2


def test_hardened_preflight_rejects_image_declared_volumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command, **_kwargs):
        assert list(command)[:3] == ["docker", "image", "inspect"]
        return SimpleNamespace(
            returncode=0,
            stdout=_inspect_metadata({"/workspace": {}}),
            stderr="",
        )

    monkeypatch.setattr(runtime_policy.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(runtime_policy.os, "getuid", lambda: 1000)
    monkeypatch.setattr(runtime_policy.subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="declaring writable Docker VOLUME"):
        runtime_policy.preflight(_pinned_policy())

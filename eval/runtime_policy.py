"""Versioned runtime policies for TaskGenome Bench.

``legacy-v1`` deliberately preserves the historical host execution path so
published runs can be reproduced. ``hardened-v2`` keeps prompt construction
and scoring unchanged, but executes candidate code in a locked-down Docker
container. Results from the two protocols must be reported separately.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path


LEGACY_PROTOCOL = "legacy-v1"
HARDENED_PROTOCOL = "hardened-v2"
PROTOCOLS = (LEGACY_PROTOCOL, HARDENED_PROTOCOL)
_SIZE_RE = re.compile(r"^(?P<count>[1-9][0-9]*)(?P<unit>b|k|kb|m|mb|g|gb|t|tb)?$")
_SIZE_MULTIPLIERS = {
    None: 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
}
MAX_HARDENED_MEMORY_BYTES = 64 * 1024**3
MAX_HARDENED_TMPFS_BYTES = 16 * 1024**3
MAX_HARDENED_CPUS = 64.0
MAX_HARDENED_PIDS = 4096
MAX_HARDENED_CAPTURE_BYTES = 256 * 1024 * 1024


def _validated_size_bytes(value: str, label: str, maximum: int) -> int:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string size")
    match = _SIZE_RE.fullmatch(value.strip().lower())
    if match is None:
        raise ValueError(
            f"{label} must be a positive integer with an optional b/k/m/g/t unit"
        )
    size = int(match.group("count")) * _SIZE_MULTIPLIERS[match.group("unit")]
    if size > maximum:
        raise ValueError(f"{label} exceeds the hardened-v2 maximum of {maximum} bytes")
    return size


@dataclass(frozen=True)
class RuntimePolicy:
    protocol: str = LEGACY_PROTOCOL
    backend: str = "host"
    sandbox_image: str = ""
    memory: str = "4g"
    cpus: float = 2.0
    pids_limit: int = 256
    tmpfs_size: str = "1g"
    output_limit_bytes: int = 8 * 1024 * 1024
    allow_unpinned_image: bool = False

    @property
    def is_legacy(self) -> bool:
        return self.protocol == LEGACY_PROTOCOL

    @property
    def is_hardened(self) -> bool:
        return self.protocol == HARDENED_PROTOCOL

    def validate(self, *, require_execution: bool = True) -> None:
        if self.protocol not in PROTOCOLS:
            raise ValueError(f"unknown protocol {self.protocol!r}; expected one of {PROTOCOLS}")
        if self.is_legacy and self.backend != "host":
            raise ValueError("legacy-v1 requires backend=host to preserve historical behavior")
        if self.is_hardened and self.backend != "docker":
            raise ValueError("hardened-v2 requires backend=docker")
        if self.is_hardened:
            if not self.sandbox_image:
                raise ValueError(
                    "hardened-v2 requires --sandbox-image (or TASKGENOME_SANDBOX_IMAGE)"
                )
            if re.search(r"\s|://|[?#]", self.sandbox_image):
                raise ValueError("sandbox image must be a plain Docker image reference")
            digest_pinned = bool(re.search(r"@sha256:[0-9a-f]{64}$", self.sandbox_image))
            if not digest_pinned:
                if self.allow_unpinned_image:
                    raise ValueError(
                        "an unpinned development image cannot be labelled hardened-v2; "
                        "use a digest-pinned --sandbox-image"
                    )
                raise ValueError("hardened-v2 requires a digest-pinned --sandbox-image")
            _validated_size_bytes(
                self.memory,
                "sandbox memory",
                MAX_HARDENED_MEMORY_BYTES,
            )
            _validated_size_bytes(
                self.tmpfs_size,
                "sandbox tmpfs size",
                MAX_HARDENED_TMPFS_BYTES,
            )
        if self.is_hardened and require_execution:
            if hasattr(os, "getuid") and os.getuid() == 0:
                raise ValueError("hardened-v2 requires a non-root host caller")
            if shutil.which("docker") is None:
                raise ValueError("hardened-v2 requires the docker executable")
        if not math.isfinite(self.cpus) or self.cpus <= 0:
            raise ValueError("sandbox cpus must be finite and positive")
        if self.is_hardened and self.cpus > MAX_HARDENED_CPUS:
            raise ValueError(
                f"sandbox cpus exceeds hardened-v2 maximum {MAX_HARDENED_CPUS:g}"
            )
        if type(self.pids_limit) is not int or self.pids_limit <= 0:
            raise ValueError("sandbox pids limit must be a positive integer")
        if self.is_hardened and self.pids_limit > MAX_HARDENED_PIDS:
            raise ValueError(
                f"sandbox pids limit exceeds hardened-v2 maximum {MAX_HARDENED_PIDS}"
            )
        if type(self.output_limit_bytes) is not int or self.output_limit_bytes <= 0:
            raise ValueError("sandbox output limit must be a positive integer")
        if (
            self.is_hardened
            and self.output_limit_bytes > MAX_HARDENED_CAPTURE_BYTES
        ):
            raise ValueError(
                "sandbox output limit exceeds hardened-v2 maximum "
                f"{MAX_HARDENED_CAPTURE_BYTES}"
            )


def from_args(args: object) -> RuntimePolicy:
    protocol = str(getattr(args, "protocol", LEGACY_PROTOCOL))
    requested_backend = str(getattr(args, "execution_backend", "auto"))
    if requested_backend == "auto":
        backend = "host" if protocol == LEGACY_PROTOCOL else "docker"
    else:
        backend = requested_backend
    return RuntimePolicy(
        protocol=protocol,
        backend=backend,
        sandbox_image=str(getattr(args, "sandbox_image", "") or ""),
        memory=str(getattr(args, "sandbox_memory", "4g") or "4g").strip().lower(),
        cpus=float(getattr(args, "sandbox_cpus", 2.0)),
        pids_limit=int(getattr(args, "sandbox_pids_limit", 256)),
        tmpfs_size=str(
            getattr(args, "sandbox_tmpfs_size", "1g") or "1g"
        ).strip().lower(),
        output_limit_bytes=int(
            getattr(args, "sandbox_output_limit_bytes", 8 * 1024 * 1024)
        ),
        allow_unpinned_image=bool(getattr(args, "allow_unpinned_sandbox_image", False)),
    )


def _container_pythonpath(cwd: Path) -> str:
    parts = ["/workspace"]
    skill_root = cwd / "skill"
    if skill_root.is_dir():
        for skill_dir in sorted(p for p in skill_root.iterdir() if p.is_dir()):
            rel = skill_dir.relative_to(cwd).as_posix()
            parts.append(f"/workspace/{rel}")
            scripts_dir = skill_dir / "scripts"
            if scripts_dir.is_dir():
                rel_scripts = scripts_dir.relative_to(cwd).as_posix()
                parts.append(f"/workspace/{rel_scripts}")
    return ":".join(parts)


def _container_command(cmd: list[str]) -> list[str]:
    if not cmd:
        return []
    first = cmd[0]
    try:
        is_current_python = Path(first).resolve() == Path(sys.executable).resolve()
    except OSError:
        is_current_python = False
    if is_current_python:
        return ["python", *cmd[1:]]
    return list(cmd)


def _docker_security_args(
    policy: RuntimePolicy,
    cwd: Path,
    *,
    container_name: str,
) -> list[str]:
    """Security-critical Docker flags shared by evaluation and preflight."""

    return [
        "--rm",
        "--pull",
        "never",
        "--log-driver",
        "none",
        "--name",
        container_name,
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(policy.pids_limit),
        "--ulimit",
        "nofile=1024:1024",
        "--ulimit",
        "core=0:0",
        "--memory",
        policy.memory,
        "--cpus",
        str(policy.cpus),
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--tmpfs",
        f"/tmp:rw,nosuid,nodev,size={policy.tmpfs_size}",
        "--env",
        "HOME=/tmp",
        "--env",
        "TMPDIR=/tmp",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "PYTHONHASHSEED=0",
        "--env",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
        "--env",
        f"PYTHONPATH={_container_pythonpath(cwd)}",
        "--volume",
        f"{cwd}:/workspace:ro",
        "--workdir",
        "/workspace",
    ]


def docker_command(
    policy: RuntimePolicy,
    cmd: list[str],
    cwd: Path,
    *,
    container_name: str,
    writable_files: list[Path] | None = None,
    max_file_bytes: int | None = None,
) -> list[str]:
    """Build a no-network, least-privilege Docker invocation.

    Only the per-trial temporary directory is mounted. Provider credentials and
    the source checkout are never passed to the container.
    """

    policy.validate(require_execution=True)
    cwd = cwd.resolve()
    writable_args: list[str] = []
    writable_files = list(writable_files or [])
    if writable_files:
        if type(max_file_bytes) is not int or max_file_bytes <= 0:
            raise ValueError("writable sandbox files require a positive max_file_bytes")
        writable_args.extend(
            ["--ulimit", f"fsize={max_file_bytes}:{max_file_bytes}"]
        )
    seen: set[str] = set()
    for source in writable_files:
        source = Path(source)
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"writable sandbox path is not a regular file: {source}")
        info = source.stat()
        if info.st_nlink != 1:
            raise ValueError(
                f"writable sandbox path must have exactly one hard link: {source}"
            )
        resolved = source.resolve()
        try:
            relative = resolved.relative_to(cwd)
        except ValueError as exc:
            raise ValueError(
                f"writable sandbox path escapes the workspace: {source}"
            ) from exc
        relative_posix = relative.as_posix()
        if relative_posix in seen:
            raise ValueError(f"duplicate writable sandbox path: {relative_posix}")
        if not relative.parts or any(
            re.fullmatch(r"[A-Za-z0-9._-]+", part) is None
            for part in relative.parts
        ):
            raise ValueError(
                "writable sandbox paths must use conservative POSIX path segments"
            )
        mount_parts = (str(resolved), relative_posix)
        if any(
            ord(character) < 32 or ord(character) == 127
            for text in mount_parts
            for character in text
        ):
            raise ValueError("writable sandbox paths must not contain control characters")
        if any(
            separator in text
            for text in mount_parts
            for separator in ",=:"
        ):
            raise ValueError(
                "writable sandbox paths must not contain Docker mount separators"
            )
        current = source.parent
        while current != cwd:
            if current.is_symlink() or not current.is_dir():
                raise ValueError(
                    f"writable sandbox parent is not a safe directory: {current}"
                )
            try:
                current.relative_to(cwd)
            except ValueError as exc:
                raise ValueError(
                    f"writable sandbox parent escapes the workspace: {current}"
                ) from exc
            current = current.parent
        seen.add(relative_posix)
        writable_args.extend(
            [
                "--mount",
                (
                    f"type=bind,src={resolved},"
                    f"dst=/workspace/{relative_posix}"
                ),
            ]
        )
    return [
        "docker",
        "run",
        *_docker_security_args(policy, cwd, container_name=container_name),
        *writable_args,
        policy.sandbox_image,
        *_container_command(cmd),
    ]


def _container_absent(container_name: str) -> bool:
    try:
        proc = subprocess.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"name=^/{container_name}$",
                "--format",
                "{{.Names}}",
            ],
            env=docker_cli_env(),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    return container_name not in {line.strip() for line in proc.stdout.splitlines()}


def force_remove_container(container_name: str, *, verify: bool = False) -> bool:
    """Remove a named container and optionally fail unless absence is verified."""

    if not container_name or shutil.which("docker") is None:
        if verify:
            raise ValueError("cannot verify Docker cleanup because docker is unavailable")
        return False

    for _ in range(2):
        try:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                env=docker_cli_env(),
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if _container_absent(container_name):
            return True

    if verify:
        raise ValueError(f"could not verify removal of sandbox container {container_name}")
    return False


def docker_cli_env() -> dict[str, str]:
    """Environment for the Docker client, not for candidate code."""

    keep = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "XDG_RUNTIME_DIR",
    }
    return {key: value for key, value in os.environ.items() if key in keep}


def preflight(policy: RuntimePolicy) -> dict[str, str] | None:
    """Exercise the pinned sandbox before any paid model call."""

    policy.validate(require_execution=True)
    if policy.is_legacy:
        return None
    inspect_format = (
        '{"image_id":{{json .Id}},"os":{{json .Os}},'
        '"architecture":{{json .Architecture}},'
        '"volumes":{{json .Config.Volumes}}}'
    )
    try:
        proc = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                inspect_format,
                policy.sandbox_image,
            ],
            env=docker_cli_env(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"sandbox image preflight failed: {type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "image not found").strip()[-1000:]
        raise ValueError(
            f"sandbox image is not available locally: {policy.sandbox_image}; {detail}"
        )
    try:
        inspected = json.loads(proc.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("sandbox image inspect returned invalid metadata") from exc
    if not isinstance(inspected, dict) or set(inspected) != {
        "image_id",
        "os",
        "architecture",
        "volumes",
    }:
        raise ValueError("sandbox image inspect metadata has invalid fields")
    if inspected["volumes"] not in (None, {}):
        raise ValueError(
            "hardened-v2 rejects images declaring writable Docker VOLUME paths"
        )
    identity_fields = ("image_id", "os", "architecture")
    if not all(
        isinstance(inspected[field], str) and inspected[field]
        for field in identity_fields
    ):
        raise ValueError("sandbox image inspect omitted valid identity metadata")
    identity = {field: inspected[field] for field in identity_fields}
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", identity["image_id"]):
        raise ValueError("sandbox image inspect returned an invalid image ID")

    smoke_script = """
import os
import pathlib
import sys

if os.geteuid() == 0:
    raise RuntimeError("sandbox process unexpectedly runs as root")
probe = pathlib.Path("/workspace/.taskgenome-preflight-output")
probe.write_text("ok", encoding="utf-8")
if probe.read_text(encoding="utf-8") != "ok":
    raise RuntimeError("sandbox workspace write/read verification failed")
try:
    pathlib.Path("/workspace/.taskgenome-unlisted").write_text("forbidden", encoding="utf-8")
except OSError:
    pass
else:
    raise RuntimeError("sandbox workspace permits unlisted file creation")
try:
    pathlib.Path("/.taskgenome-root-write").write_text("forbidden", encoding="utf-8")
except OSError:
    pass
else:
    raise RuntimeError("sandbox root filesystem is writable")
print(sys.version.split()[0])
""".strip()

    with tempfile.TemporaryDirectory(prefix="taskgenome_preflight_") as raw:
        smoke_dir = Path(raw).resolve()
        probe_path = smoke_dir / ".taskgenome-preflight-output"
        probe_path.touch(mode=0o600)
        container_name = f"taskgenome-preflight-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        command = docker_command(
            policy,
            [sys.executable, "-c", smoke_script],
            smoke_dir,
            container_name=container_name,
            writable_files=[probe_path],
            max_file_bytes=4096,
        )
        try:
            smoke = subprocess.run(
                command,
                cwd=smoke_dir,
                env=docker_cli_env(),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if smoke.returncode != 0:
                detail = (smoke.stderr or smoke.stdout or "smoke command failed").strip()[-1000:]
                raise ValueError(f"sandbox smoke preflight failed: {detail}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(
                f"sandbox smoke preflight failed: {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            force_remove_container(container_name, verify=True)
    return identity

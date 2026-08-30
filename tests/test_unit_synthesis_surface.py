"""Guards for the task-synthesis and asset-generation code added in 1.0.2.

These modules were ported out of a private research tree. The two failure modes
that port can reintroduce are a path that only resolves on the authors' machine,
and a whitelist wide enough to let per-task answers reach the public tree.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SYNTH_DIR = REPO_ROOT / "synth"

if str(SYNTH_DIR) not in sys.path:
    sys.path.insert(0, str(SYNTH_DIR))

SYNTH_MODULES = (
    "llm_client",
    "utils",
    "run_solution_first",
    "run_sample",
    "retry_crashes",
    "audit_env_host_pytest",
    "mini_calibration",
    "model_eval",
    "consolidate",
    "simplify_pool_layout",
    "promote_sanitized_skills",
    "selftest",
)

GENERATION_MODULES = (
    "gen_genes_llm_v3",
    "evolve_genes_v3",
    "generate_agent_skills_v3",
    "rewrite_skills_v3",
    "compare_gene_rollout_tokens",
)

# Answers live in these directories of the authoring tree. None of them may ever
# appear in a published path.
ANSWER_DIRECTORIES = (
    "_sample_pilot",
    "candidates",
    "candidates_sf",
    "_calibration",
    "_bad_solutions",
    "_quarantine",
    "_model_runs",
)

HOST_PATH = re.compile(r"""['"](/(?:data|home|Users|mnt)/[A-Za-z0-9_.-]+/)""")


@pytest.mark.parametrize("name", SYNTH_MODULES)
def test_synthesis_module_imports(name: str) -> None:
    assert importlib.import_module(name) is not None


@pytest.mark.parametrize("name", GENERATION_MODULES)
def test_generation_module_imports(name: str) -> None:
    assert importlib.import_module(name) is not None


def test_every_host_path_is_covered_by_a_pinned_exemption() -> None:
    """A host path may ship only behind a digest-pinned scan exemption."""
    policy = json.loads(
        (REPO_ROOT / "release" / "stage_c_release.v1.json").read_text(encoding="utf-8")
    )
    exemptions = policy["code_export"]["content_scan_exemptions"]

    # Scan exactly what ships. In an exported tree that is the manifest; in the
    # authoring tree it is the export whitelist. Walking the whole tree instead
    # would report authoring files that can never reach the release.
    manifest = REPO_ROOT / "PUBLIC_CODE_MANIFEST.json"
    if manifest.is_file():
        shipped = [
            REPO_ROOT / record["path"]
            for record in json.loads(manifest.read_text(encoding="utf-8"))["files"]
        ]
    else:
        code_export = policy["code_export"]
        shipped = [REPO_ROOT / entry["source"] for entry in code_export["copy_files"]]
        for glob in code_export["copy_globs"]:
            shipped.extend(sorted(REPO_ROOT.glob(glob["pattern"])))

    offenders: dict[str, str] = {}
    for path in shipped:
        if not path.is_file() or path.suffix not in {".py", ".json", ".yml", ".yaml"}:
            continue
        if path.name == Path(__file__).name:
            continue
        match = HOST_PATH.search(path.read_text(encoding="utf-8", errors="replace"))
        if match:
            offenders[path.relative_to(REPO_ROOT).as_posix()] = match.group(1)

    for relative in offenders:
        assert relative in exemptions, f"{relative} carries a host path with no exemption"
        digest = hashlib.sha256((REPO_ROOT / relative).read_bytes()).hexdigest()
        assert exemptions[relative] == digest, f"{relative} drifted from its pinned digest"


def test_release_policy_blocks_answer_directories() -> None:
    policy = json.loads(
        (REPO_ROOT / "release" / "stage_c_release.v1.json").read_text(encoding="utf-8")
    )
    forbidden = set(policy["code_export"]["forbidden_path_components"])
    assert set(ANSWER_DIRECTORIES) <= forbidden
    basenames = set(policy["code_export"]["forbidden_basenames"])
    assert {"reference_solution.py", "test_script.py", "scenario.yaml"} <= basenames


def test_synthesis_files_are_whitelisted_individually() -> None:
    """A glob over the authoring tree would sweep in reference solutions."""
    policy = json.loads(
        (REPO_ROOT / "release" / "stage_c_release.v1.json").read_text(encoding="utf-8")
    )
    code_export = policy["code_export"]
    globbed = [g["pattern"] for g in code_export["copy_globs"] if g["pattern"].startswith("synth/")]
    assert globbed == ["synth/shapes/**/*"]

    whitelisted = {e["destination"] for e in code_export["copy_files"]}
    for path in sorted(SYNTH_DIR.glob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            assert f"synth/{path.name}" in whitelisted


def test_answer_paths_are_rejected_by_the_verifier() -> None:
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    import stage_c_release as scr

    policy = json.loads(
        (REPO_ROOT / "release" / "stage_c_release.v1.json").read_text(encoding="utf-8")
    )
    for candidate in (
        "synth/candidates_sf/A0001sf/reference_solution.py",
        "synth/_sample_pilot/summary.json",
        "synth/shapes/math_reasoning/_calibration/trial_1.json",
        "synth/scenarios/T0001/scenario.yaml",
    ):
        assert scr._destination_violation(candidate, policy) is not None

    for allowed in ("synth/run_solution_first.py", "synth/shapes/math_reasoning/seeds.txt"):
        assert scr._destination_violation(allowed, policy) is None


def test_candidate_env_withholds_operator_credentials(monkeypatch) -> None:
    """Every synth subprocess runs model-generated code; none may see a key."""
    import utils

    for name, value in {
        "GEMINI_API_KEY": "must-not-leak",
        "ANTHROPIC_API_KEY": "must-not-leak",
        "AWS_SECRET_ACCESS_KEY": "must-not-leak",
        "SUB2API_KEY": "must-not-leak",
        "https_proxy": "http://proxy.invalid:8080",
        "PYTHONPATH": "/operator/site-packages",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", "/usr/bin")

    env = utils.candidate_env()

    assert not [k for k in env if "KEY" in k.upper() or "SECRET" in k.upper()]
    assert not [k for k in env if "proxy" in k.lower()]
    assert "PYTHONPATH" not in env
    assert set(env) - {"PATH", "HOME", "TMPDIR", "TMP", "TEMP", "USERPROFILE"} <= utils.SAFE_ENV_NAMES
    # The candidate still needs a usable interpreter and a scratch home.
    assert env["PATH"].endswith("/usr/bin")
    assert Path(env["HOME"]).is_dir()
    assert Path(env["HOME"]) != Path.home()


def test_every_synth_subprocess_uses_candidate_env() -> None:
    """A new subprocess call must not silently reintroduce the operator env."""
    offenders: list[str] = []
    for path in sorted(SYNTH_DIR.glob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            if "subprocess.run(" not in line:
                continue
            window = "\n".join(lines[number - 1 : number + 10])
            if "env=candidate_env()" not in window:
                offenders.append(f"{path.name}:{number}")
    assert offenders == []

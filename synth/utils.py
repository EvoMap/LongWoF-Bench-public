"""Parsing + sandbox helpers for the synthesis pipeline."""
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── 1. <file path="..."> ... </file> block parsing ──

FILE_BLOCK_RE = re.compile(
    r'<file\s+path="([^"]+)">\s*(.*?)\s*</file>',
    re.DOTALL,
)
CODE_FENCE_RE = re.compile(
    r'```([a-zA-Z]*)\n(.*?)\n```',
    re.DOTALL,
)


# ── Candidate execution environment ────────────────────────────────────────
# Everything this pipeline executes is model-generated: reference solutions,
# oracles, and candidate answers. The operator running it necessarily holds a
# provider credential, because the pipeline cannot author anything without one.
# Handing that credential to the generated code would let any generated script
# read it straight out of os.environ, so candidate subprocesses get an explicit
# allowlist instead of the operator's environment.
SAFE_ENV_NAMES = frozenset(
    {
        # A deterministic executable search path plus the locale/terminal hints
        # ordinary command-line tools expect. Do not broaden this to "all
        # non-secret" variables: provider SDKs and authenticated proxies use
        # non-obvious variable names.
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
        # Windows subprocesses need these to locate the system runtime. They are
        # harmless on POSIX and are copied only when set.
        "SYSTEMROOT",
        "WINDIR",
        "SYSTEMDRIVE",
        "COMSPEC",
        "PATHEXT",
    }
)

_sandbox_root: Optional[Path] = None


def _candidate_scratch() -> Path:
    """One disposable HOME/TMPDIR for every candidate this process runs."""
    global _sandbox_root
    if _sandbox_root is None:
        _sandbox_root = Path(tempfile.mkdtemp(prefix="longwof-synth-sandbox-"))
        (_sandbox_root / "home").mkdir(exist_ok=True)
        (_sandbox_root / "tmp").mkdir(exist_ok=True)
    return _sandbox_root


def candidate_env() -> dict[str, str]:
    """Build a credential-free environment for a generated-code subprocess.

    This is not a sandbox. The pipeline still executes untrusted code on the
    host and belongs on an expendable machine; the allowlist only keeps
    provider credentials, proxy URLs, and the operator's home directory out of
    reach of that code.
    """
    parent = os.environ
    env = {name: parent[name] for name in SAFE_ENV_NAMES if name in parent}

    interpreter_dir = str(Path(sys.executable).resolve().parent)
    path_parts = [interpreter_dir]
    if env.get("PATH"):
        path_parts.append(env["PATH"])
    env["PATH"] = os.pathsep.join(path_parts)

    scratch = _candidate_scratch()
    env["HOME"] = str(scratch / "home")
    for name in ("TMPDIR", "TMP", "TEMP"):
        env[name] = str(scratch / "tmp")
    if os.name == "nt":
        env["USERPROFILE"] = str(scratch / "home")
    return env


def parse_file_blocks(text: str) -> dict[str, str]:
    """Extract every <file path="...">...</file> block. Returns {path: content}."""
    out: dict[str, str] = {}
    for m in FILE_BLOCK_RE.finditer(text):
        path = m.group(1).strip()
        content = m.group(2)
        # strip wrapping ``` fences if model added them
        content = re.sub(r"^```[a-zA-Z]*\n", "", content)
        content = re.sub(r"\n```\s*$", "", content)
        out[path] = content.strip() + "\n"
    return out


def parse_code_blocks_fallback(text: str, default_filename: str) -> dict[str, str]:
    """Fallback when LLM ignored the <file> protocol and just dumped a code fence.

    Returns the LARGEST python/yaml/markdown block under default_filename.
    Only meant for single-file stages (test_script, repair). Never for multi-file
    Stage 1.
    """
    blocks = []
    for m in CODE_FENCE_RE.finditer(text):
        lang = (m.group(1) or "").lower()
        content = m.group(2).strip()
        blocks.append((lang, content))
    if not blocks:
        return {}
    # Prefer python/<empty>; pick the longest
    pythonish = [b for b in blocks if b[0] in ("", "python", "py")]
    pool = pythonish or blocks
    pool.sort(key=lambda b: -len(b[1]))
    return {default_filename: pool[0][1] + "\n"}


def _safe_join(base: Path, relpath: str) -> Path:
    """Join base/relpath after rejecting path traversal / absolute paths.

    Raises ValueError on:
      - absolute paths (`/etc/passwd`, `C:\\...`)
      - paths containing `..` segments
      - paths whose resolved location escapes `base`
    """
    p = Path(relpath)
    if p.is_absolute():
        raise ValueError(f"absolute path not allowed: {relpath!r}")
    if any(part == ".." for part in p.parts):
        raise ValueError(f"parent traversal not allowed: {relpath!r}")
    base_resolved = base.resolve()
    target_resolved = (base / p).resolve()
    try:
        target_resolved.relative_to(base_resolved)
    except ValueError:
        raise ValueError(f"path escapes candidate dir: {relpath!r}")
    return base / p


def write_files(target_dir: Path, files: dict[str, str]) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    for relpath, content in files.items():
        p = _safe_join(target_dir, relpath)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


# ── 2. reference_solution.py → public signatures (for prompt 2) ──

def extract_signatures(source: str) -> str:
    """Return only top-level def signatures + their docstrings, no bodies."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return f"# (could not parse: {e})\n"
    out_lines: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = ast.unparse(node.args)
            out_lines.append(f"def {node.name}({args}):")
            doc = ast.get_docstring(node)
            if doc:
                out_lines.append(f'    """{doc}"""')
            out_lines.append("    ...")
            out_lines.append("")
        elif isinstance(node, ast.ClassDef):
            out_lines.append(f"class {node.name}:")
            doc = ast.get_docstring(node)
            if doc:
                out_lines.append(f'    """{doc}"""')
            out_lines.append("    ...")
            out_lines.append("")
    if not out_lines:
        out_lines = ["# (script has no top-level defs; CLI script structure)"]
    return "\n".join(out_lines)


# ── 2b. Import whitelist for reference_solution.py / bad_solutions ──

ALLOWED_TOP_LEVEL_IMPORTS = {
    # stdlib
    "argparse", "json", "csv", "pathlib", "math", "re", "os", "sys", "io",
    "collections", "itertools", "functools", "dataclasses", "typing",
    "subprocess", "tempfile", "string", "random", "time", "datetime",
    "hashlib", "base64", "struct", "gzip", "bz2", "zipfile", "tarfile",
    "warnings", "copy", "traceback",
    # numerics that are always available in the gene_bench env
    "numpy", "np", "scipy", "pandas", "pd", "h5py", "sklearn", "matplotlib",
    "PIL", "yaml",
}


def imported_top_modules(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name:
                    out.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.add(node.module.split(".")[0])
    return out


def check_import_whitelist(source: str) -> Optional[str]:
    """Return error string if a forbidden import is used, else None."""
    used = imported_top_modules(source)
    bad = used - ALLOWED_TOP_LEVEL_IMPORTS
    if bad:
        return f"forbidden imports: {sorted(bad)}"
    return None


# ── 2b'. Tighter whitelist for test_script.py ──
#
# The test must build its own oracle. To prevent the test author from secretly
# delegating the answer to the same library family the reference would call
# (e.g. asking scipy.signal.find_peaks to "verify" a find-peaks task), we
# disallow heavyweight learning libraries and anything network/GPU. Numerics
# (numpy/scipy/pandas) ARE allowed because the test legitimately needs them
# for input synthesis and for closed-form oracles, but the prompt forbids
# calling the same high-level function the reference would.
FORBIDDEN_TEST_IMPORTS = {
    # Network — tests must be hermetic
    "urllib", "urllib2", "urllib3", "requests", "httpx", "http",
    "socket", "asyncio", "websocket", "websockets", "aiohttp", "smtplib",
    "ftplib", "telnetlib", "ssl",
    # GPU / heavyweight ML — tests should compute analytically, not learn
    "torch", "tensorflow", "tf", "jax", "flax", "transformers",
    "lightgbm", "xgboost", "catboost",
    # sklearn is excluded from tests (but allowed in references) because
    # an "oracle" that does e.g. KNeighborsClassifier vs the candidate's
    # KNeighborsClassifier is just self-validation.
    "sklearn",
    # Plotting / GUI — tests run headless and should not draw anything
    "matplotlib", "seaborn", "plotly",
    # Symbolic — too easy to write a one-liner that matches the ref's algo
    "sympy",
}


def check_test_import_whitelist(source: str) -> Optional[str]:
    """Return error string if test_script imports a forbidden module."""
    used = imported_top_modules(source)
    bad = used & FORBIDDEN_TEST_IMPORTS
    if bad:
        return (
            f"test imports forbidden module(s): {sorted(bad)} "
            "(network/GPU/heavy-ML libs are banned in tests; oracles must be "
            "computed independently using stdlib/numpy/scipy/pandas only)"
        )
    # Also disallow imports we don't know about — same conservative posture
    # as the reference whitelist, minus the sklearn/matplotlib relaxations.
    test_allowed = ALLOWED_TOP_LEVEL_IMPORTS - FORBIDDEN_TEST_IMPORTS
    unknown = used - test_allowed
    if unknown:
        return f"test imports non-whitelisted module(s): {sorted(unknown)}"
    return None


def check_python_syntax(source: str) -> Optional[str]:
    try:
        ast.parse(source)
        return None
    except SyntaxError as e:
        return f"SyntaxError: {e}"


def check_argparse_cli(source: str) -> Optional[str]:
    """Heuristically verify the script declares argparse with --input and --output.

    Look for `argparse.ArgumentParser`, `add_argument("--input"...)`, and
    `add_argument("--output"...)`. We don't enforce required=True etc — that's
    LLM stylistic — but the flags MUST be present.
    """
    if "argparse" not in source:
        return "no `argparse` import"
    if "ArgumentParser" not in source:
        return "no ArgumentParser() call"
    if not re.search(r'add_argument\(\s*["\']--input["\']', source):
        return "no add_argument('--input', ...)"
    if not re.search(r'add_argument\(\s*["\']--output["\']', source):
        return "no add_argument('--output', ...)"
    return None


# ── 2c. Schema checks for scenario.yaml / task.md / SKILL.md ──

REQUIRED_SCENARIO_KEYS = ("id", "name", "domain", "difficulty", "source", "tags", "required_packages")
ALLOWED_DIFFICULTY = {"easy", "medium", "hard"}
REQUIRED_TASK_SECTIONS = ("Input", "Output", "Requirements", "CLI Specification")
REQUIRED_SKILL_SECTIONS = (
    "Overview", "Workflow", "Common Pitfalls", "Error Handling",
)
# `## Quick Reference` was required in v1/v2; in v2.5 it is REMOVED (see
# SKILL_SCHEMA_v2.md). If a draft still emits a `## Quick Reference` section
# Gate A will reject it (see check_skill_md). This forces the prompt change
# in 1_scenario_draft.md to actually take effect end-to-end.
FORBIDDEN_SKILL_SECTIONS = ("Quick Reference", "Reference", "Reference Data", "Constants")
REQUIRED_CLI_LINE = "python generated.py --input <INPUT> --output <OUTPUT>"


def check_scenario_yaml(text: str) -> Optional[str]:
    try:
        import yaml
    except ImportError:
        return "PyYAML not installed (cannot validate scenario.yaml)"
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return f"YAMLError: {e}"
    if not isinstance(data, dict):
        return "scenario.yaml top-level is not a mapping"
    missing = [k for k in REQUIRED_SCENARIO_KEYS if k not in data]
    if missing:
        return f"scenario.yaml missing keys: {missing}"
    if data.get("difficulty") not in ALLOWED_DIFFICULTY:
        return f"scenario.yaml difficulty={data.get('difficulty')!r} not in {sorted(ALLOWED_DIFFICULTY)}"
    if not isinstance(data.get("tags"), list) or not data["tags"]:
        return "scenario.yaml `tags` must be a non-empty list"
    if not isinstance(data.get("required_packages"), list):
        return "scenario.yaml `required_packages` must be a list"
    return None


def check_task_md(text: str) -> Optional[str]:
    """Verify task.md has the required sections AND the exact CLI invocation line."""
    headings = set(re.findall(r'^##\s+(.+?)\s*$', text, re.MULTILINE))
    norm = {h.split("(")[0].strip() for h in headings}
    missing = [s for s in REQUIRED_TASK_SECTIONS if not any(s in h for h in norm)]
    if missing:
        return f"task.md missing sections: {missing}; saw {sorted(norm)}"
    if REQUIRED_CLI_LINE not in text:
        return f"task.md missing exact CLI line: `{REQUIRED_CLI_LINE}`"
    if "--input" not in text or "--output" not in text:
        return "task.md does not mention --input / --output flags"
    return None


def check_skill_md(text: str) -> Optional[str]:
    headings = set(re.findall(r'^##\s+(.+?)\s*$', text, re.MULTILINE))
    missing = [s for s in REQUIRED_SKILL_SECTIONS if s not in headings]
    if missing:
        return f"SKILL.md missing sections: {missing}; saw {sorted(headings)}"
    # Reject if the draft still emits a `## Quick Reference` (v2-era schema).
    # SKILL_SCHEMA_v2.md / v2.5 forbids QR; rejecting here forces consistency
    # so we don't need a separate post-hoc `polish_skill_v2.py` pass.
    bad = [s for s in FORBIDDEN_SKILL_SECTIONS if s in headings]
    if bad:
        return (f"SKILL.md has forbidden section(s) {bad} — v2.5 SKILL is "
                f"procedural-only (no Quick Reference / constant tables)")
    # Cheap content sanity: each section should have *some* body
    body_word_count = len(re.findall(r'\b\w+\b', text))
    if body_word_count < 80:
        return f"SKILL.md too short ({body_word_count} words; need >=80)"
    return None


# ── 2c'. New v2.5 checks: ref decomposition + LOC cap ──

def check_ref_decomposition(source: str, min_funcs: int = 2) -> Optional[str]:
    """Reject a reference solution that isn't decomposed into ≥ min_funcs
    named functions (excluding `main` / `_main`).

    Rationale: v2 synth produced 80% single-function linear scripts, which
    is the data-layer reason these tasks are too easy for strong models.
    Forcing decomposition pushes the median LOC up and the median task
    complexity up.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None  # caught separately by check_python_syntax
    # Count *top-level* function definitions, ignoring dunder-main wrappers.
    funcs = [n for n in tree.body
             if isinstance(n, ast.FunctionDef)
             and n.name not in {"main", "_main", "run", "_run", "cli", "_cli"}]
    if len(funcs) < min_funcs:
        all_funcs = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
        return (f"reference_solution.py decomposition: need ≥ {min_funcs} "
                f"named helper functions (excluding main/run/cli wrappers); "
                f"got {len(funcs)} (all top-level funcs: {all_funcs})")
    return None


def check_ref_loc(source: str, max_lines: int = 200) -> Optional[str]:
    """Reject a reference solution longer than max_lines.

    v2 capped at 80 lines and produced toy tasks; v2.5 raises to 200 to
    allow multi-step pipelines. Still need an upper bound to keep tasks
    tractable and avoid the LLM dumping a whole framework.
    """
    n = source.count("\n") + (0 if source.endswith("\n") else 1)
    if n > max_lines:
        return (f"reference_solution.py too long: {n} lines "
                f"(max {max_lines}); decompose or simplify")
    return None


def check_task_md_length(text: str, min_lines: int = 22) -> Optional[str]:
    """Reject task.md shorter than min_lines (non-blank).

    Floor calibrated against v1 task.md length distribution:
        min=20, P10=21, P25=23, median=26, P75=30, P90=33, max=38.
    The first cut of this gate used min=35, which is *higher than v1's
    own P75* and produced 0% Gate-A pass on a 50-candidate smoke. The
    floor was rebased to 22 so Flash-Lite can plausibly hit it while
    still catching the truly-truncated drafts (15-21 lines, the old v2
    regime).
    """
    n = sum(1 for ln in text.splitlines() if ln.strip())
    if n < min_lines:
        return (f"task.md too short: {n} non-blank lines "
                f"(need >= {min_lines}); add Input/Output schema detail")
    return None


# Library-function reference pattern. Used to scan SKILL.md (v2.5 forbids
# SDK-call leaks in the procedural prior). Matches `<lib>.<func>` where
# <lib> is one of the common numerical/data libs.
_LIB_FUNC_REF_RE = re.compile(
    r"\b(?:np|pd|plt|sns|scipy|numpy|pandas|sklearn|matplotlib|h5py|PIL|yaml)"
    r"\.[A-Za-z_][A-Za-z0-9_.]*"
)


def check_skill_no_library_functions(text: str, max_unique: int = 4,
                                     max_examples: int = 5) -> Optional[str]:
    """Reject SKILL.md only when it degenerates into a library recipe.

    v1 SKILL.md typically cites 0-2 library functions as light anchors
    (`np.histogram`, `scipy.io`) — that's procedural prior with one
    concrete API hook, totally fine. The pathology is SKILL.md becoming
    a *list* of `lib.func` calls that the model can chain to solve the
    task without thinking. We allow up to `max_unique` distinct
    mentions; beyond that, SKILL is acting as an API cheatsheet.
    """
    hits = _LIB_FUNC_REF_RE.findall(text)
    uniq = sorted(set(hits))
    if len(uniq) > max_unique:
        return (
            f"SKILL.md cites {len(uniq)} unique library functions "
            f"(allowed max {max_unique}; e.g. {uniq[:max_examples]}) — "
            "SKILL is meant to be procedural prior, not an API recipe. "
            "Trim to ≤" + str(max_unique) + " concrete library mentions; "
            "describe the remaining steps in domain terms."
        )
    return None


# ── 2d. Environment introspection ──

def get_env_snapshot() -> dict:
    """Capture the runtime environment so trace can answer 'which Python ran this'."""
    info: dict = {
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "platform": sys.platform,
        "cwd": os.getcwd(),
    }
    pkgs = ("numpy", "scipy", "pandas", "h5py", "sklearn", "matplotlib", "PIL", "yaml")
    versions: dict = {}
    for name in pkgs:
        try:
            mod = __import__(name)
            versions[name] = getattr(mod, "__version__", "unknown")
        except ImportError:
            versions[name] = None
    info["package_versions"] = versions
    return info


def preflight_required_packages(yaml_text: str) -> Optional[str]:
    """Verify scenario.yaml.required_packages can actually be imported.

    Returns error string if any required package is missing in the current
    Python environment, else None. Uses the same name->import-name mapping
    that already lives implicitly in ALLOWED_TOP_LEVEL_IMPORTS.
    """
    try:
        import yaml
    except ImportError:
        return "PyYAML not installed — cannot run preflight"
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        return f"scenario.yaml YAMLError during preflight: {e}"
    pkgs = data.get("required_packages") or []
    if not isinstance(pkgs, list):
        return None  # validated separately by check_scenario_yaml
    # Map pip-name -> import-name where they differ.
    pip_to_import = {
        "scikit-learn": "sklearn",
        "pillow": "PIL",
        "pyyaml": "yaml",
        "opencv-python": "cv2",
    }
    missing: list[str] = []
    for raw in pkgs:
        name = str(raw).strip().lower()
        # strip version specifiers like "numpy>=1.20"
        for sep in (">=", "<=", "==", ">", "<", "~="):
            if sep in name:
                name = name.split(sep)[0].strip()
                break
        if not name or name in ("python", "stdlib"):
            continue
        import_name = pip_to_import.get(name, name)
        try:
            __import__(import_name)
        except ImportError:
            missing.append(f"{raw} (import {import_name})")
    if missing:
        return (
            f"required_packages not importable in current env "
            f"({sys.executable}): {missing}"
        )
    return None


# ── 3. Static check: test_script must NOT mention reference_solution ──

def static_test_isolation_check(test_text: str) -> Optional[str]:
    """Return error string if leak found, else None."""
    if "reference_solution" in test_text:
        return "test_script.py mentions 'reference_solution'"
    try:
        tree = ast.parse(test_text)
    except SyntaxError as e:
        return f"test_script.py syntax error: {e}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if "reference_solution" in (a.name or ""):
                    return f"test imports {a.name}"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if "reference_solution" in mod:
                return f"test imports from {mod}"
    return None


def static_protocol_check(test_text: str) -> Optional[str]:
    """Verify required PASS/FAIL/SCORE lines look like they will be emitted."""
    must_have = [
        ("L1_runs", r'\bL1_runs\b'),
        ("L1_output_exists", r'\bL1_output_exists\b'),
    ]
    for name, pat in must_have:
        if not re.search(pat, test_text):
            return f"test never references {name}"
    n_l2 = len(set(re.findall(r'\bL2_[A-Za-z0-9_]+', test_text)))
    if n_l2 < 3:
        return f"test only mentions {n_l2} distinct L2_* checks, need >= 3"
    n_score = len(set(re.findall(r'SCORE:[A-Za-z0-9_]+', test_text)))
    if n_score < 1:
        return "test has no SCORE:<name>=... line"
    return None


# ── 4. Sandbox runner ──

@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def run_in_sandbox(
    candidate_solution_path: Path,
    test_script_path: Path,
    extra_files: Optional[dict[str, Path]] = None,
    timeout: int = 90,
) -> RunResult:
    """Copy candidate as generated.py + test as test_script.py into a temp dir,
    run the test, return PASS/FAIL/SCORE log on stdout.
    """
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        shutil.copy(candidate_solution_path, td_p / "generated.py")
        shutil.copy(test_script_path, td_p / "test_script.py")
        if extra_files:
            for name, src in extra_files.items():
                shutil.copy(src, td_p / name)
        try:
            r = subprocess.run(
                [sys.executable, "test_script.py"],
                cwd=td_p,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=candidate_env(),
            )
            return RunResult(r.returncode, r.stdout, r.stderr)
        except subprocess.TimeoutExpired as e:
            return RunResult(
                returncode=-1,
                stdout=(e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""),
                stderr=(e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or ""),
                timed_out=True,
            )


# ── 5. Parse PASS/FAIL/SCORE log ──

PASS_RE = re.compile(r'^PASS:([A-Za-z0-9_]+)', re.MULTILINE)
FAIL_RE = re.compile(r'^FAIL:([A-Za-z0-9_]+)', re.MULTILINE)
SCORE_RE = re.compile(r'^SCORE:([A-Za-z0-9_]+)\s*=\s*([\-0-9.eE]+)', re.MULTILINE)


@dataclass
class TestReport:
    passes: list[str] = field(default_factory=list)
    fails: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    raw_stdout: str = ""
    raw_stderr: str = ""

    @property
    def all_pass(self) -> bool:
        return len(self.fails) == 0 and len(self.passes) > 0

    @property
    def min_score(self) -> float:
        return min(self.scores.values()) if self.scores else 0.0

    @property
    def max_score(self) -> float:
        return max(self.scores.values()) if self.scores else 0.0

    def summary(self) -> str:
        return (
            f"PASS={len(self.passes)} FAIL={len(self.fails)} "
            f"SCOREs={ {k: round(v, 3) for k, v in self.scores.items()} }"
        )


def parse_test_log(stdout: str, stderr: str = "") -> TestReport:
    rep = TestReport(raw_stdout=stdout, raw_stderr=stderr)
    rep.passes = PASS_RE.findall(stdout)
    rep.fails = FAIL_RE.findall(stdout)
    for name, val in SCORE_RE.findall(stdout):
        try:
            rep.scores[name] = float(val)
        except ValueError:
            pass
    return rep


# ── 6. Toxicology truth table ──
#
# Per-bad-solution criteria. Each bad solution must be *runnable*; the test
# must distinguish it from the reference *on substance*, not by tripping over
# its crash. Specifically:
#
#   empty_output:    designed to exit 0 with a 0-byte / missing output file.
#                    Allowed to fail at L1 (output_exists/valid). MUST fail
#                    SOMEWHERE. We accept L1 failure as a legit catch.
#   format_only:     output file exists and parses (must reach L1_valid_*),
#                    but content is structurally minimal. The test must
#                    catch it at L2 or via SCORE <= 0.5 — not by crashing
#                    earlier.
#   naive_baseline:  output file exists, parses, AND passes most L2 structural
#                    checks. The test must distinguish it via SCORE <= 0.5.
#                    If naive crashes or fails L1, that is a bad-solution
#                    quality problem, not a test win.

@dataclass
class ToxVerdict:
    ok: bool
    # Three orthogonal failure buckets, each requires a different remediation:
    #   ref_failures           — task/oracle ambiguity, REJECT (cannot fix by editing test)
    #   bad_quality_failures   — bad-solution itself is broken (crashes / structural bug);
    #                            remediation is REGENERATE bad sols, not repair test
    #   bad_failures           — test is too weak (bad sol is well-formed but test
    #                            doesn't catch it); remediation is REPAIR test
    ref_failures: list[str]
    bad_quality_failures: list[str]
    bad_failures: list[str]
    per_solution: dict[str, TestReport]

    @property
    def reasons(self) -> list[str]:
        return self.ref_failures + self.bad_quality_failures + self.bad_failures

    @property
    def is_ref_problem(self) -> bool:
        return bool(self.ref_failures)

    @property
    def is_bad_quality_problem(self) -> bool:
        """True iff the only issues are bad-solution quality (no ref or test issues)."""
        return bool(self.bad_quality_failures) and not self.ref_failures and not self.bad_failures

    @property
    def is_repairable(self) -> bool:
        """True iff problems are only test-weakness (stage4c can help)."""
        return bool(self.bad_failures) and not self.ref_failures and not self.bad_quality_failures


def _has_pass(rep: TestReport, prefix: str) -> bool:
    return any(p.startswith(prefix) for p in rep.passes)


def _count_pass(rep: TestReport, prefix: str) -> int:
    return sum(1 for p in rep.passes if p.startswith(prefix))


def _count_fail(rep: TestReport, prefix: str) -> int:
    return sum(1 for f in rep.fails if f.startswith(prefix))


# When the test author emits per-case scores AND an aggregate, the aggregate
# is the right thing to evaluate "did this solution actually do the task".
# `max(scores)` is too sensitive — a trivial "constant 0" output can score 1.0
# on a "constant input" corner case while being totally wrong on the main task.
_AGGREGATE_HINTS = ("overall", "final", "aggregate", "total", "mean", "weighted", "average")


def _aggregate_score(rep: TestReport) -> float:
    """Pick the most representative single score for a TestReport.

    1. If any score's *name* contains an aggregate hint (`overall_*`,
       `final_*`, `*_mean`, `aggregate_*`, etc.), use that one (the lowest
       such, if multiple, to be conservative).
    2. Otherwise, return the arithmetic mean of all SCOREs.
    Returns 0.0 if no scores were emitted.
    """
    if not rep.scores:
        return 0.0
    aggs = [v for k, v in rep.scores.items()
            if any(h in k.lower() for h in _AGGREGATE_HINTS)]
    if aggs:
        return min(aggs)
    return sum(rep.scores.values()) / len(rep.scores)


def evaluate_toxicology(
    ref_rep: TestReport,
    bad_reps: dict[str, TestReport],
) -> ToxVerdict:
    ref_failures: list[str] = []
    bad_failures: list[str] = []
    bad_quality_failures: list[str] = []

    # ── Reference must be (essentially) perfect ──
    # Slightly forgiving rule: mean of all SCOREs must be >= 0.95 AND every
    # individual SCORE must be >= 0.85. Rationale: tests with multiple
    # sub-scores often have one borderline corner case that we don't want
    # to hard-fail on, but we still want overall accuracy to be near-perfect.
    REF_MEAN_THRESHOLD = 0.95
    REF_MIN_THRESHOLD = 0.85
    if ref_rep.fails:
        ref_failures.append(f"REF has FAILs: {ref_rep.fails}")
    if not ref_rep.passes:
        ref_failures.append("REF emitted no PASS line")
    if not ref_rep.scores:
        ref_failures.append("REF emitted no SCORE")
    else:
        # Symmetric to the BAD logic: prefer the aggregate metric when present
        # so a single hard corner case (e.g. ill-defined empty input) doesn't
        # tank an otherwise-correct reference.
        agg = _aggregate_score(ref_rep)
        if agg < REF_MEAN_THRESHOLD:
            ref_failures.append(
                f"REF aggregate SCORE={agg:.3f} < {REF_MEAN_THRESHOLD}"
            )
        # Still complain if even the *mean* is suspiciously low — protects
        # against a test that omits an aggregate and has many bad cases.
        mean_score = sum(ref_rep.scores.values()) / len(ref_rep.scores)
        if mean_score < REF_MIN_THRESHOLD:
            ref_failures.append(
                f"REF mean SCORE={mean_score:.3f} < {REF_MIN_THRESHOLD}"
            )

    BAD_AGG_THRESHOLD = 0.5  # bad-sol aggregate score must be <= this

    # ── empty_output: any FAIL at L1/L2/SCORE is fine. Crashing the test
    #    is also fine because it's literally writing nothing. ──
    if "empty_output" in bad_reps:
        rep = bad_reps["empty_output"]
        caught = bool(rep.fails) or (
            rep.scores and _aggregate_score(rep) <= BAD_AGG_THRESHOLD
        )
        if not caught:
            bad_failures.append(
                f"BAD[empty_output] not caught: agg={_aggregate_score(rep):.3f} "
                f"fails={len(rep.fails)}"
            )

    # ── format_only: must reach L1_valid_* (so we know the candidate
    #    actually produced a parseable file), THEN be caught at L2 or SCORE.
    #    If it doesn't reach L1_valid, that's bad-solution quality, not a
    #    test catch — flag for the repair loop. ──
    if "format_only" in bad_reps:
        rep = bad_reps["format_only"]
        if not _has_pass(rep, "L1_valid"):
            bad_quality_failures.append(
                f"BAD[format_only] never reached L1_valid_* "
                f"(crashed before parse check; regenerate bad sols): "
                f"PASS={rep.passes[:3]} FAIL={rep.fails[:3]}"
            )
        else:
            caught_l2 = _count_fail(rep, "L2_") > 0
            agg = _aggregate_score(rep) if rep.scores else 0.0
            caught_score = bool(rep.scores) and agg <= BAD_AGG_THRESHOLD
            if not (caught_l2 or caught_score):
                bad_failures.append(
                    f"BAD[format_only] reached L1_valid but not caught at "
                    f"L2 (0 fails) and agg SCORE={agg:.3f} > {BAD_AGG_THRESHOLD}"
                )

    # ── naive_baseline: must reach L1_valid AND most L2 checks (proves
    #    it's structurally well-formed); MUST be caught via SCORE aggregate
    #    <= BAD_AGG_THRESHOLD. ──
    if "naive_baseline" in bad_reps:
        rep = bad_reps["naive_baseline"]
        if not _has_pass(rep, "L1_valid"):
            bad_quality_failures.append(
                f"BAD[naive_baseline] never reached L1_valid_* "
                f"(crashed; regenerate bad sols): "
                f"PASS={rep.passes[:3]} FAIL={rep.fails[:3]}"
            )
        else:
            l2_pass = _count_pass(rep, "L2_")
            l2_total = l2_pass + _count_fail(rep, "L2_")
            if l2_total > 0 and l2_pass / l2_total < 0.5:
                bad_quality_failures.append(
                    f"BAD[naive_baseline] failed too many L2 checks "
                    f"({l2_pass}/{l2_total}) — looks crashy, not 'naive'; regenerate bad sols"
                )
            if not rep.scores:
                bad_failures.append("BAD[naive_baseline] emitted no SCORE")
            else:
                agg = _aggregate_score(rep)
                if agg > BAD_AGG_THRESHOLD:
                    bad_failures.append(
                        f"BAD[naive_baseline] aggregate SCORE={agg:.3f} > "
                        f"{BAD_AGG_THRESHOLD}"
                    )

    per = {"reference": ref_rep, **bad_reps}
    return ToxVerdict(
        ok=(not ref_failures and not bad_failures and not bad_quality_failures),
        ref_failures=ref_failures,
        bad_quality_failures=bad_quality_failures,
        bad_failures=bad_failures,
        per_solution=per,
    )

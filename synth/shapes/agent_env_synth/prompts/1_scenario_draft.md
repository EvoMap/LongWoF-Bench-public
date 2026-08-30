# Role

You are a benchmark scenario author for the TaskGenome Bench *agent_env_synth*
track. Your output will be parsed automatically — follow the format EXACTLY.
Respond in **English** only. Do not use Chinese characters.

# Seed

- Domain: `{domain}`
- One-line task idea: `{task_idea}`
- Scenario id (placeholder): `{scenario_id}`

---

# What this track tests

The model is dropped into a Docker-built Python sandbox containing a
small *task package* (a few `.py` modules exposing domain-specific
functions / classes) and an input `data/` directory. The model writes
`generated.py` (no CLI args; reads from `data/`, calls into the task
package, writes outputs the test_script can verify). A `pytest`-based
test_script then asserts behavior.

The benchmark MUST distinguish three conditions:
1. **no_context**  — model sees `task.md` and the package source layout
   only. A capable LLM may guess the API roughly but will get function
   names / argument orders / dataclass field names wrong on subtle points.
2. **with_skill** — model also sees `SKILL.md` (which describes the API
   contracts and idiomatic usage). Now the call sequence is unambiguous.
3. **with_gene**  — out of scope here.

So the scenario MUST satisfy:

- **API-coupling matters.** The package exposes ≥ 2 named entry points
  whose signatures are NOT obvious from common ML/data convention.
  (no_context likely fails on a wrong-keyword-argument or wrong-order
  call.)
- **Behaviorally testable.** The test_script can decide PASS/FAIL by
  asserting properties of the output file (presence of keys, value
  ranges, structural invariants).
- **Sandbox-safe.** Pure Python; no network calls; no external services;
  inputs are committed to `data/`.

---

# What to produce

Produce exactly the following files, each wrapped in an XML-style fence:

```
<file path="task.md">
... task description in Markdown ...
</file>
<file path="SKILL.md">
... API + idiom prior ...
</file>
<file path="reference_solution.py">
... working Python script that solves the task using the package ...
</file>
<file path="test_script.py">
... pytest-based oracle ...
</file>
<file path="scenario.yaml">
... YAML metadata ...
</file>
<file path="environment/Dockerfile">
... minimal Docker image ...
</file>
<file path="environment/requirements.txt">
... pinned Python deps ...
</file>
<file path="package/__init__.py">
... small helper exporting the task-specific API ...
</file>
<file path="package/<module>.py">
... 1-2 modules implementing the API used by reference_solution.py ...
</file>
<file path="data/<input_file>">
... a small concrete input ...
</file>
```

The `package/` directory MUST contain `__init__.py` and at least ONE
additional `<module>.py`. You may add more `<module>.py` files if the API
naturally splits.

The `data/` directory MUST contain at least ONE input file. Inputs should
be SMALL (≤ 4KB; favor inline literals or 10–50 line CSV / JSON over
synthetic numerical arrays).

No commentary outside the file blocks. No backtick fences around them.

---

## File `task.md`

A self-contained task brief. **20–35 non-blank lines.**

### Required sections (in this order)

1. **`# Task`** — 1-paragraph description of what the agent must accomplish,
   in domain terms. Should mention the package by import path
   (e.g. "use functions from `package.<module>`") but NOT spell out the
   exact function signatures (those go in SKILL.md).

2. **`## Inputs`** — describe the contents of `data/`. List each file by
   path, its format, and what each row / field means in domain terms.

3. **`## Outputs`** — describe what `generated.py` must produce.
   Output goes to a file `output.json` (or another extension named here).
   List EVERY required key / column / field in the output schema.

4. **`## Run Convention (REQUIRED — exact)`** — copy verbatim:

   ```
   The agent's `generated.py` MUST be runnable EXACTLY as:

       python generated.py

   with the working directory at the candidate root. It MUST read inputs
   from `./data/` and write outputs to a path named in `## Outputs` above.
   It takes NO command-line arguments.
   ```

### Forbidden in `task.md`

- ❌ Naming the exact function name + signature
  (e.g. "call `pkg.foo(x, y, z=3)`"). That defeats with_skill vs no_context.
- ❌ Network / web / external service references.
- ❌ Tasks solvable in a single library call WITHOUT using the task package.

---

## File `SKILL.md`

The API + idiom prior. Exactly THREE sections, in this order. **40–80 non-
blank lines.**

```
## API Reference

<For each public function / class the task package exposes — typically
2-4 — show:>

  ### `<module>.<callable>`
  <one-paragraph purpose>
  Signature: `<callable>(<args with types>) -> <return type>`
  Returns: <what each component of the return means>
  Raises: <any non-obvious exceptions on bad inputs>

## Workflow

<5-8 numbered steps describing the idiomatic sequence to use the package
for THIS task class. Steps reference API entries by `<module>.<callable>`.
The last step says: "Write the result to `<output_path>` per task.md
schema".>

## Common Pitfalls

<3-5 bullets. Each: `**<short name>**: <failure mode> — <fix>`. These
should target the no_context model's likely API misuse — wrong argument
order, missing keyword, wrong return-tuple unpacking, wrong dataclass field
name, etc.>
```

Constraints:

- The `## API Reference` MUST list every callable that
  `reference_solution.py` actually imports from `package`. No more, no less.
- API signatures in SKILL.md MUST byte-match (modulo whitespace) the actual
  function definitions in `package/<module>.py`.

---

## File `reference_solution.py`

A working Python script. Total length 30–120 lines.

```python
"""Reference solution for {scenario_id}.

Solves the task using the package APIs declared in SKILL.md.
Reads from ./data/, writes to ./<output_path>. No CLI args.
"""
from __future__ import annotations

import json
from pathlib import Path

from package.<module> import <api_callables>


def main() -> int:
    data_dir = Path(__file__).resolve().parent / "data"
    out_path = Path(__file__).resolve().parent / "<output_filename>"
    # ... use package APIs to compute the answer ...
    out_path.write_text(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Hard rules:
- No CLI args; pure script invocation.
- Imports from `package.<module>` MUST resolve to actual definitions in
  `package/<module>.py`.
- Reads only from `./data/`; writes only to the candidate root (one file).
- Stdlib + at most ONE of: `numpy`, `pandas`, `pyyaml`. No others.
  (The package itself may import freely from this same allowlist.)

---

## File `test_script.py`

A `pytest`-style oracle. The v3 driver invokes:

    pytest test_script.py --tb=short -q

inside the candidate dir, after first running the candidate's
`generated.py`. The test_script checks the output file produced by
`generated.py`.

```python
"""pytest oracle for {scenario_id}."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

OUT_PATH = Path(__file__).resolve().parent / "<output_filename>"


@pytest.fixture(scope="module")
def out():
    assert OUT_PATH.exists(), f"{OUT_PATH} missing — generated.py did not run or did not write output"
    return json.loads(OUT_PATH.read_text())


def test_l1_schema(out):
    """Layer 1: output must contain the required top-level keys."""
    for k in ("<key1>", "<key2>"):
        assert k in out, f"missing key: {k}"


def test_l2_value_constraints(out):
    """Layer 2: declared invariants must hold (ranges, sums, monotonicity)."""
    # ... assertions specific to the task ...


def test_l3_correctness(out):
    """Layer 3: numeric correctness against exact expected values."""
    # ... canonical-value checks ...
```

Hard rules:
- At least 2 separate `test_*` functions covering different invariants.
- Do NOT import from `package` except for type-checking helpers — the
  test must verify behavior on the *output*, not invoke the package
  internals (otherwise candidates that don't use the package would
  spuriously fail).
- Use only stdlib + `pytest` (already installed in the Docker image).

---

## File `scenario.yaml`

> **YAML 1.1 quoting**: PyYAML silently casts bare `yes/no/on/off/true/false/y/n`
> and `null/~/empty` to Python bool/None. Wrap any field whose literal value
> matches one of these tokens in single quotes (e.g. `'no'`). The fields
> below don't normally collide, but if your scenario uses one of those
> tokens as an `output_artifacts.name` extension or similar, quote it.

```yaml
id: {scenario_id}
name: <short_snake_case_name>
family: agent_env_synth
domain: {domain}
shape_version: v3.agent_env_synth.0
source: synthetic
difficulty: <easy|medium|hard>
execution_mode: pytest_pkg
output_artifacts:
  - name: <output_filename>
    kind: file
    optional: false
package_modules:
  - package.<module>      # one entry per public module the task uses
api_entries:
  - <module>.<callable_one>
  - <module>.<callable_two>
required_packages:
  - python>=3.10
  - <pkg1>
required_image: python:3.11-slim
tags:
  - {domain}
  - <one more tag>
```

---

## File `environment/Dockerfile`

```dockerfile
FROM python:3.11-slim

WORKDIR /work
COPY . /work
RUN pip install --no-cache-dir -r environment/requirements.txt

CMD ["bash", "-lc", "python generated.py && pytest test_script.py --tb=short -q"]
```

Keep it minimal. Do NOT add system packages unless strictly necessary.

## File `environment/requirements.txt`

```
pytest>=7.0
# add the deps reference_solution.py imports, pinned tightly
```

---

## File `package/__init__.py`

```python
"""Re-exports for {scenario_id}'s task-specific API."""
from package.<module> import (
    <callable_one>,
    <callable_two>,
)

__all__ = ["<callable_one>", "<callable_two>"]
```

## File `package/<module>.py`

```python
"""Task-specific helpers used by reference_solution.py."""
from __future__ import annotations

# Real, working implementations of the API listed in SKILL.md.
# These MUST be the same functions that `reference_solution.py` actually
# calls — not stubs, not raise-NotImplementedError.
```

Hard rules:
- Every callable in `## API Reference` of SKILL.md MUST exist with a
  matching signature in `package/<module>.py`.
- `reference_solution.py` MUST import from `package.*` rather than
  re-implementing the helpers locally.

---

# Final self-check (mental, before emitting)

1. ☐ Are ALL listed file blocks present?
2. ☐ Does `reference_solution.py` import from `package.<module>` and
   does that module actually exist with matching signatures?
3. ☐ Does `test_script.py` have ≥ 2 `test_*` functions and check
   the OUTPUT file (not the package internals)?
4. ☐ Is the `data/` input small (≤ 4KB) and concrete?
5. ☐ Would a no_context LLM plausibly write `generated.py` with at
   least one wrong API call (wrong kwarg name, wrong return-tuple order,
   wrong dataclass field), causing test_script to FAIL?

If any answer is no, revise that section before emitting.

# Reminders

- Output MUST be the listed file blocks and nothing else.
- English only.
- Do NOT mention test scripts or evaluation in `task.md` / `SKILL.md`.
- The task package is a small *bespoke* helper module created for this
  scenario — it is NOT a published PyPI package. Don't rely on the agent
  knowing its API by name.

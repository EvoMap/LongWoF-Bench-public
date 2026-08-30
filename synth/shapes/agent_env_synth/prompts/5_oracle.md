# Role

You are Stage-S5 oracle author for the TaskGenome Bench **agent_env_synth** solution-first pipeline.
Output English only.

# Inputs

`task.md`:

{task_md}

`_design.json`:

{design_json}

`reference_solution.py` signatures:

{reference_signatures}

`_fixture_manifest.json`:

{fixture_manifest}

Fixture input contents:

{fixture_contents_json}

Gold output contents:

{gold_contents_json}

---

# Task

Write `test_script.py` — the pytest-based oracle that verifies a solver's `generated.py`.

The oracle must:
1. Run `generated.py --input <case_dir> --output <tmp_out_dir>` for each fixture case.
2. Compare the output against the gold standard (from `_gold/<case_id>/`).
3. Assert that adversarial cases (targeting hidden conventions) fail for solvers who
   apply naive defaults.
4. Emit the PASS/FAIL/SCORE protocol tokens.

---

# Required output tokens

For each test function, emit tokens in the format:
```python
print("PASS:L1_runs")           # generated.py ran without error
print("PASS:L1_output_exists")  # all output files exist
print(f"PASS:L2_{deliverable_id}_{case_id}")    # specific deliverable matches gold
print(f"SCORE:{deliverable_id}={score:.3f}")    # per-deliverable score
```

Required token classes:
- `L1_runs`: generated.py exited with returncode 0
- `L1_output_exists`: all `io_contract.output_files` present in output dir
- `L2_<deliverable_id>_<case_id>`: per-deliverable per-case correctness check
- `SCORE:<name>=<float>`: aggregate score per deliverable

---

# Coverage requirements

1. The oracle must embed `FIXTURE_DATA` — a dict mapping `case_id` to input/gold paths.
2. The oracle must include `HIDDEN_CONVENTION_COVERAGE` — a list of convention names
   from `_design.json.hidden_conventions[].name`.
3. Each adversarial case in FIXTURE_DATA must trigger at least one assertion that fails
   for a solver applying the wrong convention.
4. The overall SCORE is the harmonic mean across deliverables and cases.

---

# Structure template

```python
"""Oracle for {candidate_id}."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path
import pytest

HERE = Path(__file__).resolve().parent

HIDDEN_CONVENTION_COVERAGE = [
    # list of hidden convention names from _design.json
]

FIXTURE_DATA = {
    "case_id_1": {
        "input_dir": str(HERE / "data" / "case_1"),
        "gold_dir": str(HERE / "_gold" / "case_1"),
    },
    # ... more cases
}

def run_generated(input_dir: str, output_dir: str):
    result = subprocess.run(
        [sys.executable, str(HERE / "generated.py"), "--input", input_dir, "--output", output_dir],
        capture_output=True, text=True, timeout=60, check=False, cwd=HERE,
    )
    return result

# One test_* function per case or deliverable...
```

---

# Quality requirements

- ≥2 distinct `L2_*` base patterns (one per deliverable).
- ≥2 test cases exercised (normal + at least one adversarial).
- Reference comparison against embedded gold (not hardcoded expected values).
- The oracle must pass 100% when `generated.py` is replaced by `reference_solution.py`.

---

# Output format

Emit exactly one file block:

<file path="test_script.py">
...
</file>

No additional output.

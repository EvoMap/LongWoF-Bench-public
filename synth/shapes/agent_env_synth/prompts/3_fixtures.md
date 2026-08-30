# Role

You are Stage-S3 fixture and gold-standard generator for the TaskGenome Bench **agent_env_synth** solution-first pipeline.
Output English only.

# Inputs

`_design.json`:

{design_json}

`reference_solution.py` (full source):

{reference_source}

---

# Task

Write `_s3_generate_fixtures_and_gold.py` — a script that:
1. Creates input fixture files in `data/` matching `io_contract.input_files`.
2. Runs `reference_solution.py` on the input to get the gold output.
3. Saves the gold output to `_gold/` directory.
4. Writes `_fixture_manifest.json` describing each test case.

The script must create ≥2 test cases:
- 1 **normal case**: straightforward input where all hidden conventions are triggered.
- 1+ **adversarial cases**: one per `adversarial_case_plan` entry, each targeting a
  specific hidden convention.

---

# `_fixture_manifest.json` schema

```json
{
  "cases": [
    {
      "case_id": "normal_case",
      "input_path": "data/normal/",
      "gold_path": "_gold/normal/",
      "targets_convention": null,
      "description": "Standard case exercising the full derivation chain"
    },
    {
      "case_id": "adv_<convention_name>",
      "input_path": "data/adv_<convention_name>/",
      "gold_path": "_gold/adv_<convention_name>/",
      "targets_convention": "<hidden_conventions[].name>",
      "description": "Adversarial case that fails if <convention> is applied incorrectly"
    }
  ]
}
```

Each case:
- `input_path`: directory containing input files matching `io_contract.input_files`.
- `gold_path`: directory containing gold output files matching `io_contract.output_files`.

---

# Requirements

1. The script must create compact input fixtures (< 50 rows / < 5 KB per file).
2. Each adversarial case must contain a "borderline value" that distinguishes correct
   vs naive application of the targeted convention.
3. After generating fixtures, the script MUST run `reference_solution.py` on each case
   and save the output to `_gold/<case_id>/`.
4. Write `_fixture_manifest.json` at the end.
5. Must complete in < 60 seconds.

---

# Output format

Emit exactly one file block:

<file path="_s3_generate_fixtures_and_gold.py">
#!/usr/bin/env python3
"""Stage-S3: generate fixtures and gold for {candidate_id}."""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REF = HERE / "reference_solution.py"


def create_input_case(case_dir: Path, case_type: str) -> None:
    """Create input files for a specific test case type."""
    case_dir.mkdir(parents=True, exist_ok=True)
    # ... create input files ...


def run_reference(input_dir: Path, output_dir: Path) -> None:
    """Run reference_solution.py on the given input/output dirs."""
    output_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, str(REF), "--input", str(input_dir), "--output", str(output_dir)],
        capture_output=True, text=True, timeout=60, check=False, cwd=HERE,
    )
    if result.returncode != 0:
        raise RuntimeError(f"reference_solution.py failed: {result.stderr[-500:]}")


def main():
    cases = []
    # Normal case
    normal_in = HERE / "data" / "normal"
    normal_gold = HERE / "_gold" / "normal"
    create_input_case(normal_in, "normal")
    run_reference(normal_in, normal_gold)
    cases.append({
        "case_id": "normal_case",
        "input_path": "data/normal",
        "gold_path": "_gold/normal",
        "targets_convention": None,
        "description": "Standard case",
    })

    # Adversarial cases (one per hidden convention)
    # ... fill in ...

    manifest = {"cases": cases}
    (HERE / "_fixture_manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Wrote _fixture_manifest.json with {len(cases)} cases")


if __name__ == "__main__":
    main()
</file>

No additional output.

# Role

You are Stage-S3 adversarial variant designer for the TaskGenome Bench **math_reasoning** solution-first pipeline.
Output English only.

# Inputs

`_design.json`:

{design_json}

`reference_solution.py` (full source):

{reference_source}

The reference answer (from running reference_solution.py):

```
{gold_answer}
```

---

# Task

Write `_s3_generate_variants.py` — a Python script that, when run as
`python _s3_generate_variants.py`, generates `_variants.json` containing adversarial
variant scenarios.

Each variant is a modified version of the problem that tests whether the solver
correctly applies one specific hidden convention from `_design.json`. A solver
who applies the **wrong** convention (the naive default) should get a different
answer than a solver who applies the **correct** convention.

---

# What `_variants.json` must contain

```json
{
  "base_answer": "<canonical answer from reference_solution.py>",
  "variants": [
    {
      "variant_id": "adv_<convention_name>",
      "targets_convention": "<hidden_conventions[].name>",
      "description": "What changes in this variant and what the wrong approach gives",
      "naive_wrong_answer": "<what the default-convention solver would output>",
      "correct_answer": "<what the oracle-convention solver should output>",
      "note": "How to verify: the delta between naive and correct must be non-trivial"
    }
  ],
  "n_variants": <count>
}
```

**Requirements:**
1. One variant per hidden convention (so `n_variants >= len(hidden_conventions)`).
2. For each variant: `naive_wrong_answer != correct_answer` — otherwise the convention test is vacuous.
3. The `correct_answer` in each variant must be derivable from the reference solution's logic.
4. The script must complete in < 30 seconds.

---

# Important notes

- For math_reasoning, variants describe **modified problem parameters or modified
  intermediate interpretations** that flip the answer when the wrong convention is used.
- You do NOT need to produce separate runnable scripts per variant. The `_variants.json`
  is used by S5 to verify that bad solutions (which miss a convention) get the wrong answer.
- Keep the script simple — it may hardcode the variant data derived from your analysis
  of the reference solution and hidden conventions.

---

# Output format

Emit exactly one file block:

<file path="_s3_generate_variants.py">
#!/usr/bin/env python3
"""Stage-S3: generate adversarial variant manifest for {candidate_id}."""
import json
from pathlib import Path

def main():
    variants = {
        "base_answer": "{gold_answer}",
        "variants": [
            # One entry per hidden convention
        ],
        "n_variants": 0,
    }
    # Set n_variants
    variants["n_variants"] = len(variants["variants"])
    Path("_variants.json").write_text(json.dumps(variants, indent=2))
    print(f"Wrote _variants.json with {{variants['n_variants']}} variants")

if __name__ == "__main__":
    main()
</file>

No additional output.

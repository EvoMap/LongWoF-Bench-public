# Role

You are Stage-S3 adversarial variant designer for the TaskGenome Bench **rule_following** solution-first pipeline.
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

Write `_s3_generate_variants.py` — a script that generates `_variants.json` with adversarial
fact-pattern variants.

Each variant changes one trap detail in the fact pattern to test whether the solver correctly
applies a specific hidden convention. A solver who applies the wrong convention (the naive
real-world default) should arrive at a different answer than the correct one.

---

# What `_variants.json` must contain

```json
{
  "base_answer": "<expected_answer from design>",
  "variants": [
    {
      "variant_id": "adv_<convention_name>",
      "targets_convention": "<hidden_conventions[].name>",
      "description": "What changes in the fact pattern and why the naive solver answers wrong",
      "naive_wrong_answer": "<what a solver applying the wrong convention outputs>",
      "correct_answer": "<what the oracle-convention solver outputs>",
      "fact_pattern_delta": "The specific fact that changes vs the base scenario"
    }
  ],
  "n_variants": <count>
}
```

**Requirements:**
1. One variant per hidden convention (n_variants >= len(hidden_conventions)).
2. Each variant's `naive_wrong_answer` must differ from `correct_answer`.
3. The `correct_answer` must be a member of `answer_space`.
4. The fact_pattern_delta must be a minimal, specific change (not a whole new scenario).

---

# Output format

Emit exactly one file block:

<file path="_s3_generate_variants.py">
#!/usr/bin/env python3
"""Stage-S3: generate adversarial variant manifest for {candidate_id}."""
import json
from pathlib import Path


def main():
    base_answer = "{gold_answer}"
    variants = [
        # One entry per hidden convention — fill in from design analysis
    ]
    manifest = {
        "base_answer": base_answer,
        "variants": variants,
        "n_variants": len(variants),
    }
    Path("_variants.json").write_text(json.dumps(manifest, indent=2))
    print(f"Wrote _variants.json with {len(variants)} variants")


if __name__ == "__main__":
    main()
</file>

No additional output.

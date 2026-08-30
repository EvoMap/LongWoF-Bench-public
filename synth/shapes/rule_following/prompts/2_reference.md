# Role

You are Stage-S2 reference author for the TaskGenome Bench **rule_following** solution-first pipeline.
Output English only.

# Input

`_design.json`:

{design_json}

---

# Task

Write `reference_solution.py` that **applies the rule chain** from `_design.json` to
derive the canonical answer.

The reference solution is the authority on the correct answer. It must:

1. Implement **each step in `rule_chain` as a separate named function**.
2. Apply every `hidden_conventions[].detail_for_oracle` exactly as specified.
3. The final answer must equal `expected_answer` from `_design.json`.
4. Print exactly two lines to stdout:
   ```
   ANSWER: <exact member of answer_space>
   ANALYSIS: <1-paragraph citing which rules applied and why ≤ 80 words>
   ```
5. Take **no command-line arguments**.

---

# Structure requirement

Function call graph must mirror `rule_chain` dependency structure:
- Each step in the chain is a Python function returning an intermediate result.
- Each function calls or uses results from its `depends_on` functions.
- A final `solve()` function chains them to produce the answer.

**Do NOT collapse into a single function or a lookup table.** The structure is needed
for the gate's call-graph depth check.

---

# Imports allowed

Stdlib only. The reference solution for rule_following is typically short (10–30 lines)
since rule application is logical, not computational.

---

# Output format

Emit exactly one file block:

<file path="reference_solution.py">
#!/usr/bin/env python3
"""Reference solution for {candidate_id}.

Implements the rule chain from _design.json applying all hidden conventions.
"""
import sys

ANSWER = None  # will be set by solve()
ANALYSIS = ""  # will be set by solve()


def <step_function_1>():
    """Rule 1: classify entity."""
    ...


def <step_function_2>(result_1):
    """Rule 2: apply threshold based on classification."""
    ...


def <step_function_3>(result_2):
    """Rule 3 (+precedence): determine action."""
    ...


def solve():
    r1 = <step_function_1>()
    r2 = <step_function_2>(r1)
    answer = <step_function_3>(r2)
    analysis = "<cite rules and boundary conditions ≤ 80 words>"
    return answer, analysis


def main() -> int:
    global ANSWER, ANALYSIS
    ANSWER, ANALYSIS = solve()
    print(f"ANSWER: {ANSWER}")
    print(f"ANALYSIS: {ANALYSIS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
</file>

No additional output.

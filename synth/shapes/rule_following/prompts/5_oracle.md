# Role

You are Stage-S5 bad-solution author for the TaskGenome Bench **rule_following** solution-first pipeline.
Output English only.

# Inputs

`task.md`:

{task_md}

`_design.json`:

{design_json}

`reference_solution.py` signatures:

{reference_signatures}

Gold answer: `{gold_answer}`
Answer space: `{answer_space}`

---

# Task

Write **three bad solutions** in `_bad_solutions/` that look plausible but give wrong answers
because they miss hidden conventions or chain hops.

Each bad solution must:
1. Print `ANSWER: <member of answer_space>` with an answer different from `{gold_answer}`.
2. Print `ANALYSIS: ...` as a second line.
3. Represent a realistic failure mode a capable LLM would exhibit.

The three archetypes:

| File | Failure mode |
|------|-------------|
| `naive_baseline.py` | Applies real-world defaults / common sense. Gives `predicted_no_context_answer`. |
| `wrong_precedence.py` | Applies rules in the naive order instead of the hidden precedence convention. |
| `missing_boundary.py` | Uses inclusive threshold where exclusive (or vice versa) per the hidden boundary convention. |

---

# Requirements

- Each bad solution must print the same two-line format: `ANSWER: ...` then `ANALYSIS: ...`.
- The answer must be a member of `{answer_space}` but must NOT equal `{gold_answer}`.
- The logic must reflect the stated failure mode (not just output a random wrong answer).
- All bad solutions are simple scripts (stdlib only, 5–20 lines each).

---

# Output format

Emit exactly three file blocks:

<file path="_bad_solutions/naive_baseline.py">
#!/usr/bin/env python3
"""Bad solution: applies real-world / common-sense default.
Failure mode: <state what real-world rule it follows and why it gives the wrong answer>
"""
import sys

ANSWER = "<predicted_no_context_answer>"
ANALYSIS = "Based on standard practice for this scenario, <reasoning that leads to wrong answer>."

def main():
    print(f"ANSWER: {ANSWER}")
    print(f"ANALYSIS: {ANALYSIS}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
</file>

<file path="_bad_solutions/wrong_precedence.py">
#!/usr/bin/env python3
"""Bad solution: applies rules in naive order instead of hidden precedence.
Failure mode: <which rules are swapped and what wrong answer results>
"""
import sys

# ... implementation that applies rules in the wrong order ...
</file>

<file path="_bad_solutions/missing_boundary.py">
#!/usr/bin/env python3
"""Bad solution: uses wrong boundary condition (inclusive vs exclusive).
Failure mode: <which hidden convention is missed and what wrong answer results>
"""
import sys

# ... implementation with wrong boundary ...
</file>

No additional output.

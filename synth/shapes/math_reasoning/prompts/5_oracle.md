# Role

You are Stage-S5 bad-solution author for the TaskGenome Bench **math_reasoning** solution-first pipeline.
Output English only.

# Inputs

`task.md`:

{task_md}

`_design.json`:

{design_json}

`reference_solution.py` signatures:

{reference_signatures}

Gold answer: `{gold_answer}`

---

# Task

Write **three bad solutions** in `_bad_solutions/` — Python scripts that look plausible
but give wrong answers because they miss one or more hidden conventions or chain hops.

Each bad solution must:
1. Print an `ANSWER:` line (so it passes L1 format check).
2. Give an answer **different from** `{gold_answer}`.
3. Represent a realistic failure mode: what would a capable model do if it missed
   one hidden convention, skipped a derivation hop, or applied a naive default?

The three bad solution archetypes:

| File | What it misses | Typical failure |
|------|---------------|-----------------|
| `naive_baseline.py` | All hidden conventions; skips derivation chain hops | Computes the "obvious" answer using naive methods |
| `wrong_convention.py` | Applies default/common convention instead of oracle convention | Gets a different intermediate value, propagates to wrong final answer |
| `chain_shortcut.py` | Skips one or more derivation chain steps | Computes final answer from wrong intermediate quantity |

---

# Critical requirements

- Each bad solution must COMPUTE (not hardcode) a wrong answer consistent with its failure mode.
- The computed wrong answer must differ from `{gold_answer}` for the stated failure.
- All three bad solutions must print exactly two lines: `ANSWER: ...` and `ANALYSIS: ...`.
- No imports beyond stdlib.

---

# Output format

Emit exactly three file blocks:

<file path="_bad_solutions/naive_baseline.py">
#!/usr/bin/env python3
"""Bad solution: naive baseline that misses the derivation chain structure.
Failure mode: <describe what it gets wrong>
"""
# ... implementation ...
</file>

<file path="_bad_solutions/wrong_convention.py">
#!/usr/bin/env python3
"""Bad solution: applies wrong/default convention instead of oracle convention.
Failure mode: <describe which convention and what default it uses instead>
"""
# ... implementation ...
</file>

<file path="_bad_solutions/chain_shortcut.py">
#!/usr/bin/env python3
"""Bad solution: skips a hop in the derivation chain.
Failure mode: <describe which step is skipped and what is computed instead>
"""
# ... implementation ...
</file>

No additional output.

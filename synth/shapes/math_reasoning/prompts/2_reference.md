# Role

You are Stage-S2 reference author for the TaskGenome Bench **math_reasoning** solution-first pipeline.
Output English only.

# Input

`_design.json`:

{design_json}

---

# Task

Write `reference_solution.py` that **computes** the final answer by implementing the exact derivation chain in `_design.json`.

The reference solution is the authority on the correct answer. It must:

0. **Bind `input_constants` first.** Define module-level constants holding the EXACT values
   from `_design.json.input_constants` (same numbers, no rounding, no re-derivation). All
   computation must flow from these constants. Do NOT introduce a different modulus / dimension
   / bound than what `input_constants` declares — the task and oracle are pinned to these exact
   numbers and any divergence makes the task unsolvable.
1. Implement **each step in `derivation_chain` as a separate named function** so the derivation structure is auditable.
2. Apply every `hidden_conventions[].detail_for_oracle` exactly as specified.
3. Compute — NOT hardcode — the answer from the problem's raw inputs (the `input_constants`).
4. Print exactly two lines to stdout:
   ```
   ANSWER: <canonical answer in the declared answer_format>
   ANALYSIS: <1-paragraph method summary ≤ 80 words>
   ```
5. Take **no command-line arguments** — `python reference_solution.py` with nothing else must work.

---

# Answer format rules

- `integer`: print a bare integer, no leading zeros except `0`.
- `fraction`: print `p/q` with gcd(|p|,q)=1, q>0, no spaces. Use `fractions.Fraction`.
- `tuple_int`: print `(x1,x2,...,xN)` with no spaces.
- `enum`: print exactly one string from `answer_space`.

---

# Derivation structure requirement

The function call graph must match the `derivation_chain` dependency structure:

- Each `step_id` in the chain corresponds to a Python function.
- A function with `depends_on: [A, B]` must call or use the result of functions A and B.
- The final answer is assembled from the last node(s) in the chain.

**Do NOT collapse the chain into a single monolithic function.** The structure is required for the S2 gate's static depth check.

---

# Imports allowed

Stdlib only: `math`, `fractions`, `itertools`, `functools`, `collections`, `decimal`, `sys`.
Do NOT use numpy, scipy, or sympy.

---

# Output format

Emit exactly one file block:

<file path="reference_solution.py">
#!/usr/bin/env python3
"""Reference solution for {candidate_id}.

Implements the derivation chain from _design.json and emits the canonical answer.
"""
# ... your implementation ...
</file>

No additional output.

---

# Quality constraints

- 20–120 lines total.
- At least 3 named helper functions (matching the derivation chain steps).
- The printed `ANSWER:` value must be the exact canonical form for `answer_format`.
- The script must be self-contained and deterministic.

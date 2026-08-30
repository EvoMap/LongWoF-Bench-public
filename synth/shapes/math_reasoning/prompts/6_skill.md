# Role

You are Stage-S6 SKILL author for the TaskGenome Bench **math_reasoning** solution-first pipeline.
Output English only.

# Inputs

`_design.json`:

{design_json}

`task.md`:

{task_md}

`reference_solution.py` signatures:

{reference_signatures}

---

# Task

Write `SKILL.md` — a *re-derivable* procedural prior that, when given to a solver
alongside `task.md`, helps them rediscover the correct method WITHOUT handing them
the answer. Think of it as transferable know-how for this problem CLASS, not an
answer key for this instance.

It should:

1. Name the high-level method and explain WHY it applies to this problem class.
2. Walk through the derivation chain at the level of "what to compute and in what order"
   without giving away the specific answer.
3. Teach the *kind* of edge-case / tie-break / boundary policy a careful solver must
   decide on (so they know WHERE the trap is and HOW to reason about it), WITHOUT
   restating the exact hidden constant, threshold value, or final token from the
   reference solution. The solver should be able to RE-DERIVE the convention, not copy it.
4. Warn about the specific traps that the `naive_trap` describes, in conceptual terms.

# Leakage rules (mirror the gene distiller)

- NEVER restate an exact hidden numeric constant, threshold, coefficient, or quoted
  literal that appears in `reference_solution.py` / oracle / `detail_for_oracle` but
  NOT in the public `task.md`. Describe the mechanic ("apply a stricter, non-default
  cutoff before rounding") instead of the value.
- NEVER write the final answer or the specific answer-option/decision token.
- You MAY mention public contract terms that already appear in `task.md` (variable
  names, output format, ordering requirement) when they are essential to the method.
- Prefer "how to reason about the boundary" over "the boundary is X".

---

# Required structure (exactly three sections)

## Method

1–2 paragraphs naming and explaining the core mathematical technique. Should:
- Name the algorithm/theorem/approach.
- Explain the structural property that makes it applicable.
- NOT give the answer to this specific problem instance.

## Decision Procedure

5–8 numbered, mechanical steps. Each step takes inputs and produces intermediate values.
Steps must correspond to the `derivation_chain` — walk through X → Y → Z.
For each hidden convention, describe *which decision the solver must make and the
principle that resolves it* (e.g. "use the strict, not inclusive, comparison here"),
but do NOT paste the exact hidden constant/threshold from `detail_for_oracle`.
The last step says: "Emit the final answer in the format declared by task.md."

## Common Pitfalls

3–5 bullets, each in the form `**<short name>**: <failure mode> — <fix>`.
Must include:
- The `naive_trap` from `_design.json` as the primary pitfall (described conceptually).
- One pitfall per hidden convention framed as "what naive reasoning assumes vs. what the
  problem structure actually requires" — without naming the exact correct value.

---

# Constraints

- Do NOT include the expected answer value, nor any exact hidden constant/threshold that
  is absent from `task.md` (multi-digit numbers, quoted literals). State the mechanic, not the number.
- Do NOT name reference_solution.py or its functions.
- The method must yield exactly ONE canonical answer when applied correctly (no ambiguity).
- 30–70 non-blank lines.

---

# Output format

Emit exactly one file block:

<file path="SKILL.md">
## Method

...

## Decision Procedure

...

## Common Pitfalls

...
</file>

No additional output.

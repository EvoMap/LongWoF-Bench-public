# Role

You are Stage-S6 SKILL author for the TaskGenome Bench **rule_following** solution-first pipeline.
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

Write `SKILL.md` — a *re-derivable* rule-reasoning manual that, given alongside
`task.md`, helps a careful solver work out the answer WITHOUT being handed it.
Teach how to reason about precedence, boundaries, and ordering in this CLASS of
synthetic rule system, not the exact private rulebook for this instance.

# Leakage rules (mirror the gene distiller)

- NEVER restate an exact hidden threshold value, numeric constant, or quoted literal
  that appears in `reference_solution.py` / oracle / `detail_for_oracle` but NOT in
  the public `task.md`. Teach the *type* of comparison/precedence to apply, not the value.
- NEVER write the final answer or the specific answer-option/decision/action token.
  Describe precedence and override mechanics without naming the winning action.
- You MAY reference public facts/labels that already appear in `task.md`.
- Goal: a solver who reads this should know HOW to interrogate the scenario and which
  boundary/precedence questions to ask — but still has to derive the actual values.

---

# Required structure (exactly three sections)

## Rule Set

2–5 numbered rules, each in this form:

```
**Rule N (<short title>)** — <1-2 sentence rule statement>

  - **Trigger**: <the KIND of condition that fires this rule>
  - **Action**: <the KIND of outcome it prescribes — not the final winning token>
  - **Boundary**: <which boundary/equality question the solver must resolve here>
```

The rules must:
- Flag that thresholds are synthetic/non-standard (so the solver must NOT assume
  real-world defaults), WITHOUT stating the exact private threshold value.
- Cover all `rule_chain` steps from `_design.json` at the mechanic level.
- Include an explicit ordering/precedence statement (e.g. "a later override rule
  beats an earlier one") that teaches the `rule_precedence_override` mechanic
  WITHOUT naming the action that ends up winning.
- Tell the solver to determine, from the public scenario, whether each comparison is
  inclusive or exclusive — rather than asserting it for them.

## Decision Procedure

5–8 numbered mechanical steps. Each step takes inputs and produces intermediate values.
Steps must mirror the `rule_chain`:

1. Check [entity property] against Rule N's trigger condition.
2. If triggered → apply Rule N using the boundary rule you determined from the scenario.
3. If Rule M also triggers → resolve it with the stated precedence/override ordering.
4. ...
5. Emit the answer in the format task.md requires (do not pre-name the option).

**The Decision Procedure must teach the mechanics re-derivably; it must NOT spell out
the exact private thresholds or the final action/answer token.**

## Common Pitfalls

3–5 bullets in the form `**<short name>**: <failure mode> — <fix>`.
Must include:
- The primary trap from `naive_trap` in `_design.json`, described conceptually.
- One pitfall per hidden convention framed as "naive assumption vs. what the rule
  system actually requires" — without naming the exact correct value/token.

---

# Constraints

- Do NOT include the expected answer value, the winning answer-option/action token, nor any
  exact hidden threshold/constant absent from `task.md`. State the mechanic, not the number/token.
- Do NOT say "python reference_solution.py" or mention script names.
- Rules must be self-contained — solver should not need external knowledge.
- 30–70 non-blank lines.

---

# Output format

Emit exactly one file block:

<file path="SKILL.md">
## Rule Set

...

## Decision Procedure

...

## Common Pitfalls

...
</file>

No additional output.

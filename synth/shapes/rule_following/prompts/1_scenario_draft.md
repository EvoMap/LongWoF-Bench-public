# Role

You are a benchmark scenario author for the TaskGenome Bench *rule-following*
track. Your output will be parsed automatically — follow the format EXACTLY.
Respond in **English** only. Do not use Chinese characters.

# Seed

- Domain: `{domain}`
- One-line scenario idea: `{task_idea}`
- Scenario id (placeholder): `{scenario_id}`

---

# What this track tests

The model is shown `task.md` (a fact pattern + a yes/no/categorical question)
and `SKILL.md` (the rule manual). It must produce a single short answer in a
controlled output schema. The answer is graded by **exact string match** on the
`ANSWER:` line; the `ANALYSIS:` line is informational and ungraded.

The benchmark MUST distinguish three conditions:
1. **no_context**  — model sees only `task.md`. Most general-purpose LLMs will
   guess based on common sense / vibes; many will get it wrong on adversarial
   fact patterns.
2. **with_skill** — model also sees `SKILL.md` (the rule manual). The rule is
   spelled out and a careful reader can derive the answer.
3. **with_gene**  — model sees a *compressed* version of the rule. (Out of
   scope here; we will distill Gene later.)

So the scenario you write MUST satisfy:

- **Surface answer is wrong.** A reasonable generalist who has not read the
  rule manual would *plausibly* answer differently than the rule actually
  prescribes. (No-context model fails.)
- **Rule answer is unique.** Reading SKILL.md, exactly one canonical answer is
  derivable. No ambiguity, no judgment call. (With-skill model passes.)
- **Mechanical, not encyclopedic.** The fact pattern is novel; the answer
  depends on *applying* the rule, not on having memorized this particular
  case. (Avoid "what does §X.Y of GDPR say?" — that's recall, not application.)

---

# WORLD-KNOWLEDGE-LEAK AVOIDANCE (NEW — read carefully)

The single biggest failure mode for this track is the synthetic rule
**accidentally agreeing with real-world ground truth**. When that happens,
a no-context LLM still answers correctly because it has internalized the
real-world ground truth from its training data — the synthetic SKILL.md
becomes redundant, and the no-context vs with-skill gap disappears.

This is what happened to R0006 in the v1 pilot: the synthetic AML rule
prescribed `block_and_request_source_of_funds` for "Tier-2 + new account
+ same-beneficiary > $25k in 24h". Flash without SKILL.md still answered
`block_and_request_source_of_funds` 3/3 because that IS what real AML
practice does for that fact pattern.

To prevent leak, your synthetic rule MUST satisfy at least ONE of:

1. **Numeric thresholds DIFFER from real-world standards.** If the real
   world uses 10,000 USD as a structuring threshold, your synthetic Rule
   uses 8,500 or 12,750 — never 10,000. If real-world FAA night VFR uses
   3 sm visibility, your Rule uses 2 sm or 4.5 sm — never 3.

2. **Action names are NOT standard regulatory verbs.** Real AML uses "file
   SAR / file CTR"; your synthetic enum may use `escalate_to_review_board`
   or `freeze_pending_documentation` — clearly fictional, never standard.

3. **The trap-fact FLIPS the real-world default.** The trap-detail in
   `task.md` is something that under real-world rules would push the
   answer one way, but under your synthetic rule pushes the OPPOSITE way.

   ✓ Good (R0001-style): real-world night VFR says 2.5 nm fails minimums
     → naive answer "prohibited". Your synthetic Rule 2 adds a cargo +
     radar-altimeter exception that PERMITS it. Flash answers
     "prohibited" (matching real-world default) — gets it WRONG.

   ✗ Bad (R0006-style): real-world AML says "block and request source"
     for that fact pattern. Your synthetic Rule 3 ALSO says "block and
     request source". Flash answers "block" (matching real-world default)
     — gets it RIGHT, no trap.

4. **The right answer requires composing TWO synthetic rules in a way
   real-world precedent doesn't suggest.** Single-rule scenarios where
   the rule mirrors a real-world rule are leaky. Multi-rule scenarios
   where the precedence ordering is fictional are leak-resistant.

### Mandatory self-check on world-knowledge leak

Before emitting, ask yourself: **"If I deleted SKILL.md and gave only
task.md to a smart LLM, what answer would it give?"** If your answer is
"the same as `expected_answer`", your scenario LEAKS — start over with a
different `expected_answer` chosen to deliberately contradict the
real-world default for this fact pattern.

Document the predicted no-context answer in `scenario.yaml` as
`predicted_no_context_answer:` (must be a member of `answer_space` AND
must NOT equal `expected_answer`). The benchmark will check that Flash
actually gives this answer in calibration.

---

# What to produce

Produce exactly FOUR files, each wrapped in an XML-style fence:

```
<file path="task.md">
... fact pattern + question in Markdown ...
</file>
<file path="SKILL.md">
... the rule manual the model should consult ...
</file>
<file path="reference_solution.py">
... a tiny Python script that prints the canonical answer ...
</file>
<file path="scenario.yaml">
... YAML metadata, INCLUDING `expected_answer` and `answer_space` ...
</file>
```

No commentary outside the four blocks. No backtick code fences around them.

---

## File 1: `task.md`

Write a self-contained fact pattern + question that a careful rule reader
could answer. **20–35 non-blank lines.**

### Required sections (in this order)

1. **`# Scenario`** — 1-paragraph fact pattern. Concrete, named entities
   (use placeholder names if needed: "Acme Corp", "Patient Jane Doe",
   "Driver licensed in 2019", "ContractParty A and B"). Should contain
   AT LEAST one "trap" detail — a fact that a casual reader would
   ignore but that actually triggers the rule's edge case.

2. **`## Question`** — exactly ONE direct question. Use one of these
   shapes:
   - "Is X permitted under <rule reference>?"  → answer space: `yes` / `no`
   - "Which of the following applies: <option_a>, <option_b>, <option_c>?"
     → answer space: a closed enum (3–5 options)
   - "What is the required action?" → answer space: a closed enum
     (e.g. `notify_within_72h`, `seek_consent`, `no_action_required`)
   - "How many <thing> may X do?" → answer space: a small integer
     (0..20) or specific value
   - Avoid open-ended questions ("explain", "describe", "summarize").

3. **`## Output Format (REQUIRED — exact)`** — copy this block verbatim,
   filling in the answer space:

   ```
   The agent MUST emit EXACTLY two lines to stdout:

       ANSWER: <one of: ...one|...two|...three>
       ANALYSIS: <one paragraph, ≤ 80 words, citing the relevant rule>

   - The `ANSWER:` line is graded by exact string match (case-insensitive).
   - The `ANALYSIS:` line is informational and ungraded.
   ```

### Forbidden in `task.md`

- ❌ Any explicit citation of a SKILL.md rule number ("per Rule 14.2.b") in
  the question itself — that leaks to no_context model.
- ❌ "According to <famous external standard>" — keep the rule self-contained
  inside SKILL.md. Pretend the model has never heard of GDPR / FAA Part 91 / etc.
- ❌ Multiple questions, follow-ups, or "explain why".

---

## File 2: `SKILL.md`

The procedural / rule prior. Exactly THREE sections, in this order. **30–60
non-blank lines.**

```
## Rule Set

<2-5 numbered rules, each in this shape:>

**Rule N (<short title>)** — <1-2 sentence rule statement, including the
trigger condition and the prescribed action / answer.>

  - **Trigger**: <precise condition, with constants where applicable>
  - **Action**: <what the rule prescribes>
  - **Boundary**: <one edge case that the trap detail in task.md hinges on>

## Decision Procedure

<5-8 numbered steps describing HOW to apply the rule set to an arbitrary
fact pattern. Steps must be mechanical: "check whether X. If yes, ...". The
last step must say: "Emit the prescribed answer in the answer space declared
by task.md".>

## Common Pitfalls

<3-5 bullets. Each is `**<short name>**: <failure mode> — <fix>`. The
pitfalls should highlight the trap detail used in this scenario AND
related traps a careful rule-reader should be alert to.>
```

Constraints:

- Rules must be **self-contained** — do NOT say "per the standard reference".
  The reader of SKILL.md should be able to derive the answer without external
  knowledge.
- The fact-pattern trap MUST be addressable by exactly one of your rules. If
  no rule covers the trap, your scenario is unfair (no_context AND with_skill
  both lose).
- Conversely: if a generalist LLM could answer correctly *without* SKILL.md
  (because it's common sense), the scenario is too easy. Add a counter-
  intuitive twist in the trap.

---

## File 3: `reference_solution.py`

A trivial canonical-answer printer. Total length 5–25 lines.

```python
"""Reference solution for {scenario_id}.

This script does NOT solve the rule application — it merely emits the
canonical answer derived offline by the scenario author. The actual
benchmark grades the candidate model's stdout against this canonical
answer using exact-match on the `ANSWER:` line.
"""
import sys

ANSWER = "<exact canonical answer string>"
ANALYSIS = (
    "<one-paragraph explanation citing Rule N. ≤ 80 words. "
    "Quote the trigger condition and the boundary that resolves the trap.>"
)


def main() -> int:
    print(f"ANSWER: {ANSWER}")
    print(f"ANALYSIS: {ANALYSIS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Hard rules:
- The `ANSWER` constant MUST be a member of the `answer_space` you declare in
  `scenario.yaml`. Verbatim, case-insensitive match.
- The script takes NO command-line arguments — `python reference_solution.py`
  with nothing else must work.
- Imports allowed: stdlib only. No numpy, pandas, etc.

---

## File 4: `scenario.yaml`

### YAML 1.1 boolean trap (MANDATORY)

PyYAML uses YAML 1.1 semantics: the bare tokens `yes`, `no`, `on`, `off`,
`true`, `false`, `y`, `n` (case-insensitive) are silently cast to Python
booleans. If your `answer_space` or `expected_answer` contains any such
token as its **literal answer string**, you MUST wrap it in single quotes.

  ✗ Wrong (loads as `[True, False]`):
    ```yaml
    answer_space:
      - yes
      - no
    expected_answer: no
    ```
  ✓ Correct:
    ```yaml
    answer_space:
      - 'yes'
      - 'no'
    expected_answer: 'no'
    ```

Free-form snake_case enum tokens (`file_sar`, `requires_ventilation`,
`reconsent_at_next_visit`, …) are safe and need no quoting. Only the
exact YAML-bool tokens above need single quotes.

### Template

```yaml
id: {scenario_id}
name: <short_snake_case_name>
family: rule_following
domain: {domain}
shape_version: v3.rule_following.0
source: synthetic
difficulty: <easy|medium|hard>
answer_space:
  - <option_one>          # quote with '...' if option matches a YAML 1.1 bool
  - <option_two>
  - <option_three>
expected_answer: <one of the answer_space members, EXACTLY; quote if YAML-bool>
predicted_no_context_answer: <the member of answer_space a no-SKILL LLM
  would give based on real-world default reasoning. MUST NOT equal
  expected_answer. Quote if YAML-bool. (Calibration verifies this.)>
trap_summary: <one sentence: which fact in task.md is the trap, and which
  Rule N in SKILL.md resolves it. Should explicitly name the real-world
  default the trap flips.>
tags:
  - {domain}
  - <one more tag>
```

Difficulty calibration:
- `easy`   — surface answer happens to coincide with rule answer (rare; this
  is what we want to AVOID for this benchmark)
- `medium` — surface answer differs from rule answer; trap is one rule deep
  (most scenarios should land here)
- `hard`   — trap requires composing two rules / a boundary case ANY rule
  reader could miss

Aim for `medium`; `easy` is rarely valuable.

---

# Final self-check (mental, before emitting)

Hard-rejected by the v3 rule_following Gate A if any answer is no:

1. ☐ Does `task.md` contain `## Question` and `## Output Format`?
2. ☐ Is the `ANSWER:` answer space a closed enum (≤ 6 members) declared in
   both `task.md` and `scenario.yaml`?
3. ☐ Is `expected_answer` in `scenario.yaml` an exact member of `answer_space`?
3a. ☐ If any answer-space token matches `yes|no|on|off|true|false|y|n` (case-
   insensitive), is it wrapped in single quotes (`'yes'`, `'no'`, …)?
4. ☐ Does `reference_solution.py` print `ANSWER: <expected_answer>` (verbatim)?
5. ☐ Does `SKILL.md` have exactly 3 sections (`## Rule Set`, `## Decision
   Procedure`, `## Common Pitfalls`)?
6. ☐ Does at least one rule in `## Rule Set` resolve the trap stated in
   `trap_summary`?
7. ☐ Is the question novel (not testable by recall of a famous standard)?
8. ☐ Would a generalist LLM without SKILL.md plausibly get this WRONG?
9. ☐ Does at least ONE of the four world-knowledge-leak guards apply
   (off-standard threshold / fictional action verb / trap-flips-default /
   multi-rule precedence)?
10. ☐ Have you filled `predicted_no_context_answer:` in scenario.yaml
    AND is it DIFFERENT from `expected_answer`?

If any answer is no, revise that section before emitting.

# Reminders

- Output MUST be the four `<file>` blocks and nothing else.
- English only.
- `reference_solution.py` MUST run end-to-end with `python reference_solution.py`
  and print exactly two lines to stdout (`ANSWER: ...` and `ANALYSIS: ...`).
- Do NOT mention test scripts or evaluation in `task.md` / `SKILL.md`.

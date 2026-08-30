# Role

You are a benchmark scenario author for the TaskGenome Bench *math-reasoning*
track. Your output will be parsed automatically — follow the format EXACTLY.
Respond in **English** only. Do not use Chinese characters.

# Seed

- Domain: `{domain}`
- One-line problem idea: `{task_idea}`
- Scenario id (placeholder): `{scenario_id}`

---

# What this track tests

The model is shown `task.md` (a math word problem) and `SKILL.md` (the
relevant procedural / algorithmic prior). It must produce a single answer in
a controlled output schema. The answer is graded by **exact-string match on
the canonicalized `ANSWER:` line** (whitespace stripped, case-insensitive,
fractions / decimals normalized per the answer-format rule below).

The benchmark MUST distinguish three conditions:
1. **no_context**  — model sees only `task.md`. A capable LLM may solve some
   problems by chain-of-thought; many will misstep on a non-obvious method
   choice or boundary condition.
2. **with_skill** — model also sees `SKILL.md` (the procedural prior). The
   correct method is named and a careful application yields the answer.
3. **with_gene**  — model sees a *compressed* version. (Out of scope here.)

So the scenario MUST satisfy:

- **First-attempt-trap**. A reasonable solver who reaches for the *obvious*
  approach gets it numerically wrong (off-by-one, wrong base case, missed
  edge, wrong rounding rule). (no_context model often fails.)
- **Skill-mediated.** A careful application of the procedure named in
  SKILL.md yields the unique correct answer. (with_skill model passes.)
- **Computable to a closed form.** The answer must be an exact value
  (integer, simple fraction, or expression in a declared canonical form),
  NOT a real-valued numerical answer requiring floating-point tolerance.

---

# BRUTE-FORCE-RESISTANCE & ANTI-TEXTBOOK (NEW — read carefully)

The v1 pilot found 3/10 trivially-easy candidates because the problems
were either (a) brute-forceable in seconds or (b) carbon-copies of
textbook problems whose canonical method Flash already memorized. To
prevent this:

### Brute-force-resistance (MANDATORY)

If your problem reduces to "find an integer in `[a, b]` satisfying
property P", the search space `b - a` MUST exceed **10^6**, OR the
property P must be expensive to check (≥ 100 ops per candidate).

  ✗ "Find smallest n > 1 with n² ≡ 1 (mod 360)." → search space 358,
    each check is O(1). Flash brute-forces it. (this was M0001 in v1)
  ✓ "Find smallest n > 1 with n² ≡ 1 (mod 27720) and gcd(n,27720)=1
    and n is composite." → search space 27,718, but property requires
    factorization → moves the difficulty into method selection.
  ✓ "Find the unique n in [1, 10^9] satisfying ..." → search space 10^9
    forces a method other than brute force.

For combinatorics problems, the same principle: if the answer is `≤ 10^4`
and naive enumeration is tractable, redesign so naive enumeration is not
tractable (raise group sizes, add a constraint that requires
inclusion-exclusion or generating functions).

### Anti-textbook list (FORBIDDEN problem classes)

The following problems are RECALL puzzles for any LLM trained on math
content. DO NOT generate scenarios that map 1:1 onto them:

- Pick's theorem on a triangle with vertices at the origin and on the axes
  (`(0,0), (a,0), (0,b)`) — formula plug-in
- Rank of `M[i,j] = (i+j) mod 2` or any 2-banded parity matrix — stock
  finite-field problem
- "k people seated in a circle, m of them mutually non-adjacent" via the
  gap method — canonical setup
- `n!` permutations with a fixed-point count → derangements `!n` formula
- Stars-and-bars on "x_1 + ... + x_k = n" with non-negativity
- Fermat's little theorem on `a^p mod p` for prime p — direct application
- Euler totient on `phi(p*q)` for two known primes — formula plug-in
- Closed-form Fibonacci / Lucas numbers when the recurrence is exactly
  `f(n) = f(n-1) + f(n-2)` — identity-by-name
- Pythagorean triple enumeration up to a small bound

If your `task_idea` seed lands on one of these, **rewrite the seed** with
a non-canonical twist before drafting:

- Add a non-default tie-breaking rule
- Change the group-action equivalence (e.g. reflections distinct vs equiv)
- Compose two operations (Pick + intersection with a square; rank with
  one extra row deleted)
- Move the trap to a corner case (zero / negative / boundary value)

### Seed-fidelity (NEW — addresses v1 M0002 / M0010 drift)

Numeric constants from the seed (group sizes, moduli, dimensions) are
**hard inputs**. You MAY:

- Add ADDITIONAL constraints not in the seed
- Tighten ambiguity in the seed wording

You MAY NOT:

- Change a numeric constant from the seed without declaring it. If you
  judge the seed as un-discriminating after applying brute-force-resistance
  and anti-textbook rules, change the constant AND record the original in
  `scenario.yaml`'s new `effective_seed:` field. Audit will surface this.

  ✗ Silent drift: seed says "K_{3,4} matchings of size 3", you write
    "K_{5,7} matchings of size 4" with no record. (this was M0010 in v1)
  ✓ Declared drift: seed says "K_{3,4} ...", you write "K_{5,7} ..."
    AND set `scenario.yaml` `effective_seed: "K_{5,7} matchings of
    size 4 (raised from seed K_{3,4} size 3 to defeat brute-force)"`.

---

# What to produce

Produce exactly FOUR files, each wrapped in an XML-style fence:

```
<file path="task.md">
... math problem statement in Markdown ...
</file>
<file path="SKILL.md">
... the procedural prior ...
</file>
<file path="reference_solution.py">
... a small Python script that prints the canonical answer ...
</file>
<file path="scenario.yaml">
... YAML metadata, INCLUDING `expected_answer`, `answer_format` ...
</file>
```

No commentary outside the four blocks. No backtick code fences around them.

---

## File 1: `task.md`

A self-contained math problem. **15–35 non-blank lines.**

### Required sections (in this order)

1. **`# Problem`** — 1-2 paragraphs stating the problem with concrete
   numbers. All quantities must be specified (no "some integer N", no
   parameters left undefined). The problem must reference a specific
   `answer_format` declared below.

2. **`## Output Format (REQUIRED — exact)`** — copy this block verbatim,
   filling in only the `<answer_format>` placeholder:

   ```
   The agent MUST emit EXACTLY two lines to stdout:

       ANSWER: <answer in <answer_format>>
       ANALYSIS: <one paragraph, ≤ 80 words, sketching the method used>

   - The `ANSWER:` line is graded by exact-string match after canonicalization.
     Allowed `answer_format` values:
       - `integer`            — base-10 integer; no leading zeros except 0
       - `fraction`           — `p/q` with q > 0, gcd(|p|,q) = 1, no spaces
       - `pair_int`           — `(a,b)` with integers a, b
       - `tuple_int`          — `(x1,x2,...,xN)` integers, declared length
       - `enum`               — one of a closed set declared in scenario.yaml
   - The `ANALYSIS:` line is informational and ungraded.
   ```

### Forbidden in `task.md`

- ❌ Real-valued answers requiring tolerance ("0.314" — switch to
  `fraction` or change the question).
- ❌ "Solve this DEQ" / "minimize this functional" without explicit
  discretization or closed-form solution.
- ❌ Multiple sub-questions ("a) ... b) ... c) ..."). One scalar / tuple
  answer per scenario.
- ❌ Naming SKILL.md procedures inside the question itself
  ("use Euclid's algorithm").
- ❌ "If you have heard of <named theorem>" — keep the prior in SKILL.md.

---

## File 2: `SKILL.md`

The procedural prior — the technique a domain practitioner would apply.
Exactly THREE sections, in this order. **30–60 non-blank lines.**

```
## Method

<1-2 paragraph description of the canonical procedure for problems of
this class. May name standard algorithms or theorems (e.g.
"the extended Euclidean algorithm", "Lagrange interpolation",
"Pick's theorem", "modular exponentiation"). Should explain WHY this
method applies — what structural property of the problem class makes
it work — not just WHAT to compute.>

## Decision Procedure

<5-8 numbered, mechanical steps. Each step takes inputs and produces
intermediate values. The last step says: "Emit the final answer in
the format declared by task.md".>

## Common Pitfalls

<3-5 bullets. Each is `**<short name>**: <failure mode> — <fix>`.
Highlight the trap that the no_context model is likely to fall into:
off-by-one base cases, sign errors, wrong tie-breaks in floor/ceil,
modular arithmetic with negatives, etc.>
```

Constraints:

- The method MUST yield exactly ONE canonical answer when applied
  correctly. Otherwise the scenario is unfair.
- The method MUST yield a DIFFERENT answer if the no_context "naive"
  approach is followed — that's where the trap lives.
- Do NOT include the final numeric answer in SKILL.md. SKILL.md teaches
  the procedure, not the answer to this particular problem.

---

## File 3: `reference_solution.py`

A small Python script that performs the actual computation (NOT a hardcoded
constant — the script must derive the answer from the problem inputs so a
reviewer can verify correctness end-to-end). Total length 15–80 lines.

```python
"""Reference solution for {scenario_id}.

Implements the procedure described in SKILL.md and emits the canonical
answer to stdout. Stdlib + (optional) `fractions`, `math`, `decimal` only.
"""
import sys
from fractions import Fraction


def solve() -> str:
    # ... derive ANSWER from the problem's stated inputs ...
    # Use Fraction for exact arithmetic when answer_format='fraction'.
    # Return a STRING in the canonical answer_format.
    raise NotImplementedError("fill me in")


def main() -> int:
    answer = solve()
    print(f"ANSWER: {answer}")
    print(f"ANALYSIS: <one-paragraph method summary, ≤ 80 words>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Hard rules:
- The script takes NO command-line arguments — `python reference_solution.py`
  with nothing else must work.
- The printed `ANSWER:` MUST match the `expected_answer` declared in
  `scenario.yaml` *byte-for-byte after canonicalization* per the
  `answer_format`.
- Imports allowed: stdlib only (`fractions`, `math`, `decimal`,
  `itertools`, `functools`, `collections`). No numpy / scipy / sympy.
- The script MUST actually compute the answer, not hardcode it.
  (We will spot-check by tweaking task inputs and re-running.)

---

## File 4: `scenario.yaml`

### YAML 1.1 quoting rule (MANDATORY)

`expected_answer` is parsed by PyYAML, which uses YAML 1.1 semantics. Any
of these will silently cast to a Python bool/None and break the oracle:

- bare `yes`, `no`, `on`, `off`, `true`, `false`, `y`, `n` → bool
- bare `null`, `~`, empty → None

Always wrap `expected_answer` in **double quotes** to keep it a string,
EVEN for plain integers:

  ✗ `expected_answer: 19`     (parses as int — usually fine, but inconsistent)
  ✗ `expected_answer: no`     (parses as `False` — breaks oracle)
  ✓ `expected_answer: "19"`
  ✓ `expected_answer: "61/216"`
  ✓ `expected_answer: "(0,1,2)"`
  ✓ `expected_answer: "no"`   (only if answer_format == 'enum')

For `answer_space` (only used when `answer_format == 'enum'`), apply the
same rule to any value matching a YAML 1.1 bool/null token.

### Template

```yaml
id: {scenario_id}
name: <short_snake_case_name>
family: math_reasoning
domain: {domain}
shape_version: v3.math_reasoning.0
source: synthetic
difficulty: <easy|medium|hard>
answer_format: <integer|fraction|pair_int|tuple_int|enum>
expected_answer: "<canonical answer string in answer_format — ALWAYS DOUBLE-QUOTED>"
# Required only if answer_format == 'enum':
answer_space:
  - <option_one>          # quote with '...' if matches a YAML 1.1 bool/null
  - <option_two>
# Optional but RECOMMENDED — if you changed any numeric constant from the
# seed, declare the effective form here so audit can spot drift. Drop the
# field entirely if the seed was used verbatim.
effective_seed: "<verbatim restatement of the problem you actually authored,
  if it differs from the input seed in any numeric / structural way>"
trap_summary: <one sentence: which naive approach the no_context model
  is likely to use, and why it gives the wrong answer>
tags:
  - {domain}
  - <one more tag>
```

Difficulty calibration:
- `easy`   — naive approach happens to give the same answer (avoid)
- `medium` — naive approach gives a *predictably wrong* answer
- `hard`   — multiple plausible methods; only one yields the canonical
  answer

Aim for `medium`. `easy` should be < 20% of the pool.

---

# Final self-check (mental, before emitting)

Hard-rejected by Gate A if any answer is no:

1. ☐ Does `task.md` state ALL inputs (no free parameters)?
2. ☐ Is the answer expressible as an EXACT value in `answer_format` (no
   floating-point tolerance needed)?
3. ☐ Does `reference_solution.py` actually compute the answer (not hardcoded
   as a return constant) and print it on the `ANSWER:` line in the canonical
   form for the declared `answer_format`?
4. ☐ Is `expected_answer` in `scenario.yaml` byte-equal to what
   `reference_solution.py` prints on the ANSWER: line, AND wrapped in
   double quotes (`expected_answer: "19"`, never bare `expected_answer: 19`)?
5. ☐ Does `SKILL.md` have exactly 3 sections (`## Method`, `## Decision
   Procedure`, `## Common Pitfalls`)?
6. ☐ Would a model without SKILL.md plausibly fall into the trap described
   in `trap_summary`?
7. ☐ Brute-force resistance: search space > 10^6 OR property check is
   non-trivial?
8. ☐ Anti-textbook: is your problem distinct from the FORBIDDEN list above?
   (Pick on axis-aligned triangle / parity matrix rank / canonical gap
   method / direct totient / Fermat little / direct stars-and-bars / direct
   derangement formula / direct Pythagorean enumeration)
9. ☐ Seed-fidelity: did you keep the seed's numeric constants verbatim?
   If not, did you declare `effective_seed:` in scenario.yaml?

If any answer is no, revise that section before emitting.

# Reminders

- Output MUST be the four `<file>` blocks and nothing else.
- English only.
- `reference_solution.py` MUST run end-to-end with `python
  reference_solution.py` and print exactly two lines to stdout.
- Do NOT mention test scripts or evaluation in `task.md` / `SKILL.md`.

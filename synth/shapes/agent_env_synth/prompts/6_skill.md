# Role

You are Stage-S6 SKILL author for the TaskGenome Bench **agent_env_synth** solution-first pipeline.
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

Write `SKILL.md` — an API usage guide that, given alongside `task.md`, helps a solver
use the package correctly, while making them RE-DERIVE the non-obvious behavior rather
than copying it. Teach what each function is for and which behaviors to verify, not the
exact hidden values.

# Leakage rules (mirror the gene distiller)

- You MAY list functions, their purpose, parameter NAMES, and the *kind* of return
  structure (these mirror public API shape).
- You must NOT paste exact hidden default VALUES, magic constants, or quoted literals
  that come from `reference_solution.py` / oracle / `detail_for_oracle` and are absent
  from `task.md`. Instead, WARN that a parameter has a non-obvious default the solver
  must check (e.g. "this defaults to a non-standard mode — inspect it before relying on it").
- NEVER give expected output values or the final answer.
- Goal: the solver learns WHERE the non-default behavior lives and how to confirm it,
  but still has to discover the actual value from the package itself.

---

# Required structure (exactly four sections)

## Overview

1 paragraph describing the package's purpose in the context of the domain problem.
Do not reveal the answer or expected output values.

## API Reference

List every function from `package_api` in `_design.json`:

```
### `<module>.<function_name>(<signature>)`

**Purpose**: ...
**Parameters**:
  - `<param>`: <type> — <what it controls; if it has a non-obvious default, SAY SO without giving the value>
**Returns**: <return type and structure>
**Watch out** (critical): <which non-default behavior to verify before trusting this call — described, not its exact value>
**Common mistake**: <what naive callers assume vs. what they must check>
```

For every `hidden_convention`, the corresponding function must carry an explicit
warning that its behavior is non-default and must be verified — but the warning
describes the *mechanic*, not the exact hidden constant/value.

## Workflow

4–6 numbered steps showing how to call the package functions in the correct sequence
to produce all deliverables. Steps must correspond to `derivation_chain`.

Include explicit notes about:
- Which function calls must precede others (the dependency chain).
- Any non-default arguments that must be passed.
- How to correctly handle the return types.

## Common Pitfalls

3–5 bullets in the form `**<short name>**: <failure mode> — <fix>`.
One bullet per hidden convention contrasting the naive assumption with what the solver
must verify — without stating the exact hidden value.

---

# Constraints

- Do NOT give the expected output values or the final answer.
- Do NOT paste exact hidden default values / magic constants absent from `task.md`; name the
  parameter and warn it is non-default instead.
- Do NOT say "see reference_solution.py".
- API reference must include signatures (function + parameter NAMES); you may omit the exact
  hidden default VALUE and instead flag it as "non-default — verify".
- 50–100 non-blank lines.

---

# Output format

Emit exactly one file block:

<file path="SKILL.md">
## Overview

...

## API Reference

...

## Workflow

...

## Common Pitfalls

...
</file>

No additional output.

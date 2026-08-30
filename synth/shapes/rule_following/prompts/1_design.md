# Role

You are Stage-S1 designer for the TaskGenome Bench **rule_following** solution-first pipeline.
Output English only.

# Input

- Domain: `{domain}`
- Seed idea: `{task_idea}`
- Candidate id: `{candidate_id}`

---

# Goal

Design a multi-hop rule-chain task skeleton **before** any task.md or SKILL.md is written.

The skeleton must target three difficulty axes:

| Axis | Requirement for rule_following |
|------|-------------------------------|
| **D3 multi-hop chain** | Answer requires applying ≥3 rules in sequence: rule_A classifies the entity → rule_B sets a threshold based on the classification → rule_C determines the action based on the threshold. task.md will present the fact pattern but NOT name which rules apply or in what order. |
| **D1 hidden precedence** | The rulebook has ≥2 precedence/priority conventions that are NOT stated in task.md. A solver who applies the "common sense" ordering gets the wrong answer. |
| **D2 composite answer** | At least one multi-part determination is required — the final answer may be an enum action, but arriving at it requires correctly classifying two or more independent sub-questions. |

**World-knowledge-leak guard** (MANDATORY — see failure mode R0006):
The synthetic rule outcome MUST NOT agree with real-world defaults for the same fact pattern.
At least one of the following must hold:
1. Numeric thresholds differ from real-world standards.
2. Action names are non-standard synthetic verbs.
3. The trap-fact FLIPS the real-world default outcome.
4. The precedence order is fictional/counterintuitive.

---

# Output format (strict)

Emit exactly one file block:

<file path="_design.json">
{
  "scenario_name": "...",
  "domain": "...",
  "fact_pattern_summary": "2–3 sentences describing the synthetic regulatory domain and the specific fact pattern to be judged",
  "answer_format": "enum",
  "answer_space": ["action_alpha", "action_beta", "action_gamma"],
  "expected_answer": "action_beta",
  "rule_chain": [
    {
      "step_id": "classify_entity_type",
      "depends_on": [],
      "description": "Classify the entity using Rule 1 to determine which tier it falls in"
    },
    {
      "step_id": "apply_tier_threshold",
      "depends_on": ["classify_entity_type"],
      "description": "Based on the tier from step 1, check Rule 2's threshold against the fact pattern values"
    },
    {
      "step_id": "determine_action",
      "depends_on": ["apply_tier_threshold"],
      "description": "Rule 3 maps the threshold result to the prescribed action, with Rule 4 as override if present"
    }
  ],
  "rules": [
    {
      "rule_id": "Rule1",
      "title": "Entity classification",
      "trigger": "...",
      "action": "...",
      "boundary": "..."
    },
    {
      "rule_id": "Rule2",
      "title": "Tier threshold",
      "trigger": "...",
      "action": "...",
      "boundary": "..."
    },
    {
      "rule_id": "Rule3",
      "title": "Action determination",
      "trigger": "...",
      "action": "...",
      "boundary": "..."
    }
  ],
  "hidden_conventions": [
    {
      "name": "rule_precedence_override",
      "operation_name_for_task": "rule precedence",
      "detail_for_oracle": "when Rule2 and Rule3 both trigger, Rule3 takes precedence; this is non-obvious from the rule ordering",
      "recoverability_reason": "SKILL.md explicitly states the precedence table in the Decision Procedure section"
    },
    {
      "name": "boundary_tie_break",
      "operation_name_for_task": "threshold comparison",
      "detail_for_oracle": "the threshold is exclusive (strictly greater than), not inclusive",
      "recoverability_reason": "SKILL.md Decision Procedure step 2 specifies 'strictly greater than'"
    }
  ],
  "predicted_no_context_answer": "action_alpha",
  "world_knowledge_leak_guard": "which of the 4 guards applies and how",
  "naive_trap": "what real-world default or common-sense reasoning gives predicted_no_context_answer instead of expected_answer"
}
</file>

No additional output.

---

# Hard constraints

1. `rule_chain` must contain ≥ 3 steps with explicit `depends_on` forming a true chain (A→B→C).
2. `hidden_conventions` must contain ≥ 2 entries with non-empty `detail_for_oracle` and `recoverability_reason`.
3. `expected_answer` must be a member of `answer_space`.
4. `predicted_no_context_answer` must differ from `expected_answer` and be in `answer_space`.
5. `answer_space` should have 3–5 members using synthetic snake_case action names.
6. At least one world-knowledge-leak guard must apply (enumerated in `world_knowledge_leak_guard`).
7. The YAML boolean trap: if any answer_space member is `yes`, `no`, `true`, `false`, note it in the design (they need single-quoting in scenario.yaml).
8. All rule thresholds must differ from real-world standards by ≥20% or use synthetic units.

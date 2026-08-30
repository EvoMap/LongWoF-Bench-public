# Role

You are Stage-S1 designer for the TaskGenome Bench **agent_env_synth** solution-first pipeline.
Output English only.

# Input

- Domain: `{domain}`
- Seed idea: `{task_idea}`
- Candidate id: `{candidate_id}`

---

# Goal

Design a coding-agent task skeleton **before** any task.md, package code, or SKILL.md is written.

The skeleton must target three difficulty axes:

| Axis | Requirement for agent_env_synth |
|------|--------------------------------|
| **D3 multi-hop chain** | The final output key Z is computed by calling function C which calls B which calls A in the package. task.md will only describe the input schema and desired output schema; the solver must figure out the call chain. |
| **D1 hidden API conventions** | ≥2 non-obvious API behaviors: undocumented keyword argument defaults, non-standard data type requirements, undocumented return type conventions. task.md omits these; SKILL.md reveals them. |
| **D2 multiple deliverables** | The solution writes ≥2 independent output files to `--output`. Each file is graded independently (pytest asserts each). Missing one → losing that deliverable's score. |

---

# Output format (strict)

Emit exactly one file block:

<file path="_design.json">
{
  "scenario_name": "...",
  "domain": "...",
  "input_contract_summary": "What data/ contains: file names, formats, column/key names",
  "required_packages": ["numpy", "pandas"],
  "io_contract": {
    "invocation": "python generated.py --input <INPUT_DIR> --output <OUTPUT_DIR>",
    "input_files": [
      {"name": "input.csv", "format": "CSV", "description": "..."}
    ],
    "output_files": [
      {"name": "result_a.json", "format": "JSON", "maps_to_deliverable": "deliverable_a", "description": "..."},
      {"name": "result_b.json", "format": "JSON", "maps_to_deliverable": "deliverable_b", "description": "..."}
    ]
  },
  "deliverables": [
    {
      "id": "deliverable_a",
      "observable_contract": "result_a.json must contain key X with value satisfying condition Y",
      "why_independent": "depends only on input rows where column Z satisfies condition W, not on deliverable_b"
    },
    {
      "id": "deliverable_b",
      "observable_contract": "...",
      "why_independent": "..."
    }
  ],
  "derivation_chain": [
    {
      "step_id": "load_and_validate",
      "depends_on": [],
      "description": "Load input.csv, validate schema, return typed DataFrame"
    },
    {
      "step_id": "compute_intermediate",
      "depends_on": ["load_and_validate"],
      "description": "Apply package.module.transform() to produce intermediate structure"
    },
    {
      "step_id": "produce_deliverables",
      "depends_on": ["compute_intermediate"],
      "description": "Call package.module.aggregate() twice (once per deliverable) from intermediate"
    }
  ],
  "package_api": [
    {
      "module": "module_name",
      "function": "function_name",
      "signature": "function_name(data: pd.DataFrame, threshold: float = 0.5) -> dict",
      "hidden_behavior": "threshold is exclusive (> not >=); default 0.5 is rarely correct"
    }
  ],
  "hidden_conventions": [
    {
      "name": "threshold_exclusivity",
      "operation_name_for_task": "threshold comparison",
      "detail_for_oracle": "package uses > (exclusive) threshold; passing >= causes off-by-one",
      "recoverability_reason": "SKILL.md API Reference documents this as 'strictly greater than'"
    },
    {
      "name": "return_type_dict",
      "operation_name_for_task": "result aggregation",
      "detail_for_oracle": "aggregate() returns a plain dict, not a pandas Series; calling .to_dict() on it raises AttributeError",
      "recoverability_reason": "SKILL.md API Reference shows the return type annotation explicitly"
    }
  ],
  "adversarial_case_plan": [
    {
      "targets_convention": "threshold_exclusivity",
      "default_guess_fails_how": "solver passes >= threshold; borderline row included in wrong deliverable",
      "expected_behavior_summary": "borderline row must NOT appear in result_a.json"
    },
    {
      "targets_convention": "return_type_dict",
      "default_guess_fails_how": "solver calls .to_dict() or .values on return value; KeyError or AttributeError",
      "expected_behavior_summary": "result_b.json keys must match dict keys directly"
    }
  ]
}
</file>

No additional output.

---

# Hard constraints

1. `derivation_chain` must have ≥3 steps forming a chain of depth ≥3 (not parallel).
2. `deliverables` must have ≥2 entries, each independently testable.
3. `hidden_conventions` must have ≥2 entries with non-empty `detail_for_oracle` and `recoverability_reason`.
4. `adversarial_case_plan` must cover every hidden convention (one entry per convention).
5. `io_contract.output_files` length must equal `deliverables` length.
6. `required_packages` may only use: stdlib, numpy, scipy, pandas, h5py, scikit-learn, matplotlib, PIL, pyyaml.
7. The package API should use ≥2 entry-point functions with non-obvious signatures (wrong-keyword-argument traps).
8. At least one hidden convention must be a non-default keyword argument that naive solvers will get wrong.

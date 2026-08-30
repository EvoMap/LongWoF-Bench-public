# Research v1 freeze

`research-v1` binds the reportable TaskGenome Bench results to Release ID
`73d08c560817d745c8415927` and manifest SHA-256 `191587d6e35b794601c096e98133577ea497ab9e09936f6818d1b9e30a14264d`.

## Frozen inputs

- Historical source commit: `df31c643d3a8b21bf6b51aa3930fd6c20189d3dc`
- Historical `run_official.py`: `caeba49f0d3707f6e5341642bcab5e7b80acf7cfbec3eb2f5a7eec8d21e816bc`
- Historical `api.py`: `926f7f69a1fa1274b892dddcb5e06257c211f7869f6f87ee1e9819f34205ce3d`
- Skill asset-set digest: `56cdc55579a77eaf315189edc69079bc16db7da3c225e97d2072a2ee52b637e0`
- Opus Gene asset-set digest: `edf8f37cb7db6d3dbdb629f53d1603b3814636144cd2498a27db37ccbe4e9f8a`
- Gemini Gene asset-set digest: `506b6eac3f607319d0eadd8fe94e7be55f53f37a2635a0e98d6747276c536bdd`
- Protocol: `legacy-v1` prompt, scoring, and runtime

The seven report experiments are listed in `results/experiment_registry.json`.
All are single recorded trials per task-condition and therefore carry the
registry's caveats. No result from a different protocol may be pooled into
these tables.

## Frozen subsets

- Full benchmark: 778 tasks
- Opus-evolved: 252 tasks
- Opus reference-distilled complement: 526 tasks (525 reference-distilled
  plus one legacy skill-distilled fallback)
- Common Opus/Gemini evolved overlap: 180 tasks

Exact task IDs and selection rules are in `results/subset_definitions.json`.

## Qwen completion

The original SiliconFlow Qwen archive covered 777 tasks and skipped T0466.
Eight T0466 trials were recorded on 2026-07-18 under `legacy-v1`; all completed
without API errors and all failed the strict verifier. The original archive and
supplement remain separate provenance segments inside the private run, while
the sanitized metrics expose a complete 778-task matrix.

## Known reproducibility limits

1. Hosted APIs are nondeterministic and provider model aliases are not immutable
   weight revisions.
2. Historical pre-v2 runs lack complete original environment and run
   fingerprints. MiniMax M3's original adapter is not in the current runner.
3. The evolved subset is selected on successful author-model exploration.
4. Evolved and reference-distilled subsets are different tasks, not randomized
   same-task treatments.
5. All report cells have one recorded trial; confidence intervals resample
   tasks and do not estimate model rerun variance.
6. Token comparisons exclude Gene distillation cost and inherit provider token
   accounting differences.

## Logic boundary

Stage A is a read-only research layer. It does not modify or invoke evaluation
logic, model adapters, prompt construction, scoring, task verifiers, the
manifest, Skills, or Genes. `tools/research_results.py` reads frozen verdicts
and emits only sanitized derived artifacts.

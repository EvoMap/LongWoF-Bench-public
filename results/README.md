# TaskGenome Bench research-v1 results

This directory contains sanitized derived metrics only. It excludes raw model
responses, prompts, verifier stdout/stderr, private judges, gold outputs, and
reference solutions.

## Rebuild

Public/offline rendering from the checked-in sanitized task metrics:

```bash
python tools/research_results.py render
```

Private authoring rebuild, which additionally verifies the archived JSONL
sources and recreates `task_metrics.csv`. The source inventory is read from
`release/research_sources.v1.json` (falling back to the legacy historical
inventory) when present; a private checkout may pass a different
metadata-only inventory with `--private-sources`. If the inventory is kept
outside the checkout, set `TASKGENOME_PRIVATE_SOURCE_ROOT` (or its
`source_root` field) to the archive root:

```bash
python tools/research_results.py build
```

The public code export intentionally omits the private inventory and raw
archives. Run `build`/`verify` only in the private authoring checkout; the
public tree supports the offline `render` command from checked-in metrics.

Verify that a fresh private rebuild is byte-identical:

```bash
python tools/research_results.py verify
```

## Statistical scope

Pass-rate intervals in `headline_results.csv` are Wilson 95% intervals over
tasks. Paired comparisons use a deterministic paired task bootstrap and exact
McNemar test. They quantify task-sampling uncertainty only. Every reported
model/condition has one recorded trial per task, so hosted-model rerun variance
is not estimated.

`pass_rate` is the exact fraction rounded to six decimals. The display-only
`pass_rate_percent` preserves the historical run-summary convention: first
round the fraction to four decimals, then format it as a one-decimal percent.
The integer `passed` and `n` columns are authoritative.

## Interpretation limits

- `opus_evolved252` is selected on successful Opus exploration and is not a
  representative sample of the full benchmark.
- Evolved and reference-distilled subsets contain different tasks; their
  difference is not a same-task causal estimate of Gene construction method.
- Full-778 Gene results mix evolved and non-evolved (reference- or
  skill-distilled) assets.
- Token totals are input + output + recorded thoughts. Provider accounting is
  not perfectly homogeneous. Gene-generation token cost is excluded from
  single-call evaluation totals; the exploration baseline excludes the later
  Gene distillation call.
- Requested model IDs are not provider-returned immutable weight revisions.
- Historical pre-v2 runs support artifact-level re-aggregation but do not
  preserve a complete original package/environment fingerprint.

# Reproducibility

LongWoF-Bench separates preservation of the published evaluation protocol
from improvements to execution security. Prompt construction and scoring are
covered by offline golden tests so a maintenance change cannot silently alter
the historical experiment.

## Reproducibility levels

There are two distinct claims:

1. **Artifact reproducibility** means the archived responses and per-task
   verdicts can be re-aggregated into the reported tables.
2. **Rerun reproducibility** means a new run uses the same task set, prompts,
   scoring rules, model identifier, parameters, and software environment.

Hosted model APIs can be nondeterministic, and preview model identifiers can
change behind a stable name. A rerun therefore need not reproduce responses
byte for byte. It should reproduce the protocol and report sampling variation,
not be presented as an identical replay.

## Versioned protocols

- `legacy-v1` freezes the prompt construction, scoring, and host-execution
  behavior used by the historical experiments. It exists for comparability and
  must not be used to execute untrusted model output on a machine containing
  credentials or valuable data.
- `hardened-v2` keeps prompt construction and scoring unchanged while applying
  a separately recorded runtime and a fail-closed code-task boundary. Its
  results must be labeled separately from `legacy-v1` results because the
  runtime environment and supported task set differ.

The legacy host subprocess now receives only an explicit credential-free
environment allowlist, with isolated `HOME` and temporary directories. This is
defense in depth for accidental inheritance; it does not change the warning
that `legacy-v1` has no host sandbox and must run only on a disposable,
credential-free machine.

Runtime scratch placement is intentionally not part of either protocol. The
runner uses the operating-system temporary directory by default; set
`GENE_BENCH_V3_TMPDIR` (or `TASKGENOME_TMPDIR` for `hardened-v2`) when a
deployment needs a dedicated scratch volume. The optional OpenAI-compatible
`sub2api` channel likewise has no endpoint default: configure
`SUB2API_BASE_URL` or `--sub2api-base-url` explicitly and record the redacted
value in the run configuration. These settings change only I/O placement or
transport, not prompt bytes, scoring, or result aggregation.

Never merge results from the two protocols into one condition without an
explicit protocol column and a compatibility analysis.

`eval/compare_runs.py` fails closed by default unless both runs preserve the
same prompt and scoring protocol, manifest, score threshold, requested model
mapping, and complete runtime-policy values (including the hardened container
image). Hardened comparisons additionally require matching asset-policy and
I/O-contract digests and, when a real execution recorded it, the same inspected
image identity. For every shared model/task, the persisted user-prompt SHA-256
must match. System-prompt SHA-256 values must match for the same non-Gene
condition; `with_gene*` system prompts may differ because the Gene asset is the
experimental treatment. A missing prompt hash is unverified, not a match.

The explicit comparison overrides only permit reporting a separately reviewed
parity analysis; they do not make mismatched runs equivalent.

The hardened image build and digest workflow is documented in
`docker/README.md`. A missing image, floating image reference, unavailable
Docker daemon, or crossed protocol/backend selection fails before any paid API
call.

The formal `hardened-v2` name is bound to the checked-in asset policy and I/O
contract by their expected SHA-256 values. A caller cannot substitute a custom
boundary and retain the same protocol label. The versioned hardened I/O
contract covers all 47 `subprocess_ref_runner`
tasks but currently admits only T0454, T0456, T0463, T0470, T0477, T0481,
T0485, and T0492. The other 39 and every `pytest_pkg`, `subprocess_cli`, and
`no_ref` code task are rejected before a provider API call. Admitted generation
workspaces contain only public `runtime.*` and `environment.*` files selected
by the release asset policy, with OOXML sanitation applied. Only exact declared
data outputs cross into a fresh trusted judge; extra intermediates are
discarded, candidate Python/control/deserialization-capable files are barred,
stale targets are removed, transfer limits are enforced, and pytest disables
task conftests and automatic plugin loading. Hardened `short_answer` remains
available because its response is data in a fixed evaluator wrapper, not
arbitrary candidate code. These controls are not a universal safety or full
task-coverage claim.

## Frozen legacy invariants

The offline suites in `tests/test_unit_reproducibility.py` and
`tests/test_unit_compare_protocols.py` lock all of the following:

- the exact official manifest digest, 778 unique task IDs, track counts, and
  execution-mode counts;
- seeded selection and trial nesting order;
- one corpus SHA-256 over the exact user and system prompt bytes for all 778
  tasks under all nine supported conditions. The contract records the frozen
  default Agent Skill path separately from the explicitly selected public
  Agent Skill corpus;
- offline request-payload goldens for the OpenAI-compatible, Gemini Vertex,
  local vLLM, and Bedrock adapters. These tests construct the actual request
  parameters with fake clients and never contact a provider;
- subprocess and pytest scoring boundaries;
- resume completion and latest-record aggregation behavior.

If an intentional change breaks one of these tests, do not overwrite the
golden values under the old protocol name. Introduce a new protocol version,
explain the change in `CHANGELOG.md`, and report new results separately.

## Environment

The checked-in `environment.yml` is the pinned Linux environment used for the
current reconstruction and new reruns. It is not evidence that the historical
Claude, Gemini, Qwen, or MiniMax provider runs used that exact operating system,
package set, or binary environment. The original environments were not fully
preserved, so historical parity must be described as partial rather than exact.
New runs should also record:

- Git commit and dirty-worktree state;
- hashes of the manifest, runner, API adapter, task prompts, Skills, and Genes;
- Python, operating system, architecture, and installed package versions;
- model alias, requested/configured model ID, provider, region, and endpoint
  class. The current result `model_id` is the ID sent in the request, not an
  exact or provider-returned resolved model identity;
- maximum tokens, thinking/reasoning settings, retry and timeout policy,
  score threshold, seed, workers, and runtime protocol;
- a credential-free snapshot of the price table used for cost estimates.

The runner defaults to `--repro-hash-mode full`, which fingerprints all
selected scenario files (excluding runtime caches) plus the active Skill/Gene
assets. `core` is a faster partial check and should not be used for an
archival rerun claim.

Secrets, bearer tokens, credential contents, and authenticated proxy URLs must
never be written to run artifacts. Paths stored for provenance should be
repository-relative when possible.

## Running checks

The default test suite is offline and does not use provider credentials:

```bash
python -m pytest
```

Offline tests and dry runs do not validate the Docker engine. A real
`hardened-v2` execution additionally requires a working daemon and a successful
runtime smoke test with the selected digest-pinned image.

It intentionally excludes the credentialed cloud diagnostic scripts. A
one-task dry run validates selection, immutable configuration/fingerprinting,
and artifact paths without building prompts, executing candidate code, or
making an API call:

```bash
python -m eval.run_official \
  --protocol legacy-v1 \
  --ids T0499 \
  --models gemini_flash \
  --conditions no_context \
  --dry-run
```

The full reference self-test is a separate release gate because it executes
task-specific code. There are two distinct documented exception sets:

- 15 `subprocess_cli_no_ref` tasks have no canonical reference solution by
  design;
- 15 curated v2.5 tasks retain the historical compatibility classifications
  in `tasks_final/legacy_v25_unrunnable.json`.

Compatibility-mode reports must name both sets instead of treating either as a
validated reference run. Any other skip, failure, or error must be
investigated before release.

## Rendered evidence

`tools/research_results.py render` regenerates the public tables, aggregate
CSVs, statistical tests, and figures from the checked-in sanitized metrics.
Every text artifact and both SVG figures are byte-reproducible on any platform,
and the offline suite compares them byte for byte.

The three PNG figures are not. Matplotlib rasterizes glyphs through the host
font stack, so an identical render produces different bytes on a different
operating system even with the pinned dependency versions. The checked-in PNGs
are the ones rendered on the CI platform (Ubuntu, Python 3.11); elsewhere the
suite compares figure dimensions and embedded metadata instead. Two of the
three also ship as SVG, which keeps the byte-level guarantee for those plots.

## Historical limitations

The first published run artifacts predate the v2 provenance schema. Their
configs contain most command-line settings and their raw outputs support
artifact-level auditing, but they do not contain a complete immutable snapshot
of the original Git state and package environment. In particular, the archived
MiniMax M3 run references a `minimax_m3` provider adapter that is not present in
the current runner. That result can be re-aggregated from the archived artifact,
but a faithful API rerun requires recovery and versioning of the original
adapter; it must not be reconstructed by guessing.

Treat archived run directories as immutable. Generate summaries into a copy or
verify that summary-only operations do not alter the original config and
provenance files.

Pre-v2 configs do not contain an immutable `run_fingerprint` and are refused
for resume by default. Prefer a new `run_id`. The explicit
`--unsafe-resume-legacy-config` escape hatch is only for a deliberately
accepted migration after the original run has been copied or archived; it does
not retroactively make that artifact fully reproducible.

Summary generation scans every append-only `results.jsonl` record, including
superseded retries, before applying the historical latest-record aggregation
rule. Conflicting persisted protocols, fingerprints, or manifest digests fail
closed. If a pre-v2 artifact has no stored protocol metadata, the summary
labels `legacy-v1` as a historical inference and marks unavailable provenance;
it never substitutes the current command-line defaults.

For new runs, `config.json` is the immutable contract.
`run_events.jsonl` records creation/resume events and credential source names
(`cli`, `env:NAME`, or `unset`) without recording credential values.

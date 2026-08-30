# LongWoF-Bench release asset bundles

LongWoF-Bench keeps one immutable canonical pool for historical
reproduction, but it must not be published as a single archive. The canonical
tree contains task prompts and runtime inputs alongside private evaluators,
gold answers, reference solutions, authoring traces, and generated artifacts.

The versioned policy in `release/asset_policy.v1.json` classifies every
discovered file as `public`, `private`, or `excluded`. Classification is
fail-closed: an unclassified file, conflicting rule, duplicate destination,
unsafe public path/content, changed manifest hash, changed task order, or
incomplete context collection makes the audit fail.

## Verified scope

Policy v1 is pinned to the current ordered set of 778 official tasks. Python
bytecode and `__pycache__`/`.pytest_cache` trees are ignored before inventory,
so a clean checkout and a locally exercised checkout produce the same report.
The current repository audit covers the following distribution assets:

- 4,590 public bundle records;
- 13,255 private bundle records;
- 292 excluded non-official files;
- 18,137 bundle records in total, because 1,556 Gene sources and 10 workbook
  sources each produce a sanitized public copy and a byte-exact private
  original;
- zero unclassified files, rule conflicts, or public-safety violations;
- zero byte-identical overlaps between public content and sensitive private
  judge, reference, oracle, legacy, or privacy content;
- 778/778 coverage for the final legacy Skill, Gemini Gene, and Opus Gene
  contexts. Alternate Agent Skill and rewritten Skill variants remain private
  provenance assets and are not part of the public bundle.

The checked-in passing report has release ID `4be80d65f0fd195aa11b168e`.

Run the audit again after any asset change; these counts are evidence for the
current policy and are not values that the tool silently assumes.

## Commands

All commands are offline and read the canonical manifest without modifying it.

```bash
# 1. Audit and optionally save the quality report.
python -B tools/release_assets.py audit \
  --report release/quality_report.v1.json

# 2a. Build only the publishable public bundle. No private/ directory is created.
python -B tools/release_assets.py build-public \
  --out /tmp/taskgenome-public-release

# 2b. In a separate access-controlled location, build only the private bundle.
python -B tools/release_assets.py build-private \
  --out /secure/taskgenome-private-release

# Maintainer compatibility command: build both into one local directory.
python -B tools/release_assets.py build \
  --out /tmp/taskgenome-release

# 3. Verify both bundles, their checksums, and their cross-bundle separation.
python -B tools/release_assets.py verify \
  --public /tmp/taskgenome-release/public \
  --private /tmp/taskgenome-release/private

# 4. Reconstruct the legacy canonical layout for trusted reproduction only.
python -B tools/release_assets.py materialize-legacy \
  --public /tmp/taskgenome-release/public \
  --private /tmp/taskgenome-release/private \
  --out /tmp/taskgenome-legacy-pool

# 5. Explain every policy decision for one task, optionally narrowing by path.
python -B tools/release_assets.py explain \
  --task-id T0487 --path test_script.py
```

The build output has this shape:

```text
taskgenome-release/
├── public/
│   ├── tasks/
│   ├── contexts/
│   ├── runtime/
│   ├── environments/
│   ├── metadata/
│   ├── release.json
│   └── SHA256SUMS
├── private/
│   ├── canonical/
│   ├── scenarios/
│   ├── provenance/
│   ├── release.json
│   └── SHA256SUMS
└── quality/
    ├── quality_report.json
    └── SHA256SUMS
```

## What belongs in each bundle

The public bundle contains task prompts, distribution-safe metadata and
contexts, runtime libraries, environment definitions, and inputs that a
reference-runner task needs before judging. It excludes evaluators, gold
answers, reference solutions, oracle Skills, private fixtures, traces, logs,
pre-generated outputs, and host-local credential/path material.

Five workbooks duplicated under `data/` and `environment/` contained an Office
`x15ac:absPath` last-save location with a host username. The release policy
applies `strip_ooxml_abs_path` only to those 10 public copies. It removes that
optional workbook metadata block without changing worksheets, cells, formulas,
or the canonical source. The corresponding private records retain the original
bytes and are the only copies marked for legacy materialization.

The two final Gene collections are also copied through the deterministic
`sanitize_gene_provenance` transform. It removes the nested `evolve` object
(rollout calls, mutation logs, token accounting, and private trace pointers)
while preserving the Gene payload and stable identity fields. The private
bundle retains the byte-exact originals. Alternate Agent Skill and rewritten
Skill collections are classified as private provenance assets rather than
public contexts, so the public runner has one unambiguous final Skill input.

The private bundle contains the canonical manifest, evaluators, private
fixtures and gold data, references, oracle/authoring material, provenance, and
legacy artifacts needed to reconstruct trusted historical runs. Do not publish
this bundle or expose it to untrusted model code.

The bundle boundary does not sanitize Git history. The authoring repository may
retain private blobs in old commits, so it must not be published with its full
`.git` object database. Distribute the generated public bundle, or import that
bundle into a fresh public history. Historical run bytes are recovered only
from a separately controlled private archive as described in
[`docs/HISTORICAL_RUNS.md`](HISTORICAL_RUNS.md).

The public bundle is a distribution boundary, not by itself an execution
sandbox. Use `hardened-v2` for new untrusted execution. The reconstructed
legacy tree is only for `legacy-v1` reproduction on a trusted machine;
`legacy-v1` intentionally retains historical host execution and is not safe
for adversarial candidate code.

## Determinism and verification

Each ordinary copied file retains its content and executable/non-executable
mode. The 10 declared workbook transformations are deterministic and their
post-transform sizes and hashes are recorded. `SHA256SUMS` lists every bundle
file except itself in sorted path order. Each `release.json` also stores:

- the canonical manifest SHA-256;
- SHA-256 of the ordered task-ID sequence;
- the policy SHA-256 plus both public/private bundle roots;
- every source path, bundle path, role, size, mode, and file SHA-256;
- an asset Merkle-style root computed as SHA-256 over sorted canonical JSON
  records for every asset (including content hash, size, mode, role,
  materialization flag, source/bundle paths, task, family, and execution mode);
- a content-bound release ID derived from the canonical manifest SHA-256,
  ordered task-ID SHA-256, policy SHA-256, public root, and private root.

`verify` recalculates these values, rejects missing or extra files, and scans
the public bundle again. A source path may occur in both bundles only for the
declared sanitized Gene/workbook public-original/private role pairs; all other
source overlap is rejected, and byte-identical sensitive overlap is always
rejected.
`materialize-legacy` first requires both bundles to pass verification and then
confirms that the reconstructed manifest matches the canonical digest.

These internal hashes detect accidental or malicious content drift after a
trusted manifest is obtained; they are integrity metadata, not publisher
authentication. Stage C wraps the verified public bundle in a deterministic,
versioned archive, publishes a SHA-256 sidecar, and requires a Sigstore bundle.
The frozen archive record is `release/public_data_artifact.v1.json`; see
`docs/STAGE_C_RELEASE.md` for the clean-history and long-term archive runbook.
After extracting the archive, `tools/public_data_smoke.py` derives a temporary
sanitized manifest from the public `release.json` and exercises the runner's
credential-free dry-run path. This keeps the public distribution layout
separate from the private canonical `tasks_final`/judge layout.

Public-content auditing streams ordinary text and the textual members of ZIP,
XLSX, DOCX, PPTX, wheel, and JAR archives. Policy v1 permits at most 512 MiB per
text member; the implementation also fails closed above 100,000 archive entries
or 2 GiB total uncompressed content.

## Compatibility limitation

T0477's public HVAC simulator deterministically derives the same process
constants that its private verification-parameter files record. The JSON copies
are private, but the values are inferable from code the historical task requires
the candidate to execute. Removing or redesigning that simulator would change
the legacy task logic, so this is documented as a legacy task-design limitation
rather than silently changing the experiment.

The schemas under `schemas/*release*.json` describe the policy, public and
private release manifests, and quality report. A classification or layout
change should create a new policy/schema version; never silently reinterpret a
published release ID.

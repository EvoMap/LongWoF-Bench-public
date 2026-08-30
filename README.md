<div align="center">

# LongWoF-Bench

### Evaluating EvoMap Genes for Verifiable Long-Workflow Tasks

<p>
  <a href="https://arxiv.org/abs/2608.23200"><img src="https://img.shields.io/badge/arXiv-2608.23200-b31b1b.svg" alt="arXiv"></a>
  <a href="https://huggingface.co/datasets/EvoMapAI/LongWoF-Bench"><img src="https://img.shields.io/badge/Hugging%20Face-Dataset-yellow.svg" alt="Hugging Face dataset"></a>
  <a href="https://github.com/EvoMap/LongWoF-Bench-public/actions/workflows/ci.yml"><img src="https://github.com/EvoMap/LongWoF-Bench-public/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="release/public_data_artifact.v1.json"><img src="https://img.shields.io/badge/public%20release-v1.0.2-0f766e.svg" alt="public release v1.0.2"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/code-Apache--2.0-3da639.svg" alt="Apache License 2.0"></a>
</p>

<p>
  <a href="#overview">Overview</a> ·
  <a href="#benchmark-at-a-glance">Benchmark</a> ·
  <a href="#paper-results">Results</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="README.zh-CN.md">简体中文</a>
</p>

</div>

<p align="center">
  <img src="docs/assets/paper/overview.png" alt="LongWoF-Bench overview: four task families, a unified task abstraction, and strict end-to-end verification" width="100%">
</p>

<p align="center"><em>Figure 1 from the paper: 778 machine-verifiable tasks unified by a common task abstraction and objective end-to-end verification.</em></p>

## Overview

LongWoF-Bench is a benchmark for evaluating whether language models can turn
long, constraint-sensitive workflows into deliverables that survive strict
machine verification. It contains **778 tasks** across four complementary
workflow families:

- **Code generation** — executable programs, file contracts, API/CLI
  compliance, and hidden functional tests.
- **Agent-environment synthesis** — package interfaces, multi-artifact
  delivery, environment behavior, and pytest-style checks.
- **Mathematical reasoning** — exact computation, formula selection, boundary
  handling, and normalized short answers.
- **Rule following** — rule applicability, precedence and overrides, legal
  output spaces, and exact decision matching.

The benchmark measures the final artifact, not whether an intermediate answer
looks plausible. A task succeeds only when every mandatory check passes.

## The benchmark contract

Each task is defined by the paper's four-part abstraction
`T = (S, E, Y, V)`:

| Symbol | Meaning |
|---|---|
| `S` | Public task specification |
| `E` | Model-accessible environment and assets |
| `Y` | Space of admissible deliverables |
| `V` | Task-specific private machine verifier |

At evaluation time, the task, runtime, decoding configuration, and verifier
stay fixed. Only the auxiliary context changes:

```text
public specification + environment
            │
            ├── No Context
            ├── Skill       (fuller procedural guidance)
            └── EvoMap Gene (compact, structured experience)
            │
            ▼
model → deliverable → private verifier → strict pass / fail
```

The verifier does not add undisclosed task requirements: all information
needed to solve a task is recoverable from its public specification,
accessible environment, interfaces, and output contract. Hidden tests,
reference solutions, gold outputs, and verifier logic are used only to decide
whether the submitted deliverable satisfies those public requirements.

## Benchmark at a glance

| Track | Tasks | Typical deliverable | Verification signal |
|---|---:|---|---|
| `code_generation` | **341** | Runnable program, CLI, files, or schemas | Isolated execution and hidden tests |
| `agent_env_synth` | **127** | Package interface and multi-file artifacts | Environment-backed pytest-style checks |
| `math_reasoning` | **151** | Exact short answer | Parsing, normalization, and exact match |
| `rule_following` | **159** | Legal discrete decision | Rule priority, answer-space validation, and exact match |
| **Total** | **778** |  |  |

The released research evidence uses four explicitly named subsets:

| Subset | Tasks | Role in the paper |
|---|---:|---|
| Full benchmark | **778** | Overall benchmark difficulty |
| Opus-evolved | **252** | Primary Gene-versus-Skill comparison |
| Reference-distilled | **526** | Gene provenance analysis |
| Opus–Gemini common evolved | **180** | Same-task producer comparison |

The 252-task subset is selected because Claude Opus found a verifier-confirmed
trajectory within the evolution budget. It is the right subset for studying
reuse of successful experience, but it is **not** a representative sample of
all 778 tasks.

## Paper results

The main comparison is on the 252 Opus-evolved tasks. Averaged across seven
consumer models, strict pass rate rises from **41.0% (No Context)** to
**51.2% (Skill)** and **62.9% (EvoMap Gene)**. Gene beats Skill for every
evaluated model by **8.7–15.5 percentage points**; for Claude Opus 4.8, the
increase is **63.9% → 79.4%**.

For the representative full benchmark, the strongest no-context result is only
**20.2% (157/778)**. The full-set Opus Gene column mixes 252 evolved, 525
reference-distilled, and one skill-distilled Gene, so it is descriptive of the
released asset collection rather than a clean ablation of Gene construction.

<p align="center">
  <img src="docs/assets/paper/evolved_gene_completion.svg" alt="Paper Figure 3: strict task pass rate on the 252 Opus-evolved tasks" width="49%">
  <img src="docs/assets/paper/discovery_reuse_cost.svg" alt="Paper Figure 5: discovery and one-shot Gene reuse token cost" width="49%">
</p>
<p align="center"><sub>Figure 3 (left) and Figure 5 (right), reproduced from the paper.</sub></p>

On the same 252 tasks, one-shot Opus + Gene reuse passes **200** tasks with
**723,480** solve-time tokens, versus **161** tasks and **803,099** tokens for
Skill. That is **39 additional passed tasks** and a **9.9% lower** solve-time
token total than Skill. Compared with the multi-round exploration that
produced the verified trajectories, one-shot reuse reduces calls from 404 to
252 and tokens by **45.8%**; the one-time Gene distillation cost is excluded.

<details>
<summary>Additional paper analyses (Figures 4 and 6)</summary>

<p align="center">
  <img src="docs/assets/paper/gene_author_comparison.svg" alt="Paper Figure 4: Opus versus Gemini Gene producer comparison on 180 common evolved tasks" width="49%">
  <img src="docs/assets/paper/workflow_type_gains.svg" alt="Paper Figure 6: Skill and Gene gains by workflow family" width="49%">
</p>
<p align="center"><sub>Figure 4 (left) compares Gene producers on the 180-task common evolved set; Figure 6 (right) breaks down gains by workflow family.</sub></p>

On the complementary 526-task reference-distilled subset, Gene trails Skill for
all seven models by **3.0–11.2 points**. This provenance-dependent reversal is
why the full 778-task results and the evolved 252-task results must be reported
separately.

</details>

The checked-in, sanitized evidence package contains the exact per-task metrics,
tables, confidence intervals, paired tests, and token accounting used for the
paper. See [`results/README.md`](results/README.md) for scope and caveats, or
jump directly to the [full-778 table](results/tables/full778.md),
[evolved-252 table](results/tables/evolved252.md),
[paired tests](results/tables/statistical_tests.md), and
[token accounting](results/tables/token_efficiency.md).

## How Genes are constructed and reused

LongWoF-Bench treats successful execution as reusable experience:

1. A producer model attempts a task without private verifier information.
2. Failed attempts receive sanitized verifier feedback and are refined within a
   fixed rollout budget.
3. After a verifier-confirmed trajectory is found, its execution-critical
   strategies, corrections, preconditions, boundary conditions, and failure
   guards are distilled into a structured **GDIv2 Gene**.
4. A consumer model receives the public task plus the Gene, without seeing the
   producer trajectory or verifier feedback.

This separates the cost of discovering a successful strategy from the cost of
reusing it, and allows verified experience to transfer across model families.

[`eval/evolve_genes_v3.py`](eval/evolve_genes_v3.py) implements the loop above.
The Skill baseline it is compared against is authored by
[`eval/generate_agent_skills_v3.py`](eval/generate_agent_skills_v3.py) and
sanitized by [`eval/rewrite_skills_v3.py`](eval/rewrite_skills_v3.py), so both
arms of the comparison are reproducible from this repository.

## Public release boundary

This repository is the **v1.0.2 public code and research-evidence release**.
The official task prompts, runtime inputs, and final tested Skill/Gene contexts
are distributed separately as the versioned public data archive described by
[`release/public_data_artifact.v1.json`](release/public_data_artifact.v1.json).
The repository ships the code that authors tasks, Skills, and Genes, but none
of the material that code produces: it deliberately does not contain private
verifiers, hidden tests, gold outputs, reference solutions, raw traces, or the
authoring task tree. Running [`synth/`](synth/) regenerates that material
locally; it is never published.

The release boundary is audited and pinned to release ID
`ad87fa3c374e7098d712d7a6`. After excluding the restricted Skill directories,
the public data archive contains 4,412 audited records; its code/data split,
checksums, and Sigstore metadata are recorded in
[`release/asset_policy.v1.json`](release/asset_policy.v1.json),
[`release/stage_c_release.v1.json`](release/stage_c_release.v1.json), and the
artifact record above.

Twelve SkillsBench-derived tasks (`T0464`, `T0465`, `T0466`, `T0467`, `T0469`,
`T0471`, `T0473`, `T0482`, `T0483`, `T0484`, `T0485`, and `T0486`) contain nested
Skill packages that are excluded from the public archive until the relevant
rightsholder grants permission. The public repository keeps only stable task
and source identifiers; it does not provide a downloader, mirror, archive URL,
credential, or automatic installer for these packages. See
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) and the
[`restricted Skill guide`](docs/RESTRICTED_SKILLS.md) for the task-level
boundary and the read-only local presence check.

Task `T0498` additionally bundled Amazon, Meta, and Google product-category
taxonomies for which no redistribution licence could be established. They are
excluded from the archive from v1.0.2 onward by the same mechanism; the task's
own data README is retained so a user who obtains the taxonomies from their
rights holders knows where they belong. See
[`release/restricted_assets.v1.json`](release/restricted_assets.v1.json).

## Quick start

The code repository alone is intentionally not a complete task pool. Download
the public data archive from the `v1.0.2` GitHub Release, verify it, and unpack
it into a directory that provides the canonical `tasks_final/` layout:

```bash
BASE=https://github.com/EvoMap/LongWoF-Bench-public/releases/download/v1.0.2
ARCHIVE=taskgenome-bench-public-data-v1.0.2.tar.gz

curl --fail --location --remote-name "$BASE/$ARCHIVE"
curl --fail --location --remote-name "$BASE/$ARCHIVE.sha256"
sha256sum --check "$ARCHIVE.sha256"
tar -xzf "$ARCHIVE"          # unpacks taskgenome-bench-public-data-v1.0.2/
```

The archive is also signed. The signature identity is this repository's release
workflow, not an individual, so verification pins both the identity and the
issuer:

```bash
curl --fail --location --remote-name "$BASE/$ARCHIVE.sigstore.json"
cosign verify-blob --bundle "$ARCHIVE.sigstore.json" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp \
  '^https://github\.com/EvoMap/LongWoF-Bench-public/\.github/workflows/sign-release\.yml@' \
  "$ARCHIVE"
```

Then point the runner at the unpacked directory:

```bash
git clone https://github.com/EvoMap/LongWoF-Bench-public.git
cd LongWoF-Bench-public

# First, verify that the public runner is present and inspect its options.
python -m eval.run_official --help

# After unpacking the public data archive into /path/to/longwof-public:
python -m eval.run_official \
  --manifest /path/to/longwof-public/tasks_final/manifest.json \
  --pool-root /path/to/longwof-public/tasks_final \
  --protocol legacy-v1 \
  --ids T0499 \
  --models gemini_flash \
  --conditions no_context,with_skill,with_gene_opus \
  --gene-opus-dir /path/to/longwof-public/tasks_final/genes_opus48 \
  --dry-run \
  --run-id readme-quickstart
```

The dry-run makes no provider call and executes no candidate code. It should
report 778 loaded tasks and three pending trials for `T0499`. Removing
`--dry-run` changes the security and credential requirements; read
[`SECURITY.md`](SECURITY.md) first. `legacy-v1` preserves historical host
execution and is suitable only for a trusted, expendable machine. The
digest-pinned `hardened-v2` path is intended for new untrusted candidate
execution, but supports only the reviewed task subset documented in the
security policy.

## Reproduce the released evidence

Render the public Markdown/LaTeX tables, aggregate CSVs, statistical tests, and
offline result figures from the checked-in sanitized metrics:

```bash
python -B tools/research_results.py render
```

The command does not call a model, execute candidate code, or access private
judges. The frozen protocol and subset rules are documented in
[`docs/RESEARCH_V1.md`](docs/RESEARCH_V1.md) and
[`docs/REPRODUCIBILITY.md`](docs/REPRODUCIBILITY.md).

## Repository map

| Path | Contents |
|---|---|
| [`eval/run_official.py`](eval/run_official.py) | Official evaluation runner |
| [`eval/evolve_genes_v3.py`](eval/evolve_genes_v3.py) | GDIv2 Gene evolver: rollout, verifier feedback, distillation |
| [`eval/generate_agent_skills_v3.py`](eval/generate_agent_skills_v3.py) | Agent Skill author for the Skill baseline |
| [`eval/rewrite_skills_v3.py`](eval/rewrite_skills_v3.py) | Leakage-audited Skill rewriter |
| [`synth/`](synth/) | Task-synthesis pipeline: seeds, authoring prompts, calibration, consolidation |
| [`results/`](results/) | Sanitized metrics, tables, tests, and generated figures |
| [`release/public_data_artifact.v1.json`](release/public_data_artifact.v1.json) | Public data archive metadata and release ID |
| [`release/asset_policy.v1.json`](release/asset_policy.v1.json) | Public/private asset classification policy |
| [`release/stage_c_release.v1.json`](release/stage_c_release.v1.json) | Stage C publication record |
| [`docs/assets/paper/`](docs/assets/paper/) | Figures copied from arXiv v2 for README alignment |
| [`examples/dev_task/`](examples/dev_task/) | Public development-task example with a public judge |
| [`SECURITY.md`](SECURITY.md) | Execution and release-security guidance |
| [`LICENSE`](LICENSE) | Apache License 2.0 for public code |
| [`DATA_LICENSE.md`](DATA_LICENSE.md) | CC BY 4.0 terms for the separate public data archive |

After the public data archive is unpacked, its `tasks_final/` directory supplies
the task specifications and final tested guidance used by the runner. The
private evaluation bundle is never part of the public release.

## Citation

If you use LongWoF-Bench, please cite the paper and the repository:

```bibtex
@misc{zhang2026longwofbenchevaluatingevomapgenes,
  title         = {LongWoF-Bench: Evaluating EvoMap Genes for Verifiable Long-Workflow Tasks},
  author        = {Zhang, Xiao and Sun, Qumeng and Li, Jiahao and Ren, Yiming
                   and Liu, Xiang and Zhang, Haoyang and Wang, Junjie},
  year          = {2026},
  eprint        = {2608.23200},
  archivePrefix = {arXiv},
  primaryClass  = {cs.CL},
  url           = {https://arxiv.org/abs/2608.23200}
}
```

The machine-readable citation is available in [`CITATION.cff`](CITATION.cff).

## License

The public code in this repository is licensed under the
[Apache License 2.0](LICENSE). The separately distributed public data archive
is licensed under [CC BY 4.0](DATA_LICENSE.md); that data license applies only
to assets explicitly included in the archive. Private evaluation assets are
not distributed. Third-party notices remain controlling for the identified
files.

<div align="center">
  <sub>LongWoF-Bench · Infinite Evolution Lab, EvoMap · Tsinghua University</sub>
</div>

# Changelog

All notable project changes are documented here. Benchmark-semantic changes
must use a new protocol or asset version rather than silently changing a
published condition.

## Unreleased

### Added

- The task-synthesis pipeline under `synth/`: the solution-first S1-S6 authoring
  driver, difficulty calibration, pool consolidation and layout, sanitized-Skill
  promotion, and the reference-against-oracle self test, together with the seed
  ideas, authoring prompts, and family-shared oracle templates in
  `synth/shapes/`.
- Gene and Skill generation on the evaluation side: `eval/evolve_genes_v3.py`,
  `eval/gen_genes_llm_v3.py`, `eval/generate_agent_skills_v3.py`,
  `eval/rewrite_skills_v3.py`, `eval/compare_gene_rollout_tokens.py`, and
  `tools/merge_run_supplement.py`.
- Versioned `legacy-v1` and `hardened-v2` runtime policies so historical
  comparability and secure execution can be reported separately.
- Secret-safe run metadata and explicit result/config schema versions.
- Offline unit and integrity tests that freeze the official manifest, prompt
  hashes, trial ordering, and legacy scoring behavior.
- A credential-free GitHub Actions workflow.
- Reproducibility, contribution, security, and citation documentation.
- A fail-closed public/private asset release policy with audit, build, verify,
  explain, and legacy-layout reconstruction commands.
- JSON Schemas for the official manifest, Gene assets, run/result contracts,
  release policy, bundles, quality report, Stage C policy, and public data archive.
- Stage C clean-code export, public-only/private-only bundle commands,
  deterministic data packaging, and a non-scored public dev example.
- A digest-pinned hardened runner image recipe and a bundled copy of the
  historical v2.5 self-test exception map.

### Fixed

- The bilingual README reported the reference-distilled 526-task Skill-over-Gene
  gap as 3.3-11.3 points. Recomputed from the counts in
  `results/tables/reference_distilled526.md`, the range is 3.0-11.2 points.
- `docs/REPRODUCIBILITY.md` pointed at `tests/test_unit_legacy_protocol.py`,
  which the public release trim removed; it now names the suites that shipped.
- The clean-history denylist spelled out the maintainer email address and
  personal dataset namespace it was meant to keep out of the release, which
  published both. Both are now generic patterns that still fail the same way.
- `run_official.py` printed a bare traceback when `--manifest` named a missing,
  unreadable, or malformed file. The CLI entry point now reports the failure and
  exits the way the rest of the module already does. `_load_manifest` keeps its
  typed exceptions, which the Gene and Skill generators call directly.
- `api.py` imported `httpx` behind a `HAS_HTTPX` flag that nothing read.
- The render determinism test compared the three PNG figures byte for byte, so
  it failed anywhere but the CI platform: Matplotlib rasterizes glyphs through
  the host font stack. Text artifacts and both SVG figures are still compared
  byte for byte; the PNGs are now compared by figure dimensions and embedded
  metadata, and `docs/REPRODUCIBILITY.md` records what each guarantee covers.
- The Simplified Chinese figure caption described the task set with a weak
  verb; it now names both mechanisms directly.
- A `sign-release` workflow that signs the published data archive with a
  Sigstore keyless signature carrying this repository's workflow identity
  rather than a maintainer's account. It checks the archive digest before
  signing and verifies the resulting bundle before attaching it. Signing from
  a workstation would publish the operator's email address to the public Rekor
  transparency log permanently, which the rest of this release takes care to
  avoid.
- `docs/STAGE_C_RELEASE.md` still quoted the pre-quarantine archive size and
  SHA-256 in its signing section, so a release manager following it would have
  signed against the wrong digest. It now quotes the shipped archive.
- The public data archive ships through the `v1.0.1` GitHub Release only.
  Zenodo was declared as the primary archive but never minted, so the policy no
  longer names it, the artifact record omits the channel instead of carrying
  null DOIs, and both schemas make a channel block optional. Publication status
  gained `uploaded_pending_public_visibility` for the state this release is
  actually in: the assets exist, the repository is not public yet, and
  `anonymous_download` stays false until it is.
- One content-scan exemption covered a test that no longer carries anything the
  scanner rejects, and both exemption digests had drifted. The unnecessary one
  is gone and the remaining one is re-pinned, so every exemption is both needed
  and accurate.
- The bilingual Quick start now gives the download, checksum, and signature
  verification commands. It previously told readers to verify the archive
  without saying where to get it.
- Every pinned repository address pointed at the authoring remote. The release
  is published from its own repository, so the CI badge, clone command,
  `CITATION.cff`, the data-artifact publication URLs, and the Stage C project
  block now name that repository instead.
- `hardened-v2` could not start. `release/asset_policy.v1.json` gained the
  restricted-Skill exclusions, but the digest the runner pins for that file
  could not be updated at the time because `run_official.py` was frozen, so
  every hardened run failed closed with a boundary digest mismatch regardless of
  input. The pinned digest now matches the shipped policy and `hardened-v2`
  starts again. No published number is affected: all 21,784 recorded trials ran
  under `legacy-v1`, and the mismatch made `hardened-v2` unusable rather than
  wrong.

### Changed

- `results/research_v1.json` records new digests for `eval/run_official.py` and
  `eval/api.py`. Both changes above are confined to code that cannot execute
  during a scored trial: the removed import was unreachable, and the manifest
  error path runs only when no run can start. The published numbers, subsets,
  and statistical tests are unchanged, and the release id is unchanged.
- The public code export now scans for host-local paths, which previously only
  the data asset scanner did. `tools/release_assets.py` and `eval/run_official.py`
  each carry one legitimately and are exempted against their current digest;
  `run_official.py` is additionally pinned by `results/research_v1.json`, so it
  cannot be edited without invalidating the published results.
- The Stage C code whitelist no longer bans the `synth` path component outright.
  The pipeline code ships from that tree, so the ban is replaced by narrower
  guards on the directories that hold per-task answers: `_sample_pilot`,
  `candidates`, `candidates_sf`, `_calibration`, `_bad_solutions`,
  `_quarantine`, and `_model_runs`. Every synthesis file is whitelisted
  individually rather than by glob.
- The historical batch wrapper now resolves the repository from its own
  location instead of a user-specific absolute path.
- The Conda environment no longer forces a user-specific installation prefix;
  package pins are unchanged.
- New run artifacts are secret-redacted, permission-restricted, protocol
  tagged, source/asset hashed, and protected by an immutable resume fingerprint.
- Run comparison refuses mixed runtime protocols unless explicitly overridden
  after a parity analysis.

### Compatibility

- `legacy-v1` retains historical host execution, prompt construction, and
  scoring for published-result comparison.
- `hardened-v2` results must be labeled and analyzed separately.

## 1.0.2 - 2026-08-30

A data-rights release. The code is unchanged; the archive is re-cut because four
bundled e-commerce category taxonomies in `T0498` had no redistribution grant.
Versions 1.0.0 and 1.0.1 contain those files and must not be redistributed.

### Removed

- The Amazon, Meta, and Google product-category taxonomies bundled with `T0498`
  (`amazon_product_categories.csv`, `amazon_product_categories_full.csv`,
  `fb_product_categories.csv`, `google_shopping_product_categories.csv`, in both
  the environment and runtime trees; 7,213,180 bytes across eight files). No
  redistribution licence could be established for any of them: the Amazon list
  derives from the Amazon Browse Node structure via a third-party site, Meta's
  taxonomy is governed by its platform terms, and Google's is a freely published
  standard carrying no explicit licence. They are excluded by the new
  `exclude_thirdparty_ecommerce_taxonomies` rule in
  `release/asset_policy.v1.json`, which mirrors the rule already used for the
  restricted Anthropic Skill packages. The task's own data README is retained so
  that a user who obtains the taxonomies from their rights holders knows where
  they belong.

### Added

- `release/restricted_assets.v1.json`, recording the withheld taxonomies, their
  origin, rights holder, and expected paths, as metadata only. It is not a
  distribution manifest.
- `release/third_party_sources.v1.json`, the upstream provenance record, is now
  shipped in the code repository rather than only alongside the data.
- Traced provenance for the three nested data sources that are cleared for
  redistribution, with the citations they require, in
  `THIRD_PARTY_NOTICES.md`: `T0454` PB2002 plate boundaries (CC BY 4.0, Bird
  2003) and its USGS earthquake catalog (U.S. Government work), `T0485` NASA
  budget material (U.S. Government work), and `T0493`'s instance from an
  MIT-licensed exam-scheduling generator.
- Carve-outs in `DATA_LICENSE.md` making explicit that the CC BY 4.0 grant
  covers LongWoF-Bench-original material only.

### Changed

- The public data archive is `taskgenome-bench-public-data-v1.0.2.tar.gz`
  (321,198,041 bytes, SHA-256 `e950c23bba0371fc70d0ef99a68877d1a0c58bbb9425cdd355a4092e920b4593`),
  carrying 4,412 assets over
  566,818,316 unpacked bytes with asset Merkle root
  `846b1d97645892aef9ad2bb7ee885d413981f36390892e3f1ea9dcfdbdb6a8e5`.
- The artifact record reports `pending_publication`, which is a value
  `verify-package` accepts; the 1.0.1 record used a status string that the
  checker rejected.
- The release audit now passes with no errors. A stray untracked scratch file
  under `tasks_final/genes_evolved/` had been leaving the inventory walk with an
  `unscanned_asset_path` error, so 1.0.1 was cut from a failing audit.

## 1.0.1 - 2026-08-27 (release candidate)

The reproducible v1.0.1 release. The archive, its checksum sidecar, and its
Sigstore bundle are attached to the `v1.0.1` GitHub Release, which is the single
distribution channel for this version. Anonymous download becomes possible when
the repository itself is made public; until then the artifact record reports
`uploaded_pending_public_visibility` rather than claiming otherwise.

### Added

- A deterministic public data archive record for 4,420 assets, including the
  frozen 321,889,608-byte archive SHA-256 and sidecar name. The count and
  digest are those of the archive re-cut after the restricted Anthropic Skill
  directories were quarantined.
- Versioned GitHub Release URLs for the archive, checksum sidecar, and Sigstore
  bundle. A channel the policy does not declare is now omitted from the artifact
  record entirely, so an absent key cannot be misread as a pending one.
- An optional artifact-record check in `tools/stage_c_release.py verify-package`
  that compares archive bytes, digest, and embedded `ARTIFACT.json` metadata.
- A public-bundle smoke adapter (`tools/public_data_smoke.py`) that derives a
  temporary sanitized manifest and confirms the credential-free runner reports
  all 778 tasks after extraction.

### Changed

- English and Simplified Chinese release READMEs now point to the same immutable
  v1.0.1 channel URLs and document the anonymous download/checksum/unpack smoke
  test that is required after publication.
- The retired Hugging Face dataset badge/link was removed so an offline HF
  snapshot cannot be mistaken for the v1.0.1 public data source.

### Release status

- `release/stage_c_release.v1.json` and
  `release/public_data_artifact.v1.json` intentionally report
  `pending_publication` and `anonymous_download: false` until the external
  channels are actually uploaded and verified.

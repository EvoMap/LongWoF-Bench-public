# Third-Party Notices

This notice describes third-party material and methodological influences in the
LongWoF-Bench v1.0.1 public data release. It does not replace the license
texts distributed with the release, and it does not relicense third-party
material under LongWoF-Bench's licenses.

The exact candidate public bundle is identified by
`release/public_data_artifact.v1.json`. Asset-level review remains a publication
gate for the final public release, especially where an upstream task contains
data originating outside the upstream benchmark repository.

## SkillsBench

- Project: SkillsBench
- Repository: <https://github.com/benchflow-ai/skillsbench>
- Pinned revision: `5720102e3d6b0d3471b9715995ff96144d9eefb7`
- Upstream license: Apache License 2.0
- License copy: `third_party_licenses/SkillsBench-Apache-2.0.txt`
- Affected LongWoF-Bench tasks: `T0452` through `T0498` inclusive (47 tasks)
- Exact upstream task mapping: the `source.task_id` field in each affected
  task's public `metadata.json`

These tasks were adapted into the LongWoF-Bench layout and evaluation protocol.
Changes include LongWoF-Bench identifiers, normalized directory layouts, runner
and verifier integration, the public/private asset split, and targeted repairs
or sanitization where recorded by the release tooling. Modified material is
therefore not represented as an unchanged SkillsBench distribution.

No root `NOTICE` file was present in the pinned SkillsBench revision. Applicable
copyright, attribution, and task-level license files found in the selected
upstream task directories are retained in the candidate bundle. Upstream
portions remain subject to Apache-2.0; LongWoF-Bench's original modifications and
original data remain subject to the licenses stated by TaskGenome.

Known nested sources that require or retain their own provenance review include,
without limitation:

- `T0454`: PB2002 plate-boundary data containing source citations in the data;
- `T0485`: source documentation for NASA budget material;
- `T0493`: a generated instance derived from the open-source exam-scheduling
  MIP generator identified in the bundled provenance document; and
- `T0498`: Amazon, Facebook, and Google/Feedonomics category taxonomies
  identified in the bundled data-source README.

These sources are recorded here so that downstream users can honour them. These nested sources
must be cleared, replaced, or explicitly licensed before the release is marked
ready for public redistribution.

## GuideBench

- Project: GuideBench
- Repository: <https://github.com/Dlxxx/GuideBench>
- Pinned revision: `78c5bfa42facee34db31e4ba03ad2c3b5a04bbbc`
- Copyright notice: Copyright (c) 2025 xiuxiu-boom
- Upstream license: MIT License
- License copy: `third_party_licenses/GuideBench-MIT.txt`

GuideBench informed the rule-retrieval setup, taxonomy vocabulary, and study
design. The 35 historical translated GuideBench scenarios under the authoring
directory `rule_following/` are not members of the frozen 778-task manifest and
are not included in the public data bundle. The final 159 rule-following tasks
(`T0650` through `T0808`) are LongWoF-Bench-authored tasks; GuideBench is cited as
a methodological influence rather than represented as a direct final-task
source.

The GuideBench MIT notice is nevertheless distributed to make this influence
and the historical derivation record explicit.

## Nested data sources

Four SkillsBench-derived tasks bundle data that did not originate with
SkillsBench. Apache-2.0 covers SkillsBench's own contribution; it does not
relicense this material. Each item below has been traced to its origin.

### Cleared for redistribution

| Task | File | Origin | Terms |
|---|---|---|---|
| `T0454` | `PB2002_boundaries.json`, `PB2002_plates.json` | PB2002 plate-boundary model, Bird (2003) | CC BY 4.0 — attribution required |
| `T0454` | `earthquakes_2024.json` | USGS FDSN event service (the query URL is retained in the file's own `metadata.url`) | U.S. Government work, public domain |
| `T0485` | `nasa_budget_incomplete.xlsx` | NASA budget material | U.S. Government work, public domain |
| `T0493` | `instance.json`, `pair_counts.csv`, `triplet_counts.csv`, `blockmap.csv`, `block_summary.csv` | Generated with the exam-scheduling MIP generator, <https://github.com/Joeyetinghan/exam-scheduling-mip-generator>; generation config recorded in the task's `provenance.md` | MIT |

Required attribution:

> Bird, P. (2003), An updated digital model of plate boundaries,
> *Geochemistry, Geophysics, Geosystems*, 4(3), 1027.
> doi:10.1029/2001GC000252. Licensed CC BY 4.0.

> Earthquake catalog data courtesy of the U.S. Geological Survey.

> NASA budget material, National Aeronautics and Space Administration.

> Exam-scheduling instance generated with
> `Joeyetinghan/exam-scheduling-mip-generator` (MIT License).

### Withheld — no redistribution grant located

`T0498` bundles e-commerce category taxonomies for which no redistribution
licence could be established:

| File | Origin as recorded in the task's data README |
|---|---|
| `amazon_product_categories.csv`, `amazon_product_categories_full.csv` | ASIN Spotlight category list, derived from the Amazon Browse Node structure |
| `fb_product_categories.csv` | Meta Marketing API product-category taxonomy |
| `google_shopping_product_categories.csv` | Google Shopping product taxonomy, obtained via a third-party listing |

Amazon's and Meta's taxonomies are proprietary and are not published under terms
that permit redistribution. Google's product taxonomy is a freely available open
standard, but is not accompanied by an explicit redistribution licence either.
These files are therefore designated for removal and will be handled the same
way as the restricted Skill packages: recorded as metadata only, with no copy,
mirror, downloader, or archive URL provided.

They are excluded from the **v1.0.2** archive by the
`exclude_thirdparty_ecommerce_taxonomies` rule in
`release/asset_policy.v1.json`. Versions 1.0.0 and 1.0.1 still contain them and
must not be redistributed. See `release/restricted_assets.v1.json`.

## Task-level bundled licenses

Some selected tasks include reusable PDF, spreadsheet, or presentation skills
with their own `LICENSE.txt` files. Those files remain attached to the relevant
task assets and control the material they identify.

## No endorsement

Names and marks of upstream projects and data providers are used only to
describe provenance. Their licenses do not imply endorsement of TaskGenome
Bench or EvoMap.

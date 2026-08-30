# Third-Party Notices

This notice describes third-party material and methodological influences in the
TaskGenome Bench private v1.0.0 pre-release. It does not replace the license
texts distributed with the release, and it does not relicense third-party
material under TaskGenome's licenses.

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
- Affected TaskGenome tasks: `T0452` through `T0498` inclusive (47 tasks)
- Exact upstream task mapping: the `source.task_id` field in each affected
  task's public `metadata.json`

These tasks were adapted into the TaskGenome layout and evaluation protocol.
Changes include TaskGenome identifiers, normalized directory layouts, runner
and verifier integration, the public/private asset split, and targeted repairs
or sanitization where recorded by the release tooling. Modified material is
therefore not represented as an unchanged SkillsBench distribution.

No root `NOTICE` file was present in the pinned SkillsBench revision. Applicable
copyright, attribution, and task-level license files found in the selected
upstream task directories are retained in the candidate bundle. Upstream
portions remain subject to Apache-2.0; TaskGenome's original modifications and
original data remain subject to the licenses stated by TaskGenome.

Known nested sources that require or retain their own provenance review include,
without limitation:

- `T0454`: PB2002 plate-boundary data containing source citations in the data;
- `T0485`: source documentation for NASA budget material;
- `T0493`: a generated instance derived from the open-source exam-scheduling
  MIP generator identified in the bundled provenance document; and
- `T0498`: Amazon, Facebook, and Google/Feedonomics category taxonomies
  identified in the bundled data-source README.

The private pre-release may be used for maintainer review. These nested sources
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
(`T0650` through `T0808`) are TaskGenome-authored tasks; GuideBench is cited as
a methodological influence rather than represented as a direct final-task
source.

The GuideBench MIT notice is nevertheless distributed to make this influence
and the historical derivation record explicit.

## Task-level bundled licenses

Some selected tasks include reusable PDF, spreadsheet, or presentation skills
with their own `LICENSE.txt` files. Those files remain attached to the relevant
task assets and control the material they identify.

## No endorsement

Names and marks of upstream projects and data providers are used only to
describe provenance. Their licenses do not imply endorsement of TaskGenome
Bench or EvoMap.

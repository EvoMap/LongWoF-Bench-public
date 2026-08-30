# Restricted Skill assets

LongWoF-Bench includes twelve SkillsBench-derived tasks whose nested Skill
directories carry Anthropic proprietary notices.  The affected task IDs are:

```text
T0464 T0465 T0466 T0467 T0469 T0471
T0473 T0482 T0483 T0484 T0485 T0486
```

The public data release excludes the listed Skill directories in their
entirety.  It does not distribute a mirror, downloader, automatic installer,
archive URL, credential, or copy of those files.  Removing only `LICENSE.txt`
would not be sufficient because the notice applies to the code, prompts,
assets, and other files in the Skill package.

The stable task-to-source mapping is recorded in
[`release/restricted_skills.v1.json`](../release/restricted_skills.v1.json).
The mapping is metadata only; it is not a distribution manifest and must not be
used to fetch the restricted assets.

## Public bundle check

Maintainers can verify that a candidate public data bundle contains none of the
restricted package directories:

```bash
python tools/check_restricted_skills.py \
  --public-bundle /path/to/public/data
```

The command is offline and read-only.  It exits non-zero if an excluded package
is present.

## User-managed local assets

Users who already possess a copy under a separate agreement with the relevant
rightsholder may check its presence without asking LongWoF-Bench to download or
copy it:

```bash
python tools/check_restricted_skills.py \
  --skills-root /path/to/authorized/scenarios \
  --task-id T0464
```

The expected layout is either `<skills-root>/<task_id>/skill/<skill-dir>` or
`<skills-root>/scenarios/<task_id>/skill/<skill-dir>`.  A successful presence
check is not an authorization decision.  The user remains responsible for the
terms that govern the local copy.

## Evaluation behavior

The public archive remains useful for task prompts, metadata, safe inputs, and
research evidence.  A public runner should report that a task is unavailable
when its restricted Skill is absent; the official hidden evaluation service may
use the private, access-controlled copy.  An independently reimplemented Skill
can be used only when its own provenance and license are clear.

Written rightsholder permission is required before any future release may
restore one of these Skill directories.  Permission must cover storage outside
the original service, copying, modification, public hosting, and redistribution
before the release policy is changed.

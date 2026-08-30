# Hardened runner image

`hardened-v2` keeps the legacy prompt construction and scoring rules, while
executing only explicitly admitted generated-code tasks in Docker. The provider
API call stays in the evaluator process. The runtime refuses real execution
unless the image reference contains a registry digest, and it never silently
falls back to host execution.

## Build and publish

Choose a Miniconda-compatible Linux base and resolve it to a real digest first.
The base must provide `conda` under `/opt/conda`. Never invent or copy a
digest from documentation:

```bash
docker pull <registry>/<miniconda-image>@sha256:<verified-base-digest>

docker build \
  --build-arg BASE_IMAGE=<registry>/<miniconda-image>@sha256:<verified-base-digest> \
  --file docker/Dockerfile.runner \
  --tag <registry>/taskgenome-runner:v1 .

docker push <registry>/taskgenome-runner:v1
docker image inspect <registry>/taskgenome-runner:v1 \
  --format '{{json .RepoDigests}}'
```

Record and use the resulting `<registry>/taskgenome-runner@sha256:<digest>`,
not the mutable `:v1` tag. Do not bake provider keys, service-account files,
proxy credentials, private release bundles, or host-specific configuration
into the image.

There is no unpinned `hardened-v2` escape hatch. The historical
`--allow-unpinned-sandbox-image` option is still parsed for command-line
compatibility, but an unpinned image fails validation even for `--dry-run`.
Use a digest-pinned local or registry reference for every `hardened-v2` run.

## Run

```bash
export TASKGENOME_SANDBOX_IMAGE=<registry>/taskgenome-runner@sha256:<digest>

python -m eval.run_official \
  --protocol hardened-v2 \
  --ids T0454 \
  --models <supported-model-alias> \
  --conditions no_context
```

Before any paid API call in a real run, the evaluator verifies that Docker is
available and that the exact image is already present locally. It then launches
a short container smoke test using the same security flags, verifies non-root
execution, a read-only trial workspace with one explicitly writable probe file,
and a read-only container root, and confirms cleanup. It also rejects images
that declare Docker `VOLUME` paths and records the inspected local image ID,
OS, and architecture. A real hardened run therefore requires access to a
working Docker daemon; a dry run does not demonstrate that the smoke test will
pass.
Each candidate process gets:

- no network;
- a read-only container root;
- all Linux capabilities dropped and `no-new-privileges`;
- the caller's non-root UID/GID;
- strict finite CPU, bounded memory/tmpfs, process, file-descriptor, timeout,
  core-dump, captured-output, and generated-file limits;
- a fresh read-only trial directory, with only exact precreated contract output
  files mounted writable and no daemon log driver;
- a fixed minimal environment with no provider credentials.

The default limits are part of the recorded run contract and can be changed
only through explicit `--sandbox-*` options.

The formal protocol accepts only the checked-in asset policy and I/O contract
whose SHA-256 values are pinned by the evaluator. A custom policy or contract
must use a separately named future development protocol; it cannot be labeled
`hardened-v2`.

## Isolation scope

Code execution is fail closed. The versioned contract in
`release/hardened_io_contract.v1.json` lists all 47
`subprocess_ref_runner` tasks and currently admits only T0454, T0456, T0463,
T0470, T0477, T0481, T0485, and T0492. The other 39 and all `pytest_pkg`,
`subprocess_cli`, and `no_ref` code tasks are rejected before a provider API
call.

For an admitted task, the generation workspace contains only assets classified
by the release policy as public `runtime.*` or `environment.*`; OOXML assets
receive the release sanitizer. The evaluator snapshots that workspace, then
creates a fresh judge from trusted canonical assets and copies only the exact
data outputs declared by the contract. Extra intermediates are discarded.
Candidate Python, control files, and dangerous deserialization formats never
enter the judge. The transfer removes stale destination targets and enforces
file-count and byte limits. Pytest judging disables task `conftest.py` files and
automatic third-party plugin loading.

Hardened `short_answer` tasks remain available because the model response is
handled as data by a fixed evaluator-owned wrapper, not executed as arbitrary
candidate code.

The container and transfer boundaries protect the host checkout, provider
credentials, network, and bounded host resources for the admitted paths. They
do not establish universal code safety, complete hidden-test secrecy, or full
benchmark compatibility.

## Compatibility boundary

The image installs the checked-in Conda package pins. System binaries supplied
by the chosen base and provider-side model behavior remain external inputs.
Validate reference solutions and replay representative archived cases before
claiming parity for a published image digest.

Do not describe `hardened-v2` as a universal hidden-test or task-confidentiality
boundary. Report its host-isolation guarantees separately from per-task judge
secrecy.

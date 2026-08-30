# Security Policy

## Reporting a vulnerability

Please report vulnerabilities through GitHub's private security-advisory
feature for the LongWoF-Bench repository. Do not open a public issue for a
secret leak, sandbox escape, command-injection path, or exposure of private
evaluation assets.

Include the affected commit, protocol, reproduction steps, and potential impact
without attaching live credentials or sensitive verifier data.

## Execution warning

Model-generated code is untrusted code.

- `legacy-v1` freezes the prompt construction, scoring, and host-execution
  behavior used by the historical experiments. It provides no security
  boundary. Run it only in an expendable, isolated environment with no
  credentials, private files, or network trust.
- `hardened-v2` keeps prompt construction and scoring unchanged while applying
  a separately recorded container runtime and a fail-closed task boundary. Use
  a digest-pinned runtime image and retain the no-network, non-root, read-only,
  capability, process, memory, CPU, and timeout restrictions.

The task-synthesis pipeline in `synth/` executes model-generated code on the
host as an ordinary part of authoring: it runs each candidate reference
solution, each generated oracle, and each calibration trial. It provides no
security boundary and belongs on the same expendable, isolated machine as
`legacy-v1`. It differs from the evaluator in one way that matters here: it
cannot run at all without a provider credential, so "run it without
credentials" is not an available mitigation. Candidate subprocesses therefore
receive an explicit allowlisted environment (`synth/utils.py:candidate_env`)
with the operator's credentials, proxy settings, and home directory removed.

`hardened-v2` always requires an image reference ending in a verified
`@sha256:<digest>`, including dry runs. The historical
`--allow-unpinned-sandbox-image` option no longer authorizes an unpinned image
to be labeled `hardened-v2`; it is retained only for command-line compatibility
and fails closed.

Containerization reduces risk but is not a guarantee against vulnerabilities in
the container engine, kernel, image, or mounted task assets. Keep the host and
runtime image patched and avoid privileged container settings.

Code tasks are rejected unless they have a reviewed entry in
`release/hardened_io_contract.v1.json`. The contract covers all 47
`subprocess_ref_runner` tasks but currently admits only T0454, T0456, T0463,
T0470, T0477, T0481, T0485, and T0492. The other 39 and every `pytest_pkg`,
`subprocess_cli`, and `no_ref` code task fail before any provider API call.

The formal `hardened-v2` protocol is pinned to the checked-in release asset
policy and I/O contract SHA-256 values. Custom policies/contracts are not
accepted under that protocol name because they could silently widen the public
asset or task allowlist.

For an admitted `subprocess_ref_runner` task, candidate generation receives
only files classified as public `runtime.*` or `environment.*` assets by the
release asset policy. OOXML files in that view receive the release sanitizer.
After generation exits, the evaluator creates a fresh judge workspace from
trusted canonical assets and transfers only the exact data paths named in the
versioned I/O contract. Unlisted intermediate files are discarded. Candidate
Python, control files, and formats that would require dangerous deserialization
do not cross into the judge. Exact destination targets are removed before the
copy, file-count and byte limits are enforced, and pytest is invoked without
task `conftest.py` files or automatic third-party plugin loading.

The candidate workspace is mounted read-only. Only exact regular output files
named by the contract are precreated and individually bind-mounted writable,
with a per-file `fsize` cap derived so all declared files together cannot
exceed the contract total. The runtime disables Docker logging and core dumps,
rejects image-declared writable `VOLUME` paths, and records the inspected local
image ID, OS, and architecture in the run fingerprint.

Hardened `short_answer` tasks are still admitted. Their candidate response is
encoded as data in a fixed evaluator-owned wrapper, rather than installed or
executed as arbitrary candidate code.

The Docker boundary and per-task transfer contract reduce specific risks; they
are not a universal code-safety, hidden-test-secrecy, or task-coverage claim.
The boundary is designed to protect the host checkout, provider credentials,
network, and bounded host resources from admitted candidate code. A real
hardened execution requires a working Docker daemon so its smoke test and trial
containers can run.

## Secrets

Pass provider credentials through the evaluator or the synthesis pipeline
process only. Candidate processes and containers must not inherit them; in
`synth/` this is enforced by an allowlist rather than by a container, so treat
it as defense in depth on an already-expendable host, not as isolation. Run metadata may record whether
a credential source was supplied, but must never serialize its value. Treat
authenticated proxy URLs, service-account JSON, cloud response bodies, and
environment dumps as sensitive.

If a credential is committed or appears in an artifact, revoke it first, then
remove it from current files and repository history as appropriate.

## Evaluation assets

Public task bundles must not contain private tests, gold outputs, oracle Skills,
reference solutions, or evolution traces that reveal verifier-only behavior.
Running `synth/` regenerates exactly this material into its candidate output
directories; those directories are ignored by Git and are rejected by
`tools/stage_c_release.py verify-code`, and must not be published.
Access to private evaluation assets should be read-only and limited to the
evaluator. The public/private boundary is recorded in
`release/asset_policy.v1.json` and `release/stage_c_release.v1.json`.

## Supported versions

Security fixes are applied to the latest default branch. Historical run
artifacts and `legacy-v1` are retained for research reproducibility, not as a
secure execution environment.

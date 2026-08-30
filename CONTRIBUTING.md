# Contributing

Contributions are welcome when they preserve the distinction between the
historical benchmark protocol and new experimental variants.

## Development setup

Use Python 3.11, install the project dependencies, and run the offline suite:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest
```

The default suite never needs an API key. Files named `test_unit_*.py` are the
project tests collected by CI. Credentialed provider diagnostics are manual
tools and must not be converted into CI tests or invoked during collection.

## Compatibility rules

- Do not change prompt bytes, task selection, trial keys, result parsing, or
  scoring under an existing protocol name.
- If a semantic change is necessary, add a new protocol version and keep the
  legacy path available for published-result reproduction.
- Update golden hashes only when the corresponding benchmark asset version is
  intentionally changed. Record the old and new digests in the changelog.
- Keep runtime-security changes separate from model, prompt, and scoring
  changes so their effects can be measured independently.
- Never mix run records produced with different manifests, protocol versions,
  model IDs, or guidance-asset hashes in one run directory.

## Task and asset changes

A task change should include an integrity check for its public prompt, runtime
assets, verifier, and required Gene/Skill files. Preserve task IDs once results
have been published. New or repaired tasks require a new manifest digest and a
clear compatibility note; do not silently replace an asset used by a published
run.

Do not use destructive self-test options such as quarantine in a working tree
containing the canonical benchmark unless that data migration is the explicit
purpose of the change.

## Security and data handling

- Never commit API keys, service-account files, authenticated URLs, or raw
  environment dumps.
- Do not expose private verifier, gold, oracle, or reference-solution assets in
  a public task bundle.
- Use `hardened-v2` for untrusted generated code. `legacy-v1` is only a
  compatibility mode.
- Keep generated run artifacts out of ordinary source commits unless they are
  a deliberate, checksummed research release.

## Pull request checklist

- [ ] `python -m pytest` passes without network access or credentials.
- [ ] `bash -n run_778_thinklow_opus_gemini.sh` passes.
- [ ] Legacy golden tests remain unchanged, or a new protocol version is added.
- [ ] Documentation states the manifest, protocol, model ID, and asset source.
- [ ] No secret or machine-specific absolute path is introduced.
- [ ] User data and unrelated working-tree changes are preserved.

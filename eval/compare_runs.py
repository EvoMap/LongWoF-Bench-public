#!/usr/bin/env python3
"""Compare two gene-generation methods in-sample (each gene tested on its own task).

Reads two run_official result dirs and prints, per model and family, the pass%
under each condition plus the deltas vs no_context:

    base-run  : provides no_context, with_skill, with_gene  (e.g. eval_k3)
    gene-run  : provides an alternative with_gene            (e.g. eval_k3_fb)

so you can read Δskill, optional Δsanitized_skill, Δgene(base), Δgene(alt)
side by side, plus the per-task flip churn (fail->pass / pass->fail) for each
gene variant.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import re
from pathlib import Path

V3_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUNS_ROOT = V3_ROOT / "_runs"
LEGACY_PROTOCOL = "legacy-v1"
HARDENED_PROTOCOL = "hardened-v2"
GENE_CONDITION_PREFIX = "with_gene"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RUNTIME_POLICY_KEYS = (
    "protocol",
    "backend",
    "sandbox_image",
    "memory",
    "cpus",
    "pids_limit",
    "tmpfs_size",
    "output_limit_bytes",
    "allow_unpinned_image",
)


def last_results(path: Path) -> dict[str, dict]:
    by_key: dict[str, dict] = {}
    if not path.exists():
        raise SystemExit(f"missing results: {path}")
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        k = (d.get("trial") or {}).get("trial_key")
        if k:
            by_key[k] = d
    return by_key


def runtime_protocol(run_dir: Path) -> str:
    configured: str | None = None
    config_path = run_dir / "config.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"invalid run config: {config_path}: {exc}") from exc
        configured = config.get("runtime_protocol") or config.get("protocol")
        if configured:
            configured = str(configured)

    protocols: set[str] = set()
    results_path = run_dir / "results.jsonl"
    if results_path.exists():
        for line in results_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                value = json.loads(line).get("runtime_protocol")
            except json.JSONDecodeError:
                continue
            if value:
                protocols.add(str(value))
    if len(protocols) > 1:
        raise SystemExit(
            f"run contains mixed runtime protocols: {run_dir}: {sorted(protocols)}"
        )
    result_protocol = next(iter(protocols), None)
    if configured and result_protocol and configured != result_protocol:
        raise SystemExit(
            "run config/results protocol mismatch: "
            f"{run_dir}: config={configured}, results={result_protocol}"
        )
    return configured or result_protocol or LEGACY_PROTOCOL


def require_comparable_protocols(
    base_run_dir: Path,
    gene_run_dir: Path,
    *,
    allow_cross_protocol: bool,
) -> tuple[str, str]:
    base_protocol = runtime_protocol(base_run_dir)
    gene_protocol = runtime_protocol(gene_run_dir)
    if base_protocol != gene_protocol and not allow_cross_protocol:
        raise SystemExit(
            "refusing cross-protocol comparison: "
            f"base={base_protocol}, gene={gene_protocol}; "
            "pass --allow-cross-protocol only after a documented parity analysis"
        )
    return base_protocol, gene_protocol


def _load_run_config(run_dir: Path) -> dict:
    config_path = run_dir / "config.json"
    if not config_path.exists():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid run config: {config_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"run config must be an object: {config_path}")
    return payload


def _result_records(run_dir: Path) -> list[dict]:
    results_path = run_dir / "results.jsonl"
    if not results_path.exists():
        raise SystemExit(f"missing results: {results_path}")
    records: list[dict] = []
    for line_number, line in enumerate(
        results_path.read_text(encoding="utf-8", errors="replace").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"invalid result record: {results_path}:{line_number}: {exc}"
            ) from exc
        if not isinstance(record, dict):
            raise SystemExit(
                f"result record must be an object: {results_path}:{line_number}"
            )
        records.append(record)
    return records


def _consistent_metadata(
    *,
    run_dir: Path,
    config: dict,
    records: list[dict],
    key: str,
    default: str | None = None,
) -> str | None:
    configured = config.get(key)
    values = {
        str(record[key])
        for record in records
        if record.get(key) not in (None, "")
    }
    if len(values) > 1:
        raise SystemExit(
            f"run contains mixed {key} values: {run_dir}: {sorted(values)}"
        )
    recorded = next(iter(values), None)
    if configured not in (None, ""):
        configured = str(configured)
        if recorded is not None and configured != recorded:
            raise SystemExit(
                f"run config/results {key} mismatch: {run_dir}: "
                f"config={configured}, results={recorded}"
            )
        return configured
    return recorded or default


def _score_pass_threshold(run_dir: Path, config: dict) -> float | None:
    value = config.get("score_pass_threshold")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SystemExit(
            f"invalid score_pass_threshold in {run_dir / 'config.json'}: {value!r}"
        )
    threshold = float(value)
    if not math.isfinite(threshold):
        raise SystemExit(
            f"invalid score_pass_threshold in {run_dir / 'config.json'}: {value!r}"
        )
    return threshold


def _runtime_policy(run_dir: Path, config: dict) -> dict | None:
    value = config.get("runtime_policy")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SystemExit(f"invalid runtime_policy in {run_dir / 'config.json'}")
    return value


def _hardened_asset_boundary(run_dir: Path, config: dict) -> dict | None:
    value = config.get("hardened_asset_boundary")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SystemExit(
            f"invalid hardened_asset_boundary in {run_dir / 'config.json'}"
        )
    image_identity = value.get("image_identity")
    if image_identity is not None and not isinstance(image_identity, dict):
        raise SystemExit(
            "invalid hardened_asset_boundary.image_identity in "
            f"{run_dir / 'config.json'}"
        )
    return value


def _prompt_hashes(
    run_dir: Path,
    records: list[dict],
    models: list[str],
) -> dict[tuple[str, str, str], tuple[str | None, str | None]]:
    """Return latest (user, system) hashes for each selected logical trial.

    Append-only retries may contain an early API error without prompt hashes.
    Non-empty hashes for the same logical trial must nevertheless stay stable.
    """

    selected_models = set(models)
    latest: dict[tuple[str, str, str], tuple[str | None, str | None]] = {}
    seen: dict[tuple[str, str, str], tuple[set[str], set[str]]] = {}
    for line_number, record in enumerate(records, start=1):
        trial = record.get("trial")
        if not isinstance(trial, dict):
            continue
        model = trial.get("model")
        if model not in selected_models:
            continue
        task_id = trial.get("task_id")
        condition = trial.get("condition")
        if not all(
            isinstance(value, str) and value
            for value in (model, task_id, condition)
        ):
            raise SystemExit(
                "invalid selected trial identity in "
                f"{run_dir / 'results.jsonl'}:{line_number}"
            )
        key = (model, task_id, condition)
        payload = record.get("prompt_sha256")
        if payload is None:
            hashes: tuple[str | None, str | None] = (None, None)
        else:
            if not isinstance(payload, dict):
                raise SystemExit(
                    f"invalid prompt_sha256 in {run_dir / 'results.jsonl'}:{line_number}"
                )
            user_hash = payload.get("user")
            system_hash = payload.get("system")
            if not (
                isinstance(user_hash, str)
                and SHA256_RE.fullmatch(user_hash)
                and isinstance(system_hash, str)
                and SHA256_RE.fullmatch(system_hash)
            ):
                raise SystemExit(
                    f"invalid prompt_sha256 in {run_dir / 'results.jsonl'}:{line_number}"
                )
            hashes = (user_hash, system_hash)
            user_seen, system_seen = seen.setdefault(key, (set(), set()))
            user_seen.add(user_hash)
            system_seen.add(system_hash)
        latest[key] = hashes

    for key, (user_seen, system_seen) in seen.items():
        if len(user_seen) > 1 or len(system_seen) > 1:
            raise SystemExit(
                "run contains changing prompt hashes for logical trial "
                f"{key!r}: {run_dir}"
            )
    return latest


def run_identity(run_dir: Path, models: list[str]) -> dict:
    config = _load_run_config(run_dir)
    records = _result_records(run_dir)
    registry = config.get("model_registry")
    if registry is None:
        registry = {}
    if not isinstance(registry, dict):
        raise SystemExit(f"invalid model_registry in {run_dir / 'config.json'}")

    configured_models: dict[str, tuple[str, ...] | None] = {}
    requested_models: dict[str, str | None] = {}
    for model in models:
        entry = registry.get(model)
        if entry is None:
            configured_models[model] = None
        elif not isinstance(entry, list) or not entry or not all(
            isinstance(value, str) for value in entry
        ):
            raise SystemExit(
                f"invalid model_registry entry for {model!r}: {run_dir}"
            )
        else:
            configured_models[model] = tuple(entry)

        requested_ids = {
            str(record["model_id"])
            for record in records
            if (record.get("trial") or {}).get("model") == model
            and record.get("model_id") not in (None, "")
        }
        if len(requested_ids) > 1:
            raise SystemExit(
                f"run contains mixed requested/configured model ids for {model!r}: "
                f"{run_dir}: {sorted(requested_ids)}"
            )
        requested_id = next(iter(requested_ids), None)
        requested_models[model] = requested_id
        configured = configured_models[model]
        if configured and requested_id and configured[0] != requested_id:
            raise SystemExit(
                f"run config/results requested model id mismatch for {model!r}: "
                f"{run_dir}: config={configured[0]}, results={requested_id}"
            )

    return {
        "runtime_protocol": runtime_protocol(run_dir),
        "prompt_protocol": _consistent_metadata(
            run_dir=run_dir,
            config=config,
            records=records,
            key="prompt_protocol",
            default=LEGACY_PROTOCOL,
        ),
        "scoring_protocol": _consistent_metadata(
            run_dir=run_dir,
            config=config,
            records=records,
            key="scoring_protocol",
            default=LEGACY_PROTOCOL,
        ),
        "manifest_sha256": _consistent_metadata(
            run_dir=run_dir,
            config=config,
            records=records,
            key="manifest_sha256",
        ),
        "score_pass_threshold": _score_pass_threshold(run_dir, config),
        "runtime_policy": _runtime_policy(run_dir, config),
        "hardened_asset_boundary": _hardened_asset_boundary(run_dir, config),
        "model_registry": configured_models,
        # run_official writes the ID requested from the provider. It does not
        # currently persist a provider-returned resolved model identity.
        "requested_model_ids": requested_models,
        "prompt_hashes": _prompt_hashes(run_dir, records, models),
    }


def _same_json_value(left: object, right: object) -> bool:
    return json.dumps(
        left, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ) == json.dumps(right, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _append_required_value_issue(
    issues: list[str],
    *,
    label: str,
    left: object,
    right: object,
) -> None:
    if left is None or right is None:
        issues.append(f"{label} is missing (base={left!r}, gene={right!r})")
    elif not _same_json_value(left, right):
        issues.append(f"{label} differs (base={left!r}, gene={right!r})")


def _append_runtime_policy_issues(base: dict, gene: dict, issues: list[str]) -> None:
    left = base["runtime_policy"]
    right = gene["runtime_policy"]
    if left is None or right is None:
        issues.append(f"runtime_policy is missing (base={left!r}, gene={right!r})")
        return
    keys = sorted(set(RUNTIME_POLICY_KEYS) | set(left) | set(right))
    for key in keys:
        left_value = left.get(key)
        right_value = right.get(key)
        if key not in left or key not in right:
            issues.append(
                f"runtime_policy.{key} is missing "
                f"(base={left_value!r}, gene={right_value!r})"
            )
        elif not _same_json_value(left_value, right_value):
            issues.append(
                f"runtime_policy.{key} differs "
                f"(base={left_value!r}, gene={right_value!r})"
            )


def _append_hardened_boundary_issues(base: dict, gene: dict, issues: list[str]) -> None:
    left = base["hardened_asset_boundary"]
    right = gene["hardened_asset_boundary"]
    needs_boundary = (
        base["runtime_protocol"] == HARDENED_PROTOCOL
        or gene["runtime_protocol"] == HARDENED_PROTOCOL
        or left is not None
        or right is not None
    )
    if not needs_boundary:
        return
    if left is None or right is None:
        issues.append(
            "hardened_asset_boundary is missing "
            f"(base={left!r}, gene={right!r})"
        )
        return
    for key in (
        "asset_policy_sha256",
        "io_contract_schema_version",
        "io_contract_sha256",
    ):
        _append_required_value_issue(
            issues,
            label=f"hardened_asset_boundary.{key}",
            left=left.get(key),
            right=right.get(key),
        )

    left_has_image = "image_identity" in left
    right_has_image = "image_identity" in right
    left_image = left.get("image_identity")
    right_image = right.get("image_identity")
    if left_has_image != right_has_image:
        issues.append(
            "hardened_asset_boundary.image_identity is missing "
            f"(base={left_image!r}, gene={right_image!r})"
        )
    elif left_image is not None or right_image is not None:
        _append_required_value_issue(
            issues,
            label="hardened_asset_boundary.image_identity",
            left=left_image,
            right=right_image,
        )


def _append_prompt_hash_issues(base: dict, gene: dict, issues: list[str]) -> None:
    left = base["prompt_hashes"]
    right = gene["prompt_hashes"]

    left_tasks = {(model, task_id) for model, task_id, _condition in left}
    right_tasks = {(model, task_id) for model, task_id, _condition in right}
    for model, task_id in sorted(left_tasks & right_tasks):
        left_rows = {
            key: hashes
            for key, hashes in left.items()
            if key[:2] == (model, task_id)
        }
        right_rows = {
            key: hashes
            for key, hashes in right.items()
            if key[:2] == (model, task_id)
        }
        left_user = {
            hashes[0] for hashes in left_rows.values() if hashes[0] is not None
        }
        right_user = {
            hashes[0] for hashes in right_rows.values() if hashes[0] is not None
        }
        left_missing = any(hashes[0] is None for hashes in left_rows.values())
        right_missing = any(hashes[0] is None for hashes in right_rows.values())
        label = f"user prompt hash for model={model!r}, task={task_id!r}"
        if left_missing or right_missing or not left_user or not right_user:
            issues.append(
                f"{label} is missing for at least one persisted trial "
                f"(base_missing={left_missing}, gene_missing={right_missing})"
            )
        elif len(left_user) != 1 or len(right_user) != 1:
            issues.append(
                f"{label} is internally inconsistent "
                f"(base={sorted(left_user)!r}, gene={sorted(right_user)!r})"
            )
        elif left_user != right_user:
            issues.append(
                f"{label} differs "
                f"(base={next(iter(left_user))!r}, gene={next(iter(right_user))!r})"
            )

    for key in sorted(set(left) & set(right)):
        model, task_id, condition = key
        left_system = left[key][1]
        right_system = right[key][1]
        label = (
            "system prompt hash for "
            f"model={model!r}, task={task_id!r}, condition={condition!r}"
        )
        if left_system is None or right_system is None:
            issues.append(
                f"{label} is missing (base={left_system!r}, gene={right_system!r})"
            )
        elif condition.startswith(GENE_CONDITION_PREFIX):
            # Different Gene assets are the treatment compare_runs was built
            # to compare, so their system prompts may intentionally differ.
            continue
        elif left_system != right_system:
            issues.append(
                f"{label} differs (base={left_system!r}, gene={right_system!r})"
            )


def require_comparable_identities(
    base_run_dir: Path,
    gene_run_dir: Path,
    *,
    models: list[str],
    allow_identity_mismatch: bool,
) -> tuple[dict, dict, list[str]]:
    base = run_identity(base_run_dir, models)
    gene = run_identity(gene_run_dir, models)
    issues: list[str] = []

    for key in (
        "prompt_protocol",
        "scoring_protocol",
        "manifest_sha256",
        "score_pass_threshold",
    ):
        left = base[key]
        right = gene[key]
        if left is None or right is None:
            issues.append(f"{key} is missing (base={left!r}, gene={right!r})")
        elif left != right:
            issues.append(f"{key} differs (base={left!r}, gene={right!r})")

    for model in models:
        for key, label in (
            ("model_registry", "configured model registry"),
            ("requested_model_ids", "requested/configured model id"),
        ):
            left = base[key][model]
            right = gene[key][model]
            if left is None or right is None:
                issues.append(
                    f"{label} for {model!r} is missing "
                    f"(base={left!r}, gene={right!r})"
                )
            elif left != right:
                issues.append(
                    f"{label} for {model!r} differs "
                    f"(base={left!r}, gene={right!r})"
                )

    _append_runtime_policy_issues(base, gene, issues)
    _append_hardened_boundary_issues(base, gene, issues)
    _append_prompt_hash_issues(base, gene, issues)

    if issues and not allow_identity_mismatch:
        raise SystemExit(
            "refusing comparison of runs with unverified or different identities: "
            + "; ".join(issues)
            + ". Pass --allow-identity-mismatch only after a documented parity analysis."
        )
    return base, gene, issues


def passed_map(results: dict[str, dict], condition: str) -> dict[tuple[str, str], int]:
    """(model, task_id) -> 0/1 for the given condition."""
    out: dict[tuple[str, str], int] = {}
    for d in results.values():
        t = d.get("trial") or {}
        if t.get("condition") != condition:
            continue
        out[(t.get("model"), t.get("task_id"))] = 1 if (d.get("eval") or {}).get("passed") else 0
    return out


def family_of(results: dict[str, dict]) -> dict[str, str]:
    fam: dict[str, str] = {}
    for d in results.values():
        t = d.get("trial") or {}
        fam[t.get("task_id")] = t.get("family")
    return fam


def pct(xs: list[int]) -> float:
    return 100.0 * sum(xs) / len(xs) if xs else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs-root", default=str(DEFAULT_RUNS_ROOT))
    ap.add_argument("--base-run-id", default="eval_k3", help="run with no_context, with_skill, with_gene")
    ap.add_argument("--gene-run-id", default="eval_k3_fb", help="run providing the alternative with_gene")
    ap.add_argument("--base-label", default="gene(K3)")
    ap.add_argument("--gene-label", default="gene(K3+fb)")
    ap.add_argument("--skill-condition", default="with_skill")
    ap.add_argument("--sanitized-skill-condition", default="", help="optional extra skill condition, e.g. with_sanitized_skill")
    ap.add_argument("--sanitized-skill-label", default="san_skill")
    ap.add_argument("--models", default="gemini_pro,gemini_flash")
    ap.add_argument(
        "--allow-cross-protocol",
        action="store_true",
        help="Allow legacy-v1/hardened-v2 comparison after an explicit parity analysis",
    )
    ap.add_argument(
        "--allow-identity-mismatch",
        action="store_true",
        help=(
            "Allow missing/different run policy, asset, requested-model, or prompt "
            "identity only after an explicit, documented parity analysis"
        ),
    )
    args = ap.parse_args()

    rr = Path(args.runs_root)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    base_run_dir = rr / args.base_run_id
    gene_run_dir = rr / args.gene_run_id
    base_protocol, gene_protocol = require_comparable_protocols(
        base_run_dir,
        gene_run_dir,
        allow_cross_protocol=args.allow_cross_protocol,
    )
    print(f"runtime protocols: base={base_protocol}, gene={gene_protocol}")
    _base_identity, _gene_identity, identity_issues = require_comparable_identities(
        base_run_dir,
        gene_run_dir,
        models=models,
        allow_identity_mismatch=args.allow_identity_mismatch,
    )
    if identity_issues:
        print(
            "WARNING: identity mismatch override enabled: "
            + "; ".join(identity_issues)
        )
    base = last_results(base_run_dir / "results.jsonl")
    alt = last_results(gene_run_dir / "results.jsonl")

    nc = passed_map(base, "no_context")
    sk = passed_map(base, args.skill_condition)
    sk2 = passed_map(base, args.sanitized_skill_condition) if args.sanitized_skill_condition else {}
    g0 = passed_map(base, "with_gene")
    g1 = passed_map(alt, "with_gene")
    fam = {**family_of(alt), **family_of(base)}

    for model in models:
        # tasks present under all needed conditions for this model
        ids = sorted({tid for (m, tid) in nc if m == model}
                     & {tid for (m, tid) in sk if m == model}
                     & {tid for (m, tid) in g0 if m == model}
                     & {tid for (m, tid) in g1 if m == model})
        if sk2:
            ids = sorted(set(ids) & {tid for (m, tid) in sk2 if m == model})
        if not ids:
            print(f"\n### {model}: no overlapping tasks across all conditions; skip")
            continue
        groups = collections.defaultdict(list)
        for tid in ids:
            groups[fam.get(tid, "?")].append(tid)

        print(f"\n### {model}  (n={len(ids)} shared tasks)")
        if sk2:
            hdr = (
                f"{'family':18s} {'n':>4} {'no_ctx':>7} {'skill':>7} {args.sanitized_skill_label:>10} "
                f"{args.base_label:>10} {args.gene_label:>11} | {'Δskill':>7} {'Δ'+args.sanitized_skill_label:>11} "
                f"{'Δ'+args.base_label:>11} {'Δ'+args.gene_label:>12}"
            )
        else:
            hdr = f"{'family':18s} {'n':>4} {'no_ctx':>7} {'skill':>7} {args.base_label:>10} {args.gene_label:>11} | {'Δskill':>7} {'Δ'+args.base_label:>11} {'Δ'+args.gene_label:>12}"
        print(hdr)
        print("-" * len(hdr))

        def row(label, tids):
            v_nc = [nc[(model, t)] for t in tids]
            v_sk = [sk[(model, t)] for t in tids]
            v_g0 = [g0[(model, t)] for t in tids]
            v_g1 = [g1[(model, t)] for t in tids]
            p_nc, p_sk, p_g0, p_g1 = pct(v_nc), pct(v_sk), pct(v_g0), pct(v_g1)
            if sk2:
                v_sk2 = [sk2[(model, t)] for t in tids]
                p_sk2 = pct(v_sk2)
                print(
                    f"{label:18s} {len(tids):>4} {p_nc:>6.1f}% {p_sk:>6.1f}% {p_sk2:>9.1f}% "
                    f"{p_g0:>9.1f}% {p_g1:>10.1f}% | {p_sk-p_nc:>+6.1f} {p_sk2-p_nc:>+10.1f} "
                    f"{p_g0-p_nc:>+10.1f} {p_g1-p_nc:>+11.1f}"
                )
            else:
                print(f"{label:18s} {len(tids):>4} {p_nc:>6.1f}% {p_sk:>6.1f}% {p_g0:>9.1f}% {p_g1:>10.1f}% | "
                      f"{p_sk-p_nc:>+6.1f} {p_g0-p_nc:>+10.1f} {p_g1-p_nc:>+11.1f}")

        for f in sorted(groups):
            row(f, groups[f])
        row("ALL", ids)

        # flip churn for each gene variant vs no_context
        def flips(gmap):
            fp = sum(1 for t in ids if nc[(model, t)] == 0 and gmap[(model, t)] == 1)
            pf = sum(1 for t in ids if nc[(model, t)] == 1 and gmap[(model, t)] == 0)
            return fp, pf
        f0 = flips(g0)
        f1 = flips(g1)
        print(f"  churn {args.base_label}: fail->pass={f0[0]} pass->fail={f0[1]} net={f0[0]-f0[1]:+d}   "
              f"{args.gene_label}: fail->pass={f1[0]} pass->fail={f1[1]} net={f1[0]-f1[1]:+d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Rewrite each task's SKILL.md into a *sanitized*, gene-style baseline.

This is the SKILL analogue of ``evolve_genes_v3.py``. The original ``SKILL.md``
files were authored by the synthesis pipeline as an oracle/full-spec upper bound:
they deliberately restate the hidden conventions (exact thresholds, magic numbers,
final answer/decision tokens). That makes ``with_skill`` an unfair comparison
against ``with_gene``, whose distiller is constrained by a mechanical leakage audit.

This script re-authors one SKILL.md per task with the SAME two controls gene uses:

    1. A generalization-oriented prompt that teaches the *re-derivable* method and
       pitfalls but forbids restating the final answer or any hidden-only constant.
    2. A mechanical leakage audit (reused from ``evolve_genes_v3``): private =
       hard literals (multi-digit numbers / structured quoted strings / CLI flags)
       that occur in the task's HIDDEN artifacts (reference / oracle / scenario)
       but NOT in the public ``task.md``. Any rewritten skill that restates a
       private literal, or a public answer-option decision token, is rejected and
       regenerated.

The reference solution (a passed solution) IS shown to the author model -- exactly
like gene's ``reference_distilled`` mode -- but the audit strips every instance
constant it might copy. Hidden artifacts are read ONLY to power the audit and the
teacher demonstration; the gradable answer never enters the eval prompt.

Output: ``<out_dir>/<task_id>.md`` (archive copy). With ``--write-inplace`` the
sanitized skill is also written into each task dir as ``--inplace-name`` (default
``SKILL_sanitized.md``), and ``--emit-manifest`` writes a patched manifest whose
``files.skill`` points at it, so you can evaluate the fair skill baseline with:

    python run_official.py --models gemini_flash,gemini_pro \
        --conditions with_skill --manifest <out_dir>/manifest_rewritten_skill.json
"""

from __future__ import annotations

import argparse
import collections
import copy
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
V3_ROOT = HERE.parent
POOL_ROOT = V3_ROOT / "tasks_final"
DEFAULT_MANIFEST = POOL_ROOT / "manifest.json"
DEFAULT_OUT_DIR = POOL_ROOT / "skills_rewritten"

# Reuse the v3 API registry + verifier/distiller helpers, exactly as
# evolve_genes_v3 does. Importing evolve_genes_v3 cascades those imports and gives
# us the shared leakage-audit primitives (build_private_vocab, etc.).
# Keep this eval directory first so sibling imports resolve to this tree's API
# registry, including Bedrock aliases such as bedrock_opus.
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import run_official as ro  # noqa: E402
import gen_genes_llm_v3 as gg  # noqa: E402
import evolve_genes_v3 as ev  # noqa: E402  (reuse private-vocab + answer-option audit)
from api import MODEL_REGISTRY  # noqa: E402


# ---------------------------------------------------------------------------
# Prompts (generic; no per-family branches in the text itself).
# ---------------------------------------------------------------------------

REWRITE_SYSTEM = """You re-author a task's SKILL.md into a SANITIZED, re-derivable procedural guide.

You are shown the PUBLIC task prompt and a reference solution that PASSED the hidden checker. Produce transferable know-how for problems SIMILAR to this one: name the method, the order of operations, the boundary decisions a careful solver must make, and the failure modes to defend against. The reader should be able to RE-DERIVE the correct behavior -- not copy an answer key.

Leakage rules (these are mandatory):
- NEVER restate the final answer, the expected output values, or the specific answer-option / decision / action token.
- NEVER restate an exact hidden numeric constant, threshold, coefficient, magic number, or quoted literal that comes from the reference solution / oracle and is NOT already in the public task. Describe the MECHANIC instead of the value (e.g. "use a strict full-elapsed-day cutoff, not a calendar-day difference" rather than the exact number of seconds).
- You MAY mention public contract terms that already appear in the task: CLI flags, file names, column names, output keys, and the required output format, when essential to the method.
- For rule-style tasks, describe threshold / precedence / override mechanics WITHOUT naming the winning token.
- Be concrete and operational about the METHOD; avoid filler like "standard processing" or "validate inputs" with no specifics.

Output format:
- Markdown only. Use exactly the section headings you are told to use.
- Do NOT wrap the whole document in code fences. Do NOT add any preamble or trailing commentary."""

REWRITE_USER_TEMPLATE = """--- task (public; the solver sees this) ---
{task}

--- a reference solution that PASSES the hidden checker (teacher demo; DO NOT copy its constants/answers) ---
{solution}

Write the sanitized SKILL.md now, using EXACTLY these section headings in order:
{sections}

Output only the markdown."""

REWRITE_RETRY_SUFFIX = """

PREVIOUS OUTPUT WAS REJECTED: it restated hidden answer literals / answer-option tokens that are forbidden:
{leaks}

Regenerate. Remove every one of those literals/tokens and instead describe the underlying mechanic. Keep the same section headings. Output ONLY the corrected markdown."""


# Per-family section shape, mirroring the synth S6 prompts so the rewritten skill
# reads naturally for that family. Anything not listed uses the code-style default.
FAMILY_SECTIONS = {
    "rule_following": ["## Rule Set", "## Decision Procedure", "## Common Pitfalls"],
    "math_reasoning": ["## Method", "## Decision Procedure", "## Common Pitfalls"],
    "agent_env_synth": ["## Overview", "## API Reference", "## Workflow", "## Common Pitfalls"],
}
DEFAULT_SECTIONS = ["## Overview", "## Workflow", "## Common Pitfalls", "## Error Handling"]


def _sections_for(family: str) -> list[str]:
    return FAMILY_SECTIONS.get(family, DEFAULT_SECTIONS)


_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n(.*?)\n```\s*$", re.DOTALL)


def _strip_outer_fence(text: str) -> str:
    text = (text or "").strip()
    m = _FENCE_RE.match(text)
    if m:
        return m.group(1).strip()
    return text


# ---------------------------------------------------------------------------
# Mechanical leakage scan (same matching logic as evolve_genes_v3.find_*_leakage,
# adapted to free-form markdown instead of a GDIv2 payload object).
# ---------------------------------------------------------------------------

def scan_text_leakage(text: str, private_hard: set[str], answer_options: set[str]) -> list[str]:
    low = text.lower()
    leaks: list[str] = []
    seen: set[str] = set()
    for tok in private_hard:
        if len(re.findall(r"[A-Za-z0-9]", tok)) < 2 or tok in seen:
            continue
        if re.search(r"[A-Za-z]", tok):
            hit = re.search(rf"(?<![A-Za-z0-9_]){re.escape(tok)}(?![A-Za-z0-9_])", low) is not None
        else:
            hit = re.search(rf"(?<![0-9.]){re.escape(tok)}(?![0-9.])", text) is not None
        if hit:
            seen.add(tok)
            leaks.append(tok)
    for opt in answer_options:
        if len(re.findall(r"[A-Za-z0-9]", opt)) < 2 or opt in seen:
            continue
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(opt)}(?![A-Za-z0-9_])", low):
            seen.add(opt)
            leaks.append(opt)
    return leaks


def validate_skill(text: str, family: str, private_hard: set[str], answer_options: set[str]) -> tuple[bool, str]:
    if not text or len(re.findall(r"\b\w+\b", text)) < 40:
        return False, "too short / empty"
    low = text.lower()
    missing = [s for s in _sections_for(family) if s.lower() not in low]
    if missing:
        return False, f"missing sections: {missing}"
    leaks = scan_text_leakage(text, private_hard, answer_options)
    if leaks:
        return False, "leakage: " + ", ".join(sorted(leaks)[:12])
    return True, ""


# ---------------------------------------------------------------------------
# Per-task rewrite.
# ---------------------------------------------------------------------------

@dataclass
class RewriteResult:
    task_id: str
    family: str
    status: str
    source: str
    attempts: int
    calls: list[dict[str, Any]]
    skill_text: str = ""


def _read_existing_skill(row: dict[str, Any], task_dir: Path) -> str:
    files = row.get("files") if isinstance(row.get("files"), dict) else {}
    skill_rel = files.get("skill") or "SKILL.md"
    p = task_dir / skill_rel
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def rewrite_one(
    row: dict[str, Any],
    pool_root: Path,
    keys: dict[str, str],
    model_alias: str,
    max_tokens: int,
    attempts: int,
) -> RewriteResult:
    task_id = str(row.get("task_id"))
    family = str(row.get("family"))
    task_dir = ro._task_dir(row, pool_root)
    files = row.get("files") if isinstance(row.get("files"), dict) else {}
    public_task_md = ro._load_text(task_dir / (files.get("task") or "task.md"))

    # Teacher demo: prefer the reference solution (gene's reference_distilled mode);
    # fall back to the existing SKILL.md text so we can still sanitize text-only tasks.
    ref = ev.read_reference_solution(row, task_dir)
    if ref.strip():
        source = "reference"
        solution_blob = ref
    else:
        existing = _read_existing_skill(row, task_dir)
        if not existing.strip():
            return RewriteResult(task_id, family, "no_source", "", 0, [])
        source = "existing_skill"
        solution_blob = existing

    private_hard = ev.build_private_vocab(row, task_dir, public_task_md)
    answer_options = ev.extract_public_answer_options(public_task_md)
    sections = "\n".join(_sections_for(family))

    user = REWRITE_USER_TEMPLATE.format(
        task=gg._truncate(public_task_md, 3500),
        solution=gg._truncate(solution_blob, 6000),
        sections=sections,
    )

    calls: list[dict[str, Any]] = []
    last_leaks = ""
    for idx in range(max(1, attempts)):
        system = REWRITE_SYSTEM if idx == 0 else REWRITE_SYSTEM + REWRITE_RETRY_SUFFIX.format(leaks=last_leaks)
        try:
            # Skill rewrite = reformatting, not solving; keep thinking off.
            resp = gg._llm_chat(model_alias, user, system, keys, max_tokens=max_tokens, effort="off")
            text = _strip_outer_fence(str(resp.get("response") or ""))
            ok, err = validate_skill(text, family, private_hard, answer_options)
            calls.append({
                "attempt": idx, "ok": ok, "error": "" if ok else err,
                "input_tokens": int(resp.get("input_tokens") or 0),
                "output_tokens": int(resp.get("output_tokens") or 0),
            })
            if ok:
                return RewriteResult(task_id, family, "ok", source, idx + 1, calls, text)
            last_leaks = err.replace("leakage: ", "")
        except Exception as exc:  # noqa: BLE001
            last_leaks = f"{type(exc).__name__}: {exc}"
            calls.append({"attempt": idx, "ok": False, "error": last_leaks})
    return RewriteResult(task_id, family, "failed", source, attempts, calls)


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    pool_root = Path(args.pool_root).resolve()
    manifest_path = Path(args.manifest).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    payload, all_rows = ro._load_manifest(manifest_path)
    rows = list(all_rows)
    if args.families:
        wanted = set(gg._csv_arg(args.families))
        rows = [r for r in rows if str(r.get("family")) in wanted]
    if args.ids:
        wanted = set(gg._csv_arg(args.ids))
        rows = [r for r in rows if str(r.get("task_id")) in wanted]

    task_rows = ev.select_per_family(rows, args.per_family_limit, args.seed)

    keys = gg._resolve_keys(args)
    if MODEL_REGISTRY[args.model][1] == "gemini" and not keys["gemini_key"]:
        raise SystemExit("gemini key required (GEMINI_KEY/GEMINI_API_KEY/GOOGLE_API_KEY or --gemini-key)")

    fam_counts = collections.Counter(str(r.get("family")) for r in task_rows)
    (out_dir / "selected_ids.json").write_text(
        json.dumps({"seed": args.seed, "ids": [str(r.get("task_id")) for r in task_rows]}, indent=2),
        encoding="utf-8")

    print(f"manifest: {manifest_path}")
    print(f"selected tasks (1 sanitized skill each): {len(task_rows)}  per-family: {dict(fam_counts)}")
    print(f"model: {args.model} -> {MODEL_REGISTRY[args.model][0]}  attempts: {args.attempts}")
    print(f"out_dir: {out_dir}  write_inplace: {args.write_inplace} ({args.inplace_name})")

    if args.dry_run:
        for r in task_rows[:20]:
            print(f"  {str(r.get('task_id')):6s} {str(r.get('family')):16s}")
        print(f"  ... total {len(task_rows)}")
        return 0

    log_path = out_dir / "_rewrite_log.jsonl"
    rewritten_ids: set[str] = set()

    pending_rows = task_rows
    if args.skip_existing:
        pending_rows = [r for r in task_rows
                        if not (out_dir / f"{str(r.get('task_id'))}.md").exists()]
        print(f"skip-existing: {len(task_rows) - len(pending_rows)} skills already present; "
              f"{len(pending_rows)} remaining to generate")

    def process(row: dict[str, Any]) -> RewriteResult:
        t0 = time.time()
        res = rewrite_one(row, pool_root, keys, args.model, args.skill_max_tokens, args.attempts)
        res.calls.append({"elapsed_s": round(time.time() - t0, 2)})
        return res

    total = len(pending_rows)
    done = n_ok = 0
    with log_path.open("a" if args.skip_existing else "w", encoding="utf-8") as log_fh:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(process, row): row for row in pending_rows}
            for fut in as_completed(futures):
                row = futures[fut]
                res = fut.result()
                done += 1
                if res.status == "ok":
                    (out_dir / f"{res.task_id}.md").write_text(res.skill_text, encoding="utf-8")
                    if args.write_inplace:
                        task_dir = ro._task_dir(row, pool_root)
                        (task_dir / args.inplace_name).write_text(res.skill_text, encoding="utf-8")
                    rewritten_ids.add(res.task_id)
                    n_ok += 1
                log_fh.write(json.dumps({
                    "task_id": res.task_id, "family": res.family, "status": res.status,
                    "source": res.source, "attempts": res.attempts, "calls": res.calls,
                }, ensure_ascii=False) + "\n")
                log_fh.flush()
                print(f"[{done}/{total}] {res.task_id} -> {res.status} ({res.source})", flush=True)

    if args.emit_manifest:
        # Patch every task that has a rewritten skill ON DISK (covers both this run
        # and any earlier resumed runs), not just the ids produced in this process.
        done_ids = {p.stem for p in out_dir.glob("T*.md")} | rewritten_ids
        patched = copy.deepcopy(payload)
        for r in patched.get("tasks", []):
            if isinstance(r, dict) and str(r.get("task_id")) in done_ids:
                f = r.get("files") if isinstance(r.get("files"), dict) else {}
                f["skill"] = args.inplace_name
                r["files"] = f
        man_out = out_dir / "manifest_rewritten_skill.json"
        man_out.write_text(json.dumps(patched, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"patched manifest (files.skill -> {args.inplace_name}): {man_out}")

    print()
    print("done.")
    print(f"  sanitized skills written: {n_ok}/{total}")
    print(f"  out_dir: {out_dir}")
    if args.write_inplace and args.emit_manifest:
        ids_csv = ",".join(sorted(rewritten_ids))
        print()
        print("next: evaluate the FAIR skill baseline (uses the rewritten SKILL files):")
        print(f"  python run_official.py --models gemini_flash,gemini_pro \\")
        print(f"    --conditions with_skill \\")
        print(f"    --manifest {out_dir / 'manifest_rewritten_skill.json'} \\")
        print(f"    --ids '{ids_csv if len(ids_csv) <= 200 else '<ids from selected_ids.json>'}'")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    p.add_argument("--pool-root", default=str(POOL_ROOT))
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    p.add_argument("--model", default="gemini_pro", help="author model alias (same registry as gene)")
    p.add_argument("--families", default="", help="comma-separated family filter")
    p.add_argument("--ids", default="", help="comma-separated task_id filter")
    p.add_argument("--seed", type=int, default=42, help="deterministic per-family sampling")
    p.add_argument("--per-family-limit", type=int, default=0, help="cap tasks per family (0 = all)")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--skip-existing", action="store_true",
                   help="resume: skip tasks whose <task_id>.md already exists in out_dir (append to log)")
    p.add_argument("--attempts", type=int, default=3, help="max rewrite attempts (retry on leakage)")
    p.add_argument("--skill-max-tokens", type=int, default=2400)
    p.add_argument("--write-inplace", action="store_true",
                   help="also write the sanitized skill into each task dir as --inplace-name")
    p.add_argument("--inplace-name", default="SKILL_sanitized.md")
    p.add_argument("--emit-manifest", action="store_true",
                   help="write a patched manifest whose files.skill points at the rewritten skill")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--gemini-key", default="")
    p.add_argument("--yunwu-key", default="")
    p.add_argument("--siliconflow-key", default="")
    p.add_argument("--evomap-key", default="")
    p.add_argument("--bedrock-key", default="")
    p.add_argument("--local-base-url", default="")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv or sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())

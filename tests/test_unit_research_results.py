from __future__ import annotations

import csv
import hashlib
import json
import struct
import subprocess
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"


def read_csv(name: str) -> list[dict[str, str]]:
    with (RESULTS / name).open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_research_registry_has_seven_complete_report_experiments() -> None:
    registry = json.loads((RESULTS / "experiment_registry.json").read_text())
    experiments = registry["experiments"]
    assert registry["research_version"] == "research-v1"
    assert len(experiments) == 7
    assert {row["report_status"] for row in experiments} == {"included_with_caveats"}
    assert {row["task_count"] for row in experiments} == {778}
    assert {row["logical_trial_count"] for row in experiments} == {3112}
    assert {tuple(row["conditions"]) for row in experiments} == {
        ("no_context", "with_skill", "with_gene_gemini", "with_gene_opus")
    }


def test_public_task_metrics_are_complete_and_sanitized() -> None:
    rows = read_csv("task_metrics.csv")
    assert len(rows) == 7 * 4 * 778
    matrix = Counter((row["model_alias"], row["condition"]) for row in rows)
    assert len(matrix) == 28
    assert set(matrix.values()) == {778}
    assert {row["trial_count"] for row in rows} == {"1"}
    assert {row["protocol"] for row in rows} == {"legacy-v1"}
    forbidden = {
        "raw_response",
        "extracted_code",
        "stdout",
        "stderr",
        "prompt",
        "gold",
        "reference_solution",
        "error_type",
    }
    assert forbidden.isdisjoint(rows[0])


def test_subset_definitions_are_exact_and_disjoint_where_required() -> None:
    payload = json.loads((RESULTS / "subset_definitions.json").read_text())
    definitions = payload["definitions"]
    assert {name: row["task_count"] for name, row in definitions.items()} == {
        "full778": 778,
        "opus_evolved252": 252,
        "opus_reference_distilled526": 526,
        "common_evolved180": 180,
    }
    evolved = set(definitions["opus_evolved252"]["task_ids"])
    reference = set(definitions["opus_reference_distilled526"]["task_ids"])
    assert not evolved & reference
    assert evolved | reference == set(definitions["full778"]["task_ids"])


def test_headline_counts_match_frozen_blog_results() -> None:
    rows = read_csv("headline_results.csv")

    def one(subset: str, model: str, condition: str) -> dict[str, str]:
        return next(
            row
            for row in rows
            if row["subset"] == subset
            and row["model_alias"] == model
            and row["condition"] == condition
        )

    assert one("full778", "bedrock_opus48", "no_context")["pass_rate_percent"] == "20.2"
    assert one("full778", "bedrock_opus48", "with_skill")["pass_rate_percent"] == "39.9"
    assert one("full778", "bedrock_opus48", "with_gene_opus")["pass_rate_percent"] == "39.2"
    assert one("opus_evolved252", "bedrock_opus48", "with_gene_opus")["passed"] == "200"
    assert one("opus_evolved252", "sf_qwen_moe", "with_gene_opus")["pass_rate_percent"] == "61.9"
    assert one("opus_reference_distilled526", "sf_qwen_coder30b", "with_gene_opus")["pass_rate_percent"] == "7.2"


def test_evolved_gene_advantage_is_paired_and_significant_for_all_models() -> None:
    rows = [
        row
        for row in read_csv("statistical_tests.csv")
        if row["subset"] == "opus_evolved252"
    ]
    assert len(rows) == 7
    assert all(float(row["delta_pp"]) > 0 for row in rows)
    assert all(float(row["mcnemar_exact_p"]) < 0.05 for row in rows)
    assert all(float(row["paired_bootstrap_95_low_pp"]) > 0 for row in rows)


def test_research_freeze_hashes_all_derived_artifacts_and_logic_boundary() -> None:
    freeze = json.loads((RESULTS / "research_v1.json").read_text())
    assert freeze["release_id"] == "73d08c560817d745c8415927"
    for item in freeze["derived_artifacts"]:
        path = ROOT / item["path"]
        assert path.stat().st_size == item["size"]
        assert sha256(path) == item["sha256"]
    for relative, digest in freeze["protected_logic_files_at_freeze"].items():
        protected = ROOT / relative
        if protected.is_file():
            assert sha256(protected) == digest
            continue
        assert relative == "tasks_final/manifest.json"
        quality = json.loads(
            (ROOT / "release/quality_report.v1.json").read_text(encoding="utf-8")
        )
        assert quality["canonical"]["manifest_sha256"] == digest


def _png_shape_and_metadata(path: Path) -> tuple[tuple[int, int, int, int], dict[str, str]]:
    """Return ((width, height, bit depth, color type), tEXt metadata) for a PNG."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"not a PNG: {path}"
    header = struct.unpack(">IIBB", data[16:26])
    metadata: dict[str, str] = {}
    offset = 8
    while offset + 8 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        if kind == b"tEXt":
            key, _, value = data[offset + 8 : offset + 8 + length].partition(b"\x00")
            metadata[key.decode("latin-1")] = value.decode("latin-1")
        elif kind == b"IEND":
            break
        offset += 12 + length
    return header, metadata


# Text artifacts and the vector figures are byte-reproducible on any platform.
BYTE_EXACT_RENDER_OUTPUTS = (
    "headline_results.csv",
    "track_results.csv",
    "statistical_tests.csv",
    "token_efficiency.csv",
    "token_efficiency_by_track.csv",
    "figures/evolved_results.svg",
    "figures/token_efficiency.svg",
    "tables/full778.md",
    "tables/evolved252.md",
    "tables/reference_distilled526.md",
    "tables/common180.md",
    "tables/evolution_depth.md",
    "tables/tracks_evolved252.md",
    "tables/tracks_full778.md",
    "tables/statistical_tests.md",
    "tables/token_efficiency.md",
    "tables/token_efficiency_by_track.md",
    "tables/tech_report_tables.tex",
)

# The rasterized figures are not. Matplotlib renders glyphs through the host
# font stack, so the same numbers produce different bytes on a different
# operating system even at an identical version pin. The checked-in PNGs are
# the ones rendered on the CI platform; everywhere else this asserts the figure
# shape and embedded metadata instead. The first two also ship as SVG above,
# which keeps a byte-level guarantee for those plots; fig-3-3 is raster-only,
# so its numbers are covered by the byte-exact track tables it is drawn from.
RASTERIZED_RENDER_OUTPUTS = (
    "figures/evolved_results.png",
    "figures/token_efficiency.png",
    "figures/fig-3-3-opus-gene-vs-skill-by-task-type.png",
)


def test_public_render_command_is_deterministic(tmp_path: Path) -> None:
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "research_results.py"),
            "render",
            "--source",
            str(RESULTS),
            "--output",
            str(tmp_path),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    for relative in BYTE_EXACT_RENDER_OUTPUTS:
        assert (tmp_path / relative).read_bytes() == (RESULTS / relative).read_bytes()
    for relative in RASTERIZED_RENDER_OUTPUTS:
        rendered, published = tmp_path / relative, RESULTS / relative
        assert rendered.is_file(), relative
        assert _png_shape_and_metadata(rendered) == _png_shape_and_metadata(published), relative


def test_public_registry_keeps_only_path_independent_artifact_provenance() -> None:
    text = (RESULTS / "experiment_registry.json").read_text(encoding="utf-8")
    assert "_runs" + "/" not in text
    assert "results_path" not in text
    assert "config_path" not in text
    registry = json.loads(text)
    artifact = registry["experiments"][0]["raw_artifact"]
    assert artifact["results_sha256"]
    assert artifact["config_sha256"]
    assert artifact["results_size"] > 0
    assert artifact["config_size"] > 0

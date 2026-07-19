from __future__ import annotations

import hashlib
import json
from pathlib import Path

GOLDEN_ROOT = Path("tests/fixtures/package_b/golden")
FROZEN_MANIFEST_SHA256 = "b7ea6e42174cde472442d46bdb4bbdd78906591f67b2d79b453b4f248110fc43"


def test_golden_manifest_matches_every_tracked_fixture() -> None:
    root = GOLDEN_ROOT
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    actual = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    }
    assert manifest == {"schema_version": 1, "files": actual}


def test_package_b_golden_manifest_is_byte_immutable() -> None:
    assert hashlib.sha256((GOLDEN_ROOT / "manifest.json").read_bytes()).hexdigest() == FROZEN_MANIFEST_SHA256


def test_selected_plan_preserves_all_production_groups_in_concatenation_order() -> None:
    source = json.loads((GOLDEN_ROOT / "expected/filled_region.path_plan.json").read_text(encoding="utf-8"))
    selected = json.loads((GOLDEN_ROOT / "expected/selected_path_plan.json").read_text(encoding="utf-8"))
    groups = (
        ("fill", source["fill_paths"]),
        ("stroke", source["stroke_paths"]),
        ("boundary", source["fill_boundary_paths"]),
    )
    assert all(paths for _, paths in groups)

    expected_sequence = [(path["id"], group) for group, paths in groups for path in paths]
    actual_sequence = [(path["id"], path["group"]) for path in selected["selected_paths_before_ordering"]]
    assert selected["source_path_plan"] == source["input"]
    assert actual_sequence == expected_sequence


def test_comfy_selected_output_uses_first_inserted_unsorted_node() -> None:
    history = json.loads((GOLDEN_ROOT / "comfy_history_multi_output.json").read_text(encoding="utf-8"))
    selected = json.loads((GOLDEN_ROOT / "expected/comfy_selected_output.json").read_text(encoding="utf-8"))
    node_ids = list(history["outputs"])
    assert node_ids != sorted(node_ids)
    assert node_ids != sorted(node_ids, key=int)

    first_node = history["outputs"][node_ids[0]]
    assert selected == first_node["images"][0]

from __future__ import annotations

import ast
from pathlib import Path

from drawingmachine.domain.planning.models import PathPlanConfig
from drawingmachine.domain.planning.strokes.dedupe import dedupe_overlapping_short_paths
from drawingmachine.domain.planning.strokes.postprocess import postprocess_stroke_paths
from drawingmachine.domain.planning.strokes.tracing import trace_skeleton_paths

PLANNING_ROOT = Path(__file__).resolve().parents[4] / "src/drawingmachine/domain/planning"


def test_trace_skeleton_paths_preserves_endpoint_first_depth_first_order() -> None:
    skeleton = frozenset({(1, 0), (1, 1), (0, 2), (1, 2), (2, 2)})

    assert trace_skeleton_paths(skeleton, width=4, height=4) == (
        ((1, 0), (1, 1), (1, 2), (0, 2), (1, 1), (2, 2), (1, 2)),
    )


def test_postprocess_merges_close_collinear_endpoints() -> None:
    paths, metrics = postprocess_stroke_paths(
        (((0, 0), (2, 0)), ((3, 0), (5, 0))),
        scale=1.0,
        config=PathPlanConfig(
            drop_short_stroke_mm=0.0,
            merge_endpoint_distance_mm=1.1,
            merge_angle_deg=1.0,
            dedupe_short_path_length_mm=0.0,
        ),
    )

    assert paths == (((0, 0), (2, 0), (3, 0), (5, 0)),)
    assert metrics == {
        "dropped_short_count": 0,
        "merged_pair_count": 1,
        "deduped_short_count": 0,
        "deduped_short_length_mm": 0.0,
    }


def test_postprocess_angle_filter_rejects_perpendicular_endpoint_merge() -> None:
    paths, metrics = postprocess_stroke_paths(
        (((0, 0), (2, 0)), ((2, 1), (2, 3))),
        scale=1.0,
        config=PathPlanConfig(
            drop_short_stroke_mm=0.0,
            merge_endpoint_distance_mm=1.1,
            merge_angle_deg=30.0,
            dedupe_short_path_length_mm=0.0,
        ),
    )

    assert paths == (((0, 0), (2, 0)), ((2, 1), (2, 3)))
    assert metrics["merged_pair_count"] == 0


def test_postprocess_drops_strokes_below_exact_length_threshold() -> None:
    paths, metrics = postprocess_stroke_paths(
        (((0, 0), (1, 0)), ((0, 0), (3, 0))),
        scale=1.0,
        config=PathPlanConfig(
            drop_short_stroke_mm=2.0,
            merge_endpoint_distance_mm=0.0,
            dedupe_short_path_length_mm=0.0,
        ),
    )

    assert paths == (((0, 0), (3, 0)),)
    assert metrics["dropped_short_count"] == 1


def test_dedupe_removes_only_short_aligned_overlapping_path() -> None:
    paths, metrics = dedupe_overlapping_short_paths(
        (((0, 0), (10, 0)), ((2, 0), (4, 0)), ((2, 2), (4, 2))),
        scale=1.0,
        config=PathPlanConfig(
            dedupe_short_path_length_mm=3.0,
            dedupe_distance_mm=0.1,
            dedupe_angle_deg=1.0,
            dedupe_overlap_ratio=0.9,
        ),
    )

    assert paths == (((0, 0), (10, 0)), ((2, 2), (4, 2)))
    assert metrics == {"deduped_short_count": 1, "deduped_short_length_mm": 2.0}


def test_stroke_modules_use_only_canonical_polyline_length() -> None:
    for relative_path in ("strokes/dedupe.py", "strokes/postprocess.py"):
        tree = ast.parse((PLANNING_ROOT / relative_path).read_text(encoding="utf-8"))
        imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imported_names = {alias.name for node in imports for alias in node.names}

        assert any(
            node.module == "drawingmachine.domain.planning.geometry"
            and any(alias.name == "polyline_length" for alias in node.names)
            for node in imports
        ), f"{relative_path} must import the B7 canonical planning.geometry.polyline_length"
        assert any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "polyline_length"
            for node in ast.walk(tree)
        ), f"{relative_path} must call the B7 canonical polyline_length"
        assert not any(
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "pixel_polyline_length"
            for node in ast.walk(tree)
        ), f"{relative_path} must not define a duplicate pixel_polyline_length"
        assert "pixel_polyline_length" not in imported_names
        assert not any(
            isinstance(node, ast.Constant) and node.value == "pixel_polyline_length" for node in ast.walk(tree)
        ), f"{relative_path} must not export pixel_polyline_length"

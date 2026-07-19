from __future__ import annotations

import math

import pytest

import drawingmachine.domain.planning.fills.hatch as hatch_module
from drawingmachine.domain.planning.fills.hatch import (
    _build_boundary_graph,
    _connect_via_arc,
    _connect_via_boundary,
    _nearest_boundary_point,
    _path_near_boundary,
    build_fill_records,
    serpentine_fill_paths,
)
from drawingmachine.domain.planning.models import PathPlanConfig
from drawingmachine.domain.planning.service import _path_plan_checks, _record_points
from drawingmachine.domain.planning.strokes.dedupe import (
    _is_redundant_short_path,
    _min_distance_to_polyline,
    _path_direction,
    _path_overlap_ratio,
    _point_segment_distance,
    _sample_path_points,
    angle_between,
    dedupe_overlapping_short_paths,
    normalize_direction,
)
from drawingmachine.domain.planning.strokes.postprocess import (
    _bridge_pixels,
    _find_best_merge,
    _head_direction,
    _merge_pixel_paths,
    _tail_direction,
    build_centerline_base_records,
    build_stroke_records,
    path_to_record,
)


def test_planning_service_reports_every_fallback_blocker_and_warning() -> None:
    assert _record_points({"points": []}) == ()

    foreground = frozenset({(0, 0)})
    skeleton = frozenset({(0, 0)})
    assert "empty_binary" in {check["code"] for check in _path_plan_checks(frozenset(), skeleton, 10, 10, [], [], [])}
    assert "many_stroke_paths" in {
        check["code"] for check in _path_plan_checks(foreground, skeleton, 10, 10, [{}] * 2501, [], [])
    }
    assert "many_fill_paths" in {
        check["code"] for check in _path_plan_checks(foreground, skeleton, 10, 10, [], [{}] * 1201, [])
    }
    assert "fill_without_hatch" in {
        check["code"]
        for check in _path_plan_checks(
            foreground,
            skeleton,
            10,
            10,
            [],
            [],
            [{"classification": "fill_candidate"}],
        )
    }


@pytest.mark.parametrize(
    ("config", "paths"),
    [
        (PathPlanConfig(dedupe_short_path_length_mm=0.0), (((0, 0), (1, 0)),)),
        (
            PathPlanConfig(dedupe_short_path_length_mm=2.0, dedupe_distance_mm=-1.0),
            (((0, 0), (1, 0)),),
        ),
        (
            PathPlanConfig(
                dedupe_short_path_length_mm=2.0,
                dedupe_distance_mm=1.0,
                dedupe_overlap_ratio=0.0,
            ),
            (((0, 0), (1, 0)),),
        ),
    ],
)
def test_dedupe_disabled_configurations_return_input_unchanged(
    config: PathPlanConfig,
    paths: tuple[tuple[tuple[int, int], ...], ...],
) -> None:
    assert dedupe_overlapping_short_paths(paths, 0.0, config) == (
        paths,
        {"deduped_short_count": 0, "deduped_short_length_mm": 0.0},
    )


def test_dedupe_skips_zero_length_and_threshold_length_paths() -> None:
    paths = (((0, 0),), ((0, 0), (3, 0)), ((0, 2), (2, 2)))

    assert (
        dedupe_overlapping_short_paths(
            paths,
            1.0,
            PathPlanConfig(
                dedupe_short_path_length_mm=3.0,
                dedupe_distance_mm=0.1,
                dedupe_overlap_ratio=0.9,
            ),
        )[0]
        == paths
    )


def test_redundant_short_path_rejects_ineligible_comparisons() -> None:
    config = PathPlanConfig(
        dedupe_short_path_length_mm=10.0,
        dedupe_distance_mm=0.1,
        dedupe_angle_deg=5.0,
        dedupe_overlap_ratio=0.9,
    )
    short = ((0, 0), (2, 0))
    perpendicular = ((0, 0), (0, 5))
    equal_length = ((0, 1), (2, 1))

    assert not _is_redundant_short_path(0, short, (short,), [2.0], [0], 0.1, config)
    assert not _is_redundant_short_path(0, short, (short, equal_length), [2.0, 2.0], [1], 0.1, config)
    assert not _is_redundant_short_path(
        0,
        short,
        (short, perpendicular),
        [2.0, 5.0],
        [1],
        0.1,
        config,
    )
    assert not _is_redundant_short_path(
        0,
        short,
        (short, ((0, 3), (5, 3))),
        [2.0, 5.0],
        [1],
        0.1,
        config,
    )


def test_dedupe_geometry_helpers_cover_degenerate_and_clamped_cases() -> None:
    assert normalize_direction(0.0, 0.0) == (0.0, 0.0)
    assert normalize_direction(3.0, 4.0) == (0.6, 0.8)
    assert angle_between((0.0, 0.0), (1.0, 0.0)) == 180.0
    assert angle_between((1.0, 0.0), (1.0, 0.0)) == 0.0
    assert _path_direction(((0, 0),)) == (0.0, 0.0)
    assert _path_direction(((0, 0), (2, 0), (0, 0))) == (1.0, 0.0)
    assert _sample_path_points(((0, 0),), 1.0) == ()
    assert _sample_path_points(((0, 0), (2, 0)), 0.5) == (
        (0.0, 0.0),
        (0.5, 0.0),
        (1.0, 0.0),
        (1.5, 0.0),
        (2.0, 0.0),
    )
    assert _path_overlap_ratio(((0, 0),), ((0, 0), (1, 0)), 1.0) == 0.0
    assert _path_overlap_ratio(((0, 0), (1, 0)), ((0, 3), (1, 3)), 0.1) == 0.0
    assert math.isinf(_min_distance_to_polyline((0.0, 0.0), ((0, 0),)))
    assert _point_segment_distance((3.0, 4.0), (0, 0), (0, 0)) == 5.0
    assert _point_segment_distance((-1.0, 0.0), (0, 0), (2, 0)) == 1.0
    assert _point_segment_distance((3.0, 0.0), (0, 0), (2, 0)) == 1.0


def test_postprocess_helpers_cover_short_paths_filters_and_bridges() -> None:
    assert _head_direction(((0, 0),)) == (0.0, 0.0)
    assert _tail_direction(((0, 0),)) == (0.0, 0.0)
    assert _find_best_merge([((0, 0),), ((0, 0), (1, 0))], 2.0, 180.0) is None
    assert _find_best_merge([((0, 0), (1, 0)), ((5, 0),)], 10.0, 180.0) is None
    assert _find_best_merge([((0, 0), (1, 0)), ((5, 0), (6, 0))], 1.0, 180.0) is None
    assert _find_best_merge([((0, 0), (1, 0)), ((1, 1), (1, 2))], 2.0, 10.0) is None
    assert _find_best_merge(
        [((0, 0), (1, 0)), ((3, 0), (4, 0)), ((2, 0), (3, 0))],
        3.0,
        1.0,
    ) == (1, 2, True, True)
    assert _merge_pixel_paths(((0, 0), (1, 0)), ((1, 0), (2, 0))) == ((0, 0), (1, 0), (2, 0))
    assert _merge_pixel_paths(((0, 0), (1, 0)), ((3, 0), (4, 0))) == (
        (0, 0),
        (1, 0),
        (2, 0),
        (3, 0),
        (4, 0),
    )
    assert _bridge_pixels((0, 0), (1, 1)) == ()


def test_path_record_builders_cover_rejections_simplification_and_extras() -> None:
    assert path_to_record("short", "test", ((0.0, 0.0),), 0.0, 0.0) is None
    assert path_to_record("short", "test", ((0.0, 0.0), (1.0, 0.0)), 0.0, 2.0) is None
    assert path_to_record(
        "kept",
        "test",
        ((0.0, 0.0), (0.5, 0.01), (1.0, 0.0)),
        0.1,
        0.0,
        {"tag": "extra"},
    ) == {
        "id": "kept",
        "source": "test",
        "points_mm": [[0.0, 0.0], [1.0, 0.0]],
        "length_mm": 1.0,
        "tag": "extra",
    }
    config = PathPlanConfig(min_path_length_mm=2.0)
    paths = (((0, 0), (1, 0)), ((0, 0), (3, 0)))
    assert [record["id"] for record in build_stroke_records(paths, 1.0, 0.0, 0.0, config)] == ["stroke_0002"]
    assert [record["id"] for record in build_centerline_base_records(paths, 1.0, 0.0, 0.0, config)] == [
        "centerline_base_0002"
    ]


def test_hatch_helpers_cover_empty_small_and_unusable_regions() -> None:
    assert serpentine_fill_paths(frozenset(), 4, 4, 1.0, 1.0, 1.0, 1.0) == ()
    small = frozenset((x, y) for x in range(3) for y in range(2))
    assert serpentine_fill_paths(small, 4, 4, 1.0, 1.0, 1.0, 0.0) == ()
    narrow = frozenset((0, y) for y in range(10))
    assert serpentine_fill_paths(narrow, 3, 12, 1.0, 1.0, 3.0, 0.0) == ()


def test_boundary_helpers_cover_snapping_limits_and_disconnected_paths() -> None:
    graph = {(0, 0): ((1, 0),), (1, 0): ((0, 0),)}
    region = frozenset(graph)
    assert _nearest_boundary_point((3, 3), graph, 1) is None
    assert _nearest_boundary_point((2, 0), graph, 1) == (1, 0)
    assert _connect_via_boundary((5, 5), (1, 0), graph, region, 2, 5) is None
    assert _connect_via_boundary((0, 0), (5, 5), graph, region, 2, 5) is None
    assert _connect_via_boundary((0, 0), (0, 0), graph, region, 1, 5) == ((0, 0), (0, 0))
    assert _connect_via_boundary((0, 0), (2, 0), {**graph, (2, 0): ()}, region, 1, 5) is None
    assert _connect_via_boundary((0, 0), (1, 0), graph, region, 1, 1) is None
    assert _path_near_boundary(((9, 9),), graph, tolerance=1) is False
    assert _path_near_boundary(((0, 0), (1, 0)), graph, tolerance=0) is True


def test_boundary_connection_snaps_both_endpoints() -> None:
    graph = {(0, 0): ((1, 0),), (1, 0): ((0, 0),)}

    assert _connect_via_boundary((0, 1), (2, 1), graph, frozenset(graph), 2, 5) == (
        (0, 0),
        (1, 0),
    )


def test_boundary_connection_rejects_degenerate_or_off_boundary_simplification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = {(0, 0): ((1, 0),), (1, 0): ((0, 0),)}
    monkeypatch.setattr(hatch_module, "simplify_polyline", lambda points, tolerance_mm: ((0.0, 0.0),))
    assert _connect_via_boundary((0, 0), (1, 0), graph, frozenset(graph), 1, 5) is None

    monkeypatch.setattr(
        hatch_module,
        "simplify_polyline",
        lambda points, tolerance_mm: ((0.0, 0.0), (1.0, 0.0)),
    )
    monkeypatch.setattr(hatch_module, "_path_near_boundary", lambda path, boundary_graph, tolerance: False)
    assert _connect_via_boundary((0, 0), (1, 0), graph, frozenset(graph), 1, 5) is None


def test_arc_and_fill_record_helpers_cover_failure_and_filtering() -> None:
    assert _connect_via_arc((0, 1), (0, 1), frozenset(), 1) is None
    assert _connect_via_arc((0, 2), (0, 1), frozenset(), 1) is None
    assert _connect_via_arc((0, 0), (0, 3), frozenset(), 1) is None
    records = build_fill_records(
        (((0, 0), (1, 0)), ((0, 0), (3, 0))),
        1.0,
        0.0,
        0.0,
        PathPlanConfig(min_path_length_mm=2.0, hatch_spacing_mm=1.25),
    )
    assert records == [
        {
            "id": "fill_0002",
            "source": "hatch",
            "points_mm": [[0.0, 0.0], [3.0, 0.0]],
            "length_mm": 3.0,
            "spacing_mm": 1.25,
        }
    ]


def test_boundary_graph_excludes_fully_surrounded_center() -> None:
    region = frozenset((x, y) for x in range(1, 4) for y in range(1, 4))
    graph = _build_boundary_graph(region, 5, 5)

    assert (2, 2) not in graph
    assert set(graph) == region - {(2, 2)}

from __future__ import annotations

from PIL import Image, ImageDraw

from drawingmachine.domain.planning.fills.hatch import (
    _build_boundary_graph,
    _connect_via_arc,
    _connect_via_boundary,
    connect_scanline_endpoints,
    serpentine_fill_paths,
)
from drawingmachine.domain.planning.models import PathPlanConfig
from drawingmachine.domain.planning.service import build_path_plan


def test_serpentine_fill_alternates_scanline_direction() -> None:
    region = frozenset((x, y) for y in range(1, 6) for x in range(1, 8))

    assert serpentine_fill_paths(
        region,
        width=10,
        height=8,
        mm_per_px=1.0,
        hatch_spacing_mm=2.0,
        min_run_mm=2.0,
        min_thickness_mm=2.0,
    ) == (((1, 1), (7, 1), (7, 3), (1, 3), (1, 5), (7, 5)),)


def test_scanline_connection_prefers_boundary_path_before_arc() -> None:
    region = frozenset((x, y) for y in range(1, 6) for x in range(1, 8))
    graph = _build_boundary_graph(region, width=10, height=8)
    boundary = _connect_via_boundary((1, 1), (1, 3), graph, region, spacing_px=2, max_distance_px=6)
    arc = _connect_via_arc((1, 1), (1, 3), region, spacing_px=2)

    assert boundary == ((1, 1), (1, 3))
    assert arc == ((1, 1), (3, 2), (1, 3))
    assert (
        connect_scanline_endpoints(
            (1, 1),
            (1, 3),
            graph,
            region,
            spacing_px=2,
            max_boundary_distance_px=6,
        )
        == boundary
    )


def test_scanline_connection_falls_back_to_arc_when_boundary_is_unavailable() -> None:
    region = frozenset({(1, 1), (2, 2), (1, 3)})

    assert connect_scanline_endpoints(
        (1, 1),
        (1, 3),
        {},
        region,
        spacing_px=1,
        max_boundary_distance_px=3,
    ) == ((1, 1), (2, 2), (1, 3))


def test_fill_candidates_are_removed_before_stroke_thinning() -> None:
    image = Image.new("L", (24, 16), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle((2, 2, 11, 11), fill=0)
    draw.line(((15, 8), (22, 8)), fill=0, width=1)

    result = build_path_plan(
        image,
        source_name="in-memory.png",
        config=PathPlanConfig(
            canvas_width_mm=24.0,
            canvas_height_mm=16.0,
            threshold=128,
            invert=False,
            min_component_area_px=1,
            hatch_spacing_mm=2.0,
            hatch_min_run_mm=2.0,
            fill_min_thickness_mm=2.0,
        ),
    )

    fill_square = frozenset((x, y) for y in range(2, 12) for x in range(2, 12))
    assert result.skeleton
    assert result.skeleton.isdisjoint(fill_square)
    assert result.skeleton <= result.foreground

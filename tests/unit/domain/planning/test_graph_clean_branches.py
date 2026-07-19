from __future__ import annotations

from typing import Any, cast

from drawingmachine.domain.planning.experiments import graph_clean
from drawingmachine.domain.planning.models import PathPlanConfig


def _edge(edge_id: int, start: tuple[int, int], end: tuple[int, int], points: list[tuple[int, int]]) -> Any:
    return {"id": edge_id, "start": start, "end": end, "points": points, "length_px": float(len(points) - 1)}


def test_graph_clean_empty_singleton_line_loop_and_records() -> None:
    assert graph_clean.skeleton_graph_edges(set(), 5, 5) == ([], [], {})
    assert graph_clean.trace_simple_loop_component({(2, 2)}, 5, 5) == []
    edges, loops, degrees = graph_clean.skeleton_graph_edges({(1, 2), (2, 2), (3, 2)}, 5, 5)
    assert edges and not loops and degrees
    loop = {(1, 1), (2, 1), (2, 2), (1, 2)}
    traced = graph_clean.trace_simple_loop_component(loop, 4, 4)
    assert len(traced) >= 2
    records, metrics = graph_clean.build_graph_clean_stroke_records(
        {(1, 2), (2, 2), (3, 2)},
        5,
        5,
        1.0,
        0.0,
        0.0,
        PathPlanConfig(simplify_tolerance_mm=0.0, min_path_length_mm=0.0),
        graph_clean.GraphCleanConfig(spur_length_mm=0.1, min_main_edge_length_mm=0.1),
    )
    assert records and metrics["graph_clean_record_count"] == len(records)


def test_remove_spurs_covers_both_orientations_nonspur_and_main_edge_requirement() -> None:
    junction = (5, 5)
    edges = [
        _edge(0, (4, 5), junction, [(4, 5), junction]),
        _edge(1, junction, (6, 5), [junction, (6, 5), (7, 5), (8, 5)]),
        _edge(2, junction, (5, 8), [junction, (5, 6), (5, 7), (5, 8)]),
        _edge(3, junction, (5, 4), [junction, (5, 4)]),
        _edge(4, (0, 0), (1, 0), [(0, 0), (1, 0), (2, 0), (3, 0)]),
    ]
    degrees = {(4, 5): 1, (5, 4): 1, junction: 4}
    remaining, stats = graph_clean.remove_graph_spurs(
        cast(Any, edges), degrees, 1.0, graph_clean.GraphCleanConfig(spur_length_mm=2.0, min_main_edge_length_mm=2.0)
    )
    assert stats["removed_count"] == 2
    assert {edge["id"] for edge in remaining} == {1, 2, 4}
    unchanged, stats = graph_clean.remove_graph_spurs(
        cast(Any, [edges[0]]),
        degrees,
        1.0,
        graph_clean.GraphCleanConfig(spur_length_mm=2.0, min_main_edge_length_mm=2.0),
    )
    assert unchanged and stats["removed_count"] == 0


def test_pair_and_assemble_cover_short_angle_used_and_orientation_branches() -> None:
    node = (5, 5)
    edges = [
        _edge(0, node, (1, 5), [node, (4, 5), (3, 5), (2, 5), (1, 5)]),
        _edge(1, node, (9, 5), [node, (6, 5), (7, 5), (8, 5), (9, 5)]),
        _edge(2, node, (5, 6), [node, (5, 6)]),
    ]
    pairs, count = graph_clean.pair_through_junction_edges(
        cast(Any, edges),
        {node: 3},
        1.0,
        graph_clean.GraphCleanConfig(min_main_edge_length_mm=2.0, through_angle_deg=150.0),
    )
    assert count == 1 and pairs[(node, 0)] == 1
    paths = graph_clean.assemble_graph_paths(cast(Any, edges), pairs)
    assert paths and len(paths[0]) > len(edges[0]["points"])
    assert graph_clean.orient_edge_points(edges[0], node) == edges[0]["points"]
    assert graph_clean.orient_edge_points(edges[0], (1, 5)) == list(reversed(edges[0]["points"]))
    assert graph_clean.orient_edge_points(edges[0], (99, 99)) == edges[0]["points"]
    assert graph_clean.edge_other_node(edges[0], node) == (1, 5)
    assert graph_clean.edge_other_node(edges[0], (1, 5)) == node
    assert graph_clean.edge_direction_from_node(_edge(9, node, node, [node]), node) == (0.0, 0.0)
    assert graph_clean.choose_graph_path_start(edges[0], {}) == node
    assert graph_clean.choose_graph_path_start(edges[0], {(node, 0): 1}) == (1, 5)
    assert graph_clean.choose_graph_path_start(edges[0], {(node, 0): 1, ((1, 5), 0): 1}) == node


def test_graph_small_helpers_cover_invalid_records_and_incident_endpoints() -> None:
    edge = _edge(0, (0, 0), (1, 0), [(0, 0), (1, 0)])
    incident = graph_clean.graph_incident_edges(cast(Any, [edge]))
    assert set(incident) == {(0, 0), (1, 0)}
    assert graph_clean._record_points({"points_mm": "bad"}) == ()
    assert graph_clean._record_points({"points_mm": [[0, 0], [1, 1]]}) == ((0.0, 0.0), (1.0, 1.0))
    assert graph_clean._record_length({"length_mm": True}) == 0.0
    assert graph_clean._record_length({"length_mm": "bad"}) == 0.0
    assert graph_clean._record_length({"length_mm": 1.25}) == 1.25

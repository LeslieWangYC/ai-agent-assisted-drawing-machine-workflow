from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TypedDict, cast

from drawingmachine.domain.planning.geometry import points_px_to_mm, polyline_length
from drawingmachine.domain.planning.imaging import connected_components, neighbors8
from drawingmachine.domain.planning.models import BinarizedImage, PathPlanConfig, Pixel, Point
from drawingmachine.domain.planning.strokes.dedupe import angle_between, normalize_direction
from drawingmachine.domain.planning.strokes.postprocess import path_to_record
from drawingmachine.json_types import JsonObject

status = "experimental_not_default"


@dataclass(frozen=True, slots=True)
class GraphCleanConfig:
    spur_length_mm: float = 1.0
    min_main_edge_length_mm: float = 2.0
    through_angle_deg: float = 150.0


class _GraphEdge(TypedDict):
    id: int
    start: Pixel
    end: Pixel
    points: list[Pixel]
    length_px: float


def build_graph_clean_stroke_records(
    skeleton: set[Pixel] | frozenset[Pixel],
    width: int,
    height: int,
    scale: float,
    offset_x_mm: float,
    offset_y_mm: float,
    config: PathPlanConfig,
    graph_config: GraphCleanConfig,
) -> tuple[list[JsonObject], JsonObject]:
    paths_px, metrics = graph_clean_skeleton_paths(skeleton, width, height, scale, graph_config)
    records: list[JsonObject] = []
    for index, path in enumerate(paths_px, start=1):
        record = path_to_record(
            f"graph_stroke_{index:04d}",
            "skeleton_graph_clean",
            points_px_to_mm(tuple(path), scale, offset_x_mm, offset_y_mm),
            config.simplify_tolerance_mm,
            config.min_path_length_mm,
        )
        if record is not None:
            records.append(record)
    metrics.update(
        {
            "graph_clean_record_count": len(records),
            "graph_clean_length_mm": round(sum(polyline_length(_record_points(path)) for path in records), 3),
            "graph_clean_short_record_count_lt_1mm": sum(1 for path in records if _record_length(path) < 1.0),
            "graph_clean_short_record_count_lt_2mm": sum(1 for path in records if _record_length(path) < 2.0),
        }
    )
    return records, metrics


def graph_clean_skeleton_paths(
    skeleton: set[Pixel] | frozenset[Pixel],
    width: int,
    height: int,
    scale: float,
    graph_config: GraphCleanConfig,
) -> tuple[list[list[Pixel]], JsonObject]:
    edges, loop_paths, node_degrees = skeleton_graph_edges(skeleton, width, height)
    remaining_edges, spur_stats = remove_graph_spurs(edges, node_degrees, scale, graph_config)
    pair_map, through_merge_count = pair_through_junction_edges(remaining_edges, node_degrees, scale, graph_config)
    paths = assemble_graph_paths(remaining_edges, pair_map)
    paths.extend(loop_paths)
    endpoint_count = sum(1 for degree in node_degrees.values() if degree <= 1)
    junction_count = sum(1 for degree in node_degrees.values() if degree > 2)
    return paths, cast(
        JsonObject,
        {
            "graph_node_count": len(node_degrees),
            "graph_endpoint_count": endpoint_count,
            "graph_junction_count": junction_count,
            "graph_edge_count": len(edges),
            "graph_loop_path_count": len(loop_paths),
            "graph_spur_removed_count": spur_stats["removed_count"],
            "graph_spur_removed_length_mm": spur_stats["removed_length_mm"],
            "graph_through_merge_count": through_merge_count,
            "graph_path_count_before_record_filter": len(paths),
        },
    )


def skeleton_graph_edges(
    skeleton: set[Pixel] | frozenset[Pixel],
    width: int,
    height: int,
) -> tuple[list[_GraphEdge], list[list[Pixel]], dict[Pixel, int]]:
    if not skeleton:
        return [], [], {}
    neighbor_map = {
        pixel: sorted(neighbor for neighbor in neighbors8(pixel[0], pixel[1], width, height) if neighbor in skeleton)
        for pixel in skeleton
    }
    node_degrees = {
        pixel: skeleton_neighbor_group_count(pixel, adjacent)
        for pixel, adjacent in neighbor_map.items()
        if skeleton_neighbor_group_count(pixel, adjacent) != 2
    }
    node_pixels = set(node_degrees)
    visited_edges: set[frozenset[Pixel]] = set()
    edges: list[_GraphEdge] = []

    for node in sorted(node_pixels):
        for neighbor in neighbor_map[node]:
            edge_key = frozenset((node, neighbor))
            if edge_key in visited_edges:
                continue
            path = [node]
            previous = node
            current = neighbor
            visited_edges.add(edge_key)
            while True:
                path.append(current)
                if current in node_pixels:
                    break
                candidates = [point for point in neighbor_map[current] if point != previous]
                if not candidates:
                    break
                next_pixel = _choose_next_skeleton_step(previous, current, candidates)
                edge_key = frozenset((current, next_pixel))
                if edge_key in visited_edges:
                    break
                visited_edges.add(edge_key)
                previous, current = current, next_pixel
            if len(path) >= 2:
                edges.append(
                    {
                        "id": len(edges),
                        "start": path[0],
                        "end": path[-1],
                        "points": path,
                        "length_px": polyline_length(tuple(path)),
                    }
                )

    loop_paths: list[list[Pixel]] = []
    image = BinarizedImage(frozenset(skeleton), width, height, 0, False)
    for component in connected_components(image):
        pixels = set(component.pixels)
        if node_pixels & pixels:
            continue
        loop = trace_simple_loop_component(pixels, width, height)
        if len(loop) >= 2:
            loop_paths.append(loop)
    return edges, loop_paths, node_degrees


def skeleton_neighbor_group_count(pixel: Pixel, adjacent: list[Pixel]) -> int:
    pixel_x, pixel_y = pixel
    occupied = set(adjacent)
    ring = [
        (pixel_x, pixel_y - 1),
        (pixel_x + 1, pixel_y - 1),
        (pixel_x + 1, pixel_y),
        (pixel_x + 1, pixel_y + 1),
        (pixel_x, pixel_y + 1),
        (pixel_x - 1, pixel_y + 1),
        (pixel_x - 1, pixel_y),
        (pixel_x - 1, pixel_y - 1),
    ]
    values = [point in occupied for point in ring]
    return sum(
        (not current) and next_value for current, next_value in zip(values, values[1:] + values[:1], strict=True)
    )


def trace_simple_loop_component(pixels: set[Pixel], width: int, height: int) -> list[Pixel]:
    neighbor_map = {
        pixel: sorted(neighbor for neighbor in neighbors8(pixel[0], pixel[1], width, height) if neighbor in pixels)
        for pixel in pixels
    }
    start = sorted(pixels)[0]
    if not neighbor_map[start]:
        return []
    path = [start]
    previous = start
    current = neighbor_map[start][0]
    visited_edges = {frozenset((previous, current))}
    while True:
        path.append(current)
        if current == start:
            break
        candidates = [point for point in neighbor_map[current] if point != previous]
        if not candidates:
            break
        next_pixel = candidates[0]
        edge_key = frozenset((current, next_pixel))
        if edge_key in visited_edges:
            break
        visited_edges.add(edge_key)
        previous, current = current, next_pixel
    return path


def remove_graph_spurs(
    edges: list[_GraphEdge],
    node_degrees: dict[Pixel, int],
    scale: float,
    graph_config: GraphCleanConfig,
) -> tuple[list[_GraphEdge], JsonObject]:
    spur_limit_px = graph_config.spur_length_mm / max(scale, 0.000001)
    main_limit_px = graph_config.min_main_edge_length_mm / max(scale, 0.000001)
    incident = graph_incident_edges(edges)
    remove_ids: set[int] = set()
    removed_length_px = 0.0
    for edge in edges:
        start = edge["start"]
        end = edge["end"]
        start_degree = node_degrees.get(start, 2)
        end_degree = node_degrees.get(end, 2)
        if edge["length_px"] >= spur_limit_px:
            continue
        if start_degree <= 1 and end_degree > 2:
            junction = end
        elif end_degree <= 1 and start_degree > 2:
            junction = start
        else:
            continue
        main_edges = [
            other
            for other in incident.get(junction, [])
            if other["id"] != edge["id"] and other["length_px"] >= main_limit_px
        ]
        if len(main_edges) >= 2:
            remove_ids.add(edge["id"])
            removed_length_px += edge["length_px"]
    return [edge for edge in edges if edge["id"] not in remove_ids], cast(
        JsonObject,
        {
            "removed_count": len(remove_ids),
            "removed_length_mm": round(removed_length_px * scale, 3),
        },
    )


def pair_through_junction_edges(
    edges: list[_GraphEdge],
    node_degrees: dict[Pixel, int],
    scale: float,
    graph_config: GraphCleanConfig,
) -> tuple[dict[tuple[Pixel, int], int], int]:
    main_limit_px = graph_config.min_main_edge_length_mm / max(scale, 0.000001)
    incident = graph_incident_edges(edges)
    pair_map: dict[tuple[Pixel, int], int] = {}
    merge_count = 0
    for node, node_edges in incident.items():
        if node_degrees.get(node, 2) <= 2:
            continue
        candidates: list[tuple[float, int, int]] = []
        for first_index, first in enumerate(node_edges):
            if first["length_px"] < main_limit_px:
                continue
            first_direction = edge_direction_from_node(first, node)
            for second in node_edges[first_index + 1 :]:
                if second["length_px"] < main_limit_px:
                    continue
                angle = angle_between(first_direction, edge_direction_from_node(second, node))
                if angle >= graph_config.through_angle_deg:
                    candidates.append((angle, first["id"], second["id"]))
        used: set[int] = set()
        for _, first_id, second_id in sorted(candidates, reverse=True):
            if first_id in used or second_id in used:
                continue
            pair_map[(node, first_id)] = second_id
            pair_map[(node, second_id)] = first_id
            used.add(first_id)
            used.add(second_id)
            merge_count += 1
    return pair_map, merge_count


def assemble_graph_paths(
    edges: list[_GraphEdge],
    pair_map: dict[tuple[Pixel, int], int],
) -> list[list[Pixel]]:
    edges_by_id = {edge["id"]: edge for edge in edges}
    visited: set[int] = set()
    paths: list[list[Pixel]] = []
    for edge in sorted(edges, key=lambda item: item["length_px"], reverse=True):
        if edge["id"] in visited:
            continue
        start_node = choose_graph_path_start(edge, pair_map)
        points: list[Pixel] = []
        current_edge = edge
        current_start = start_node
        while current_edge["id"] not in visited:
            oriented = orient_edge_points(current_edge, current_start)
            if points:
                points.extend(oriented[1:])
            else:
                points.extend(oriented)
            visited.add(current_edge["id"])
            exit_node = edge_other_node(current_edge, current_start)
            next_edge_id = pair_map.get((exit_node, current_edge["id"]))
            if next_edge_id is None or next_edge_id in visited:
                break
            current_edge = edges_by_id[next_edge_id]
            current_start = exit_node
        if len(points) >= 2:
            paths.append(points)
    return paths


def graph_incident_edges(edges: list[_GraphEdge]) -> dict[Pixel, list[_GraphEdge]]:
    incident: dict[Pixel, list[_GraphEdge]] = {}
    for edge in edges:
        incident.setdefault(edge["start"], []).append(edge)
        incident.setdefault(edge["end"], []).append(edge)
    return incident


def edge_direction_from_node(edge: _GraphEdge, node: Pixel) -> Point:
    points = orient_edge_points(edge, node)
    if len(points) < 2:
        return 0.0, 0.0
    outer = points[min(len(points) - 1, 4)]
    return normalize_direction(outer[0] - node[0], outer[1] - node[1])


def orient_edge_points(edge: _GraphEdge, start_node: Pixel) -> list[Pixel]:
    points = edge["points"]
    if edge["start"] == start_node:
        return points
    if edge["end"] == start_node:
        return list(reversed(points))
    return points


def edge_other_node(edge: _GraphEdge, node: Pixel) -> Pixel:
    if edge["start"] == node:
        return edge["end"]
    return edge["start"]


def choose_graph_path_start(edge: _GraphEdge, pair_map: dict[tuple[Pixel, int], int]) -> Pixel:
    start = edge["start"]
    end = edge["end"]
    edge_id = edge["id"]
    if (start, edge_id) not in pair_map:
        return start
    if (end, edge_id) not in pair_map:
        return end
    return start


def _choose_next_skeleton_step(previous: Pixel, current: Pixel, candidates: list[Pixel]) -> Pixel:
    delta_x = current[0] - previous[0]
    delta_y = current[1] - previous[1]
    previous_length = math.hypot(delta_x, delta_y) or 1.0

    def score(point: Pixel) -> tuple[float, float]:
        next_delta_x = point[0] - current[0]
        next_delta_y = point[1] - current[1]
        next_length = math.hypot(next_delta_x, next_delta_y) or 1.0
        dot = (delta_x * next_delta_x + delta_y * next_delta_y) / (previous_length * next_length)
        cross = abs(delta_x * next_delta_y - delta_y * next_delta_x)
        return dot, -cross

    return max(candidates, key=score)


def _record_points(record: JsonObject) -> tuple[Point, ...]:
    points = record.get("points_mm")
    if not isinstance(points, list):
        return ()
    return tuple((float(point[0]), float(point[1])) for point in cast(list[list[float]], points))


def _record_length(record: JsonObject) -> float:
    length = record.get("length_mm")
    if isinstance(length, bool) or not isinstance(length, int | float):
        return 0.0
    return float(length)


__all__ = [
    "GraphCleanConfig",
    "assemble_graph_paths",
    "build_graph_clean_stroke_records",
    "edge_direction_from_node",
    "edge_other_node",
    "graph_clean_skeleton_paths",
    "graph_incident_edges",
    "orient_edge_points",
    "pair_through_junction_edges",
    "remove_graph_spurs",
    "skeleton_graph_edges",
    "skeleton_neighbor_group_count",
    "status",
    "trace_simple_loop_component",
]

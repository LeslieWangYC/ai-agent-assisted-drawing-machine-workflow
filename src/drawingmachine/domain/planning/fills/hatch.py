from __future__ import annotations

import math
from typing import cast

from drawingmachine.domain.planning.fills.boundaries import neighbors4
from drawingmachine.domain.planning.fills.regions import fill_candidate_pixels
from drawingmachine.domain.planning.geometry import points_px_to_mm, simplify_polyline
from drawingmachine.domain.planning.imaging import connected_components, neighbors8
from drawingmachine.domain.planning.models import BinarizedImage, PathPlanConfig, Pixel
from drawingmachine.domain.planning.strokes.postprocess import path_to_record
from drawingmachine.json_types import JsonObject

PixelPath = tuple[Pixel, ...]
BoundaryGraph = dict[Pixel, tuple[Pixel, ...]]


def serpentine_fill_paths(
    foreground: frozenset[Pixel],
    width: int,
    height: int,
    mm_per_px: float,
    hatch_spacing_mm: float,
    min_run_mm: float,
    min_thickness_mm: float,
) -> tuple[PixelPath, ...]:
    candidates = fill_candidate_pixels(
        foreground,
        width,
        height,
        mm_per_px,
        min_run_mm,
        min_thickness_mm,
    )
    if not candidates:
        return ()

    spacing_px = max(1, round(hatch_spacing_mm / mm_per_px))
    min_run_px = max(2, round(min_run_mm / mm_per_px))
    all_paths: list[PixelPath] = []
    image = BinarizedImage(candidates, width, height, 0, False)

    for component in connected_components(image):
        pixels = component.pixels
        if len(pixels) < 8:
            continue

        boundary_graph = _build_boundary_graph(pixels, width, height)
        max_boundary_distance_px = spacing_px * 3
        runs_by_y: dict[int, list[tuple[int, int]]] = {}
        for y_position in range(component.min_y, component.max_y + 1, spacing_px):
            x_position = component.min_x
            while x_position <= component.max_x:
                while x_position <= component.max_x and (x_position, y_position) not in pixels:
                    x_position += 1
                start = x_position
                while x_position <= component.max_x and (x_position, y_position) in pixels:
                    x_position += 1
                end = x_position - 1
                if end - start + 1 >= min_run_px:
                    runs_by_y.setdefault(y_position, []).append((start, end))

        if not runs_by_y:
            continue

        sorted_y_positions = sorted(runs_by_y)
        active_paths: list[list[tuple[int, int, int]]] = []
        for y_index, y_position in enumerate(sorted_y_positions):
            runs = runs_by_y[y_position]
            if y_index == 0:
                for start, end in runs:
                    active_paths.append([(y_position, start, end)])
                continue

            previous_y = sorted_y_positions[y_index - 1]
            matched: set[int] = set()
            for start, end in runs:
                best_index: int | None = None
                best_overlap = -1
                for path_index, path in enumerate(active_paths):
                    if path_index in matched:
                        continue
                    previous_run = path[-1]
                    _, previous_start, previous_end = previous_run
                    if previous_run[0] != previous_y:
                        continue
                    overlap = max(0, min(end, previous_end) - max(start, previous_start))
                    if overlap <= 0:
                        continue
                    connected = False
                    for reverse_previous in (False, True):
                        for reverse_next in (False, True):
                            previous_point = (
                                (previous_end, previous_y) if not reverse_previous else (previous_start, previous_y)
                            )
                            next_point = (start, y_position) if not reverse_next else (end, y_position)
                            if (
                                connect_scanline_endpoints(
                                    previous_point,
                                    next_point,
                                    boundary_graph,
                                    pixels,
                                    spacing_px=spacing_px,
                                    max_boundary_distance_px=max_boundary_distance_px,
                                )
                                is not None
                            ):
                                if overlap > best_overlap:
                                    best_overlap = overlap
                                    best_index = path_index
                                connected = True
                                break
                        if connected:
                            break

                if best_index is not None:
                    matched.add(best_index)
                    active_paths[best_index].append((y_position, start, end))
                else:
                    active_paths.append([(y_position, start, end)])

        for path in active_paths:
            if len(path) == 1:
                y_position, start, end = path[0]
                all_paths.append(((start, y_position), (end, y_position)))
                continue

            points: list[Pixel] = []
            for run_index, (y_position, start, end) in enumerate(path):
                left_to_right = run_index % 2 == 0
                if not points:
                    points.append((start, y_position) if left_to_right else (end, y_position))
                else:
                    previous = points[-1]
                    run_start = (start, y_position) if left_to_right else (end, y_position)
                    if y_position == previous[1] + spacing_px:
                        connection = connect_scanline_endpoints(
                            previous,
                            run_start,
                            boundary_graph,
                            pixels,
                            spacing_px=spacing_px,
                            max_boundary_distance_px=max_boundary_distance_px,
                        )
                        if connection:
                            points.extend(connection[1:])
                        else:
                            all_paths.append(tuple(points))
                            points = [run_start]
                    else:
                        all_paths.append(tuple(points))
                        points = [run_start]
                run_end = (end, y_position) if left_to_right else (start, y_position)
                if points[-1] != run_end:
                    points.append(run_end)
            if len(points) >= 2:
                all_paths.append(tuple(points))

    return tuple(all_paths)


def connect_scanline_endpoints(
    previous_end: Pixel,
    next_start: Pixel,
    boundary_graph: BoundaryGraph,
    region_pixels: frozenset[Pixel],
    *,
    spacing_px: int,
    max_boundary_distance_px: int,
) -> PixelPath | None:
    boundary_connection = _connect_via_boundary(
        previous_end,
        next_start,
        boundary_graph,
        region_pixels,
        spacing_px,
        max_boundary_distance_px,
    )
    if boundary_connection is not None:
        return boundary_connection
    return _connect_via_arc(previous_end, next_start, region_pixels, spacing_px)


def _connect_via_arc(
    previous_end: Pixel,
    next_start: Pixel,
    region_pixels: frozenset[Pixel],
    spacing_px: int,
) -> PixelPath | None:
    start_x, start_y = previous_end
    end_x, end_y = next_start
    delta_y = end_y - start_y
    if delta_y <= 0:
        return None

    delta_x = end_x - start_x
    number_of_steps = max(2, delta_y)
    for bulge_scale in (1.0, 0.5, 0.25):
        for bulge_direction in (1, -1):
            fits = True
            arc_points: list[Pixel] = []
            for step in range(1, number_of_steps):
                fraction = step / number_of_steps
                base_x = start_x + delta_x * fraction
                base_y = start_y + delta_y * fraction
                bulge = math.sin(fraction * math.pi) * spacing_px * bulge_scale * bulge_direction
                point = round(base_x + bulge), round(base_y)
                if point not in region_pixels:
                    fits = False
                    break
                arc_points.append(point)
            if fits:
                return (previous_end, *arc_points, next_start)
    return None


def _connect_via_boundary(
    previous_end: Pixel,
    next_start: Pixel,
    boundary_graph: BoundaryGraph,
    region_pixels: frozenset[Pixel],
    spacing_px: int,
    max_distance_px: int,
) -> PixelPath | None:
    del region_pixels
    if previous_end not in boundary_graph:
        snapped_previous = _nearest_boundary_point(previous_end, boundary_graph, max(1, spacing_px // 2))
        if snapped_previous is None:
            return None
        previous_end = snapped_previous
    if next_start not in boundary_graph:
        snapped_next = _nearest_boundary_point(next_start, boundary_graph, max(1, spacing_px // 2))
        if snapped_next is None:
            return None
        next_start = snapped_next
    if previous_end == next_start:
        return previous_end, next_start

    queue: list[tuple[Pixel, PixelPath]] = [(previous_end, (previous_end,))]
    visited = {previous_end}
    boundary_path: PixelPath | None = None
    while queue:
        position, path = queue.pop(0)
        if len(path) > max_distance_px:
            return None
        if position == next_start:
            boundary_path = path
            break
        for neighbor in boundary_graph.get(position, ()):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, (*path, neighbor)))
    if boundary_path is None:
        return None

    simplified = simplify_polyline(
        tuple((float(x_position), float(y_position)) for x_position, y_position in boundary_path),
        tolerance_mm=2.0,
    )
    if len(simplified) < 2:
        return None
    result = tuple((round(x_position), round(y_position)) for x_position, y_position in simplified)
    if not _path_near_boundary(result, boundary_graph, tolerance=4):
        return None
    return result


def _path_near_boundary(path: PixelPath, boundary_graph: BoundaryGraph, tolerance: int = 4) -> bool:
    boundary_set = set(boundary_graph)
    for point in path:
        found = False
        for delta_x in range(-tolerance, tolerance + 1):
            for delta_y in range(-tolerance, tolerance + 1):
                if (point[0] + delta_x, point[1] + delta_y) in boundary_set:
                    found = True
                    break
            if found:
                break
        if not found:
            return False
    return True


def _build_boundary_graph(
    component_pixels: frozenset[Pixel],
    width: int,
    height: int,
) -> BoundaryGraph:
    boundary = frozenset(
        (x_position, y_position)
        for x_position, y_position in component_pixels
        if any(
            (neighbor_x, neighbor_y) not in component_pixels
            for neighbor_x, neighbor_y in neighbors4(x_position, y_position, width, height)
        )
    )
    graph: BoundaryGraph = {}
    for pixel in boundary:
        graph[pixel] = tuple(
            neighbor for neighbor in neighbors8(pixel[0], pixel[1], width, height) if neighbor in boundary
        )
    return graph


def _nearest_boundary_point(
    point: Pixel,
    boundary_graph: BoundaryGraph,
    max_search_radius: int = 3,
) -> Pixel | None:
    point_x, point_y = point
    for radius in range(1, max_search_radius + 1):
        for delta_y in range(-radius, radius + 1):
            for delta_x in range(-radius, radius + 1):
                candidate = point_x + delta_x, point_y + delta_y
                if candidate in boundary_graph:
                    return candidate
    return None


def build_fill_records(
    paths_px: tuple[PixelPath, ...],
    scale: float,
    offset_x_mm: float,
    offset_y_mm: float,
    config: PathPlanConfig,
) -> list[JsonObject]:
    records: list[JsonObject] = []
    for index, path in enumerate(paths_px, start=1):
        record = path_to_record(
            f"fill_{index:04d}",
            "hatch",
            points_px_to_mm(path, scale, offset_x_mm, offset_y_mm),
            0.0,
            config.min_path_length_mm,
            extra=cast(JsonObject, {"spacing_mm": config.hatch_spacing_mm}),
        )
        if record is not None:
            records.append(record)
    return records


__all__ = ["build_fill_records", "connect_scanline_endpoints", "serpentine_fill_paths"]

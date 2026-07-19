from __future__ import annotations

from typing import cast

from drawingmachine.domain.planning.fills.regions import fill_candidate_pixels, hatch_ids_for_region
from drawingmachine.domain.planning.geometry import points_px_to_mm
from drawingmachine.domain.planning.imaging import connected_components, neighbors8
from drawingmachine.domain.planning.models import BinarizedImage, PathPlanConfig, Pixel
from drawingmachine.domain.planning.strokes.postprocess import path_to_record
from drawingmachine.json_types import JsonObject

PixelPath = tuple[Pixel, ...]


def build_fill_regions(
    foreground: frozenset[Pixel],
    width: int,
    height: int,
    scale: float,
    offset_x_mm: float,
    offset_y_mm: float,
    config: PathPlanConfig,
    fill_paths: list[JsonObject],
) -> tuple[list[JsonObject], list[JsonObject]]:
    regions: list[JsonObject] = []
    boundary_records: list[JsonObject] = []
    boundary_index = 1
    candidate_pixels = fill_candidate_pixels(
        foreground,
        width,
        height,
        scale,
        config.hatch_min_run_mm,
        config.fill_min_thickness_mm,
    )
    image = BinarizedImage(candidate_pixels, width, height, 0, False)
    for region_index, component in enumerate(connected_components(image), start=1):
        if component.area_px < config.min_component_area_px:
            continue

        boundary_paths_px = boundary_paths_for_component(component.pixels, width, height)
        region_boundary_ids: list[str] = []
        for path_px in boundary_paths_px:
            record = path_to_record(
                f"fill_boundary_{boundary_index:04d}",
                "fill_boundary",
                points_px_to_mm(path_px, scale, offset_x_mm, offset_y_mm),
                config.simplify_tolerance_mm,
                config.min_path_length_mm,
                extra=cast(JsonObject, {"region_id": f"fill_region_{region_index:04d}"}),
            )
            if record is not None:
                boundary_records.append(record)
                record_id = record["id"]
                if isinstance(record_id, str):
                    region_boundary_ids.append(record_id)
                boundary_index += 1

        regions.append(
            cast(
                JsonObject,
                {
                    "id": f"fill_region_{region_index:04d}",
                    "boundary_path_ids": region_boundary_ids,
                    "hatch_path_ids": hatch_ids_for_region(
                        fill_paths,
                        component.min_x,
                        component.min_y,
                        component.max_x,
                        component.max_y,
                        scale,
                        offset_x_mm,
                        offset_y_mm,
                    ),
                    "area_px": component.area_px,
                    "area_mm2": round(component.area_px * scale * scale, 4),
                    "bbox_px": [component.min_x, component.min_y, component.max_x, component.max_y],
                    "bbox_mm": [round(component.width_px * scale, 3), round(component.height_px * scale, 3)],
                    "estimated_avg_width_mm": round(min(component.width_px, component.height_px) * scale, 3),
                    "fill_method": "hatch_plus_boundary",
                },
            )
        )
    return regions, boundary_records


def boundary_paths_for_component(
    pixels: frozenset[Pixel],
    width: int,
    height: int,
) -> tuple[PixelPath, ...]:
    boundary = frozenset(
        (x_position, y_position)
        for x_position, y_position in pixels
        if any(
            (neighbor_x, neighbor_y) not in pixels
            for neighbor_x, neighbor_y in neighbors4(x_position, y_position, width, height)
        )
    )
    if not boundary:
        return ()
    return _trace_boundary_pixels(boundary, width, height)


def neighbors4(x_position: int, y_position: int, width: int, height: int) -> tuple[Pixel, ...]:
    neighbors: list[Pixel] = []
    for delta_x, delta_y in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        neighbor_x = x_position + delta_x
        neighbor_y = y_position + delta_y
        if 0 <= neighbor_x < width and 0 <= neighbor_y < height:
            neighbors.append((neighbor_x, neighbor_y))
    return tuple(neighbors)


def _trace_boundary_pixels(
    boundary: frozenset[Pixel],
    width: int,
    height: int,
) -> tuple[PixelPath, ...]:
    neighbor_map = {
        pixel: tuple(neighbor for neighbor in neighbors8(pixel[0], pixel[1], width, height) if neighbor in boundary)
        for pixel in boundary
    }
    visited_edges: set[frozenset[Pixel]] = set()
    paths: list[PixelPath] = []
    starts = [pixel for pixel, adjacent in neighbor_map.items() if len(adjacent) != 2]
    starts.extend(pixel for pixel in boundary if pixel not in starts)
    for start in starts:
        for next_pixel in neighbor_map[start]:
            edge = frozenset((start, next_pixel))
            if edge in visited_edges:
                continue
            path = [start]
            previous = start
            current = next_pixel
            visited_edges.add(edge)
            while True:
                path.append(current)
                adjacent = neighbor_map[current]
                candidates = [point for point in adjacent if point != previous]
                unvisited = [point for point in candidates if frozenset((current, point)) not in visited_edges]
                if len(adjacent) != 2 or not unvisited:
                    break
                following = _choose_next_boundary_step(previous, current, unvisited)
                visited_edges.add(frozenset((current, following)))
                previous, current = current, following
                if current == start:
                    path.append(current)
                    break
            if len(path) >= 2:
                paths.append(tuple(path))
    return tuple(paths)


def _choose_next_boundary_step(previous: Pixel, current: Pixel, candidates: list[Pixel]) -> Pixel:
    if len(candidates) == 1:
        return candidates[0]
    delta_x = current[0] - previous[0]
    delta_y = current[1] - previous[1]
    return min(
        candidates,
        key=lambda point: abs((point[0] - current[0]) - delta_x) + abs((point[1] - current[1]) - delta_y),
    )


__all__ = ["boundary_paths_for_component", "build_fill_regions", "neighbors4"]

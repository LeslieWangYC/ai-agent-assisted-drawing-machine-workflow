from __future__ import annotations

import math

from drawingmachine.domain.planning.imaging import connected_components, neighbors8
from drawingmachine.domain.planning.models import BinarizedImage, Pixel

PixelPath = tuple[Pixel, ...]


def trace_skeleton_paths(
    skeleton: frozenset[Pixel],
    width: int,
    height: int,
) -> tuple[PixelPath, ...]:
    if not skeleton:
        return ()
    neighbor_map = {
        pixel: tuple(
            sorted(neighbor for neighbor in neighbors8(pixel[0], pixel[1], width, height) if neighbor in skeleton)
        )
        for pixel in skeleton
    }
    visited_edges: set[frozenset[Pixel]] = set()
    paths: list[PixelPath] = []
    endpoints = sorted(pixel for pixel, adjacent in neighbor_map.items() if len(adjacent) <= 1)
    junctions = sorted(pixel for pixel, adjacent in neighbor_map.items() if len(adjacent) > 2)
    regular = sorted(pixel for pixel, adjacent in neighbor_map.items() if len(adjacent) == 2)
    starts = endpoints + junctions + regular

    for start in starts:
        while True:
            start_candidates = [
                point for point in neighbor_map[start] if frozenset((start, point)) not in visited_edges
            ]
            if not start_candidates:
                break
            path = [start]
            previous: Pixel | None = None
            current = start
            while True:
                candidates = [
                    point for point in neighbor_map[current] if frozenset((current, point)) not in visited_edges
                ]
                if not candidates:
                    break
                next_pixel = candidates[0] if previous is None else _choose_next_step(previous, current, candidates)
                visited_edges.add(frozenset((current, next_pixel)))
                previous, current = current, next_pixel
                path.append(current)
                if current == start:
                    break
            if len(path) >= 2:
                paths.append(tuple(path))
    return tuple(paths)


def continuous_skeleton_base_paths(
    skeleton: frozenset[Pixel],
    width: int,
    height: int,
) -> tuple[PixelPath, ...]:
    image = BinarizedImage(skeleton, width, height, 0, False)
    paths: list[PixelPath] = []
    for component in connected_components(image):
        pixels = component.pixels
        if len(pixels) < 2:
            continue
        neighbor_map = {
            pixel: tuple(
                sorted(neighbor for neighbor in neighbors8(pixel[0], pixel[1], width, height) if neighbor in pixels)
            )
            for pixel in pixels
        }
        endpoints = [pixel for pixel, adjacent in neighbor_map.items() if len(adjacent) <= 1]
        start = sorted(endpoints or pixels)[0]
        visited_edges: set[frozenset[Pixel]] = set()
        path = [start]
        _walk_depth_first(start, neighbor_map, visited_edges, path)
        if len(path) >= 2:
            paths.append(tuple(path))
    return tuple(paths)


def _walk_depth_first(
    current: Pixel,
    neighbor_map: dict[Pixel, tuple[Pixel, ...]],
    visited_edges: set[frozenset[Pixel]],
    path: list[Pixel],
) -> None:
    stack: list[tuple[Pixel, int]] = [(current, 0)]
    while stack:
        node, next_index = stack[-1]
        neighbors = neighbor_map[node]
        advanced = False
        while next_index < len(neighbors):
            neighbor = neighbors[next_index]
            next_index += 1
            edge = frozenset((node, neighbor))
            if edge in visited_edges:
                continue
            stack[-1] = (node, next_index)
            visited_edges.add(edge)
            path.append(neighbor)
            stack.append((neighbor, 0))
            advanced = True
            break
        if advanced:
            continue
        stack.pop()
        if stack:
            path.append(stack[-1][0])


def _choose_next_step(previous: Pixel, current: Pixel, candidates: list[Pixel]) -> Pixel:
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


__all__ = ["continuous_skeleton_base_paths", "trace_skeleton_paths"]

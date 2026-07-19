from __future__ import annotations

from drawingmachine.domain.planning.models import BinarizedImage, ConnectedComponent, Pixel


def connected_components(image: BinarizedImage) -> tuple[ConnectedComponent, ...]:
    unvisited = set(image.foreground)
    components: list[ConnectedComponent] = []
    while unvisited:
        start = unvisited.pop()
        stack = [start]
        pixels = {start}
        min_x = max_x = start[0]
        min_y = max_y = start[1]
        while stack:
            current_x, current_y = stack.pop()
            min_x = min(min_x, current_x)
            max_x = max(max_x, current_x)
            min_y = min(min_y, current_y)
            max_y = max(max_y, current_y)
            for neighbor in neighbors8(current_x, current_y, image.width, image.height):
                if neighbor in unvisited:
                    unvisited.remove(neighbor)
                    pixels.add(neighbor)
                    stack.append(neighbor)
        components.append(
            ConnectedComponent(
                pixels=frozenset(pixels),
                area_px=len(pixels),
                min_x=min_x,
                min_y=min_y,
                max_x=max_x,
                max_y=max_y,
                width_px=max_x - min_x + 1,
                height_px=max_y - min_y + 1,
            )
        )
    return tuple(components)


def neighbors8(x: int, y: int, width: int, height: int) -> tuple[Pixel, ...]:
    values: list[Pixel] = []
    for delta_y in (-1, 0, 1):
        for delta_x in (-1, 0, 1):
            if delta_x == 0 and delta_y == 0:
                continue
            neighbor_x = x + delta_x
            neighbor_y = y + delta_y
            if 0 <= neighbor_x < width and 0 <= neighbor_y < height:
                values.append((neighbor_x, neighbor_y))
    return tuple(values)


def remove_small_components(image: BinarizedImage, *, min_area: int) -> BinarizedImage:
    if min_area <= 1:
        return image
    kept: set[Pixel] = set()
    for component in connected_components(image):
        if component.area_px >= min_area:
            kept.update(component.pixels)
    return BinarizedImage(frozenset(kept), image.width, image.height, image.threshold, image.inverted)


__all__ = ["connected_components", "neighbors8", "remove_small_components"]

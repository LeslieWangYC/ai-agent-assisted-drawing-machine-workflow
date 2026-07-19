from __future__ import annotations

from drawingmachine.domain.planning.models import BinarizedImage, Pixel


def thin(image: BinarizedImage, *, max_iterations: int = 500) -> BinarizedImage:
    result = {pixel for pixel in image.foreground if 0 < pixel[0] < image.width - 1 and 0 < pixel[1] < image.height - 1}
    for _ in range(max_iterations):
        changed = False
        for step in (0, 1):
            to_remove: set[Pixel] = set()
            for x, y in result:
                p2 = (x, y - 1) in result
                p3 = (x + 1, y - 1) in result
                p4 = (x + 1, y) in result
                p5 = (x + 1, y + 1) in result
                p6 = (x, y + 1) in result
                p7 = (x - 1, y + 1) in result
                p8 = (x - 1, y) in result
                p9 = (x - 1, y - 1) in result
                values = [p2, p3, p4, p5, p6, p7, p8, p9]
                count = sum(values)
                if count < 2 or count > 6:
                    continue
                transitions = sum(
                    (not current) and following
                    for current, following in zip(values, values[1:] + values[:1], strict=True)
                )
                if transitions != 1:
                    continue
                keep = (
                    (p2 and p4 and p6) or (p4 and p6 and p8),
                    (p2 and p4 and p8) or (p2 and p6 and p8),
                )[step]
                if not keep:
                    to_remove.add((x, y))
            if to_remove:
                result.difference_update(to_remove)
                changed = True
        if not changed:
            break
    return BinarizedImage(frozenset(result), image.width, image.height, image.threshold, image.inverted)


__all__ = ["thin"]

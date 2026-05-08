"""Grid-based area-of-effect calculations.

The grid uses 5-foot squares standard in D&D. Coordinates are in squares, with conversion to/from feet.
"""

import math
from dataclasses import dataclass


SQUARE_SIZE_FT = 5  # default; pass feet_per_square explicitly to override


@dataclass
class GridPoint:
    x: int
    y: int

    def distance_to_squares(self, other: "GridPoint") -> int:
        """Chebyshev distance (5e diagonal: each diagonal counts as 1 square, but every other counts as 2)."""
        dx = abs(self.x - other.x)
        dy = abs(self.y - other.y)
        return max(dx, dy) + min(dx, dy) // 2

    def distance_to_ft(self, other: "GridPoint", feet_per_square: int = SQUARE_SIZE_FT) -> int:
        return self.distance_to_squares(other) * feet_per_square


def feet_to_squares(ft: int, feet_per_square: int = SQUARE_SIZE_FT) -> int:
    return max(1, ft // feet_per_square)


def sphere_squares(center: GridPoint, radius_ft: int, feet_per_square: int = SQUARE_SIZE_FT) -> set[tuple[int, int]]:
    """Return squares within radius_ft of center."""
    radius_sq = feet_to_squares(radius_ft, feet_per_square)
    affected = set()
    for dx in range(-radius_sq, radius_sq + 1):
        for dy in range(-radius_sq, radius_sq + 1):
            point = GridPoint(center.x + dx, center.y + dy)
            if point.distance_to_squares(center) <= radius_sq:
                affected.add((point.x, point.y))
    return affected


def cube_squares(origin: GridPoint, size_ft: int, feet_per_square: int = SQUARE_SIZE_FT) -> set[tuple[int, int]]:
    """Return squares in a cube starting at origin, size_ft on each side."""
    size_sq = feet_to_squares(size_ft, feet_per_square)
    affected = set()
    for dx in range(size_sq):
        for dy in range(size_sq):
            affected.add((origin.x + dx, origin.y + dy))
    return affected


def cone_squares(origin: GridPoint, direction: tuple[int, int], size_ft: int, feet_per_square: int = SQUARE_SIZE_FT) -> set[tuple[int, int]]:
    """Return squares in a cone of size_ft length emanating from origin in given direction.

    Cone width at distance d from origin is d (so a 15ft cone is 3 squares wide at its tip).
    Direction is a unit vector tuple like (1, 0), (0, 1), (1, 1), etc.
    """
    size_sq = feet_to_squares(size_ft, feet_per_square)
    affected = set()
    dx, dy = direction
    if dx == 0 and dy == 0:
        return affected

    is_diagonal = dx != 0 and dy != 0

    for dist in range(1, size_sq + 1):
        if is_diagonal:
            for offset in range(-(dist // 2), dist // 2 + 1):
                # Diagonal cone widens perpendicular to direction
                center_x = origin.x + dist * dx
                center_y = origin.y + dist * dy
                affected.add((center_x + offset * (-dy), center_y + offset * dx))
        else:
            # Axis-aligned cone
            if dx != 0:
                for offset in range(-(dist // 2), dist // 2 + 1):
                    affected.add((origin.x + dist * dx, origin.y + offset))
            else:
                for offset in range(-(dist // 2), dist // 2 + 1):
                    affected.add((origin.x + offset, origin.y + dist * dy))

    return affected


def line_squares(origin: GridPoint, direction: tuple[int, int], length_ft: int, width_ft: int = 5, feet_per_square: int = SQUARE_SIZE_FT) -> set[tuple[int, int]]:
    """Return squares in a line from origin in given direction."""
    length_sq = feet_to_squares(length_ft, feet_per_square)
    width_sq = feet_to_squares(width_ft, feet_per_square)
    half_width = width_sq // 2
    affected = set()
    dx, dy = direction
    for dist in range(1, length_sq + 1):
        center_x = origin.x + dist * dx
        center_y = origin.y + dist * dy
        for offset in range(-half_width, half_width + 1):
            if dx == 0:
                affected.add((center_x + offset, center_y))
            elif dy == 0:
                affected.add((center_x, center_y + offset))
            else:
                affected.add((center_x + offset * (-dy), center_y + offset * dx))
    return affected


def creatures_in_area(creatures: list, affected_squares: set[tuple[int, int]]) -> list:
    """Filter list of creatures (with .position attribute) to those in the affected squares."""
    return [c for c in creatures if c.position and (c.position.x, c.position.y) in affected_squares]


def compute_area(area_spec: dict, origin: GridPoint, direction: tuple[int, int] = (1, 0), feet_per_square: int = SQUARE_SIZE_FT) -> set[tuple[int, int]]:
    """Compute affected squares from an area spec dict (from spells.json)."""
    shape = area_spec["shape"]
    if shape == "sphere":
        return sphere_squares(origin, area_spec["radius_ft"], feet_per_square)
    if shape == "cube":
        return cube_squares(origin, area_spec["size_ft"], feet_per_square)
    if shape == "cone":
        return cone_squares(origin, direction, area_spec["size_ft"], feet_per_square)
    if shape == "line":
        return line_squares(origin, direction, area_spec["length_ft"], area_spec.get("width_ft", 5), feet_per_square)
    raise ValueError(f"Unknown area shape: {shape}")

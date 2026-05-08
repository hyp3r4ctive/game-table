"""Line-of-sight and lighting-aware visibility computation.

Coordinates are grid cells (integer col, row). Walls are continuous line segments
{x1, y1, x2, y2} in grid-unit space (one cell width = 1 unit). Zones are rectangles
or circles in the same grid-unit space.

Cell centers depend on grid_type:
- "square": center of cell (col, row) is at (col + 0.5, row + 0.5).
- "hex" (flat-top): centers are staggered. Matches the rendering in session.html:
  hexW = sqrt(3)/2, vSpacing = 0.75. Cell (col, row) center is at
  (col*hexW + hexW/2 + (row%2)*hexW/2, row*vSpacing + 0.5).

Distances are Euclidean in grid-unit space. feet_per_square converts game-distance
(e.g. 60ft darkvision) into a grid-unit radius for the sight check.
"""

from typing import Iterable
import math

SQUARE_FT_DEFAULT = 5  # D&D 5e default

HEX_W = math.sqrt(3) / 2  # horizontal distance between hex column centers, in grid units
HEX_V_SPACING = 0.75  # vertical distance between hex row centers, in grid units


_EPS = 1e-7


def _cell_center(col: int, row: int, grid_type: str = "square") -> tuple[float, float]:
    """Pixel-equivalent (grid-unit) center of cell (col, row)."""
    if grid_type == "hex":
        cx = col * HEX_W + HEX_W / 2 + (row % 2) * HEX_W / 2
        cy = row * HEX_V_SPACING + 0.5
        return cx, cy
    return col + 0.5, row + 0.5


def _segments_intersect(ax, ay, bx, by, cx, cy, dx, dy) -> bool:
    """Proper segment intersection.

    Returns False for collinear or endpoint-touching cases. This matters for
    line-of-sight: a ray that grazes a wall corner shouldn't be blocked,
    otherwise standing at the corner of a wall blocks vision in every direction.
    """
    def orient(px, py, qx, qy, rx, ry):
        v = (qx - px) * (ry - py) - (qy - py) * (rx - px)
        if v > _EPS: return 1
        if v < -_EPS: return -1
        return 0
    o1 = orient(ax, ay, bx, by, cx, cy)
    o2 = orient(ax, ay, bx, by, dx, dy)
    o3 = orient(cx, cy, dx, dy, ax, ay)
    o4 = orient(cx, cy, dx, dy, bx, by)
    if o1 == 0 or o2 == 0 or o3 == 0 or o4 == 0:
        return False
    return o1 != o2 and o3 != o4


def _ray_blocked(ox, oy, tx, ty, walls: list[dict]) -> bool:
    """A ray is blocked by a wall if it properly crosses the wall (excluding
    grazing the endpoints). Special case: a wall corner shared by two or more
    walls blocks the ray if the ray passes through that corner.
    """
    if not walls:
        return False
    endpoint_walls: dict[tuple[float, float], int] = {}
    for w in walls:
        for ex, ey in ((w["x1"], w["y1"]), (w["x2"], w["y2"])):
            if abs(ex - ox) < _EPS and abs(ey - oy) < _EPS:
                continue
            cross = (tx - ox) * (ey - oy) - (ty - oy) * (ex - ox)
            if abs(cross) >= _EPS:
                continue
            if abs(tx - ox) >= abs(ty - oy):
                t = (ex - ox) / (tx - ox) if (tx - ox) != 0 else -1
            else:
                t = (ey - oy) / (ty - oy) if (ty - oy) != 0 else -1
            if _EPS < t < 1 - _EPS:
                key = (round(ex, 6), round(ey, 6))
                endpoint_walls[key] = endpoint_walls.get(key, 0) + 1
    if any(c >= 2 for c in endpoint_walls.values()):
        return True

    for w in walls:
        if (abs(ox - w["x1"]) < _EPS and abs(oy - w["y1"]) < _EPS) or \
           (abs(ox - w["x2"]) < _EPS and abs(oy - w["y2"]) < _EPS):
            continue
        if _segments_intersect(ox, oy, tx, ty, w["x1"], w["y1"], w["x2"], w["y2"]):
            return True
    return False


def _point_in_polygon(px: float, py: float, points: list) -> bool:
    """Standard ray-casting point-in-polygon test. points is a list of [x, y]."""
    n = len(points)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = float(points[i][0]), float(points[i][1])
        xj, yj = float(points[j][0]), float(points[j][1])
        denom = (yj - yi)
        if denom == 0:
            denom = 1e-12
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / denom + xi):
            inside = not inside
        j = i
    return inside


def _zone_contains(zone: dict, col: int, row: int, grid_type: str = "square") -> bool:
    """Test if cell (col, row) center lies within the zone (rect, circle, or polygon)."""
    cx, cy = _cell_center(col, row, grid_type)
    shape = zone.get("shape")
    if shape == "circle":
        zcx = float(zone.get("cx", 0))
        zcy = float(zone.get("cy", 0))
        r = float(zone.get("r", 0))
        return (cx - zcx) ** 2 + (cy - zcy) ** 2 <= r * r
    if shape == "polygon":
        return _point_in_polygon(cx, cy, zone.get("points") or [])
    return (zone["x"] <= cx < zone["x"] + zone.get("w", 1)
            and zone["y"] <= cy < zone["y"] + zone.get("h", 1))


def zone_at(col: int, row: int, zones: list[dict], filter_type: str | None = None, grid_type: str = "square") -> dict | None:
    for z in zones:
        if filter_type and z.get("type") != filter_type:
            continue
        if _zone_contains(z, col, row, grid_type):
            return z
    return None


def visible_squares(
    origin_col: float,
    origin_row: float,
    vision_normal_ft: int,
    darkvision_ft: int,
    light_emission_ft: int,
    walls: list[dict],
    zones: list[dict],
    grid_cols: int,
    grid_rows: int,
    feet_per_square: int = SQUARE_FT_DEFAULT,
    grid_type: str = "square",
) -> set[tuple[int, int]]:
    """Cells visible from origin given vision params.

    vision_normal_ft: 0 means unlimited (assume bright outdoor day).
    darkvision_ft: distance at which dark counts as dim for this creature.
    light_emission_ft: how far this creature illuminates around itself (e.g. torch).
    """
    if vision_normal_ft <= 0:
        normal_radius = (grid_cols ** 2 + grid_rows ** 2) ** 0.5 + 1
    else:
        normal_radius = vision_normal_ft / feet_per_square
    darkvision_radius = darkvision_ft / feet_per_square
    emission_radius = light_emission_ft / feet_per_square

    max_radius = max(normal_radius, darkvision_radius, emission_radius)
    visible: set[tuple[int, int]] = set()

    ox, oy = _cell_center(int(origin_col), int(origin_row), grid_type)

    for sy in range(grid_rows):
        for sx in range(grid_cols):
            cx, cy = _cell_center(sx, sy, grid_type)
            dist = math.hypot(cx - ox, cy - oy)
            if dist > max_radius:
                continue
            if _ray_blocked(ox, oy, cx, cy, walls):
                continue
            if zone_at(sx, sy, zones, "magical_dark", grid_type):
                continue
            zone = zone_at(sx, sy, zones, grid_type=grid_type)
            zone_type = zone.get("type") if zone else "bright"
            if zone_type in (None, "bright", "dim", "difficult", "water"):
                if dist <= max(normal_radius, emission_radius, darkvision_radius):
                    visible.add((sx, sy))
            elif zone_type == "dark":
                if dist <= max(emission_radius, darkvision_radius):
                    visible.add((sx, sy))
    return visible


def party_visible(
    party: list[dict],
    walls: list[dict],
    zones: list[dict],
    grid_cols: int,
    grid_rows: int,
    feet_per_square: int = SQUARE_FT_DEFAULT,
    grid_type: str = "square",
) -> set[tuple[int, int]]:
    """Union of vision sets for each party member with a position."""
    seen: set[tuple[int, int]] = set()
    for p in party:
        x = p.get("position_x")
        y = p.get("position_y")
        if x is None or y is None:
            continue
        if p.get("is_enemy") or not p.get("is_active", True):
            continue
        seen |= visible_squares(
            x, y,
            vision_normal_ft=p.get("vision_normal_ft", 0) or 0,
            darkvision_ft=p.get("darkvision_ft", 0) or 0,
            light_emission_ft=p.get("light_emission_ft", 0) or 0,
            walls=walls,
            zones=zones,
            grid_cols=grid_cols,
            grid_rows=grid_rows,
            feet_per_square=feet_per_square,
            grid_type=grid_type,
        )
    return seen


def can_see_square(
    origin_col: float,
    origin_row: float,
    target_col: int,
    target_row: int,
    vision_normal_ft: int,
    darkvision_ft: int,
    light_emission_ft: int,
    walls: list[dict],
    zones: list[dict],
    grid_cols: int,
    grid_rows: int,
    feet_per_square: int = SQUARE_FT_DEFAULT,
    grid_type: str = "square",
) -> bool:
    """True if the target cell is visible from the origin."""
    visible = visible_squares(
        origin_col, origin_row,
        vision_normal_ft, darkvision_ft, light_emission_ft,
        walls, zones, grid_cols, grid_rows,
        feet_per_square=feet_per_square, grid_type=grid_type,
    )
    return (target_col, target_row) in visible


def vision_radii_for_party(party: list[dict]) -> list[dict]:
    """Per-character sight-radius records for rendering vision circles."""
    out = []
    for p in party:
        if p.get("is_enemy") or not p.get("is_active", True):
            continue
        if p.get("position_x") is None:
            continue
        out.append({
            "id": p.get("id"),
            "name": p.get("name"),
            "x": p.get("position_x"),
            "y": p.get("position_y"),
            "darkvision_ft": p.get("darkvision_ft", 0) or 0,
            "vision_normal_ft": p.get("vision_normal_ft", 0) or 0,
            "light_emission_ft": p.get("light_emission_ft", 0) or 0,
        })
    return out

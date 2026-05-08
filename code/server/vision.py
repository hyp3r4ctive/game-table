"""Line-of-sight and lighting-aware visibility computation.

Coordinates are grid squares (integer x, y). Walls are continuous segments
{x1, y1, x2, y2} in the same grid space (corners are integer-aligned but
segments can pass through any point). Zones are rectangles {type, x, y, w, h}
with x, y, w, h in grid-square units (h=row count etc.).

Each square = 1 inch physical (per the table) but represents 5 ft in-game.
"""

from typing import Iterable
import math

SQUARE_FT = 5  # one grid square = 5 feet in D&D rules


_EPS = 1e-7


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
    # Count how many walls touch each endpoint that lies on the ray.
    endpoint_walls: dict[tuple[float, float], int] = {}
    for w in walls:
        for ex, ey in ((w["x1"], w["y1"]), (w["x2"], w["y2"])):
            if abs(ex - ox) < _EPS and abs(ey - oy) < _EPS:
                continue  # origin is at this endpoint; doesn't block its own vision
            cross = (tx - ox) * (ey - oy) - (ty - oy) * (ex - ox)
            if abs(cross) >= _EPS:
                continue  # not collinear with ray
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
        # Origin AT a wall endpoint: that wall can't block the player at its corner.
        if (abs(ox - w["x1"]) < _EPS and abs(oy - w["y1"]) < _EPS) or \
           (abs(ox - w["x2"]) < _EPS and abs(oy - w["y2"]) < _EPS):
            continue
        if _segments_intersect(ox, oy, tx, ty, w["x1"], w["y1"], w["x2"], w["y2"]):
            return True
    return False


def _zone_contains(zone: dict, sx: int, sy: int) -> bool:
    """Test if grid square (sx, sy) center lies within the zone (rect or circle)."""
    cx, cy = sx + 0.5, sy + 0.5
    if zone.get("shape") == "circle":
        zcx = float(zone.get("cx", 0))
        zcy = float(zone.get("cy", 0))
        r = float(zone.get("r", 0))
        return (cx - zcx) ** 2 + (cy - zcy) ** 2 <= r * r
    # Default: rectangle
    return (zone["x"] <= sx < zone["x"] + zone.get("w", 1)
            and zone["y"] <= sy < zone["y"] + zone.get("h", 1))


def zone_at(sx: int, sy: int, zones: list[dict], filter_type: str | None = None) -> dict | None:
    for z in zones:
        if filter_type and z.get("type") != filter_type:
            continue
        if _zone_contains(z, sx, sy):
            return z
    return None


def visible_squares(
    origin_x: float,
    origin_y: float,
    vision_normal_ft: int,
    darkvision_ft: int,
    light_emission_ft: int,
    walls: list[dict],
    zones: list[dict],
    grid_cols: int,
    grid_rows: int,
) -> set[tuple[int, int]]:
    """Return set of (x, y) squares visible from the origin point.

    vision_normal_ft: 0 means unlimited (assume bright outdoor day).
    darkvision_ft: distance at which dark counts as dim for this creature.
    light_emission_ft: how far this creature illuminates around itself (e.g. torch).
    """
    if vision_normal_ft <= 0:
        # 0 means "unlimited" (bright outdoor day with no obstructions).
        # Use a value larger than any possible map diagonal.
        normal_radius_sq = (grid_cols ** 2 + grid_rows ** 2) ** 0.5 + 1
    else:
        normal_radius_sq = vision_normal_ft / SQUARE_FT
    darkvision_sq = darkvision_ft / SQUARE_FT
    emission_sq = light_emission_ft / SQUARE_FT

    max_radius = max(normal_radius_sq, darkvision_sq, emission_sq)
    visible: set[tuple[int, int]] = set()

    ox = origin_x + 0.5
    oy = origin_y + 0.5

    for sy in range(grid_rows):
        for sx in range(grid_cols):
            cx = sx + 0.5
            cy = sy + 0.5
            dist_sq = math.hypot(cx - ox, cy - oy)
            if dist_sq > max_radius:
                continue
            if _ray_blocked(ox, oy, cx, cy, walls):
                continue
            if zone_at(sx, sy, zones, "magical_dark"):
                continue
            zone = zone_at(sx, sy, zones)
            zone_type = zone.get("type") if zone else "bright"
            if zone_type in (None, "bright", "dim", "difficult", "water"):
                if dist_sq <= max(normal_radius_sq, emission_sq, darkvision_sq):
                    visible.add((sx, sy))
            elif zone_type == "dark":
                if dist_sq <= max(emission_sq, darkvision_sq):
                    visible.add((sx, sy))
    return visible


def party_visible(
    party: list[dict],
    walls: list[dict],
    zones: list[dict],
    grid_cols: int,
    grid_rows: int,
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
        )
    return seen


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

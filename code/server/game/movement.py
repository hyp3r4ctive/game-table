"""Movement validation and cost computation.

Given a path (list of cells the creature steps through), this module
verifies adjacency between consecutive cells, checks each step against
walls on the active map, and computes the foot-cost of the path under
the campaign's diagonal-cost rule. Difficult-terrain zones double the
entered-cell cost.

Pathfinding for "tap destination" mode lives here too — a BFS that
respects walls and treats difficult terrain as 2x cost.
"""

from math import sqrt
from typing import Optional


def _square_neighbors(x: int, y: int) -> list[tuple[int, int]]:
    return [(x + dx, y + dy)
            for dx in (-1, 0, 1) for dy in (-1, 0, 1)
            if not (dx == 0 and dy == 0)]


def _hex_neighbors(x: int, y: int) -> list[tuple[int, int]]:
    """Pointy-top offset coordinates with even-row shift."""
    if y % 2 == 0:
        deltas = [(-1, 0), (1, 0), (-1, -1), (0, -1), (-1, 1), (0, 1)]
    else:
        deltas = [(-1, 0), (1, 0), (0, -1), (1, -1), (0, 1), (1, 1)]
    return [(x + dx, y + dy) for dx, dy in deltas]


def neighbors(x: int, y: int, grid_type: str) -> list[tuple[int, int]]:
    return _hex_neighbors(x, y) if grid_type == "hex" else _square_neighbors(x, y)


def is_adjacent(ax: int, ay: int, bx: int, by: int, grid_type: str) -> bool:
    return (bx, by) in neighbors(ax, ay, grid_type)


def is_diagonal_step(ax: int, ay: int, bx: int, by: int, grid_type: str) -> bool:
    """Square-grid only: diagonal = both x and y change."""
    if grid_type == "hex":
        return False
    return abs(bx - ax) == 1 and abs(by - ay) == 1


def _cell_center_xy(x: int, y: int, grid_type: str) -> tuple[float, float]:
    if grid_type == "hex":
        hex_w = sqrt(3) / 2
        return (x * hex_w + hex_w / 2 + (y % 2) * hex_w / 2, y * 0.75 + 0.5)
    return (x + 0.5, y + 0.5)


def _segments_cross(p1, p2, p3, p4) -> bool:
    """True if segment p1p2 properly crosses p3p4 (open intervals)."""
    def ccw(a, b, c):
        return (c[1] - a[1]) * (b[0] - a[0]) - (b[1] - a[1]) * (c[0] - a[0])
    d1 = ccw(p3, p4, p1)
    d2 = ccw(p3, p4, p2)
    d3 = ccw(p1, p2, p3)
    d4 = ccw(p1, p2, p4)
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
       ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True
    return False


def is_blocked_by_wall(walls: list, ax: int, ay: int, bx: int, by: int, grid_type: str) -> bool:
    a = _cell_center_xy(ax, ay, grid_type)
    b = _cell_center_xy(bx, by, grid_type)
    for w in walls or []:
        try:
            if _segments_cross(a, b, (w["x1"], w["y1"]), (w["x2"], w["y2"])):
                return True
        except (KeyError, TypeError):
            continue
    return False


def _point_in_polygon(px: float, py: float, points: list) -> bool:
    inside = False
    j = len(points) - 1
    for i in range(len(points)):
        xi, yi = points[i][0], points[i][1]
        xj, yj = points[j][0], points[j][1]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / ((yj - yi) or 1e-9) + xi):
            inside = not inside
        j = i
    return inside


def cell_in_zone(zone: dict, x: int, y: int, grid_type: str) -> bool:
    cx, cy = _cell_center_xy(x, y, grid_type)
    shape = zone.get("shape", "rect")
    if shape == "rect":
        zx, zy = zone.get("x", 0), zone.get("y", 0)
        return zx <= cx <= zx + zone.get("w", 0) and zy <= cy <= zy + zone.get("h", 0)
    if shape == "circle":
        zcx, zcy = zone.get("cx", 0), zone.get("cy", 0)
        r = zone.get("r", 0)
        return (cx - zcx) ** 2 + (cy - zcy) ** 2 <= r ** 2
    if shape == "polygon":
        return _point_in_polygon(cx, cy, zone.get("points", []))
    return False


def is_difficult(zones: list, x: int, y: int, grid_type: str) -> bool:
    for z in zones or []:
        if z.get("type") == "difficult" and cell_in_zone(z, x, y, grid_type):
            return True
    return False


def step_cost_ft(ax: int, ay: int, bx: int, by: int,
                 feet_per_square: int, grid_type: str,
                 diagonal_rule: str = "5_5_5",
                 diagonals_used: int = 0,
                 difficult: bool = False) -> int:
    """Cost in feet to step from (ax,ay) to adjacent (bx,by). Difficult terrain doubles.

    For 5_10_5 the caller passes the count of diagonals already used this turn so
    every other diagonal costs 2x feet_per_square.
    """
    if grid_type == "hex":
        cost = feet_per_square
    else:
        if is_diagonal_step(ax, ay, bx, by, "square") and diagonal_rule == "5_10_5":
            cost = feet_per_square * 2 if (diagonals_used % 2 == 1) else feet_per_square
        else:
            cost = feet_per_square
    if difficult:
        cost *= 2
    return cost


def validate_path(start: tuple[int, int], path: list[tuple[int, int]],
                  walls: list, zones: list, feet_per_square: int,
                  grid_type: str, diagonal_rule: str = "5_5_5") -> dict:
    """Walk the path step-by-step. Returns:
        {ok, total_cost_ft, steps: [{from, to, cost, difficult}], blocked_at?, reason?}
    """
    cur = start
    diagonals_used = 0
    total = 0
    steps: list[dict] = []
    for nxt in path:
        if not is_adjacent(cur[0], cur[1], nxt[0], nxt[1], grid_type):
            return {"ok": False, "blocked_at": list(nxt), "reason": "not_adjacent",
                    "total_cost_ft": total, "steps": steps}
        if is_blocked_by_wall(walls, cur[0], cur[1], nxt[0], nxt[1], grid_type):
            return {"ok": False, "blocked_at": list(nxt), "reason": "wall",
                    "total_cost_ft": total, "steps": steps}
        difficult = is_difficult(zones, nxt[0], nxt[1], grid_type)
        cost = step_cost_ft(cur[0], cur[1], nxt[0], nxt[1],
                            feet_per_square, grid_type,
                            diagonal_rule=diagonal_rule,
                            diagonals_used=diagonals_used,
                            difficult=difficult)
        if grid_type != "hex" and is_diagonal_step(cur[0], cur[1], nxt[0], nxt[1], grid_type):
            diagonals_used += 1
        total += cost
        steps.append({"from": list(cur), "to": list(nxt), "cost": cost, "difficult": difficult})
        cur = nxt
    return {"ok": True, "total_cost_ft": total, "steps": steps,
            "diagonals_used": diagonals_used, "final": list(cur)}


def find_path(start: tuple[int, int], goal: tuple[int, int],
              walls: list, zones: list, grid_type: str,
              grid_cols: int, grid_rows: int,
              max_cost_ft: Optional[int] = None,
              feet_per_square: int = 5,
              diagonal_rule: str = "5_5_5") -> Optional[list[tuple[int, int]]]:
    """BFS shortest-cost path from start → goal, respecting walls and difficult terrain.
    Returns the list of cells (excluding start, including goal), or None if unreachable.
    """
    import heapq
    if start == goal:
        return []
    visited = {start: 0}
    heap = [(0, 0, start, [])]  # (cost, tiebreaker, cell, path-so-far)
    counter = 0
    while heap:
        cost, _, cell, path = heapq.heappop(heap)
        if cell == goal:
            return path
        for nb in neighbors(cell[0], cell[1], grid_type):
            nx, ny = nb
            if nx < 0 or ny < 0 or nx >= grid_cols or ny >= grid_rows:
                continue
            if is_blocked_by_wall(walls, cell[0], cell[1], nx, ny, grid_type):
                continue
            difficult = is_difficult(zones, nx, ny, grid_type)
            step_c = step_cost_ft(cell[0], cell[1], nx, ny,
                                  feet_per_square, grid_type,
                                  diagonal_rule=diagonal_rule,
                                  difficult=difficult)
            new_cost = cost + step_c
            if max_cost_ft is not None and new_cost > max_cost_ft:
                continue
            if nb in visited and visited[nb] <= new_cost:
                continue
            visited[nb] = new_cost
            counter += 1
            heapq.heappush(heap, (new_cost, counter, nb, path + [nb]))
    return None

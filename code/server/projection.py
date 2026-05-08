"""Hardware geometry for the rear-projection battle map.

The projector throws a landscape image filling a 60×48 inch play area cut
into the table top. Acrylic sits on a 0.25-inch lip around all four sides.
DM is mounted under one short edge (default: left). Players sit around the
opposite long region. The map is biased away from the DM toward the players,
with two extra margins on the player edge: a 0.25" default margin and
another 0.25" geometry-tolerance buffer.

Numbers here drive (a) the projector view's render bounds, (b) defaults
for newly created maps. Physical hardware change → tweak constants here.
"""

from math import sqrt, floor

PLAY_AREA_INCHES: tuple[float, float] = (60.0, 48.0)
LIP_INCHES: float = 0.25
PLAYER_EDGE_DEFAULT_MARGIN_INCHES: float = 0.25
PLAYER_EDGE_GEOMETRY_MARGIN_INCHES: float = 0.25
DEFAULT_INCHES_PER_CELL: float = 1.0
DEFAULT_DM_EDGE: str = "left"  # "top" | "bottom" | "left" | "right"

DEFAULT_MAP_GRID_TYPE: str = "square"
DEFAULT_MAP_PHYSICAL_INCHES: tuple[float, float] = (47.0, 47.0)


def _player_edge_extra() -> float:
    return PLAYER_EDGE_DEFAULT_MARGIN_INCHES + PLAYER_EDGE_GEOMETRY_MARGIN_INCHES


def effective_area(dm_edge: str = DEFAULT_DM_EDGE) -> dict:
    """Inset render rect in projector inches. Origin top-left of the 60×48 window.
    Lip is applied on all sides; the side opposite the DM gets the extra margin.
    """
    w, h = PLAY_AREA_INCHES
    lip = LIP_INCHES
    extra = _player_edge_extra()
    x, y = lip, lip
    iw, ih = w - 2 * lip, h - 2 * lip
    if dm_edge == "left":
        iw -= extra  # extra on right (player) edge
    elif dm_edge == "right":
        x += extra
        iw -= extra
    elif dm_edge == "top":
        ih -= extra  # extra on bottom (player) edge
    elif dm_edge == "bottom":
        y += extra
        ih -= extra
    return {
        "x": x, "y": y, "w": iw, "h": ih,
        "total_w": w, "total_h": h, "dm_edge": dm_edge,
        "lip": lip,
        "player_default_margin": PLAYER_EDGE_DEFAULT_MARGIN_INCHES,
        "player_geometry_margin": PLAYER_EDGE_GEOMETRY_MARGIN_INCHES,
        "player_extra_total": extra,
    }


def fit_grid_dims(grid_type: str = "square",
                  inches_per_cell: float = DEFAULT_INCHES_PER_CELL,
                  dm_edge: str = DEFAULT_DM_EDGE) -> tuple[int, int]:
    """Cols, rows that completely fill the effective render rect at the given scale.
    Used when the DM wants a map that covers the whole projection area.
    """
    eff = effective_area(dm_edge)
    if grid_type == "hex":
        size = inches_per_cell
        cols = max(4, floor(eff["w"] / (sqrt(3) / 2 * size)))
        rows = max(4, floor((eff["h"] / size - 1.0) / 0.75) + 1)
    else:
        cols = max(4, floor(eff["w"] / inches_per_cell))
        rows = max(4, floor(eff["h"] / inches_per_cell))
    return cols, rows


def default_map_dims(grid_type: str = DEFAULT_MAP_GRID_TYPE,
                     inches_per_cell: float = DEFAULT_INCHES_PER_CELL) -> tuple[int, int]:
    """Cols, rows for the default new map (smaller than the full render area).
    Targets DEFAULT_MAP_PHYSICAL_INCHES — 47×47 by default — leaving DM-side
    space empty for round/turn info or DM tools.
    """
    pw, ph = DEFAULT_MAP_PHYSICAL_INCHES
    if grid_type == "hex":
        size = inches_per_cell
        cols = max(4, floor(pw / (sqrt(3) / 2 * size)))
        rows = max(4, floor((ph / size - 1.0) / 0.75) + 1)
    else:
        cols = max(4, floor(pw / inches_per_cell))
        rows = max(4, floor(ph / inches_per_cell))
    return cols, rows

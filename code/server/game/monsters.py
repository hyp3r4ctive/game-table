"""Monster catalog. Loaded from data/monsters.json on import.

Each entry is a stat block usable as the source for spawning enemy LiveCharacters
in a session. The DM picks by key; the spawn endpoint creates a hidden Character
(is_enemy=True) and a LiveCharacter with the template's stats.

The `notes` field is freeform text describing attacks, traits, breath weapons,
multi-attack actions etc. — DM resolves narratively. The mechanically-enforced
fields (HP, AC, stats, resistances, etc.) drive the engine.
"""

import json
from pathlib import Path

_PATH = Path(__file__).parent.parent / "data" / "monsters.json"
MONSTERS: dict = {}
try:
    MONSTERS = json.loads(_PATH.read_text())
except FileNotFoundError:
    MONSTERS = {}


def get_monster(key: str) -> dict | None:
    return MONSTERS.get(key)


def list_monsters() -> list[dict]:
    return sorted(
        ({"key": k, **v} for k, v in MONSTERS.items()),
        key=lambda m: (_cr_sort_key(m.get("challenge_rating", "0")), m.get("name", "")),
    )


def _cr_sort_key(cr: str) -> float:
    """Convert CR string ('1/4', '2', '10') to a sortable float."""
    if not cr:
        return 0.0
    if "/" in cr:
        try:
            num, den = cr.split("/")
            return float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            return 0.0
    try:
        return float(cr)
    except ValueError:
        return 0.0


# 5e XP-by-CR table (DMG p.274).
CR_TO_XP = {
    "0": 10, "1/8": 25, "1/4": 50, "1/2": 100,
    "1": 200, "2": 450, "3": 700, "4": 1100, "5": 1800,
    "6": 2300, "7": 2900, "8": 3900, "9": 5000, "10": 5900,
    "11": 7200, "12": 8400, "13": 10000, "14": 11500, "15": 13000,
    "16": 15000, "17": 18000, "18": 20000, "19": 22000, "20": 25000,
    "21": 33000, "22": 41000, "23": 50000, "24": 62000,
    "25": 75000, "26": 90000, "27": 105000, "28": 120000,
    "29": 135000, "30": 155000,
}


def cr_to_xp(cr: str) -> int:
    return int(CR_TO_XP.get(cr.strip() if cr else "", 0))


# Encounter difficulty thresholds per PC by level (DMG p.82).
DIFFICULTY_BY_LEVEL = {
    1: (25, 50, 75, 100), 2: (50, 100, 150, 200), 3: (75, 150, 225, 400),
    4: (125, 250, 375, 500), 5: (250, 500, 750, 1100), 6: (300, 600, 900, 1400),
    7: (350, 750, 1100, 1700), 8: (450, 900, 1400, 2100),
    9: (550, 1100, 1600, 2400), 10: (600, 1200, 1900, 2800),
    11: (800, 1600, 2400, 3600), 12: (1000, 2000, 3000, 4500),
    13: (1100, 2200, 3400, 5100), 14: (1250, 2500, 3800, 5700),
    15: (1400, 2800, 4300, 6400), 16: (1600, 3200, 4800, 7200),
    17: (2000, 3900, 5900, 8800), 18: (2100, 4200, 6300, 9500),
    19: (2400, 4900, 7300, 10900), 20: (2800, 5700, 8500, 12700),
}


def party_thresholds(levels: list[int]) -> tuple[int, int, int, int]:
    """Sum (easy, medium, hard, deadly) thresholds across all PC levels."""
    e = m = h = d = 0
    for lvl in levels:
        L = max(1, min(int(lvl), 20))
        rows = DIFFICULTY_BY_LEVEL.get(L)
        if not rows:
            continue
        e += rows[0]; m += rows[1]; h += rows[2]; d += rows[3]
    return e, m, h, d


def encounter_multiplier(num_enemies: int) -> float:
    """5e DMG multiplier on enemy XP based on enemy count."""
    if num_enemies <= 0:
        return 0.0
    if num_enemies == 1:
        return 1.0
    if num_enemies == 2:
        return 1.5
    if num_enemies <= 6:
        return 2.0
    if num_enemies <= 10:
        return 2.5
    if num_enemies <= 14:
        return 3.0
    return 4.0

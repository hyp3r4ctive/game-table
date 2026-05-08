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

import json
from pathlib import Path

CONDITIONS_FILE = Path(__file__).parent.parent / "data" / "conditions.json"

with open(CONDITIONS_FILE) as f:
    CONDITIONS = json.load(f)


def get_condition(name: str) -> dict:
    """Look up a condition by name (case-insensitive)."""
    key = name.lower().replace(" ", "_")
    return CONDITIONS.get(key)


def has_effect(active_conditions: list[str], effect: str) -> bool:
    """Check if any active condition gives a particular mechanical effect."""
    for cond_name in active_conditions:
        cond = get_condition(cond_name)
        if cond and effect in cond.get("effects", []):
            return True
        # also check effects from sub-conditions like "incapacitated" implied by paralyzed
        if cond and "incapacitated" in cond.get("effects", []):
            incap = get_condition("incapacitated")
            if incap and effect in incap.get("effects", []):
                return True
    return False


def all_effects(active_conditions: list[str]) -> set[str]:
    """Return the set of all mechanical effects from a creature's active conditions."""
    effects = set()
    for cond_name in active_conditions:
        cond = get_condition(cond_name)
        if cond:
            effects.update(cond.get("effects", []))
            # incapacitated is implied by some
            if "incapacitated" in cond.get("effects", []):
                incap = get_condition("incapacitated")
                if incap:
                    effects.update(incap.get("effects", []))
    return effects


def list_all_conditions() -> list[dict]:
    """Return all known conditions for UI display."""
    return [{"key": k, **v} for k, v in CONDITIONS.items()]

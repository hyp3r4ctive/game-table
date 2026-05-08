"""Loaders for 5e SRD reference data: races, classes, backgrounds, skills."""

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"

with open(DATA_DIR / "races.json") as f:
    RACES = json.load(f)

with open(DATA_DIR / "classes.json") as f:
    CLASSES = json.load(f)

with open(DATA_DIR / "backgrounds.json") as f:
    BACKGROUNDS = json.load(f)

with open(DATA_DIR / "skills.json") as f:
    SKILLS = json.load(f)

ABILITIES = ["strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"]


def proficiency_bonus(level: int) -> int:
    if level < 5:
        return 2
    if level < 9:
        return 3
    if level < 13:
        return 4
    if level < 17:
        return 5
    return 6


def ability_modifier(score: int) -> int:
    return (score - 10) // 2


def list_races() -> list[dict]:
    return [{"key": k, **{kk: vv for kk, vv in v.items() if kk != "subraces"}, "subraces": list((v.get("subraces") or {}).keys())} for k, v in RACES.items()]


def list_classes() -> list[dict]:
    return [{"key": k, "name": v["name"], "hit_die": v["hit_die"], "spellcasting": v.get("spellcasting")} for k, v in CLASSES.items() if not k.startswith("_")]


def list_backgrounds() -> list[dict]:
    return [{"key": k, **v} for k, v in BACKGROUNDS.items()]


def get_race(key: str) -> dict | None:
    return RACES.get(key)


def get_class(key: str) -> dict | None:
    if key.startswith("_"):
        return None
    return CLASSES.get(key)


def get_background(key: str) -> dict | None:
    return BACKGROUNDS.get(key)


def slot_table_for_class(class_key: str, level: int) -> dict:
    cls = get_class(class_key)
    if not cls or not cls.get("spellcasting"):
        return {}
    table_name = cls["spellcasting"].get("table")
    if table_name == "full":
        return CLASSES["_full_caster_slots"].get(str(level), {})
    if table_name == "half":
        starts = cls["spellcasting"].get("starts_at_level", 1)
        if level < starts:
            return {}
        return CLASSES["_half_caster_slots"].get(str(level), {})
    if table_name == "warlock":
        entry = CLASSES["_warlock_slots"].get(str(level))
        if not entry:
            return {}
        return {str(entry["slot_level"]): entry["slots"]}
    return {}

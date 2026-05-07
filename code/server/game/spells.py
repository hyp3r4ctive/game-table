"""Spell loader and casting resolver.

The casting flow returns a structured result describing what happened, which the UI/DM uses to apply effects.
The server tracks resource consumption (slots, concentration) and rolls dice. The DM/UI applies the consequences
to specific targets (after server has identified those targets via grid math for AoE).
"""

import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from . import dice
from . import grid

SPELLS_FILE = Path(__file__).parent.parent / "data" / "spells.json"

with open(SPELLS_FILE) as f:
    SPELLS = json.load(f)


@dataclass
class SpellTarget:
    """Represents a creature affected by a spell, for the cast result."""
    name: str
    hit: bool = True  # whether the spell affects them at all
    save_required: Optional[str] = None  # ability name if save needed
    save_dc: Optional[int] = None
    expected_damage_dice: Optional[str] = None
    expected_damage_type: Optional[str] = None
    on_save_effect: Optional[str] = None  # "no_effect", "half_damage"
    conditions_to_apply: list[dict] = field(default_factory=list)
    notes: str = ""


@dataclass
class CastResult:
    spell_name: str
    caster_name: str
    slot_used: int  # 0 for cantrips
    effect_type: str
    description: str
    targets: list[SpellTarget] = field(default_factory=list)
    affected_squares: list[tuple[int, int]] = field(default_factory=list)
    healing_dice: Optional[str] = None
    healing_modifier_label: Optional[str] = None  # "spellcasting" if it adds spellcasting mod
    damage_rolls: list[dice.RollResult] = field(default_factory=list)
    requires_concentration: bool = False
    notes: list[str] = field(default_factory=list)


def get_spell(name: str) -> Optional[dict]:
    key = name.lower().replace(" ", "_").replace("-", "_")
    return SPELLS.get(key)


def list_all_spells() -> list[dict]:
    return [{"key": k, **v} for k, v in SPELLS.items()]


def list_spells_by_level(level: int) -> list[dict]:
    return [{"key": k, **v} for k, v in SPELLS.items() if v["level"] == level]


def _scaled_damage_dice(spell: dict, slot_level: int) -> Optional[str]:
    """Compute the damage dice string accounting for slot upcasting."""
    if not spell.get("damage"):
        return None
    base = spell["damage"][0]
    base_dice = base["dice"]
    base_level = spell["level"]
    if base_level == 0:
        # Cantrip scaling by character level (handled by caller passing slot_level=character level)
        scaling = base.get("scaling_levels", {})
        chosen_dice = base_dice
        for level_threshold_str in sorted(scaling.keys(), key=int):
            if slot_level >= int(level_threshold_str):
                chosen_dice = scaling[level_threshold_str]
        return chosen_dice
    extra_levels = max(0, slot_level - base_level)
    if extra_levels == 0 or "scaling" not in base:
        return base_dice
    scaling = base["scaling"]
    extra_dice_str = scaling.get("extra_dice", "")
    if not extra_dice_str:
        return base_dice
    extra_count, extra_sides, _ = dice.parse_dice(extra_dice_str)
    base_count, base_sides, base_mod = dice.parse_dice(base_dice)
    if base_sides != extra_sides:
        return base_dice  # safety, shouldn't happen with current data
    new_count = base_count + extra_count * extra_levels * scaling.get("per_slot_above", 1)
    sign = "+" if base_mod >= 0 else "-"
    return f"{new_count}d{base_sides}{sign}{abs(base_mod)}"


def cast_spell(
    spell_name: str,
    caster_name: str,
    slot_level: int,
    spell_save_dc: int,
    spell_attack_modifier: int,
    spellcasting_modifier: int,
    target_names: list[str] = None,
    target_positions: list[grid.GridPoint] = None,
    aoe_origin: Optional[grid.GridPoint] = None,
    aoe_direction: tuple[int, int] = (1, 0),
    creatures_in_range: list = None,
) -> CastResult:
    """Resolve a spell cast.

    target_names: explicitly targeted creatures (for single/multi-target)
    aoe_origin: where the AoE is centered (for area spells)
    creatures_in_range: list of objects with .name and .position attributes - server filters to those in AoE
    """
    spell = get_spell(spell_name)
    if not spell:
        raise ValueError(f"Unknown spell: {spell_name}")

    target_names = target_names or []
    creatures_in_range = creatures_in_range or []

    result = CastResult(
        spell_name=spell["name"],
        caster_name=caster_name,
        slot_used=slot_level if spell["level"] > 0 else 0,
        effect_type=spell["effect_type"],
        description=spell["description"],
        requires_concentration=spell.get("concentration", False),
    )

    # Compute AoE squares if applicable
    if "area" in spell and aoe_origin is not None:
        affected = grid.compute_area(spell["area"], aoe_origin, aoe_direction)
        result.affected_squares = sorted(affected)
        in_area = grid.creatures_in_area(creatures_in_range, affected)
        target_names = [c.name for c in in_area]

    effect_type = spell["effect_type"]

    if effect_type == "attack":
        # Spell attack roll vs each target's AC (resolved by combat module separately, here we just describe)
        damage_dice = _scaled_damage_dice(spell, slot_level)
        damage_type = spell["damage"][0]["type"] if spell.get("damage") else None
        for tn in target_names:
            t = SpellTarget(name=tn, expected_damage_dice=damage_dice, expected_damage_type=damage_type)
            t.notes = f"Make ranged spell attack with +{spell_attack_modifier} vs {tn}'s AC"
            result.targets.append(t)

    elif effect_type == "auto_hit":
        # Magic missile style
        damage_dice = spell["damage"][0]["dice"]
        damage_type = spell["damage"][0]["type"]
        if "darts" in spell:
            base_darts = spell["darts"]["base"]
            extra_darts = (slot_level - spell["level"]) * spell["darts"].get("per_slot_above", 0)
            total_darts = base_darts + extra_darts
            result.notes.append(f"{total_darts} darts, distribute among targets")
        for tn in target_names:
            t = SpellTarget(name=tn, expected_damage_dice=damage_dice, expected_damage_type=damage_type)
            t.notes = f"Auto-hits, deals damage"
            result.targets.append(t)

    elif effect_type == "save_for_half":
        damage_dice = _scaled_damage_dice(spell, slot_level)
        damage_type = spell["damage"][0]["type"] if spell.get("damage") else None
        save_info = spell["save"]
        # Roll damage once, all targets share roll
        damage_roll = dice.roll(damage_dice)
        result.damage_rolls.append(damage_roll)
        for tn in target_names:
            t = SpellTarget(
                name=tn,
                save_required=save_info["ability"],
                save_dc=spell_save_dc,
                expected_damage_dice=damage_dice,
                expected_damage_type=damage_type,
                on_save_effect=save_info["on_success"],
            )
            t.notes = f"DC {spell_save_dc} {save_info['ability']} save. {damage_roll.total} damage on fail, {damage_roll.total // 2} on success."
            result.targets.append(t)

    elif effect_type == "save_or_condition":
        save_info = spell["save"]
        for tn in target_names:
            t = SpellTarget(
                name=tn,
                save_required=save_info["ability"],
                save_dc=spell_save_dc,
                conditions_to_apply=spell.get("conditions_applied", []),
                on_save_effect=save_info["on_success"],
            )
            t.notes = f"DC {spell_save_dc} {save_info['ability']} save. On fail: {', '.join(c['name'] for c in spell.get('conditions_applied', []))}"
            if save_info.get("save_at_end_of_turn"):
                t.notes += " (repeats save at end of each turn)"
            result.targets.append(t)

    elif effect_type == "save_or_debuff":
        save_info = spell["save"]
        for tn in target_names:
            t = SpellTarget(
                name=tn,
                save_required=save_info["ability"],
                save_dc=spell_save_dc,
                conditions_to_apply=spell.get("conditions_applied", []),
                on_save_effect=save_info["on_success"],
            )
            t.notes = f"DC {spell_save_dc} {save_info['ability']} save"
            result.targets.append(t)

    elif effect_type == "buff":
        for tn in target_names:
            t = SpellTarget(
                name=tn,
                conditions_to_apply=spell.get("conditions_applied", []),
            )
            t.notes = f"Buff applied: {', '.join(c['name'] for c in spell.get('conditions_applied', []))}"
            result.targets.append(t)

    elif effect_type == "healing":
        healing = spell["healing"]
        base_dice = healing["dice"]
        extra_levels = max(0, slot_level - spell["level"])
        if extra_levels and "scaling" in healing:
            extra_str = healing["scaling"]["extra_dice"]
            extra_count, _, _ = dice.parse_dice(extra_str)
            base_count, base_sides, base_mod = dice.parse_dice(base_dice)
            new_count = base_count + extra_count * extra_levels
            healing_dice = f"{new_count}d{base_sides}"
        else:
            healing_dice = base_dice
        result.healing_dice = healing_dice
        if healing.get("modifier") == "spellcasting":
            result.healing_modifier_label = f"+{spellcasting_modifier} (spellcasting modifier)"
        for tn in target_names:
            t = SpellTarget(name=tn)
            t.notes = f"Heal for {healing_dice}{(' + ' + str(spellcasting_modifier)) if healing.get('modifier') == 'spellcasting' else ''}"
            result.targets.append(t)

    elif effect_type == "hp_threshold":
        # Sleep style
        threshold_dice = spell["hp_threshold"]["dice"]
        extra_levels = max(0, slot_level - spell["level"])
        if extra_levels and "scaling" in spell["hp_threshold"]:
            extra_str = spell["hp_threshold"]["scaling"]["extra_dice"]
            extra_count, _, _ = dice.parse_dice(extra_str)
            base_count, base_sides, _ = dice.parse_dice(threshold_dice)
            threshold_dice = f"{base_count + extra_count * extra_levels}d{base_sides}"
        roll_result = dice.roll(threshold_dice)
        result.damage_rolls.append(roll_result)
        result.notes.append(f"Affects {roll_result.total} HP worth of creatures (lowest HP first, ignoring unconscious)")
        for tn in target_names:
            t = SpellTarget(
                name=tn,
                conditions_to_apply=spell.get("conditions_applied", []),
            )
            result.targets.append(t)

    elif effect_type == "manual":
        result.notes.append("This spell requires manual DM resolution.")

    return result

"""Combat resolution: initiative, turn management, attacks, saves, damage."""

from dataclasses import dataclass, field
from typing import Optional
from . import dice
from . import conditions


@dataclass
class AttackResult:
    attacker: str
    target: str
    attack_roll: dice.RollResult
    target_ac: int
    hit: bool
    critical: bool
    damage_rolls: list[dice.RollResult] = field(default_factory=list)
    total_damage: int = 0
    damage_types: list[str] = field(default_factory=list)
    description: str = ""
    image_hit: bool = False  # Mirror Image: attack hit an illusory duplicate (no damage to caster)
    effective_total: int = 0  # d20 + modifier + extra dice (the number compared vs effective_ac)
    fumble: bool = False  # natural-1 (attack roll's chosen die was a 1)


@dataclass
class SaveResult:
    target: str
    ability: str
    dc: int
    roll: dice.RollResult
    success: bool
    description: str = ""


@dataclass
class DamageInstance:
    amount: int
    type: str
    source: Optional[str] = None


@dataclass
class TurnState:
    creature_name: str
    action_used: bool = False
    bonus_action_used: bool = False
    reaction_used: bool = False
    movement_used_ft: int = 0
    movement_max_ft: int = 30


@dataclass
class CombatTracker:
    in_combat: bool = False
    round_number: int = 0
    initiative_order: list[tuple[str, int]] = field(default_factory=list)  # (name, initiative)
    current_index: int = 0
    turn_state: Optional[TurnState] = None

    def current_creature(self) -> Optional[str]:
        if not self.in_combat or not self.initiative_order:
            return None
        return self.initiative_order[self.current_index][0]

    def start_combat(self, creatures_with_initiative: list[tuple[str, int]]):
        """Start combat with a pre-rolled initiative list."""
        self.initiative_order = sorted(creatures_with_initiative, key=lambda x: -x[1])
        self.current_index = 0
        self.round_number = 1
        self.in_combat = True
        if self.initiative_order:
            self.turn_state = TurnState(creature_name=self.initiative_order[0][0])

    def end_combat(self):
        self.in_combat = False
        self.initiative_order = []
        self.current_index = 0
        self.round_number = 0
        self.turn_state = None

    def next_turn(self, next_creature_max_speed: int = 30):
        if not self.initiative_order:
            return
        self.current_index = (self.current_index + 1) % len(self.initiative_order)
        if self.current_index == 0:
            self.round_number += 1
        next_creature = self.initiative_order[self.current_index][0]
        self.turn_state = TurnState(creature_name=next_creature, movement_max_ft=next_creature_max_speed)


def roll_initiative(dex_modifier: int, conditions_active: list[str] = None) -> dice.RollResult:
    """Roll initiative. Conditions can grant advantage (e.g., alert feat)."""
    advantage = False
    disadvantage = False
    if conditions_active:
        effects = conditions.all_effects(conditions_active)
        if "auto_fail_initiative" in effects:
            return dice.RollResult(expression="initiative", rolls=[1], modifier=dex_modifier, total=1)
    return dice.roll_d20(dex_modifier, advantage=advantage, disadvantage=disadvantage)


def make_attack(
    attacker_name: str,
    target_name: str,
    to_hit_modifier: int,
    target_ac: int,
    damage_dice: str,
    damage_type: str,
    attacker_conditions: list[str] = None,
    target_conditions: list[str] = None,
    distance_ft: int = 5,
    extra_attack_dice: list[str] = None,
    subtract_attack_dice: list[str] = None,
    extra_advantage: bool = False,
    extra_disadvantage: bool = False,
    damage_bonus: int = 0,
    extra_damage_on_hit: list = None,
    target_ac_bonus: int = 0,
    image_redirect_ac: Optional[int] = None,
    crit_rule: str = "double_dice",
) -> AttackResult:
    """Resolve a single attack roll and damage if it hits."""
    attacker_conditions = attacker_conditions or []
    target_conditions = target_conditions or []

    advantage = False
    disadvantage = False

    attacker_effects = conditions.all_effects(attacker_conditions)
    target_effects = conditions.all_effects(target_conditions)

    if "disadvantage_on_attack_rolls" in attacker_effects:
        disadvantage = True
    if "attack_rolls_have_advantage" in attacker_effects:
        advantage = True
    if "attack_rolls_have_disadvantage" in attacker_effects:
        disadvantage = True
    if "attacked_with_advantage" in target_effects:
        advantage = True
    if "attacked_with_disadvantage" in target_effects:
        disadvantage = True

    # Prone special case: melee attacks within 5ft have advantage, ranged have disadvantage
    if "attacked_with_advantage_within_5ft" in target_effects and distance_ft <= 5:
        advantage = True
    if "attacked_with_disadvantage_beyond_5ft" in target_effects and distance_ft > 5:
        disadvantage = True
    # Effect-derived adv/dis (e.g. handler hooks) merge with condition-derived.
    if extra_advantage:
        advantage = True
    if extra_disadvantage:
        disadvantage = True

    # Adv and disadv cancel
    if advantage and disadvantage:
        advantage = False
        disadvantage = False

    attack_roll = dice.roll_d20(to_hit_modifier, advantage=advantage, disadvantage=disadvantage)
    # Extra dice added to / subtracted from the to-hit roll (Bless +1d4 / Bane -1d4).
    extra_attack_total = 0
    extra_attack_rolls: list[tuple[int, "dice.RollResult"]] = []  # (sign, roll)
    for d in (extra_attack_dice or []):
        r = dice.roll(d)
        extra_attack_rolls.append((1, r))
        extra_attack_total += r.total
    for d in (subtract_attack_dice or []):
        r = dice.roll(d)
        extra_attack_rolls.append((-1, r))
        extra_attack_total -= r.total
    effective_attack_total = attack_roll.total + extra_attack_total
    # Mirror Image: deflection redirects the attack to an illusory duplicate. Use
    # the image's AC for the hit check; on hit, mark image_hit so callers consume
    # an image instead of damaging the protected creature.
    base_ac = image_redirect_ac if image_redirect_ac is not None else target_ac
    effective_ac = base_ac + target_ac_bonus
    is_crit = dice.is_critical(attack_roll)
    is_fumble = dice.is_fumble(attack_roll)

    # Auto-crit on melee within 5 ft against paralyzed/unconscious
    if distance_ft <= 5 and "melee_attacks_within_5ft_auto_crit" in target_effects and effective_attack_total >= effective_ac:
        is_crit = True

    hit = is_crit or (not is_fumble and effective_attack_total >= effective_ac)

    result = AttackResult(
        attacker=attacker_name,
        target=target_name,
        attack_roll=attack_roll,
        target_ac=effective_ac,
        hit=hit,
        critical=is_crit,
        image_hit=(hit and image_redirect_ac is not None),
        effective_total=effective_attack_total,
        fumble=is_fumble,
    )

    if result.image_hit:
        result.description = f"{attacker_name}'s attack hits an illusory duplicate of {target_name} (image destroyed)"
        return result

    if hit:
        if is_crit:
            count, sides, modifier = dice.parse_dice(damage_dice)
            if crit_rule == "max_then_dice":
                # Maxed first set + rolled second set + modifier. Brutal homebrew.
                max_part = count * sides
                rolled = dice.roll(f"{count}d{sides}")
                damage_roll = dice.RollResult(
                    expression=f"crit({damage_dice}: max{max_part}+{rolled.expression}{'+' if modifier >= 0 else '-'}{abs(modifier)})",
                    rolls=[max_part] + list(rolled.rolls),
                    modifier=modifier,
                    total=max_part + rolled.total + modifier,
                )
            else:
                crit_dice = f"{count * 2}d{sides}{'+' if modifier >= 0 else '-'}{abs(modifier)}"
                damage_roll = dice.roll(crit_dice)
        else:
            damage_roll = dice.roll(damage_dice)
        result.damage_rolls.append(damage_roll)
        result.total_damage = damage_roll.total + damage_bonus
        result.damage_types.append(damage_type)
        # Effect-derived extra damage on hit (Hunter's Mark: 1d6 force, etc.)
        for entry in (extra_damage_on_hit or []):
            extra_dice_str, extra_type = entry[0], entry[1]
            extra_roll = dice.roll(extra_dice_str)
            result.damage_rolls.append(extra_roll)
            result.total_damage += extra_roll.total
            result.damage_types.append(extra_type)
        mod_label = (" " + " ".join(f"{'+' if s>0 else '-'}{r.total}({r.expression})" for s, r in extra_attack_rolls)) if extra_attack_rolls else ""
        result.description = f"{attacker_name} hits {target_name} for {result.total_damage} damage" + (" (CRIT!)" if is_crit else "") + mod_label
    else:
        mod_label = (" " + " ".join(f"{'+' if s>0 else '-'}{r.total}({r.expression})" for s, r in extra_attack_rolls)) if extra_attack_rolls else ""
        result.description = f"{attacker_name} misses {target_name}" + (" (fumble)" if is_fumble else "") + mod_label

    return result


def make_save(
    target_name: str,
    ability: str,
    save_modifier: int,
    dc: int,
    target_conditions: list[str] = None,
    extra_dice: list[str] = None,
    subtract_dice: list[str] = None,
    extra_advantage: bool = False,
    extra_disadvantage: bool = False,
    bonus: int = 0,
) -> SaveResult:
    """Roll a saving throw."""
    target_conditions = target_conditions or []
    effects = conditions.all_effects(target_conditions)

    advantage = False
    disadvantage = False

    if f"auto_fail_{ability.lower()}_saves" in effects:
        roll_result = dice.RollResult(expression="auto-fail", rolls=[1], modifier=save_modifier, total=1)
        return SaveResult(
            target=target_name, ability=ability, dc=dc, roll=roll_result, success=False,
            description=f"{target_name} auto-fails {ability} save (paralyzed/stunned/etc)",
        )

    if "disadvantage_on_saving_throws" in effects:
        disadvantage = True
    if ability.lower() == "dexterity" and "disadvantage_on_dexterity_saves" in effects:
        disadvantage = True
    if "advantage_on_saving_throws" in effects:
        advantage = True
    if extra_advantage:
        advantage = True
    if extra_disadvantage:
        disadvantage = True

    if advantage and disadvantage:
        advantage = False
        disadvantage = False

    roll_result = dice.roll_d20(save_modifier + bonus, advantage=advantage, disadvantage=disadvantage)
    extra_total = 0
    extra_rolls: list[tuple[int, "dice.RollResult"]] = []
    for d in (extra_dice or []):
        r = dice.roll(d)
        extra_rolls.append((1, r))
        extra_total += r.total
    for d in (subtract_dice or []):
        r = dice.roll(d)
        extra_rolls.append((-1, r))
        extra_total -= r.total
    final_total = roll_result.total + extra_total
    success = final_total >= dc
    suffix = (" " + " ".join(f"{'+' if s>0 else '-'}{r.total}({r.expression})" for s, r in extra_rolls)) if extra_rolls else ""
    roll_result.total = final_total
    return SaveResult(
        target=target_name, ability=ability, dc=dc, roll=roll_result, success=success,
        description=f"{target_name} {'succeeds' if success else 'fails'} {ability} save (rolled {final_total}{suffix} vs DC {dc})",
    )


def apply_damage(
    current_hp: int,
    temp_hp: int,
    max_hp: int,
    damages: list[DamageInstance],
    resistances: list[str] = None,
    immunities: list[str] = None,
    vulnerabilities: list[str] = None,
) -> tuple[int, int, int]:
    """Apply damage with resistance/immunity/vulnerability. Returns (new_current_hp, new_temp_hp, total_taken)."""
    resistances = resistances or []
    immunities = immunities or []
    vulnerabilities = vulnerabilities or []
    total_taken = 0

    for dmg in damages:
        amount = dmg.amount
        if dmg.type in immunities:
            continue
        if dmg.type in resistances:
            amount = amount // 2
        if dmg.type in vulnerabilities:
            amount = amount * 2

        if temp_hp > 0:
            absorbed = min(temp_hp, amount)
            temp_hp -= absorbed
            amount -= absorbed
        current_hp = max(0, current_hp - amount)
        total_taken += amount

    return current_hp, temp_hp, total_taken


def apply_healing(current_hp: int, max_hp: int, amount: int) -> tuple[int, int]:
    """Apply healing. Returns (new_hp, actual_healed)."""
    new_hp = min(max_hp, current_hp + amount)
    return new_hp, new_hp - current_hp

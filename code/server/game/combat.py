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

    # Adv and disadv cancel
    if advantage and disadvantage:
        advantage = False
        disadvantage = False

    attack_roll = dice.roll_d20(to_hit_modifier, advantage=advantage, disadvantage=disadvantage)
    is_crit = dice.is_critical(attack_roll)
    is_fumble = dice.is_fumble(attack_roll)

    # Auto-crit on melee within 5 ft against paralyzed/unconscious
    if distance_ft <= 5 and "melee_attacks_within_5ft_auto_crit" in target_effects and attack_roll.total >= target_ac:
        is_crit = True

    hit = is_crit or (not is_fumble and attack_roll.total >= target_ac)

    result = AttackResult(
        attacker=attacker_name,
        target=target_name,
        attack_roll=attack_roll,
        target_ac=target_ac,
        hit=hit,
        critical=is_crit,
    )

    if hit:
        if is_crit:
            count, sides, modifier = dice.parse_dice(damage_dice)
            crit_dice = f"{count * 2}d{sides}{'+' if modifier >= 0 else '-'}{abs(modifier)}"
            damage_roll = dice.roll(crit_dice)
        else:
            damage_roll = dice.roll(damage_dice)
        result.damage_rolls.append(damage_roll)
        result.total_damage = damage_roll.total
        result.damage_types.append(damage_type)
        result.description = f"{attacker_name} hits {target_name} for {damage_roll.total} {damage_type} damage" + (" (CRIT!)" if is_crit else "")
    else:
        result.description = f"{attacker_name} misses {target_name}" + (" (fumble)" if is_fumble else "")

    return result


def make_save(
    target_name: str,
    ability: str,
    save_modifier: int,
    dc: int,
    target_conditions: list[str] = None,
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

    if advantage and disadvantage:
        advantage = False
        disadvantage = False

    roll_result = dice.roll_d20(save_modifier, advantage=advantage, disadvantage=disadvantage)
    success = roll_result.total >= dc
    return SaveResult(
        target=target_name, ability=ability, dc=dc, roll=roll_result, success=success,
        description=f"{target_name} {'succeeds' if success else 'fails'} {ability} save (rolled {roll_result.total} vs DC {dc})",
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

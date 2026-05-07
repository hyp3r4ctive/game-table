import random
import re
from dataclasses import dataclass, field


@dataclass
class RollResult:
    expression: str
    rolls: list[int] = field(default_factory=list)
    modifier: int = 0
    total: int = 0
    advantage: bool = False
    disadvantage: bool = False
    description: str = ""


def parse_dice(expression: str) -> tuple[int, int, int]:
    """Parse '2d6+3' -> (2, 6, 3). Modifier can be negative or absent."""
    expression = expression.replace(" ", "").lower()
    match = re.match(r"^(\d*)d(\d+)([+-]\d+)?$", expression)
    if not match:
        raise ValueError(f"Bad dice expression: {expression}")
    count = int(match.group(1)) if match.group(1) else 1
    sides = int(match.group(2))
    modifier = int(match.group(3)) if match.group(3) else 0
    return count, sides, modifier


def roll(expression: str, advantage: bool = False, disadvantage: bool = False) -> RollResult:
    """Roll a dice expression like '2d6+3' or '1d20'. Advantage/disadvantage only meaningful for single d20 rolls."""
    count, sides, modifier = parse_dice(expression)
    result = RollResult(expression=expression, modifier=modifier, advantage=advantage, disadvantage=disadvantage)

    if (advantage or disadvantage) and count == 1 and sides == 20:
        roll_a = random.randint(1, 20)
        roll_b = random.randint(1, 20)
        result.rolls = [roll_a, roll_b]
        chosen = max(roll_a, roll_b) if advantage else min(roll_a, roll_b)
        result.total = chosen + modifier
    else:
        result.rolls = [random.randint(1, sides) for _ in range(count)]
        result.total = sum(result.rolls) + modifier

    return result


def roll_d20(modifier: int = 0, advantage: bool = False, disadvantage: bool = False) -> RollResult:
    sign = "+" if modifier >= 0 else "-"
    return roll(f"1d20{sign}{abs(modifier)}", advantage=advantage, disadvantage=disadvantage)


def is_critical(result: RollResult) -> bool:
    """A natural 20 on a d20 is a critical hit."""
    if not result.rolls:
        return False
    if result.advantage:
        return max(result.rolls) == 20
    return result.rolls[0] == 20


def is_fumble(result: RollResult) -> bool:
    """A natural 1 on a d20 is a critical fail."""
    if not result.rolls:
        return False
    if result.disadvantage:
        return min(result.rolls) == 1
    return result.rolls[0] == 1


def ability_modifier(score: int) -> int:
    """Standard 5e ability modifier."""
    return (score - 10) // 2


def proficiency_bonus_for_level(level: int) -> int:
    """5e proficiency bonus by character level."""
    if level < 5:
        return 2
    if level < 9:
        return 3
    if level < 13:
        return 4
    if level < 17:
        return 5
    return 6

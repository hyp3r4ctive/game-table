from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Size(Enum):
    TINY = "tiny"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    HUGE = "huge"
    GARGANTUAN = "gargantuan"


class DamageType(Enum):
    SLASHING = "slashing"
    PIERCING = "piercing"
    BLUDGEONING = "bludgeoning"
    FIRE = "fire"
    COLD = "cold"
    LIGHTNING = "lightning"
    THUNDER = "thunder"
    ACID = "acid"
    POISON = "poison"
    NECROTIC = "necrotic"
    RADIANT = "radiant"
    PSYCHIC = "psychic"
    FORCE = "force"


class ConditionType(Enum):
    BLINDED = "blinded"
    CHARMED = "charmed"
    DEAFENED = "deafened"
    FRIGHTENED = "frightened"
    GRAPPLED = "grappled"
    INCAPACITATED = "incapacitated"
    INVISIBLE = "invisible"
    PARALYZED = "paralyzed"
    PETRIFIED = "petrified"
    POISONED = "poisoned"
    PRONE = "prone"
    RESTRAINED = "restrained"
    STUNNED = "stunned"
    UNCONSCIOUS = "unconscious"


@dataclass
class AbilityScores:
    strength: int = 10
    dexterity: int = 10
    constitution: int = 10
    intelligence: int = 10
    wisdom: int = 10
    charisma: int = 10

    def modifier(self, score: int) -> int:
        return (score - 10) // 2


@dataclass
class Position:
    x: int  # grid coordinates on battle map
    y: int

    def distance_to(self, other: "Position") -> int:
        # 5e diagonal: every other diagonal counts as 10ft
        dx = abs(self.x - other.x)
        dy = abs(self.y - other.y)
        return max(dx, dy) * 5


@dataclass
class Damage:
    amount: int
    type: DamageType


@dataclass
class Condition:
    type: ConditionType
    duration_rounds: Optional[int] = None  # None = until removed
    source: Optional[str] = None  # who/what applied it


@dataclass
class Spell:
    name: str
    level: int  # 0 for cantrips
    school: str
    casting_time: str
    range_ft: int
    components: list[str]  # ["V", "S", "M"]
    duration: str
    description: str
    damage_dice: Optional[str] = None  # e.g. "8d6"
    damage_type: Optional[DamageType] = None
    save_dc_ability: Optional[str] = None  # which ability saves
    requires_concentration: bool = False


@dataclass
class Item:
    name: str
    weight_lb: float
    value_gp: int
    description: str


@dataclass
class Weapon(Item):
    damage_dice: str = "1d4"
    damage_type: DamageType = DamageType.BLUDGEONING
    properties: list[str] = field(default_factory=list)  # finesse, versatile, etc
    range_ft: Optional[tuple[int, int]] = None  # (normal, long) for ranged


@dataclass
class Armor(Item):
    armor_class: int = 10
    armor_type: str = "light"  # light, medium, heavy, shield
    stealth_disadvantage: bool = False


@dataclass
class Action:
    """A thing a creature can do on its turn."""
    name: str
    description: str
    action_cost: str  # "action", "bonus_action", "reaction", "free"
    damage_dice: Optional[str] = None
    damage_type: Optional[DamageType] = None
    range_ft: Optional[int] = None
    save_dc: Optional[int] = None
    save_ability: Optional[str] = None


@dataclass
class Creature:
    """Base class for anything with stats - players, enemies, NPCs."""
    name: str
    size: Size
    creature_type: str  # humanoid, beast, dragon, etc.
    abilities: AbilityScores = field(default_factory=AbilityScores)
    max_hp: int = 1
    current_hp: int = 1
    temp_hp: int = 0
    armor_class: int = 10
    speed_ft: int = 30
    position: Optional[Position] = None
    conditions: list[Condition] = field(default_factory=list)
    resistances: list[DamageType] = field(default_factory=list)
    immunities: list[DamageType] = field(default_factory=list)
    vulnerabilities: list[DamageType] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    inventory: list[Item] = field(default_factory=list)
    initiative: int = 0  # rolled at start of combat

    def take_damage(self, damage: Damage) -> int:
        amount = damage.amount
        if damage.type in self.immunities:
            return 0
        if damage.type in self.resistances:
            amount = amount // 2
        if damage.type in self.vulnerabilities:
            amount = amount * 2
        if self.temp_hp > 0:
            absorbed = min(self.temp_hp, amount)
            self.temp_hp -= absorbed
            amount -= absorbed
        self.current_hp = max(0, self.current_hp - amount)
        return amount

    def heal(self, amount: int):
        self.current_hp = min(self.max_hp, self.current_hp + amount)

    def is_alive(self) -> bool:
        return self.current_hp > 0


@dataclass
class Player(Creature):
    """A player character."""
    player_id: str = ""  # which physical seat / which user
    character_class: str = ""
    level: int = 1
    experience: int = 0
    background: str = ""
    spells_known: list[Spell] = field(default_factory=list)
    spell_slots: dict[int, int] = field(default_factory=dict)  # level -> remaining
    proficiency_bonus: int = 2
    skills: list[str] = field(default_factory=list)  # proficient skills
    saving_throws: list[str] = field(default_factory=list)  # proficient saves
    death_saves_success: int = 0
    death_saves_failure: int = 0
    inspiration: bool = False


@dataclass
class Enemy(Creature):
    """A creature controlled by the DM."""
    challenge_rating: float = 0
    xp_value: int = 0
    is_legendary: bool = False
    legendary_actions: list[Action] = field(default_factory=list)
    lair_actions: list[Action] = field(default_factory=list)


@dataclass
class NPC(Creature):
    """A non-combat NPC."""
    occupation: str = ""
    disposition: str = "neutral"  # friendly, neutral, hostile
    dialogue_notes: str = ""


@dataclass
class TerrainFeature:
    name: str
    position: Position
    blocks_movement: bool = False
    blocks_sight: bool = False
    is_difficult_terrain: bool = False
    description: str = ""


@dataclass
class Environment:
    """The current scene."""
    name: str
    description: str
    width_squares: int = 20  # battle map size
    height_squares: int = 20
    terrain: list[TerrainFeature] = field(default_factory=list)
    lighting: str = "bright"  # bright, dim, dark
    background_audio: Optional[str] = None  # filename for ambient
    map_image: Optional[str] = None  # filename for projection


@dataclass
class CombatState:
    in_combat: bool = False
    round_number: int = 0
    turn_order: list[str] = field(default_factory=list)  # creature names in init order
    current_turn_index: int = 0

    def current_creature(self) -> Optional[str]:
        if not self.in_combat or not self.turn_order:
            return None
        return self.turn_order[self.current_turn_index]

    def next_turn(self):
        self.current_turn_index = (self.current_turn_index + 1) % len(self.turn_order)
        if self.current_turn_index == 0:
            self.round_number += 1


@dataclass
class GameSession:
    """The whole world state."""
    session_id: str
    dm_id: str
    players: dict[str, Player] = field(default_factory=dict)
    enemies: dict[str, Enemy] = field(default_factory=dict)
    npcs: dict[str, NPC] = field(default_factory=dict)
    environment: Optional[Environment] = None
    combat: CombatState = field(default_factory=CombatState)
    session_log: list[str] = field(default_factory=list)  # event history

    def all_creatures(self) -> list[Creature]:
        return list(self.players.values()) + list(self.enemies.values()) + list(self.npcs.values())

    def get_creature(self, name: str) -> Optional[Creature]:
        for c in self.all_creatures():
            if c.name == name:
                return c
        return None

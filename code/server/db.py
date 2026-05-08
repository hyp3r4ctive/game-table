from sqlmodel import SQLModel, Field, Relationship, create_engine, Session
from sqlalchemy import Column, JSON
from typing import Optional
from datetime import datetime

DATABASE_URL = "sqlite:///./game.db"
engine = create_engine(DATABASE_URL, echo=False)


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Character(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    owner_id: int = Field(foreign_key="user.id")
    name: str
    player_name: str = ""
    race: str = ""
    subrace: str = ""
    character_class: str = ""
    subclass: str = ""
    background: str = ""
    alignment: str = ""
    level: int = 1
    experience_points: int = 0
    max_hp: int = 10
    current_hp: int = 10
    temp_hp: int = 0
    armor_class: int = 10
    speed_ft: int = 30
    initiative_bonus: int = 0
    hit_die: str = "d8"
    hit_dice_used: int = 0
    strength: int = 10
    dexterity: int = 10
    constitution: int = 10
    intelligence: int = 10
    wisdom: int = 10
    charisma: int = 10
    spellcasting_ability: str = ""
    inspiration: bool = False
    darkvision_ft: int = 0
    vision_normal_ft: int = 0
    light_emission_ft: int = 0
    saving_throw_profs: list = Field(default_factory=list, sa_column=Column(JSON))
    skill_profs: list = Field(default_factory=list, sa_column=Column(JSON))
    skill_expertises: list = Field(default_factory=list, sa_column=Column(JSON))
    languages: list = Field(default_factory=list, sa_column=Column(JSON))
    features: list = Field(default_factory=list, sa_column=Column(JSON))
    equipment: list = Field(default_factory=list, sa_column=Column(JSON))
    money: dict = Field(default_factory=dict, sa_column=Column(JSON))
    spells_known: list = Field(default_factory=list, sa_column=Column(JSON))
    spell_slots_max: dict = Field(default_factory=dict, sa_column=Column(JSON))
    spell_slots_used: dict = Field(default_factory=dict, sa_column=Column(JSON))
    personality_traits: str = ""
    ideals: str = ""
    bonds: str = ""
    flaws: str = ""
    backstory: str = ""
    is_template: bool = True
    is_enemy: bool = False
    notes: str = ""
    dm_notes: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Campaign(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    dm_id: int = Field(foreign_key="user.id")
    name: str
    description: str = ""
    is_one_shot: bool = False
    is_active: bool = True
    persistent_death_saves: bool = False
    rules: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CampaignMember(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id")
    user_id: int = Field(foreign_key="user.id")
    role: str  # "dm" or "player"


class CampaignCharacter(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id")
    character_id: int = Field(foreign_key="character.id")
    role: str  # "player_character", "npc", "enemy", "ally"
    is_active: bool = True


class JoinRequest(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id")
    user_id: int = Field(foreign_key="user.id")
    character_id: Optional[int] = Field(default=None, foreign_key="character.id")
    message: str = ""
    status: str = "pending"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class GameSession(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id")
    dm_id: int = Field(foreign_key="user.id")
    is_active: bool = True
    pushed_to_table: bool = False
    in_combat: bool = False
    round_number: int = 0
    current_turn_index: int = 0
    initiative_order: list = Field(default_factory=list, sa_column=Column(JSON))
    # Per-turn state for the current creature, reset on next_turn().
    action_used: bool = False
    bonus_action_used: bool = False
    reaction_used: bool = False
    movement_used_ft: int = 0
    movement_extra_ft: int = 0  # bonus from Dash, Haste, etc.
    is_dodging: bool = False
    is_disengaging: bool = False
    event_log: list = Field(default_factory=list, sa_column=Column(JSON))
    seat_assignments: dict = Field(default_factory=dict, sa_column=Column(JSON))
    seat_overrides: dict = Field(default_factory=dict, sa_column=Column(JSON))
    active_map_id: Optional[int] = Field(default=None, foreign_key="map.id")
    fog_revealed: list = Field(default_factory=list, sa_column=Column(JSON))
    drawings: list = Field(default_factory=list, sa_column=Column(JSON))
    pending_actions: list = Field(default_factory=list, sa_column=Column(JSON))
    pending_walk: dict = Field(default_factory=dict, sa_column=Column(JSON))
    pending_aoe: dict = Field(default_factory=dict, sa_column=Column(JSON))
    pending_reaction: dict = Field(default_factory=dict, sa_column=Column(JSON))
    undo_stack: list = Field(default_factory=list, sa_column=Column(JSON))
    started_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = None


class Map(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id")
    name: str
    image_path: Optional[str] = None
    grid_cols: int = 60
    grid_rows: int = 48
    grid_type: str = "square"  # "square" or "hex" (pointy-top)
    feet_per_square: int = 5  # in-game distance per cell (D&D 5e default)
    inches_per_square: float = 1.0  # physical inches per cell on the table
    walls: list = Field(default_factory=list, sa_column=Column(JSON))
    zones: list = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Spell(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: Optional[int] = Field(default=None, foreign_key="campaign.id", index=True)
    key: str = Field(index=True)
    name: str
    level: int = 0
    school: str = ""
    casting_time: str = "action"
    range_ft: int = 0
    duration: str = "instantaneous"
    concentration: bool = False
    components: list = Field(default_factory=list, sa_column=Column(JSON))
    material_component: str = ""
    effect_type: str = ""
    requires_sight: bool = True
    target_type: str = "creature_seen"
    damage: list = Field(default_factory=list, sa_column=Column(JSON))
    healing: dict = Field(default_factory=dict, sa_column=Column(JSON))
    save: dict = Field(default_factory=dict, sa_column=Column(JSON))
    area: dict = Field(default_factory=dict, sa_column=Column(JSON))
    attack: dict = Field(default_factory=dict, sa_column=Column(JSON))
    darts: dict = Field(default_factory=dict, sa_column=Column(JSON))
    beams: dict = Field(default_factory=dict, sa_column=Column(JSON))
    hp_threshold: dict = Field(default_factory=dict, sa_column=Column(JSON))
    conditions_applied: list = Field(default_factory=list, sa_column=Column(JSON))  # legacy
    applies_effects: list = Field(default_factory=list, sa_column=Column(JSON))
    scaling: dict = Field(default_factory=dict, sa_column=Column(JSON))
    max_targets: Optional[int] = None
    valid_targets: str = ""
    description: str = ""
    is_homebrew: bool = False
    created_by_user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class LiveCharacter(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="gamesession.id")
    source_character_id: Optional[int] = Field(default=None, foreign_key="character.id")
    owner_id: Optional[int] = Field(default=None, foreign_key="user.id")
    name: str
    character_class: str = ""
    level: int = 1
    max_hp: int = 10
    current_hp: int = 10
    temp_hp: int = 0
    armor_class: int = 10
    speed_ft: int = 30
    strength: int = 10
    dexterity: int = 10
    constitution: int = 10
    intelligence: int = 10
    wisdom: int = 10
    charisma: int = 10
    is_enemy: bool = False
    is_active: bool = True
    initiative: int = 0
    position_x: Optional[int] = None
    position_y: Optional[int] = None
    darkvision_ft: int = 0
    vision_normal_ft: int = 0
    light_emission_ft: int = 0
    conditions: list = Field(default_factory=list, sa_column=Column(JSON))
    spell_slots: dict = Field(default_factory=dict, sa_column=Column(JSON))
    saving_throw_profs: list = Field(default_factory=list, sa_column=Column(JSON))
    # 5e death-save state: PCs at 0 HP roll saves at start of turn.
    death_save_successes: int = 0
    death_save_failures: int = 0
    is_dead: bool = False
    is_stable: bool = False
    reaction_used: bool = False
    damage_resistances: list = Field(default_factory=list, sa_column=Column(JSON))
    damage_immunities: list = Field(default_factory=list, sa_column=Column(JSON))
    damage_vulnerabilities: list = Field(default_factory=list, sa_column=Column(JSON))
    melee_reach_ft: int = 5
    attacks_per_action: int = 1
    attacks_remaining_this_action: int = 0
    sneak_attack_dice: int = 0
    sneak_attack_used_this_turn: bool = False
    class_features: list = Field(default_factory=list, sa_column=Column(JSON))


class ActiveEffect(SQLModel, table=True):
    """In-flight effect on a live character or on an area of the map.

    target_live_id null  → area effect (use `area` for shape/position)
    handler_key blank    → freeform/noted-only (engine ticks duration, no math hook)
    is_concentration true → tied to caster's concentration; only one per caster
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="gamesession.id", index=True)
    target_live_id: Optional[int] = Field(default=None, foreign_key="livecharacter.id", index=True)
    caster_live_id: Optional[int] = Field(default=None, foreign_key="livecharacter.id")
    spell_key: str = ""
    name: str
    description: str = ""
    handler_key: str = ""
    is_concentration: bool = False
    duration_rounds: Optional[int] = None
    duration_basis: str = "caster_end_of_turn"
    save_each_turn: dict = Field(default_factory=dict, sa_column=Column(JSON))
    area: dict = Field(default_factory=dict, sa_column=Column(JSON))
    payload: dict = Field(default_factory=dict, sa_column=Column(JSON))
    started_round: int = 0
    started_turn_index: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)


_SQLITE_TYPE_MAP = {
    "INTEGER": "INTEGER",
    "TEXT": "TEXT",
    "VARCHAR": "TEXT",
    "BOOLEAN": "INTEGER",
    "FLOAT": "REAL",
    "DATETIME": "TEXT",
    "JSON": "TEXT",
}


def _sa_type_to_sqlite(col_type) -> str:
    name = type(col_type).__name__.upper()
    return _SQLITE_TYPE_MAP.get(name, "TEXT")


def _sqlite_default(col) -> str | None:
    if col.default is not None and not callable(col.default.arg if hasattr(col.default, "arg") else col.default):
        val = col.default.arg if hasattr(col.default, "arg") else col.default
        if isinstance(val, bool):
            return "1" if val else "0"
        if isinstance(val, (int, float)):
            return str(val)
        if isinstance(val, str):
            return f"'{val.replace(chr(39), chr(39)*2)}'"
    return None


def _migrate_add_missing_columns():
    """Add columns to existing tables that the model declares but the DB lacks. SQLite-only."""
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table_name, table in SQLModel.metadata.tables.items():
            if table_name not in existing_tables:
                continue
            existing_cols = {c["name"] for c in inspector.get_columns(table_name)}
            for col in table.columns:
                if col.name in existing_cols:
                    continue
                type_name = type(col.type).__name__.upper()
                col_type = _sa_type_to_sqlite(col.type)
                default_clause = ""
                if type_name == "JSON":
                    default_clause = ""  # leave NULL; code handles None
                else:
                    default_val = _sqlite_default(col)
                    if default_val is not None:
                        default_clause = f" DEFAULT {default_val}"
                    elif not col.nullable and col.default is None:
                        default_clause = " DEFAULT ''" if col_type == "TEXT" else " DEFAULT 0"
                stmt = f'ALTER TABLE "{table_name}" ADD COLUMN "{col.name}" {col_type}{default_clause}'
                conn.execute(text(stmt))


def init_db():
    SQLModel.metadata.create_all(engine)
    _migrate_add_missing_columns()


def get_session():
    with Session(engine) as session:
        yield session

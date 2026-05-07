from sqlmodel import SQLModel, Field, Relationship, create_engine, Session
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
    character_class: str = ""
    level: int = 1
    max_hp: int = 10
    current_hp: int = 10
    armor_class: int = 10
    speed_ft: int = 30
    strength: int = 10
    dexterity: int = 10
    constitution: int = 10
    intelligence: int = 10
    wisdom: int = 10
    charisma: int = 10
    is_template: bool = True
    is_enemy: bool = False
    notes: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Campaign(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    dm_id: int = Field(foreign_key="user.id")
    name: str
    description: str = ""
    is_one_shot: bool = False
    is_active: bool = True
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


def init_db():
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session

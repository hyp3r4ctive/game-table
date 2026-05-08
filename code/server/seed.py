"""Reset and seed the database with test data.

Usage (from code/server, with venv active):
    python seed.py            # nuke + seed default test data
    python seed.py --keep     # seed test data without nuking existing
    python seed.py --nuke     # nuke only, no seeding

Default seed creates:
    - DM:     username 'dm'        password 'pw'
    - Players: 'player1'..'player6' password 'pw'
    - Campaign 'Test Campaign' owned by dm, with all 6 players approved as
      members; each player has a level-5 character assigned as a player_character.
"""

import argparse
import os
from pathlib import Path
from sqlmodel import Session, select

from db import (
    engine, init_db,
    User, Character, Campaign, CampaignMember, CampaignCharacter,
    GameSession, LiveCharacter, Map, JoinRequest,
)
from auth import hash_password


# Six varied SRD-ish parties for testing visibility / classes / vision
PARTY = [
    {
        "user": "player1", "name": "Drizzt", "race": "elf", "subrace": "drow",
        "character_class": "ranger", "subclass": "Hunter", "level": 5,
        "max_hp": 44, "armor_class": 16, "speed_ft": 30,
        "strength": 14, "dexterity": 18, "constitution": 14,
        "intelligence": 12, "wisdom": 16, "charisma": 10,
        "darkvision_ft": 120, "vision_normal_ft": 0, "light_emission_ft": 0,
        "hit_die": "d10",
        "saving_throw_profs": ["strength", "dexterity"],
        "skill_profs": ["Stealth", "Perception", "Survival"],
        "languages": ["Common", "Elvish", "Undercommon"],
    },
    {
        "user": "player2", "name": "Thorin", "race": "dwarf", "subrace": "mountain_dwarf",
        "character_class": "fighter", "subclass": "Champion", "level": 5,
        "max_hp": 52, "armor_class": 18, "speed_ft": 25,
        "strength": 17, "dexterity": 12, "constitution": 16,
        "intelligence": 10, "wisdom": 12, "charisma": 8,
        "darkvision_ft": 60, "vision_normal_ft": 0, "light_emission_ft": 0,
        "hit_die": "d10",
        "saving_throw_profs": ["strength", "constitution"],
        "skill_profs": ["Athletics", "Intimidation"],
        "languages": ["Common", "Dwarvish"],
    },
    {
        "user": "player3", "name": "Pipsqueak", "race": "halfling", "subrace": "lightfoot",
        "character_class": "rogue", "subclass": "Thief", "level": 5,
        "max_hp": 33, "armor_class": 15, "speed_ft": 25,
        "strength": 8, "dexterity": 18, "constitution": 12,
        "intelligence": 14, "wisdom": 12, "charisma": 14,
        "darkvision_ft": 0, "vision_normal_ft": 0, "light_emission_ft": 30,  # holds a torch
        "hit_die": "d8",
        "saving_throw_profs": ["dexterity", "intelligence"],
        "skill_profs": ["Stealth", "Sleight of Hand", "Acrobatics", "Perception"],
        "skill_expertises": ["Stealth", "Sleight of Hand"],
        "languages": ["Common", "Halfling", "Thieves' Cant"],
    },
    {
        "user": "player4", "name": "Vexora", "race": "elf", "subrace": "high_elf",
        "character_class": "wizard", "subclass": "School of Evocation", "level": 5,
        "max_hp": 28, "armor_class": 12, "speed_ft": 30,
        "strength": 8, "dexterity": 14, "constitution": 14,
        "intelligence": 18, "wisdom": 13, "charisma": 11,
        "darkvision_ft": 60, "vision_normal_ft": 0, "light_emission_ft": 0,
        "hit_die": "d6", "spellcasting_ability": "intelligence",
        "saving_throw_profs": ["intelligence", "wisdom"],
        "skill_profs": ["Arcana", "Investigation", "History"],
        "languages": ["Common", "Elvish", "Draconic"],
        "spell_slots_max": {"1": 4, "2": 3, "3": 2},
        "spells_known": ["fire_bolt", "magic_missile", "shield"],
    },
    {
        "user": "player5", "name": "Ser Aldric", "race": "human", "subrace": "",
        "character_class": "paladin", "subclass": "Oath of Devotion", "level": 5,
        "max_hp": 49, "armor_class": 18, "speed_ft": 30,
        "strength": 16, "dexterity": 10, "constitution": 14,
        "intelligence": 10, "wisdom": 12, "charisma": 16,
        "darkvision_ft": 0, "vision_normal_ft": 0, "light_emission_ft": 0,
        "hit_die": "d10", "spellcasting_ability": "charisma",
        "saving_throw_profs": ["wisdom", "charisma"],
        "skill_profs": ["Athletics", "Persuasion", "Religion"],
        "languages": ["Common", "Celestial"],
        "spell_slots_max": {"1": 4, "2": 2},
        "spells_known": ["bless", "cure_wounds"],
    },
    {
        "user": "player6", "name": "Zylvara", "race": "tiefling", "subrace": "",
        "character_class": "warlock", "subclass": "The Fiend", "level": 5,
        "max_hp": 38, "armor_class": 13, "speed_ft": 30,
        "strength": 8, "dexterity": 14, "constitution": 14,
        "intelligence": 12, "wisdom": 11, "charisma": 18,
        "darkvision_ft": 60, "vision_normal_ft": 0, "light_emission_ft": 0,
        "hit_die": "d8", "spellcasting_ability": "charisma",
        "saving_throw_profs": ["wisdom", "charisma"],
        "skill_profs": ["Deception", "Arcana", "Persuasion"],
        "languages": ["Common", "Infernal"],
        "spell_slots_max": {"3": 2},  # warlock pact magic, 2 slots at level 3
        "spells_known": ["fire_bolt"],
    },
]


def nuke():
    """Drop the SQLite file outright. Cleaner than DELETE FROM since it
    rebuilds the schema fresh and avoids stale auto-increments."""
    db_path = Path(__file__).parent / "game.db"
    if db_path.exists():
        db_path.unlink()
        print(f"deleted {db_path}")
    # Also clear any uploaded maps (preserve .gitkeep)
    maps_dir = Path(__file__).parent / "static" / "maps"
    if maps_dir.exists():
        for child in maps_dir.iterdir():
            if child.name == ".gitkeep":
                continue
            if child.is_dir():
                for f in child.iterdir():
                    f.unlink()
                child.rmdir()
            else:
                child.unlink()
        print(f"cleared {maps_dir}")


def seed():
    init_db()
    with Session(engine) as s:
        # DM
        dm = User(username="dm", password_hash=hash_password("pw"))
        s.add(dm); s.commit(); s.refresh(dm)

        # Campaign
        campaign = Campaign(dm_id=dm.id, name="Test Campaign",
                            description="Auto-seeded test campaign.")
        s.add(campaign); s.commit(); s.refresh(campaign)
        s.add(CampaignMember(campaign_id=campaign.id, user_id=dm.id, role="dm"))

        # Players + characters
        for spec in PARTY:
            u = User(username=spec["user"], password_hash=hash_password("pw"))
            s.add(u); s.commit(); s.refresh(u)
            ch = Character(
                owner_id=u.id,
                name=spec["name"],
                race=spec["race"], subrace=spec.get("subrace", ""),
                character_class=spec["character_class"],
                subclass=spec.get("subclass", ""),
                level=spec["level"],
                max_hp=spec["max_hp"], current_hp=spec["max_hp"],
                armor_class=spec["armor_class"], speed_ft=spec["speed_ft"],
                strength=spec["strength"], dexterity=spec["dexterity"],
                constitution=spec["constitution"], intelligence=spec["intelligence"],
                wisdom=spec["wisdom"], charisma=spec["charisma"],
                darkvision_ft=spec["darkvision_ft"],
                vision_normal_ft=spec["vision_normal_ft"],
                light_emission_ft=spec["light_emission_ft"],
                hit_die=spec.get("hit_die", "d8"),
                spellcasting_ability=spec.get("spellcasting_ability", ""),
                saving_throw_profs=spec.get("saving_throw_profs", []),
                skill_profs=spec.get("skill_profs", []),
                skill_expertises=spec.get("skill_expertises", []),
                languages=spec.get("languages", ["Common"]),
                spell_slots_max=spec.get("spell_slots_max", {}),
                spells_known=spec.get("spells_known", []),
            )
            s.add(ch); s.commit(); s.refresh(ch)
            s.add(CampaignMember(campaign_id=campaign.id, user_id=u.id, role="player"))
            s.add(CampaignCharacter(campaign_id=campaign.id, character_id=ch.id,
                                    role="player_character"))
        s.commit()

        # A starter blank-grid map so the DM can immediately see the battle map UI
        m = Map(campaign_id=campaign.id, name="Blank arena",
                grid_cols=60, grid_rows=48, grid_type="square")
        s.add(m); s.commit()

    print("seeded:")
    print("  DM:       dm / pw")
    print("  Players:  player1..player6 / pw")
    print("  Campaign: 'Test Campaign' (1 DM, 6 PCs, 1 blank 60x48 map)")


def main():
    p = argparse.ArgumentParser(description="Reset/seed the game DB.")
    p.add_argument("--nuke", action="store_true", help="delete game.db and exit (no seed)")
    p.add_argument("--keep", action="store_true", help="seed without nuking existing data")
    args = p.parse_args()

    if args.nuke and args.keep:
        p.error("--nuke and --keep are mutually exclusive")

    if args.nuke:
        nuke()
        return
    if not args.keep:
        nuke()
    seed()


if __name__ == "__main__":
    main()

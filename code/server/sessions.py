import asyncio
import json
from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from datetime import datetime
from typing import Optional

from db import (
    get_session,
    User,
    Character,
    Campaign,
    CampaignMember,
    CampaignCharacter,
    GameSession,
    LiveCharacter,
    Map,
    engine,
)
from auth import get_current_user
from game import dice, combat, conditions, spells, grid
import events
import vision as vision_mod

router = APIRouter()
templates = Jinja2Templates(directory="templates")

EVENT_LOG_LIMIT = 50


def _ability_mod(score: int) -> int:
    return (score - 10) // 2


def _default_spell_slots(level: int) -> dict:
    if level >= 5:
        return {"1": 4, "2": 3, "3": 2}
    if level >= 3:
        return {"1": 4, "2": 2}
    if level >= 1:
        return {"1": 2}
    return {}


def _log(db_session: Session, gs: GameSession, message: str):
    log = list(gs.event_log or [])
    log.insert(0, {"t": datetime.utcnow().isoformat(timespec="seconds"), "msg": message})
    gs.event_log = log[:EVENT_LOG_LIMIT]
    db_session.add(gs)


def _commit_and_publish(db_session: Session, session_id: int):
    db_session.commit()
    events.publish(session_id)


def _require_session(db_session: Session, session_id: int, user: User, dm_only: bool = False) -> GameSession:
    gs = db_session.get(GameSession, session_id)
    if not gs or not gs.is_active:
        raise HTTPException(404, "session not found")
    if dm_only and gs.dm_id != user.id:
        raise HTTPException(403, "DM only")
    return gs


def _require_live(db_session: Session, session_id: int, live_id: int) -> LiveCharacter:
    lc = db_session.get(LiveCharacter, live_id)
    if not lc or lc.session_id != session_id:
        raise HTTPException(404, "live character not found")
    return lc


def _live_to_combat_tracker(gs: GameSession) -> combat.CombatTracker:
    tracker = combat.CombatTracker(
        in_combat=gs.in_combat,
        round_number=gs.round_number,
        initiative_order=[(entry["name"], entry["init"]) for entry in (gs.initiative_order or [])],
        current_index=gs.current_turn_index,
    )
    return tracker


@router.post("/campaigns/{campaign_id}/sessions/start")
def start_session(
    campaign_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404)
    if campaign.dm_id != user.id:
        raise HTTPException(403, "only the DM can start a session")

    existing = db.exec(
        select(GameSession).where(GameSession.campaign_id == campaign_id, GameSession.is_active == True)
    ).first()
    if existing:
        return RedirectResponse(f"/sessions/{existing.id}", status_code=303)

    gs = GameSession(campaign_id=campaign_id, dm_id=user.id)
    db.add(gs)
    db.commit()
    db.refresh(gs)

    cc_rows = db.exec(
        select(CampaignCharacter).where(
            CampaignCharacter.campaign_id == campaign_id,
            CampaignCharacter.is_active == True,
            CampaignCharacter.role == "player_character",
        )
    ).all()
    for i, cc in enumerate(cc_rows):
        char = db.get(Character, cc.character_id)
        if not char:
            continue
        lc = LiveCharacter(
            session_id=gs.id,
            source_character_id=char.id,
            owner_id=char.owner_id,
            name=char.name,
            character_class=char.character_class,
            level=char.level,
            max_hp=char.max_hp,
            current_hp=char.current_hp,
            armor_class=char.armor_class,
            speed_ft=char.speed_ft,
            strength=char.strength,
            dexterity=char.dexterity,
            constitution=char.constitution,
            intelligence=char.intelligence,
            wisdom=char.wisdom,
            charisma=char.charisma,
            is_enemy=False,
            spell_slots=_default_spell_slots(char.level),
            position_x=2 + i,
            position_y=2,
            darkvision_ft=getattr(char, "darkvision_ft", 0) or 0,
            vision_normal_ft=getattr(char, "vision_normal_ft", 0) or 0,
            light_emission_ft=getattr(char, "light_emission_ft", 0) or 0,
        )
        db.add(lc)

    _log(db, gs, f"Session started by {user.username}")
    db.commit()
    events.publish(gs.id)
    return RedirectResponse(f"/sessions/{gs.id}", status_code=303)


@router.post("/sessions/{session_id}/push")
def push_session_to_table(
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user, dm_only=True)
    others = db.exec(select(GameSession).where(GameSession.pushed_to_table == True, GameSession.id != session_id)).all()
    for other in others:
        other.pushed_to_table = False
        db.add(other)
    gs.pushed_to_table = True
    _log(db, gs, "Session pushed to table")
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/unpush")
def unpush_session_from_table(
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user, dm_only=True)
    gs.pushed_to_table = False
    gs.seat_assignments = {}
    _log(db, gs, "Session pulled from table")
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/end")
def end_session(
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user, dm_only=True)
    gs.is_active = False
    gs.in_combat = False
    gs.ended_at = datetime.utcnow()
    _log(db, gs, "Session ended")
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/campaigns/{gs.campaign_id}", status_code=303)


@router.get("/sessions/{session_id}", response_class=HTMLResponse)
def view_session(
    session_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = db.get(GameSession, session_id)
    if not gs or not gs.is_active:
        raise HTTPException(404)
    is_dm = gs.dm_id == user.id
    is_member = db.exec(
        select(CampaignMember).where(
            CampaignMember.campaign_id == gs.campaign_id,
            CampaignMember.user_id == user.id,
        )
    ).first() is not None
    if not is_dm and not is_member:
        raise HTTPException(403, "not a member of this campaign")
    campaign = db.get(Campaign, gs.campaign_id)
    lcs = db.exec(select(LiveCharacter).where(LiveCharacter.session_id == session_id)).all()
    lcs = sorted(lcs, key=lambda c: (c.is_enemy, c.name))
    current_name = None
    if gs.in_combat and gs.initiative_order:
        idx = gs.current_turn_index
        if 0 <= idx < len(gs.initiative_order):
            current_name = gs.initiative_order[idx]["name"]
    spell_list = sorted(
        [{"key": k, "name": v["name"], "level": v["level"]} for k, v in spells.SPELLS.items()],
        key=lambda s: (s["level"], s["name"]),
    )
    condition_list = sorted(conditions.list_all_conditions(), key=lambda c: c["name"])
    campaign_maps = db.exec(select(Map).where(Map.campaign_id == gs.campaign_id)).all()
    active_map = db.get(Map, gs.active_map_id) if gs.active_map_id else None
    return templates.TemplateResponse(request, "session.html", {
        "user": user,
        "campaign": campaign,
        "gs": gs,
        "is_dm": is_dm,
        "live_characters": lcs,
        "current_name": current_name,
        "spells": spell_list,
        "conditions": condition_list,
        "campaign_maps": campaign_maps,
        "active_map": active_map,
    })


@router.post("/sessions/{session_id}/combat/start")
def start_combat(
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user, dm_only=True)
    lcs = db.exec(select(LiveCharacter).where(LiveCharacter.session_id == session_id, LiveCharacter.is_active == True)).all()
    if not lcs:
        raise HTTPException(400, "no active characters")
    rolls = []
    for lc in lcs:
        active_conds = [c["name"] for c in (lc.conditions or [])]
        roll = combat.roll_initiative(_ability_mod(lc.dexterity), active_conds)
        lc.initiative = roll.total
        rolls.append({"id": lc.id, "name": lc.name, "init": roll.total})
        db.add(lc)
    rolls.sort(key=lambda r: -r["init"])
    gs.initiative_order = rolls
    gs.current_turn_index = 0
    gs.round_number = 1
    gs.in_combat = True
    order_str = ", ".join(f"{r['name']} ({r['init']})" for r in rolls)
    _log(db, gs, f"Combat started. Initiative: {order_str}")
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/combat/end")
def end_combat(
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user, dm_only=True)
    gs.in_combat = False
    gs.initiative_order = []
    gs.current_turn_index = 0
    gs.round_number = 0
    _log(db, gs, "Combat ended")
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/combat/next-turn")
def next_turn(
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user, dm_only=True)
    if not gs.in_combat or not gs.initiative_order:
        raise HTTPException(400, "not in combat")
    gs.current_turn_index = (gs.current_turn_index + 1) % len(gs.initiative_order)
    if gs.current_turn_index == 0:
        gs.round_number += 1
        _log(db, gs, f"-- Round {gs.round_number} --")
    name = gs.initiative_order[gs.current_turn_index]["name"]
    _log(db, gs, f"{name}'s turn")
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/dice/roll")
def roll_dice(
    session_id: int,
    expression: str = Form(),
    advantage: bool = Form(False),
    disadvantage: bool = Form(False),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user)
    try:
        result = dice.roll(expression, advantage=advantage, disadvantage=disadvantage)
    except ValueError as e:
        raise HTTPException(400, str(e))
    rolls_str = ",".join(str(r) for r in result.rolls)
    tag = " adv" if advantage else (" dis" if disadvantage else "")
    _log(db, gs, f"{user.username} rolled {expression}{tag}: [{rolls_str}] = {result.total}")
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/attack")
def attack(
    session_id: int,
    attacker_id: int = Form(),
    target_id: int = Form(),
    to_hit_modifier: int = Form(0),
    damage_dice: str = Form("1d6"),
    damage_type: str = Form("slashing"),
    distance_ft: int = Form(5),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user)
    attacker = _require_live(db, session_id, attacker_id)
    target = _require_live(db, session_id, target_id)
    attacker_conds = [c["name"] for c in (attacker.conditions or [])]
    target_conds = [c["name"] for c in (target.conditions or [])]
    result = combat.make_attack(
        attacker.name, target.name, to_hit_modifier, target.armor_class,
        damage_dice, damage_type, attacker_conds, target_conds, distance_ft,
    )
    if result.hit:
        target.current_hp, target.temp_hp, taken = combat.apply_damage(
            target.current_hp, target.temp_hp, target.max_hp,
            [combat.DamageInstance(amount=result.total_damage, type=damage_type)],
        )
        db.add(target)
        msg = f"{result.description} (HP: {target.current_hp}/{target.max_hp})"
    else:
        msg = result.description
    _log(db, gs, msg)
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/save")
def save_throw(
    session_id: int,
    target_id: int = Form(),
    ability: str = Form(),
    save_modifier: int = Form(0),
    dc: int = Form(),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user)
    target = _require_live(db, session_id, target_id)
    target_conds = [c["name"] for c in (target.conditions or [])]
    result = combat.make_save(target.name, ability, save_modifier, dc, target_conds)
    _log(db, gs, result.description)
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/damage")
def apply_damage(
    session_id: int,
    target_id: int = Form(),
    amount: int = Form(),
    damage_type: str = Form("untyped"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user)
    target = _require_live(db, session_id, target_id)
    target.current_hp, target.temp_hp, taken = combat.apply_damage(
        target.current_hp, target.temp_hp, target.max_hp,
        [combat.DamageInstance(amount=amount, type=damage_type)],
    )
    db.add(target)
    _log(db, gs, f"{target.name} takes {taken} {damage_type} damage (HP: {target.current_hp}/{target.max_hp})")
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/heal")
def apply_heal(
    session_id: int,
    target_id: int = Form(),
    amount: int = Form(),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user)
    target = _require_live(db, session_id, target_id)
    new_hp, healed = combat.apply_healing(target.current_hp, target.max_hp, amount)
    target.current_hp = new_hp
    db.add(target)
    _log(db, gs, f"{target.name} heals {healed} (HP: {target.current_hp}/{target.max_hp})")
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/conditions/add")
def add_condition(
    session_id: int,
    target_id: int = Form(),
    condition_name: str = Form(),
    duration_rounds: Optional[int] = Form(None),
    source: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user)
    target = _require_live(db, session_id, target_id)
    if not conditions.get_condition(condition_name):
        raise HTTPException(400, f"unknown condition: {condition_name}")
    conds = list(target.conditions or [])
    if any(c["name"] == condition_name for c in conds):
        return RedirectResponse(f"/sessions/{session_id}", status_code=303)
    conds.append({"name": condition_name, "duration_rounds": duration_rounds, "source": source or None})
    target.conditions = conds
    db.add(target)
    _log(db, gs, f"{target.name} gains {condition_name}")
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/conditions/remove")
def remove_condition(
    session_id: int,
    target_id: int = Form(),
    condition_name: str = Form(),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user)
    target = _require_live(db, session_id, target_id)
    conds = [c for c in (target.conditions or []) if c["name"] != condition_name]
    target.conditions = conds
    db.add(target)
    _log(db, gs, f"{target.name} loses {condition_name}")
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/cast")
def cast(
    session_id: int,
    spell_name: str = Form(),
    caster_id: int = Form(),
    slot_level: int = Form(1),
    target_ids: str = Form(""),
    aoe_x: Optional[int] = Form(None),
    aoe_y: Optional[int] = Form(None),
    aoe_dx: int = Form(1),
    aoe_dy: int = Form(0),
    spell_save_dc: int = Form(13),
    spell_attack_modifier: int = Form(5),
    spellcasting_modifier: int = Form(3),
    enforce_slots: bool = Form(False),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user)
    caster = _require_live(db, session_id, caster_id)
    spell = spells.get_spell(spell_name)
    if not spell:
        raise HTTPException(400, f"unknown spell: {spell_name}")

    if spell["level"] > 0 and enforce_slots:
        slots = dict(caster.spell_slots or {})
        key = str(slot_level)
        if slots.get(key, 0) <= 0:
            raise HTTPException(400, f"no level {slot_level} slots remaining")
        slots[key] = slots[key] - 1
        caster.spell_slots = slots
        db.add(caster)
    elif spell["level"] > 0:
        slots = dict(caster.spell_slots or {})
        key = str(slot_level)
        if slots.get(key, 0) > 0:
            slots[key] = slots[key] - 1
            caster.spell_slots = slots
            db.add(caster)

    target_id_list = [int(x) for x in target_ids.split(",") if x.strip()]
    target_lcs = []
    for tid in target_id_list:
        lc = db.get(LiveCharacter, tid)
        if lc and lc.session_id == session_id:
            target_lcs.append(lc)

    creatures_in_range = []
    if aoe_x is not None and aoe_y is not None:
        all_lcs = db.exec(select(LiveCharacter).where(LiveCharacter.session_id == session_id, LiveCharacter.is_active == True)).all()
        for lc in all_lcs:
            if lc.position_x is not None and lc.position_y is not None:
                creatures_in_range.append(_PosWrapper(lc.name, lc.position_x, lc.position_y, lc.id))

    aoe_origin = grid.GridPoint(aoe_x, aoe_y) if aoe_x is not None and aoe_y is not None else None

    result = spells.cast_spell(
        spell_name=spell_name,
        caster_name=caster.name,
        slot_level=slot_level if spell["level"] > 0 else caster.level,
        spell_save_dc=spell_save_dc,
        spell_attack_modifier=spell_attack_modifier,
        spellcasting_modifier=spellcasting_modifier,
        target_names=[t.name for t in target_lcs] or None,
        aoe_origin=aoe_origin,
        aoe_direction=(aoe_dx, aoe_dy),
        creatures_in_range=creatures_in_range,
    )

    summary = f"{caster.name} casts {result.spell_name}"
    if result.slot_used:
        summary += f" (slot {result.slot_used})"
    _log(db, gs, summary)
    for note in result.notes:
        _log(db, gs, f"  {note}")
    for tgt in result.targets:
        _log(db, gs, f"  -> {tgt.name}: {tgt.notes}")
    if result.healing_dice:
        roll = dice.roll(result.healing_dice)
        heal_total = roll.total + (spellcasting_modifier if result.healing_modifier_label else 0)
        _log(db, gs, f"  heals {heal_total} ({result.healing_dice}{' + ' + str(spellcasting_modifier) if result.healing_modifier_label else ''})")

    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/spawn-enemy")
def spawn_enemy(
    session_id: int,
    name: str = Form(),
    max_hp: int = Form(10),
    armor_class: int = Form(10),
    dexterity: int = Form(10),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user, dm_only=True)
    enemy_count = len(db.exec(select(LiveCharacter).where(LiveCharacter.session_id == session_id, LiveCharacter.is_enemy == True)).all())
    map_obj = db.get(Map, gs.active_map_id) if gs.active_map_id else None
    cols = map_obj.grid_cols if map_obj else 60
    px = max(0, cols - 3 - enemy_count)
    lc = LiveCharacter(
        session_id=session_id,
        owner_id=user.id,
        name=name,
        max_hp=max_hp,
        current_hp=max_hp,
        armor_class=armor_class,
        dexterity=dexterity,
        is_enemy=True,
        position_x=px,
        position_y=2,
    )
    db.add(lc)
    _log(db, gs, f"Enemy spawned: {name} (HP {max_hp}, AC {armor_class})")
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


class _PosWrapper:
    """Lightweight wrapper exposing .name and .position for grid.creatures_in_area."""
    def __init__(self, name, x, y, lc_id):
        self.name = name
        self.position = grid.GridPoint(x, y)
        self.id = lc_id


@router.get("/sessions/{session_id}/state")
def session_state(
    session_id: int,
    perspective: str = "all",  # "all" (DM/full), "party" (projector), "enemy_obscured" (kiosk)
    db: Session = Depends(get_session),
):
    """JSON state. perspective=all shows everything; party respects vision; enemy_obscured hides enemy HP."""
    gs = db.get(GameSession, session_id)
    if not gs or not gs.is_active:
        raise HTTPException(404)
    lcs = db.exec(select(LiveCharacter).where(LiveCharacter.session_id == session_id)).all()
    active_map = db.get(Map, gs.active_map_id) if gs.active_map_id else None
    current_id = None
    if gs.in_combat and gs.initiative_order:
        idx = gs.current_turn_index
        if 0 <= idx < len(gs.initiative_order):
            current_id = gs.initiative_order[idx].get("id")

    obscure_enemies = perspective in ("party", "enemy_obscured")

    pc_index = 0
    enemy_index = 0
    default_cols = active_map.grid_cols if active_map else 60
    for lc in lcs:
        if lc.position_x is None or lc.position_y is None:
            if lc.is_enemy:
                lc.position_x = max(0, default_cols - 3 - enemy_index)
                lc.position_y = 2
                enemy_index += 1
            else:
                lc.position_x = 2 + pc_index
                lc.position_y = 2
                pc_index += 1
            db.add(lc)
    db.commit()

    party_data = [{
        "id": lc.id, "name": lc.name, "is_enemy": lc.is_enemy, "is_active": lc.is_active,
        "position_x": lc.position_x, "position_y": lc.position_y,
        "vision_normal_ft": lc.vision_normal_ft or 0,
        "darkvision_ft": lc.darkvision_ft or 0,
        "light_emission_ft": lc.light_emission_ft or 0,
    } for lc in lcs]

    currently_visible: set | None = None
    explored_visible: set | None = None
    vision_circles: list = []
    if perspective == "party" and active_map:
        walls = active_map.walls or []
        zones = active_map.zones or []
        currently_visible = vision_mod.party_visible(party_data, walls, zones, active_map.grid_cols, active_map.grid_rows)
        prior = {tuple(s) for s in (gs.fog_revealed or [])}
        explored_visible = currently_visible | prior
        vision_circles = vision_mod.vision_radii_for_party(party_data)

    def _serialize_lc(lc: LiveCharacter):
        in_view = True
        if currently_visible is not None and lc.is_enemy:
            if lc.position_x is None or (lc.position_x, lc.position_y) not in currently_visible:
                in_view = False
        out = {
            "id": lc.id,
            "name": lc.name,
            "is_enemy": lc.is_enemy,
            "is_active": lc.is_active,
            "x": lc.position_x if in_view else None,
            "y": lc.position_y if in_view else None,
            "conditions": [c["name"] for c in (lc.conditions or [])] if in_view else [],
            "is_current": lc.id == current_id,
            "in_view": in_view,
        }
        if obscure_enemies and lc.is_enemy:
            pct = (lc.current_hp / lc.max_hp * 100) if lc.max_hp else 0
            if lc.current_hp <= 0:
                status = "down"
            elif pct < 25:
                status = "near death"
            elif pct < 50:
                status = "bloodied"
            elif pct < 90:
                status = "wounded"
            else:
                status = "healthy"
            out["status"] = status
        else:
            out.update({
                "current_hp": lc.current_hp,
                "max_hp": lc.max_hp,
                "temp_hp": lc.temp_hp,
                "armor_class": lc.armor_class,
            })
        return out

    return {
        "session_id": gs.id,
        "in_combat": gs.in_combat,
        "round_number": gs.round_number,
        "current_id": current_id,
        "initiative_order": gs.initiative_order or [],
        "perspective": perspective,
        "map": {
            "id": active_map.id,
            "name": active_map.name,
            "image_path": active_map.image_path,
            "grid_cols": active_map.grid_cols,
            "grid_rows": active_map.grid_rows,
            "grid_type": active_map.grid_type or "square",
            "walls": active_map.walls or [],
            "zones": active_map.zones or [],
        } if active_map else None,
        "fog_revealed": (sorted(list(explored_visible)) if explored_visible is not None else (gs.fog_revealed or [])),
        "currently_visible": sorted(list(currently_visible)) if currently_visible is not None else None,
        "drawings": gs.drawings or [],
        "vision_circles": vision_circles,
        "live_characters": [_serialize_lc(lc) for lc in lcs],
    }


@router.get("/sessions/{session_id}/events")
async def session_events(session_id: int):
    """SSE stream. Sends a 'state_changed' event whenever any mutation publishes one."""
    queue = events.subscribe(session_id)

    async def gen():
        try:
            yield "retry: 2000\n\n"
            yield ": connected\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=20.0)
                    data = json.dumps(msg)
                    yield f"event: {msg['event']}\ndata: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            events.unsubscribe(session_id, queue)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@router.post("/sessions/{session_id}/set-map")
def set_active_map(
    session_id: int,
    map_id: int = Form(),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user, dm_only=True)
    m = db.get(Map, map_id)
    if not m or m.campaign_id != gs.campaign_id:
        raise HTTPException(400, "map not found in this campaign")
    gs.active_map_id = map_id
    gs.fog_revealed = []
    gs.drawings = []
    _log(db, gs, f"Map set: {m.name}")
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


def _refresh_explored_fog(db_session: Session, gs: GameSession):
    """Union the party's current vision into gs.fog_revealed (persistent exploration memory)."""
    if not gs.active_map_id:
        return
    m = db_session.get(Map, gs.active_map_id)
    if not m:
        return
    lcs = db_session.exec(select(LiveCharacter).where(LiveCharacter.session_id == gs.id)).all()
    party = [{
        "id": lc.id, "name": lc.name, "is_enemy": lc.is_enemy, "is_active": lc.is_active,
        "position_x": lc.position_x, "position_y": lc.position_y,
        "vision_normal_ft": lc.vision_normal_ft or 0,
        "darkvision_ft": lc.darkvision_ft or 0,
        "light_emission_ft": lc.light_emission_ft or 0,
    } for lc in lcs]
    seen = vision_mod.party_visible(party, m.walls or [], m.zones or [], m.grid_cols, m.grid_rows)
    prior = {tuple(s) for s in (gs.fog_revealed or [])}
    merged = prior | seen
    gs.fog_revealed = sorted([list(s) for s in merged])
    db_session.add(gs)


@router.post("/sessions/{session_id}/move")
async def move_token(
    session_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else dict(await request.form())
    target_id = int(body.get("target_id"))
    x = int(body.get("x"))
    y = int(body.get("y"))
    gs = _require_session(db, session_id, user, dm_only=True)
    lc = _require_live(db, session_id, target_id)
    lc.position_x = x
    lc.position_y = y
    db.add(lc)
    if not lc.is_enemy:
        _refresh_explored_fog(db, gs)
    db.commit()
    events.publish(session_id)
    return {"ok": True, "id": target_id, "x": x, "y": y}


@router.post("/sessions/{session_id}/fog")
async def update_fog(
    session_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    body = await request.json()
    action = body.get("action", "reveal")
    squares = body.get("squares", [])
    gs = _require_session(db, session_id, user, dm_only=True)
    revealed = {tuple(s) for s in (gs.fog_revealed or [])}
    incoming = {(int(s[0]), int(s[1])) for s in squares}
    if action == "reveal":
        revealed |= incoming
    elif action == "hide":
        revealed -= incoming
    elif action == "clear":
        revealed = set()
    elif action == "reveal_all":
        m = db.get(Map, gs.active_map_id) if gs.active_map_id else None
        if m:
            revealed = {(x, y) for x in range(m.grid_cols) for y in range(m.grid_rows)}
    gs.fog_revealed = [list(s) for s in revealed]
    db.add(gs)
    db.commit()
    events.publish(session_id)
    return {"ok": True, "revealed_count": len(revealed)}


@router.post("/sessions/{session_id}/walls")
async def update_walls(
    session_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    body = await request.json()
    action = body.get("action", "add")
    gs = _require_session(db, session_id, user, dm_only=True)
    if not gs.active_map_id:
        raise HTTPException(400, "no active map")
    m = db.get(Map, gs.active_map_id)
    walls = list(m.walls or [])
    if action == "add":
        walls.append({
            "x1": float(body["x1"]), "y1": float(body["y1"]),
            "x2": float(body["x2"]), "y2": float(body["y2"]),
        })
    elif action == "clear":
        walls = []
    elif action == "undo":
        if walls:
            walls.pop()
    elif action == "remove_near":
        px, py = float(body["x"]), float(body["y"])
        threshold = 0.5
        def dist_to_seg(w):
            x1, y1, x2, y2 = w["x1"], w["y1"], w["x2"], w["y2"]
            dx, dy = x2 - x1, y2 - y1
            if dx == 0 and dy == 0:
                return ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
            t = max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
            cx, cy = x1 + t * dx, y1 + t * dy
            return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
        walls = [w for w in walls if dist_to_seg(w) > threshold]
    m.walls = walls
    db.add(m)
    _refresh_explored_fog(db, gs)
    db.commit()
    events.publish(session_id)
    return {"ok": True, "count": len(walls)}


@router.post("/sessions/{session_id}/zones")
async def update_zones(
    session_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    body = await request.json()
    action = body.get("action", "add")
    gs = _require_session(db, session_id, user, dm_only=True)
    if not gs.active_map_id:
        raise HTTPException(400, "no active map")
    m = db.get(Map, gs.active_map_id)
    zones = list(m.zones or [])
    if action == "add":
        shape = body.get("shape", "rect")
        z = {"type": body.get("type", "bright"), "shape": shape}
        if shape == "circle":
            z["cx"] = float(body["cx"])
            z["cy"] = float(body["cy"])
            z["r"] = float(body["r"])
        else:
            z["x"] = int(body["x"])
            z["y"] = int(body["y"])
            z["w"] = int(body["w"])
            z["h"] = int(body["h"])
        zones.append(z)
    elif action == "clear":
        zones = []
    elif action == "undo":
        if zones:
            zones.pop()
    elif action == "remove_at":
        px, py = float(body["x"]), float(body["y"])
        def hits(z):
            if z.get("shape") == "circle":
                return (px - z["cx"]) ** 2 + (py - z["cy"]) ** 2 <= z["r"] ** 2
            return z["x"] <= px < z["x"] + z["w"] and z["y"] <= py < z["y"] + z["h"]
        zones = [z for z in zones if not hits(z)]
    m.zones = zones
    db.add(m)
    _refresh_explored_fog(db, gs)
    db.commit()
    events.publish(session_id)
    return {"ok": True, "count": len(zones)}


@router.post("/sessions/{session_id}/draw")
async def add_drawing(
    session_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    body = await request.json()
    action = body.get("action", "add")
    gs = _require_session(db, session_id, user, dm_only=True)
    drawings = list(gs.drawings or [])
    if action == "add":
        stroke = {
            "color": body.get("color", "#3ab5a6"),
            "width": int(body.get("width", 3)),
            "points": body.get("points", []),
        }
        drawings.append(stroke)
    elif action == "clear":
        drawings = []
    elif action == "undo":
        if drawings:
            drawings.pop()
    gs.drawings = drawings
    db.add(gs)
    db.commit()
    events.publish(session_id)
    return {"ok": True, "count": len(drawings)}

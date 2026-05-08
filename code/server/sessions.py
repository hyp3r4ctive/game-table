import asyncio
import json
import uuid
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
from game import dice, combat, conditions, spells, grid, movement
from game import rules as rules_mod
import events
import vision as vision_mod

router = APIRouter()
templates = Jinja2Templates(directory="templates")

EVENT_LOG_LIMIT = 50


def _ability_mod(score: int) -> int:
    return (score - 10) // 2


_ABILITY_FULL = {
    "str": "strength", "dex": "dexterity", "con": "constitution",
    "int": "intelligence", "wis": "wisdom", "cha": "charisma",
}


def _ability_full_name(s: str) -> str:
    s = (s or "").lower()
    return _ABILITY_FULL.get(s, s)


def _current_lc(db_session: Session, gs: GameSession) -> Optional[LiveCharacter]:
    """LiveCharacter whose turn it currently is, or None if not in combat."""
    if not gs.in_combat or not gs.initiative_order:
        return None
    try:
        entry = gs.initiative_order[gs.current_turn_index]
    except IndexError:
        return None
    name = entry.get("name") if isinstance(entry, dict) else (entry[0] if isinstance(entry, (list, tuple)) else None)
    if not name:
        return None
    return db_session.exec(
        select(LiveCharacter).where(
            LiveCharacter.session_id == gs.id, LiveCharacter.name == name
        )
    ).first()


def _can_act(gs: GameSession, lc: LiveCharacter, user: User) -> bool:
    """DM can always act. Player can only act for their own character on their turn."""
    if gs.dm_id == user.id:
        return True
    if lc is None or lc.owner_id != user.id:
        return False
    if gs.in_combat:
        cur = _current_initiative_name(gs)
        if cur != lc.name:
            return False
    return True


def _current_initiative_name(gs: GameSession) -> Optional[str]:
    if not gs.in_combat or not gs.initiative_order:
        return None
    try:
        entry = gs.initiative_order[gs.current_turn_index]
    except IndexError:
        return None
    return entry.get("name") if isinstance(entry, dict) else (entry[0] if isinstance(entry, (list, tuple)) else None)


def _reset_turn_state(gs: GameSession) -> None:
    gs.action_used = False
    gs.bonus_action_used = False
    gs.reaction_used = False
    gs.movement_used_ft = 0
    gs.movement_extra_ft = 0
    gs.is_dodging = False
    gs.is_disengaging = False


def _movement_budget_ft(db_session: Session, gs: GameSession, lc: LiveCharacter) -> int:
    """Total feet of movement allowed this turn = base speed + active-effect modifiers + turn extras (Dash)."""
    base = lc.speed_ft or 30
    delta = 0
    multiplier = 1.0
    try:
        from db import ActiveEffect
        effects = db_session.exec(
            select(ActiveEffect).where(
                ActiveEffect.session_id == gs.id,
                ActiveEffect.target_live_id == lc.id,
            )
        ).all()
        for eff in effects:
            payload = eff.payload or {}
            if "speed_delta" in payload:
                delta += int(payload["speed_delta"])
            if "speed_multiplier" in payload:
                multiplier *= float(payload["speed_multiplier"])
    except Exception:
        pass
    return max(0, int((base + delta) * multiplier) + (gs.movement_extra_ft or 0))


def _action_cost_for_spell(spell: dict) -> str:
    """Returns 'action' | 'bonus_action' | 'reaction' | 'free' based on the spell's casting_time."""
    ct = (spell.get("casting_time") or "action").lower().replace(" ", "_")
    if ct in ("action", "bonus_action", "reaction"):
        return ct
    return "action"


def _proficiency_bonus(level: int) -> int:
    return 2 + max(0, (level - 1) // 4)


def _all_active_effects_for_session(db_session: Session, session_id: int) -> list:
    from db import ActiveEffect
    return db_session.exec(
        select(ActiveEffect).where(ActiveEffect.session_id == session_id)
    ).all()


def _spellcasting_modifiers(lc: LiveCharacter, char: Optional[Character]) -> tuple[int, int, int]:
    """Returns (spell_save_dc, spell_attack_mod, spellcasting_mod) for this character.
    Uses Character.spellcasting_ability if set, else picks the highest of int/wis/cha.
    """
    pb = _proficiency_bonus(lc.level or 1)
    ability = (char.spellcasting_ability if char else "") or ""
    if ability:
        score = getattr(lc, _ability_full_name(ability), 10)
    else:
        score = max(lc.intelligence or 10, lc.wisdom or 10, lc.charisma or 10)
    cm = (score - 10) // 2
    return 8 + pb + cm, pb + cm, cm


def _basic_attack_profile(lc: LiveCharacter) -> tuple[int, str, str]:
    """Default unarmed/finesse profile until equipped weapons are modeled.
    Returns (to_hit_modifier, damage_dice, damage_type).
    """
    pb = _proficiency_bonus(lc.level or 1)
    str_mod = (lc.strength - 10) // 2
    dex_mod = (lc.dexterity - 10) // 2
    use_mod = max(str_mod, dex_mod)
    sign = "+" if use_mod >= 0 else "-"
    return pb + use_mod, f"1d8{sign}{abs(use_mod)}", "slashing"


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


def _check_sight(db_session: Session, gs: GameSession, actor: LiveCharacter, target_x: Optional[int], target_y: Optional[int]) -> Optional[str]:
    """Return None if the actor can see (target_x, target_y) on the active map,
    else an error message. Skipped silently when there is no map, no actor
    position, or no target position to check."""
    if not gs.active_map_id:
        return None
    if actor.position_x is None or actor.position_y is None:
        return None
    if target_x is None or target_y is None:
        return None
    m = db_session.get(Map, gs.active_map_id)
    if not m:
        return None
    if vision_mod.can_see_square(
        actor.position_x, actor.position_y,
        int(target_x), int(target_y),
        actor.vision_normal_ft or 0,
        actor.darkvision_ft or 0,
        actor.light_emission_ft or 0,
        m.walls or [], m.zones or [],
        m.grid_cols, m.grid_rows,
        feet_per_square=m.feet_per_square or 5,
        grid_type=m.grid_type or "square",
    ):
        return None
    return f"{actor.name} cannot see that square (line of sight blocked)"


FLAG_LABELS = {
    "sight_blocked": "no line of sight",
    "no_slots": "no spell slot remaining",
    "out_of_range": "target out of range",
    "missing_components": "missing material components",
    "action_used": "no action remaining this turn",
    "bonus_action_used": "no bonus action remaining this turn",
    "reaction_used": "no reaction remaining",
    "not_your_turn": "not your turn",
    "no_movement": "not enough movement remaining",
}


def _is_pc_action(actor: LiveCharacter) -> bool:
    """PC actions go through the DM-approval gate; enemy/DM actions don't."""
    return not actor.is_enemy


RULES = ("sight", "slots", "range", "components")


def _campaign_bypass(db_session: Session, gs: GameSession) -> set[str]:
    """Rules disabled at the campaign level become permanent bypasses."""
    c = db_session.get(Campaign, gs.campaign_id)
    rules = (c.rules if c else None) or {}
    return {r for r in RULES if not rules.get(r, True)}


def _consume_turn_resource(db_session: Session, gs: GameSession, lc: LiveCharacter, kind: str) -> list[str]:
    """Try to consume `kind` ('action'|'bonus_action'|'reaction'|'free') for `lc`.
    Returns [] on success (and decrements gs.<kind>_used), or [flag] if blocked.

    Skipped when (a) the action_economy rule is off, (b) we're not in combat,
    or (c) the actor isn't the current turn-holder (out-of-turn moves like
    opportunity attacks aren't tracked yet — DM's responsibility).
    """
    if kind == "free":
        return []
    c = db_session.get(Campaign, gs.campaign_id)
    if not rules_mod.get_rule(c, "action_economy"):
        return []
    if not gs.in_combat:
        return []
    if kind == "reaction":
        if gs.reaction_used:
            return ["reaction_used"]
        gs.reaction_used = True
        return []
    cur_name = _current_initiative_name(gs)
    if cur_name != lc.name:
        return []  # off-turn; not tracked
    flag = f"{kind}_used"
    if getattr(gs, flag, False):
        return [flag]
    setattr(gs, flag, True)
    return []


def _validate_attack(db_session: Session, gs: GameSession, attacker: LiveCharacter, target: LiveCharacter, bypass: set[str], distance_ft: Optional[int] = None) -> list[str]:
    import math as _math
    flags: list[str] = []
    if "sight" not in bypass and attacker.id != target.id:
        if _check_sight(db_session, gs, attacker, target.position_x, target.position_y):
            flags.append("sight_blocked")
    if "range" not in bypass and distance_ft is not None and distance_ft > 0:
        m = db_session.get(Map, gs.active_map_id) if gs.active_map_id else None
        if m and attacker.position_x is not None and target.position_x is not None:
            fps = m.feet_per_square or 5
            gtype = m.grid_type or "square"
            ax, ay = vision_mod._cell_center(attacker.position_x, attacker.position_y, gtype)
            tx, ty = vision_mod._cell_center(target.position_x, target.position_y, gtype)
            actual_ft = _math.hypot(tx - ax, ty - ay) * fps
            if actual_ft > distance_ft:
                flags.append("out_of_range")
    return flags


def _validate_cast(db_session: Session, gs: GameSession, caster: LiveCharacter, spell: dict, target_lcs: list[LiveCharacter], aoe_x: Optional[int], aoe_y: Optional[int], slot_level: int, bypass: set[str]) -> list[str]:
    import math as _math
    flags: list[str] = []
    target_type = spell.get("target_type", "creature_seen")
    if "sight" not in bypass and spell.get("requires_sight", True):
        if target_type not in ("self", "area_self"):
            for tlc in target_lcs:
                if tlc.id == caster.id:
                    continue
                if _check_sight(db_session, gs, caster, tlc.position_x, tlc.position_y):
                    flags.append("sight_blocked")
                    break
            if "sight_blocked" not in flags and aoe_x is not None and aoe_y is not None:
                if _check_sight(db_session, gs, caster, aoe_x, aoe_y):
                    flags.append("sight_blocked")
    if "range" not in bypass and spell.get("range_ft", 0) > 0 and target_type not in ("self", "area_self"):
        m = db_session.get(Map, gs.active_map_id) if gs.active_map_id else None
        fps = (m.feet_per_square or 5) if m else 5
        gtype = (m.grid_type or "square") if m else "square"
        if caster.position_x is not None and caster.position_y is not None:
            cx, cy = vision_mod._cell_center(caster.position_x, caster.position_y, gtype)
            def _dist_ft(tx, ty):
                tcx, tcy = vision_mod._cell_center(int(tx), int(ty), gtype)
                return _math.hypot(tcx - cx, tcy - cy) * fps
            if aoe_x is not None and aoe_y is not None:
                if _dist_ft(aoe_x, aoe_y) > spell["range_ft"]:
                    flags.append("out_of_range")
            else:
                for tlc in target_lcs:
                    if tlc.id == caster.id or tlc.position_x is None:
                        continue
                    if _dist_ft(tlc.position_x, tlc.position_y) > spell["range_ft"]:
                        flags.append("out_of_range")
                        break
    if "slots" not in bypass and spell.get("level", 0) > 0:
        slots = caster.spell_slots or {}
        if slots.get(str(slot_level), 0) <= 0:
            flags.append("no_slots")
    return flags


UNDO_STACK_LIMIT = 50


def _snapshot_state(db_session: Session, gs: GameSession) -> dict:
    """Capture per-session state that mutates during play, for undo."""
    lcs = db_session.exec(select(LiveCharacter).where(LiveCharacter.session_id == gs.id)).all()
    lc_data = [{
        "id": lc.id,
        "name": lc.name,
        "current_hp": lc.current_hp, "max_hp": lc.max_hp, "temp_hp": lc.temp_hp,
        "armor_class": lc.armor_class,
        "conditions": list(lc.conditions or []),
        "spell_slots": dict(lc.spell_slots or {}),
        "position_x": lc.position_x, "position_y": lc.position_y,
        "is_active": lc.is_active, "initiative": lc.initiative,
        "is_enemy": lc.is_enemy,
    } for lc in lcs]
    return {
        "live_characters": lc_data,
        "lc_ids": [lc.id for lc in lcs],
        "event_log": list(gs.event_log or []),
        "in_combat": gs.in_combat,
        "round_number": gs.round_number,
        "current_turn_index": gs.current_turn_index,
        "initiative_order": list(gs.initiative_order or []),
        "pending_actions": list(gs.pending_actions or []),
    }


def _push_undo(db_session: Session, gs: GameSession, label: str) -> None:
    snap = _snapshot_state(db_session, gs)
    stack = list(gs.undo_stack or [])
    stack.append({"label": label, "before": snap, "ts": datetime.utcnow().isoformat(timespec="seconds")})
    if len(stack) > UNDO_STACK_LIMIT:
        stack = stack[-UNDO_STACK_LIMIT:]
    gs.undo_stack = stack
    db_session.add(gs)


def _restore_snapshot(db_session: Session, gs: GameSession, snap: dict) -> None:
    snapshot_ids = set(snap.get("lc_ids") or [])
    current_lcs = db_session.exec(select(LiveCharacter).where(LiveCharacter.session_id == gs.id)).all()
    # Delete any LCs created after the snapshot.
    for lc in current_lcs:
        if lc.id not in snapshot_ids:
            db_session.delete(lc)
    # Restore captured LC fields. (LCs that were deleted between snapshot and now are gone for good — undo doesn't resurrect.)
    for d in snap["live_characters"]:
        lc = db_session.get(LiveCharacter, d["id"])
        if not lc:
            continue
        for k in ("current_hp", "max_hp", "temp_hp", "armor_class",
                  "conditions", "spell_slots", "position_x", "position_y",
                  "is_active", "initiative"):
            setattr(lc, k, d[k])
        db_session.add(lc)
    gs.event_log = snap["event_log"]
    gs.in_combat = snap["in_combat"]
    gs.round_number = snap["round_number"]
    gs.current_turn_index = snap["current_turn_index"]
    gs.initiative_order = snap["initiative_order"]
    gs.pending_actions = snap["pending_actions"]
    db_session.add(gs)


def _queue_pending(db_session: Session, gs: GameSession, kind: str, actor: LiveCharacter, params: dict, flags: list[str], user: User, summary: str) -> dict:
    pending = list(gs.pending_actions or [])
    entry = {
        "id": uuid.uuid4().hex[:8],
        "kind": kind,
        "actor_id": actor.id,
        "actor_name": actor.name,
        "params": params,
        "flags": flags,
        "flag_labels": [FLAG_LABELS.get(f, f) for f in flags],
        "requested_by_user_id": user.id,
        "requested_by_username": user.username,
        "summary": summary,
        "created_at": datetime.utcnow().isoformat(timespec="seconds"),
    }
    pending.append(entry)
    gs.pending_actions = pending
    db_session.add(gs)
    return entry


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
        [{
            "key": s["key"], "name": s["name"], "level": s["level"],
            "range_ft": s.get("range_ft", 0),
            "target_type": s.get("target_type", "creature_seen"),
            "effect_type": s.get("effect_type", ""),
            "area": s.get("area"),
        } for s in spells.list_all_spells(db, gs.campaign_id)],
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
    _push_undo(db, gs, "start combat")
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
    _push_undo(db, gs, "end combat")
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
    _push_undo(db, gs, "next turn")
    gs.current_turn_index = (gs.current_turn_index + 1) % len(gs.initiative_order)
    if gs.current_turn_index == 0:
        gs.round_number += 1
        _log(db, gs, f"-- Round {gs.round_number} --")
    _reset_turn_state(gs)
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


def _do_attack(db_session: Session, gs: GameSession, params: dict, user: User, bypass: set[str]) -> None:
    attacker = _require_live(db_session, gs.id, int(params["attacker_id"]))
    target = _require_live(db_session, gs.id, int(params["target_id"]))
    _push_undo(db_session, gs, f"{attacker.name} attacks {target.name}")
    effective_bypass = bypass | _campaign_bypass(db_session, gs)
    flags = _validate_attack(db_session, gs, attacker, target, effective_bypass, distance_ft=int(params.get("distance_ft", 5)))
    # Action economy: a basic attack consumes the Action.
    flags = flags + _consume_turn_resource(db_session, gs, attacker, "action")
    if flags:
        if _is_pc_action(attacker):
            summary = f"{attacker.name} attacks {target.name}"
            _queue_pending(db_session, gs, "attack", attacker, params, flags, user, summary)
            label = ", ".join(FLAG_LABELS.get(f, f) for f in flags)
            _log(db_session, gs, f"pending DM approval: {summary} ({label})")
            return
        label = ", ".join(FLAG_LABELS.get(f, f) for f in flags)
        _log(db_session, gs, f"warning: {attacker.name} attacks {target.name}: {label}")
    attacker_conds = [c["name"] for c in (attacker.conditions or [])]
    target_conds = [c["name"] for c in (target.conditions or [])]
    result = combat.make_attack(
        attacker.name, target.name,
        int(params.get("to_hit_modifier", 0)), target.armor_class,
        params.get("damage_dice", "1d6"), params.get("damage_type", "slashing"),
        attacker_conds, target_conds,
        int(params.get("distance_ft", 5)),
    )
    if result.hit:
        target.current_hp, target.temp_hp, taken = combat.apply_damage(
            target.current_hp, target.temp_hp, target.max_hp,
            [combat.DamageInstance(amount=result.total_damage, type=params.get("damage_type", "slashing"))],
        )
        db_session.add(target)
        msg = f"{result.description} (HP: {target.current_hp}/{target.max_hp})"
    else:
        msg = result.description
    _log(db_session, gs, msg)


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
    _do_attack(db, gs, {
        "attacker_id": attacker_id, "target_id": target_id,
        "to_hit_modifier": to_hit_modifier, "damage_dice": damage_dice,
        "damage_type": damage_type, "distance_ft": distance_ft,
    }, user, bypass=set())
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
    _push_undo(db, gs, f"{target.name} damaged {amount} {damage_type}")
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
    _push_undo(db, gs, f"{target.name} healed {amount}")
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
    _push_undo(db, gs, f"{target.name} gains {condition_name}")
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
    _push_undo(db, gs, f"{target.name} loses {condition_name}")
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
    _do_cast(db, gs, {
        "spell_name": spell_name, "caster_id": caster_id, "slot_level": slot_level,
        "target_ids": target_ids, "aoe_x": aoe_x, "aoe_y": aoe_y,
        "aoe_dx": aoe_dx, "aoe_dy": aoe_dy,
        "spell_save_dc": spell_save_dc, "spell_attack_modifier": spell_attack_modifier,
        "spellcasting_modifier": spellcasting_modifier,
        "enforce_slots": enforce_slots,
    }, user, bypass=set())
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


def _do_cast(db_session: Session, gs: GameSession, params: dict, user: User, bypass: set[str]) -> None:
    spell_name = params["spell_name"]
    caster = _require_live(db_session, gs.id, int(params["caster_id"]))
    _push_undo(db_session, gs, f"{caster.name} casts {spell_name}")
    spell = spells.get_spell(spell_name, db_session, gs.campaign_id)
    if not spell:
        raise HTTPException(400, f"unknown spell: {spell_name}")

    slot_level = int(params.get("slot_level", 1))
    aoe_x = params.get("aoe_x")
    aoe_y = params.get("aoe_y")
    aoe_x = int(aoe_x) if aoe_x is not None and aoe_x != "" else None
    aoe_y = int(aoe_y) if aoe_y is not None and aoe_y != "" else None

    target_id_list = [int(x) for x in str(params.get("target_ids", "")).split(",") if str(x).strip()]
    target_lcs = []
    for tid in target_id_list:
        lc = db_session.get(LiveCharacter, tid)
        if lc and lc.session_id == gs.id:
            target_lcs.append(lc)

    effective_bypass = bypass | _campaign_bypass(db_session, gs)
    flags = _validate_cast(db_session, gs, caster, spell, target_lcs, aoe_x, aoe_y, slot_level, effective_bypass)
    flags = flags + _consume_turn_resource(db_session, gs, caster, _action_cost_for_spell(spell))
    if flags:
        if _is_pc_action(caster):
            summary = f"{caster.name} casts {spell['name']}"
            _queue_pending(db_session, gs, "cast", caster, params, flags, user, summary)
            label = ", ".join(FLAG_LABELS.get(f, f) for f in flags)
            _log(db_session, gs, f"pending DM approval: {summary} ({label})")
            return
        label = ", ".join(FLAG_LABELS.get(f, f) for f in flags)
        _log(db_session, gs, f"warning: {caster.name} casts {spell['name']}: {label}")

    enforce_slots = bool(params.get("enforce_slots", False))
    if spell["level"] > 0 and enforce_slots:
        slots = dict(caster.spell_slots or {})
        key = str(slot_level)
        if slots.get(key, 0) <= 0 and "slots" not in bypass:
            raise HTTPException(400, f"no level {slot_level} slots remaining")
        slots[key] = max(0, slots.get(key, 0) - 1)
        caster.spell_slots = slots
        db_session.add(caster)
    elif spell["level"] > 0:
        slots = dict(caster.spell_slots or {})
        key = str(slot_level)
        if slots.get(key, 0) > 0:
            slots[key] = slots[key] - 1
            caster.spell_slots = slots
            db_session.add(caster)

    creatures_in_range = []
    if aoe_x is not None and aoe_y is not None:
        all_lcs = db_session.exec(select(LiveCharacter).where(LiveCharacter.session_id == gs.id, LiveCharacter.is_active == True)).all()
        for lc in all_lcs:
            if lc.position_x is not None and lc.position_y is not None:
                creatures_in_range.append(_PosWrapper(lc.name, lc.position_x, lc.position_y, lc.id))

    aoe_origin = grid.GridPoint(aoe_x, aoe_y) if aoe_x is not None and aoe_y is not None else None
    active_map = db_session.get(Map, gs.active_map_id) if gs.active_map_id else None
    fps = (active_map.feet_per_square if active_map and active_map.feet_per_square else 5)

    result = spells.cast_spell(
        spell_name=spell_name,
        caster_name=caster.name,
        slot_level=slot_level if spell["level"] > 0 else caster.level,
        spell_save_dc=int(params.get("spell_save_dc", 13)),
        spell_attack_modifier=int(params.get("spell_attack_modifier", 5)),
        spellcasting_modifier=int(params.get("spellcasting_modifier", 3)),
        target_names=[t.name for t in target_lcs] or None,
        aoe_origin=aoe_origin,
        aoe_direction=(int(params.get("aoe_dx", 1)), int(params.get("aoe_dy", 0))),
        creatures_in_range=creatures_in_range,
        spell_data=spell,
        feet_per_square=fps,
    )

    summary = f"{caster.name} casts {result.spell_name}"
    if result.slot_used:
        summary += f" (slot {result.slot_used})"
    _log(db_session, gs, summary)
    for note in result.notes:
        _log(db_session, gs, f"  {note}")

    effect_type = spell.get("effect_type", "")
    all_lcs = db_session.exec(
        select(LiveCharacter).where(LiveCharacter.session_id == gs.id)
    ).all()
    name_map = {lc.name: lc for lc in all_lcs}
    save_dc = int(params.get("spell_save_dc", 13))
    spell_atk_mod = int(params.get("spell_attack_modifier", 5))
    scm = int(params.get("spellcasting_modifier", 3))

    def _roll_save(lc: LiveCharacter, ability_raw: str):
        ability = _ability_full_name(ability_raw)
        score = getattr(lc, ability, 10)
        mod = _ability_mod(score)
        return combat.make_save(
            lc.name, ability, mod, save_dc,
            [c["name"] for c in (lc.conditions or [])],
        )

    def _hit(lc: LiveCharacter, amount: int, dmg_type: str):
        new_hp, new_temp, taken = combat.apply_damage(
            lc.current_hp, lc.temp_hp, lc.max_hp,
            [combat.DamageInstance(amount=amount, type=dmg_type)],
        )
        lc.current_hp = new_hp
        lc.temp_hp = new_temp
        db_session.add(lc)
        return taken

    if effect_type == "save_for_half":
        save_info = spell.get("save", {})
        save_ability = save_info.get("ability", "DEX")
        on_success = save_info.get("on_success", "half_damage")
        rolled = result.damage_rolls[0].total if result.damage_rolls else 0
        dmg_type = (spell.get("damage", [{}])[0]).get("type", "force")
        _log(db_session, gs, f"  damage rolled: {rolled} {dmg_type}")
        for st in result.targets:
            lc = name_map.get(st.name)
            if not lc:
                continue
            sv = _roll_save(lc, save_ability)
            outcome = "save" if sv.success else "fail"
            amount = (rolled // 2 if on_success == "half_damage" else 0) if sv.success else rolled
            taken = _hit(lc, amount, dmg_type) if amount > 0 else 0
            _log(db_session, gs, f"  -> {lc.name}: {save_ability} save {sv.roll.total} vs DC {save_dc} ({outcome}); takes {taken} {dmg_type} (HP: {lc.current_hp}/{lc.max_hp})")

    elif effect_type == "auto_hit":
        base = spell.get("damage", [{}])[0]
        damage_dice = base.get("dice", "1d4")
        dmg_type = base.get("type", "force")
        targets = [name_map[st.name] for st in result.targets if st.name in name_map]
        if "darts" in spell and targets:
            base_darts = spell["darts"]["base"]
            extra = max(0, slot_level - spell["level"]) * spell["darts"].get("per_slot_above", 0)
            total = base_darts + extra
            for i in range(total):
                lc = targets[i % len(targets)]
                roll = dice.roll(damage_dice)
                taken = _hit(lc, roll.total, dmg_type)
                _log(db_session, gs, f"  -> dart {i+1} → {lc.name}: {roll.total} {dmg_type} (HP: {lc.current_hp}/{lc.max_hp})")
        else:
            for lc in targets:
                roll = dice.roll(damage_dice)
                taken = _hit(lc, roll.total, dmg_type)
                _log(db_session, gs, f"  -> {lc.name}: {roll.total} {dmg_type} (HP: {lc.current_hp}/{lc.max_hp})")

    elif effect_type == "attack":
        base = spell.get("damage", [{}])[0]
        dmg_type = base.get("type", "force")
        targets = [name_map[st.name] for st in result.targets if st.name in name_map]
        beam_count = 1
        if "beams" in spell and targets:
            beam_count = spell["beams"].get("base", 1)
            for thresh_str in sorted(spell["beams"].get("scaling_levels", {}).keys(), key=int):
                if slot_level >= int(thresh_str):
                    beam_count = spell["beams"]["scaling_levels"][thresh_str]
        attacker_conds = [c["name"] for c in (caster.conditions or [])]
        for i in range(beam_count if "beams" in spell else len(targets)):
            lc = targets[i % len(targets)] if "beams" in spell else targets[i]
            damage_dice_for_target = result.targets[i % len(result.targets)].expected_damage_dice or base.get("dice", "1d6")
            target_conds = [c["name"] for c in (lc.conditions or [])]
            att = combat.make_attack(
                caster.name, lc.name, spell_atk_mod, lc.armor_class,
                damage_dice_for_target, dmg_type,
                attacker_conds, target_conds, distance_ft=30,
            )
            label = "beam " + str(i + 1) if "beams" in spell else "attack"
            if att.hit:
                taken = _hit(lc, att.total_damage, dmg_type)
                crit = " CRIT" if att.critical else ""
                _log(db_session, gs, f"  -> {label} → {lc.name}: hit ({att.attack_roll.total} vs AC {lc.armor_class}{crit}), {taken} {dmg_type} (HP: {lc.current_hp}/{lc.max_hp})")
            else:
                _log(db_session, gs, f"  -> {label} → {lc.name}: miss ({att.attack_roll.total} vs AC {lc.armor_class})")

    elif effect_type == "healing":
        if result.healing_dice:
            roll = dice.roll(result.healing_dice)
            bonus = scm if result.healing_modifier_label else 0
            heal_per_target = roll.total + bonus
            _log(db_session, gs, f"  heal rolled: {roll.total}{' + ' + str(bonus) if bonus else ''}")
            for st in result.targets:
                lc = name_map.get(st.name)
                if not lc:
                    continue
                new_hp, healed = combat.apply_healing(lc.current_hp, lc.max_hp, heal_per_target)
                lc.current_hp = new_hp
                db_session.add(lc)
                _log(db_session, gs, f"  -> {lc.name}: +{healed} HP (HP: {lc.current_hp}/{lc.max_hp})")

    elif effect_type in ("save_or_condition", "save_or_debuff"):
        save_info = spell.get("save", {})
        save_ability = save_info.get("ability", "WIS")
        conditions_to_apply = spell.get("conditions_applied", [])
        for st in result.targets:
            lc = name_map.get(st.name)
            if not lc:
                continue
            sv = _roll_save(lc, save_ability)
            outcome = "save" if sv.success else "fail"
            applied: list = []
            if not sv.success and conditions_to_apply:
                existing = list(lc.conditions or [])
                existing.extend(conditions_to_apply)
                lc.conditions = existing
                db_session.add(lc)
                applied = [c["name"] for c in conditions_to_apply]
            tail = f"; gains {', '.join(applied)}" if applied else ""
            _log(db_session, gs, f"  -> {lc.name}: {save_ability} save {sv.roll.total} vs DC {save_dc} ({outcome}){tail}")

    elif effect_type == "buff":
        conditions_to_apply = spell.get("conditions_applied", [])
        for st in result.targets:
            lc = name_map.get(st.name)
            if not lc:
                continue
            if conditions_to_apply:
                existing = list(lc.conditions or [])
                existing.extend(conditions_to_apply)
                lc.conditions = existing
                db_session.add(lc)
            applied = [c["name"] for c in conditions_to_apply] if conditions_to_apply else []
            _log(db_session, gs, f"  -> {lc.name}: gains {', '.join(applied) if applied else 'buff'}")

    else:
        # hp_threshold, manual, or anything novel: keep the descriptive notes for the DM.
        for tgt in result.targets:
            _log(db_session, gs, f"  -> {tgt.name}: {tgt.notes}")


@router.post("/sessions/{session_id}/pending/{pid}/approve")
def approve_pending(
    session_id: int,
    pid: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user, dm_only=True)
    pending = list(gs.pending_actions or [])
    entry = next((e for e in pending if e["id"] == pid), None)
    if not entry:
        raise HTTPException(404, "pending action not found")
    gs.pending_actions = [e for e in pending if e["id"] != pid]
    db.add(gs)
    bypass = {"sight", "slots", "range", "components"}
    _log(db, gs, f"DM approved: {entry['summary']}")
    if entry["kind"] == "attack":
        _do_attack(db, gs, entry["params"], user, bypass=bypass)
    elif entry["kind"] == "cast":
        _do_cast(db, gs, entry["params"], user, bypass=bypass)
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/pending/{pid}/deny")
def deny_pending(
    session_id: int,
    pid: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user, dm_only=True)
    pending = list(gs.pending_actions or [])
    entry = next((e for e in pending if e["id"] == pid), None)
    if not entry:
        raise HTTPException(404, "pending action not found")
    gs.pending_actions = [e for e in pending if e["id"] != pid]
    db.add(gs)
    _log(db, gs, f"DM denied: {entry['summary']}")
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/seat/{seat}/override")
def set_seat_override(
    session_id: int,
    seat: str,
    kind: str = Form("image"),
    url: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user, dm_only=True)
    if seat != "all" and not (seat.isdigit() and 1 <= int(seat) <= 6):
        raise HTTPException(400, "seat must be 1-6 or 'all'")
    if kind not in ("image", "video", "text"):
        raise HTTPException(400, "kind must be image, video, or text")
    overrides = dict(gs.seat_overrides or {})
    if not url.strip():
        overrides.pop(seat, None)
        _log(db, gs, f"DM cleared override for seat {seat}")
    else:
        overrides[seat] = {"kind": kind, "url": url.strip()}
        _log(db, gs, f"DM set {kind} override for seat {seat}")
    gs.seat_overrides = overrides
    db.add(gs)
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/seat/{seat}/clear-override")
def clear_seat_override(
    session_id: int,
    seat: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user, dm_only=True)
    overrides = dict(gs.seat_overrides or {})
    if overrides.pop(seat, None) is not None:
        gs.seat_overrides = overrides
        db.add(gs)
        _log(db, gs, f"DM cleared override for seat {seat}")
        db.commit()
        events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/undo")
def undo_last(
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user, dm_only=True)
    stack = list(gs.undo_stack or [])
    if not stack:
        raise HTTPException(400, "nothing to undo")
    entry = stack.pop()
    gs.undo_stack = stack
    _restore_snapshot(db, gs, entry["before"])
    # The restored event_log already includes the pre-action log; add a meta-entry.
    _log(db, gs, f"DM undid: {entry['label']}")
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
    _push_undo(db, gs, f"spawn enemy {name}")
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
        currently_visible = vision_mod.party_visible(
            party_data, walls, zones, active_map.grid_cols, active_map.grid_rows,
            feet_per_square=active_map.feet_per_square or 5,
            grid_type=active_map.grid_type or "square",
        )
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
        "turn_state": {
            "action_used": gs.action_used,
            "bonus_action_used": gs.bonus_action_used,
            "reaction_used": gs.reaction_used,
            "movement_used_ft": gs.movement_used_ft,
            "movement_extra_ft": gs.movement_extra_ft,
            "is_dodging": gs.is_dodging,
            "is_disengaging": gs.is_disengaging,
        },
        "pending_walk": gs.pending_walk or None,
        "map": {
            "id": active_map.id,
            "name": active_map.name,
            "image_path": active_map.image_path,
            "grid_cols": active_map.grid_cols,
            "grid_rows": active_map.grid_rows,
            "grid_type": active_map.grid_type or "square",
            "feet_per_square": active_map.feet_per_square or 5,
            "inches_per_square": active_map.inches_per_square or 2.0,
            "walls": active_map.walls or [],
            "zones": active_map.zones or [],
        } if active_map else None,
        "fog_revealed": (sorted(list(explored_visible)) if explored_visible is not None else (gs.fog_revealed or [])),
        "currently_visible": sorted(list(currently_visible)) if currently_visible is not None else None,
        "drawings": gs.drawings or [],
        "vision_circles": vision_circles,
        "live_characters": [_serialize_lc(lc) for lc in lcs],
        "active_effects": [
            {
                "id": eff.id,
                "target_live_id": eff.target_live_id,
                "caster_live_id": eff.caster_live_id,
                "name": eff.name,
                "description": eff.description,
                "handler_key": eff.handler_key,
                "is_concentration": eff.is_concentration,
                "duration_rounds": eff.duration_rounds,
                "spell_key": eff.spell_key,
            }
            for eff in _all_active_effects_for_session(db, gs.id)
        ],
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
    seen = vision_mod.party_visible(
        party, m.walls or [], m.zones or [], m.grid_cols, m.grid_rows,
        feet_per_square=m.feet_per_square or 5,
        grid_type=m.grid_type or "square",
    )
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
    _push_undo(db, gs, f"move {lc.name}")
    lc.position_x = x
    lc.position_y = y
    db.add(lc)
    if not lc.is_enemy:
        _refresh_explored_fog(db, gs)
    db.commit()
    events.publish(session_id)
    return {"ok": True, "id": target_id, "x": x, "y": y}


def _do_walk(db_session: Session, gs: GameSession, lc: LiveCharacter, path: list, user: User) -> dict:
    """Validated step-by-step movement. `path` is a list of [x, y] destinations
    starting from the cell adjacent to the actor's current position. Returns the
    same shape as movement.validate_path plus a `committed` flag.
    """
    if lc.position_x is None or lc.position_y is None:
        raise HTTPException(400, "actor has no position on the map")
    m = db_session.get(Map, gs.active_map_id) if gs.active_map_id else None
    if not m:
        raise HTTPException(400, "no active map")
    fps = m.feet_per_square or 5
    grid_type = m.grid_type or "square"
    c = db_session.get(Campaign, gs.campaign_id)
    diagonal_rule = rules_mod.get_rule(c, "diagonal_cost")
    parsed = [(int(p[0]), int(p[1])) for p in path]
    result = movement.validate_path(
        (lc.position_x, lc.position_y), parsed,
        m.walls or [], m.zones or [], fps, grid_type,
        diagonal_rule=diagonal_rule,
    )
    if not result["ok"]:
        return {**result, "committed": False}

    enforce = rules_mod.get_rule(c, "action_economy") and gs.in_combat
    if enforce:
        budget = _movement_budget_ft(db_session, gs, lc)
        remaining = budget - (gs.movement_used_ft or 0)
        if result["total_cost_ft"] > remaining:
            return {**result, "committed": False, "blocked_at": parsed[-1] if parsed else None,
                    "reason": "no_movement", "budget_remaining_ft": remaining}

    final_x, final_y = result["final"]
    _push_undo(db_session, gs, f"walk {lc.name}")
    lc.position_x = final_x
    lc.position_y = final_y
    if enforce:
        gs.movement_used_ft = (gs.movement_used_ft or 0) + result["total_cost_ft"]
    db_session.add(lc)
    if not lc.is_enemy:
        _refresh_explored_fog(db_session, gs)
    _log(db_session, gs, f"{lc.name} moves {result['total_cost_ft']}ft to ({final_x}, {final_y})")
    return {**result, "committed": True,
            "movement_used_ft": gs.movement_used_ft if enforce else None}


@router.post("/sessions/{session_id}/turn-action")
async def turn_action(
    session_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """Standard turn actions: dash | dodge | disengage | hide | help | ready | end-turn.

    body = {actor_id: int, action: str, target_id?: int (for help)}
    """
    body = await request.json()
    actor_id = int(body.get("actor_id"))
    action = (body.get("action") or "").lower()
    gs = _require_session(db, session_id, user)
    lc = _require_live(db, session_id, actor_id)
    if not _can_act(gs, lc, user):
        raise HTTPException(403, "not your turn / not your character")

    if action == "end-turn":
        if not gs.in_combat:
            raise HTTPException(400, "not in combat")
        _push_undo(db, gs, f"{lc.name} ends turn")
        gs.current_turn_index = (gs.current_turn_index + 1) % len(gs.initiative_order)
        if gs.current_turn_index == 0:
            gs.round_number += 1
            _log(db, gs, f"-- Round {gs.round_number} --")
        _reset_turn_state(gs)
        nxt = gs.initiative_order[gs.current_turn_index].get("name")
        _log(db, gs, f"{lc.name} ends turn -> {nxt}")
        db.commit()
        events.publish(session_id)
        return {"ok": True, "next": nxt}

    flags = _consume_turn_resource(db, gs, lc, "action")
    if flags:
        label = ", ".join(FLAG_LABELS.get(f, f) for f in flags)
        return {"ok": False, "flags": flags, "reason": label}

    _push_undo(db, gs, f"{lc.name} {action}")
    if action == "dash":
        gs.movement_extra_ft = (gs.movement_extra_ft or 0) + (lc.speed_ft or 30)
        _log(db, gs, f"{lc.name} dashes (+{lc.speed_ft or 30}ft movement)")
    elif action == "dodge":
        gs.is_dodging = True
        _log(db, gs, f"{lc.name} dodges")
    elif action == "disengage":
        gs.is_disengaging = True
        _log(db, gs, f"{lc.name} disengages")
    elif action == "hide":
        mod = _ability_mod(lc.dexterity)
        roll = dice.roll_d20(mod)
        _log(db, gs, f"{lc.name} hides — stealth check: {roll.total} (DM resolves vs perception)")
    elif action == "help":
        target_id = body.get("target_id")
        target_name = "ally"
        if target_id is not None:
            t = db.get(LiveCharacter, int(target_id))
            if t:
                target_name = t.name
        _log(db, gs, f"{lc.name} helps {target_name} (next ally action vs that target gains advantage)")
    elif action == "ready":
        trigger = body.get("trigger", "(unspecified)")
        readied = body.get("readied", "(unspecified)")
        _log(db, gs, f"{lc.name} readies: when {trigger} → {readied}")
    else:
        return {"ok": False, "reason": f"unknown action: {action}"}

    db.commit()
    events.publish(session_id)
    return {"ok": True, "action": action,
            "movement_extra_ft": gs.movement_extra_ft,
            "is_dodging": gs.is_dodging,
            "is_disengaging": gs.is_disengaging}


@router.post("/sessions/{session_id}/preview-walk")
async def preview_walk(
    session_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """Stage a planned walk path for the projector and seat screens to render.
    Phone calls this on each tap; server picks shortest valid path within budget.
    """
    body = await request.json()
    actor_id = int(body.get("actor_id"))
    x = int(body.get("x"))
    y = int(body.get("y"))
    gs = _require_session(db, session_id, user)
    lc = _require_live(db, session_id, actor_id)
    if not _can_act(gs, lc, user):
        raise HTTPException(403, "not your turn / not your character")
    if lc.position_x is None or lc.position_y is None:
        raise HTTPException(400, "actor has no position on the map")
    m = db.get(Map, gs.active_map_id) if gs.active_map_id else None
    if not m:
        raise HTTPException(400, "no active map")
    c = db.get(Campaign, gs.campaign_id)
    diagonal_rule = rules_mod.get_rule(c, "diagonal_cost")
    enforce = rules_mod.get_rule(c, "action_economy") and gs.in_combat
    cap = (_movement_budget_ft(db, gs, lc) - (gs.movement_used_ft or 0)) if enforce else None
    path = movement.find_path(
        (lc.position_x, lc.position_y), (x, y),
        m.walls or [], m.zones or [], m.grid_type or "square",
        m.grid_cols, m.grid_rows,
        max_cost_ft=cap,
        feet_per_square=m.feet_per_square or 5,
        diagonal_rule=diagonal_rule,
    )
    if path is None:
        # Don't clear an existing valid preview — leave the last good path on screen
        # so a stray tap doesn't wipe out the player's plan.
        return {"ok": False, "reason": "unreachable"}
    cost = sum(s["cost"] for s in movement.validate_path(
        (lc.position_x, lc.position_y), [tuple(p) for p in path],
        m.walls or [], m.zones or [], m.feet_per_square or 5,
        m.grid_type or "square", diagonal_rule=diagonal_rule,
    )["steps"])
    gs.pending_walk = {
        "actor_id": actor_id,
        "from": [lc.position_x, lc.position_y],
        "path": [list(p) for p in path],
        "cost_ft": cost,
        "destination": [x, y],
    }
    db.add(gs)
    db.commit()
    events.publish(session_id)
    return {"ok": True, "cost_ft": cost, "path": gs.pending_walk["path"]}


@router.post("/sessions/{session_id}/clear-preview")
async def clear_preview_walk(
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user)
    gs.pending_walk = {}
    db.add(gs)
    db.commit()
    events.publish(session_id)
    return {"ok": True}


@router.post("/sessions/{session_id}/confirm-walk")
async def confirm_walk(
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user)
    pw = gs.pending_walk or {}
    if not pw or not pw.get("path"):
        raise HTTPException(400, "no pending walk to confirm")
    actor_id = int(pw["actor_id"])
    lc = _require_live(db, session_id, actor_id)
    if not _can_act(gs, lc, user):
        raise HTTPException(403, "not your turn / not your character")
    out = _do_walk(db, gs, lc, pw["path"], user)
    gs.pending_walk = {}
    db.add(gs)
    db.commit()
    events.publish(session_id)
    return out


@router.post("/sessions/{session_id}/walk")
async def walk(
    session_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    body = await request.json()
    target_id = int(body.get("target_id"))
    path = body.get("path", [])
    gs = _require_session(db, session_id, user)
    lc = _require_live(db, session_id, target_id)
    if not _can_act(gs, lc, user):
        raise HTTPException(403, "not your turn / not your character")
    out = _do_walk(db, gs, lc, path, user)
    db.commit()
    events.publish(session_id)
    return out


@router.post("/sessions/{session_id}/walk-to")
async def walk_to(
    session_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """Server computes the shortest valid path from the actor to (x, y) and walks it."""
    body = await request.json()
    target_id = int(body.get("target_id"))
    x = int(body.get("x"))
    y = int(body.get("y"))
    gs = _require_session(db, session_id, user)
    lc = _require_live(db, session_id, target_id)
    if not _can_act(gs, lc, user):
        raise HTTPException(403, "not your turn / not your character")
    if lc.position_x is None or lc.position_y is None:
        raise HTTPException(400, "actor has no position on the map")
    m = db.get(Map, gs.active_map_id) if gs.active_map_id else None
    if not m:
        raise HTTPException(400, "no active map")
    c = db.get(Campaign, gs.campaign_id)
    diagonal_rule = rules_mod.get_rule(c, "diagonal_cost")
    enforce = rules_mod.get_rule(c, "action_economy") and gs.in_combat
    cap = (_movement_budget_ft(db, gs, lc) - (gs.movement_used_ft or 0)) if enforce else None
    path = movement.find_path(
        (lc.position_x, lc.position_y), (x, y),
        m.walls or [], m.zones or [], m.grid_type or "square",
        m.grid_cols, m.grid_rows,
        max_cost_ft=cap,
        feet_per_square=m.feet_per_square or 5,
        diagonal_rule=diagonal_rule,
    )
    if path is None:
        return {"ok": False, "reason": "unreachable", "committed": False}
    out = _do_walk(db, gs, lc, [list(p) for p in path], user)
    db.commit()
    events.publish(session_id)
    return out


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
        elif shape == "polygon":
            pts = body.get("points") or []
            if len(pts) < 3:
                raise HTTPException(400, "polygon needs at least 3 points")
            z["points"] = [[float(p[0]), float(p[1])] for p in pts]
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
            if z.get("shape") == "polygon":
                return vision_mod._point_in_polygon(px, py, z.get("points") or [])
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

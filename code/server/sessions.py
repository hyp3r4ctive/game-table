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
from game import effects as effects_mod
from game import effect_handlers as _effect_handlers  # noqa: F401  (registers handlers on import)
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
    """Total feet of movement allowed this turn = base speed + active-effect modifiers + turn extras (Dash).
    Conditions clamp speed (grappled/restrained/paralyzed/stunned/unconscious/petrified → 0;
    exhaustion 2-4 halves; exhaustion 5+ → 0).
    """
    base = lc.speed_ft or 30
    delta = 0
    multiplier = 1.0
    cond_names = _all_condition_names(lc)
    # Hard speed-zero conditions trump everything else.
    if any(n in cond_names for n in (
        "grappled", "restrained", "paralyzed", "stunned", "unconscious", "petrified",
        "exhaustion_5", "exhaustion_6",
    )):
        return 0
    if any(n in cond_names for n in ("exhaustion_2", "exhaustion_3", "exhaustion_4")):
        multiplier *= 0.5
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


def _apply_aura_damage(db_session: Session, gs: GameSession,
                       eff, target: LiveCharacter, caster: LiveCharacter,
                       trigger_label: str) -> None:
    """Roll save (if specified) and apply aura damage to target. Used by Spirit
    Guardians, Sickening Radiance, etc. Save halves on success."""
    if target.is_dead or (target.current_hp or 0) <= 0:
        return
    payload = eff.payload or {}
    damage_dice = payload.get("dice", "3d8")
    damage_type = payload.get("type", "radiant")
    save_ability = payload.get("save_ability")
    save_dc = int(payload.get("save_dc", 13))
    roll = dice.roll(damage_dice)
    amount = roll.total
    save_note = ""
    if save_ability:
        ability = _ability_full_name(save_ability)
        mod = _ability_mod(getattr(target, ability, 10))
        if ability in (target.saving_throw_profs or []):
            mod += _proficiency_bonus(target.level or 1)
        sv_mods = effects_mod.collect_save_modifiers(db_session, gs.id, target.id, ability)
        sv = combat.make_save(
            target.name, ability, mod, save_dc,
            [c["name"] for c in (target.conditions or [])],
            extra_dice=list(sv_mods.extra_dice),
            subtract_dice=list(sv_mods.subtract_dice),
            extra_advantage=sv_mods.advantage,
            extra_disadvantage=sv_mods.disadvantage,
            bonus=sv_mods.bonus,
        )
        if sv.success:
            amount = amount // 2
        save_note = f" ({sv.description})"
    _log(db_session, gs, f"  {eff.name} aura ({trigger_label}): {target.name} takes {amount} {damage_type}{save_note}")
    _apply_damage_to(db_session, gs, target, amount, damage_type,
                     source_attacker_id=caster.id)


def _list_aura_effects(db_session: Session, gs: GameSession) -> list:
    """All active aura_damage effects in this session, regardless of which LC they're attached to."""
    from db import ActiveEffect
    return list(db_session.exec(
        select(ActiveEffect).where(
            ActiveEffect.session_id == gs.id,
            ActiveEffect.handler_key == "aura_damage",
        )
    ).all())


def _aura_check_step(db_session: Session, gs: GameSession,
                     mover: LiveCharacter, prev_xy: tuple, new_xy: tuple) -> None:
    """Fire auras when the mover crosses from outside-radius to inside-radius
    (entry trigger per RAW Spirit Guardians)."""
    if mover.is_dead:
        return
    active_map = db_session.get(Map, gs.active_map_id) if gs.active_map_id else None
    fps = (active_map.feet_per_square if active_map else 5) or 5
    for eff in _list_aura_effects(db_session, gs):
        if eff.caster_live_id == mover.id:
            continue
        caster = db_session.get(LiveCharacter, eff.caster_live_id) if eff.caster_live_id else None
        if not caster or caster.is_dead or caster.position_x is None:
            continue
        payload = eff.payload or {}
        if payload.get("hostile_only", True) and mover.is_enemy == caster.is_enemy:
            continue
        radius_cells = max(1, int(payload.get("radius_ft", 15)) // fps)
        prev_in = max(abs(caster.position_x - prev_xy[0]), abs(caster.position_y - prev_xy[1])) <= radius_cells
        new_in = max(abs(caster.position_x - new_xy[0]), abs(caster.position_y - new_xy[1])) <= radius_cells
        if (not prev_in) and new_in:
            _apply_aura_damage(db_session, gs, eff, mover, caster, "entered area")


def _aura_check_turn_start(db_session: Session, gs: GameSession,
                           starting_lc: LiveCharacter) -> None:
    """Fire auras on the LC if they start their turn within an aura's radius."""
    if starting_lc.is_dead or starting_lc.position_x is None:
        return
    active_map = db_session.get(Map, gs.active_map_id) if gs.active_map_id else None
    fps = (active_map.feet_per_square if active_map else 5) or 5
    for eff in _list_aura_effects(db_session, gs):
        if eff.caster_live_id == starting_lc.id:
            continue
        caster = db_session.get(LiveCharacter, eff.caster_live_id) if eff.caster_live_id else None
        if not caster or caster.is_dead or caster.position_x is None:
            continue
        payload = eff.payload or {}
        if payload.get("hostile_only", True) and starting_lc.is_enemy == caster.is_enemy:
            continue
        radius_cells = max(1, int(payload.get("radius_ft", 15)) // fps)
        if max(abs(caster.position_x - starting_lc.position_x),
               abs(caster.position_y - starting_lc.position_y)) <= radius_cells:
            _apply_aura_damage(db_session, gs, eff, starting_lc, caster, "starts turn")


def _consume_mirror_image(db_session: Session, gs: GameSession, lc: LiveCharacter) -> None:
    """Decrement the images count on the lc's mirror_image effect; remove if 0."""
    from db import ActiveEffect
    eff = db_session.exec(
        select(ActiveEffect).where(
            ActiveEffect.session_id == gs.id,
            ActiveEffect.target_live_id == lc.id,
            ActiveEffect.handler_key == "mirror_image",
        )
    ).first()
    if not eff:
        return
    payload = dict(eff.payload or {})
    images = int(payload.get("images", 0)) - 1
    payload["images"] = max(0, images)
    eff.payload = payload
    eff.name = f"Mirror Image ({max(0, images)})"
    db_session.add(eff)
    if images <= 0:
        ctx = effects_mod.EffectContext(db=db_session, session_id=gs.id)
        effects_mod.remove_effect(db_session, eff, ctx)
        _log(db_session, gs, f"  {lc.name}: last image destroyed, Mirror Image ends")
    else:
        _log(db_session, gs, f"  {lc.name}: image destroyed ({images} remaining)")


def _persistent_death(db_session: Session, gs: GameSession) -> bool:
    """Read the campaign's persistent_death_saves flag (default off)."""
    c = db_session.get(Campaign, gs.campaign_id) if gs.campaign_id else None
    return bool(getattr(c, "persistent_death_saves", False))


def _death_failure_threshold(persistent: bool) -> int:
    return 4 if persistent else 3


def _apply_damage_to(db_session: Session, gs: GameSession, lc: LiveCharacter,
                     amount: int, dmg_type: str, was_crit: bool = False,
                     source_attacker_id: Optional[int] = None) -> int:
    """Centralized damage application. Handles death-save state for downed PCs,
    massive-damage instakill, and concentration checks. Returns actual damage taken.
    If source_attacker_id is provided, also opens a Hellish Rebuke window for
    eligible PC targets (non-suspending — original action already complete).
    """
    if lc.is_dead:
        return 0
    persistent = _persistent_death(db_session, gs)
    threshold = _death_failure_threshold(persistent)
    pre_hp = lc.current_hp or 0
    # Death Ward: would-be drop to 0 HP becomes a clamp at 1 HP; consume the effect.
    death_ward = next((e for e in effects_mod.list_effects_on(db_session, gs.id, lc.id)
                       if e.handler_key == "death_ward"), None)
    if death_ward and pre_hp > 0:
        provisional = pre_hp - amount
        if provisional <= 0:
            taken_clamped = max(0, pre_hp - 1)
            lc.current_hp = 1
            db_session.add(lc)
            ctx = effects_mod.EffectContext(db=db_session, session_id=gs.id)
            effects_mod.remove_effect(db_session, death_ward, ctx)
            _log(db_session, gs, f"  {lc.name}: Death Ward triggers — HP clamps to 1 (would have died)")
            if taken_clamped > 0:
                _check_concentration_on_damage(db_session, gs, lc, taken_clamped)
            return taken_clamped
    new_hp, new_temp, taken = combat.apply_damage(
        pre_hp, lc.temp_hp, lc.max_hp,
        [combat.DamageInstance(amount=amount, type=dmg_type)],
        resistances=list(lc.damage_resistances or []),
        immunities=list(lc.damage_immunities or []),
        vulnerabilities=list(lc.damage_vulnerabilities or []),
    )
    lc.current_hp = new_hp
    lc.temp_hp = new_temp

    # PC at 0 HP rules
    if not lc.is_enemy and new_hp == 0:
        if pre_hp > 0:
            # Newly downed: reset successes; failures reset only in non-persistent mode.
            lc.death_save_successes = 0
            if not persistent:
                lc.death_save_failures = 0
            lc.is_stable = False
            overflow = (pre_hp + lc.temp_hp) - amount  # overshoot is the "extra" damage
            extra_damage = -overflow  # damage past 0 HP
            if extra_damage >= (lc.max_hp or 0):
                lc.is_dead = True
                _log(db_session, gs, f"  {lc.name} suffers massive damage and dies instantly")
            else:
                fail_note = f" (carries {lc.death_save_failures} prior failures)" if persistent and lc.death_save_failures else ""
                _log(db_session, gs, f"  {lc.name} drops to 0 HP (unconscious){fail_note}")
        else:
            # Already at 0 HP, taking more damage: 1 failure, 2 if from a crit.
            lc.death_save_failures += (2 if was_crit else 1)
            _log(db_session, gs, f"  {lc.name} damage at 0 HP -> {lc.death_save_failures}/{threshold} death-save failures")
            if lc.death_save_failures >= threshold:
                lc.is_dead = True
                _log(db_session, gs, f"  {lc.name} dies ({threshold} death-save failures)")
        # NPC/enemy at 0 HP just stays at 0 (DM decides death).

    db_session.add(lc)
    if taken > 0 and pre_hp > 0:
        _check_concentration_on_damage(db_session, gs, lc, taken)
    # Hellish Rebuke trigger: target survives, has reaction + slot, and there's a
    # known source attacker. Skip if we're already inside a reaction window
    # (resume path) or the target is unconscious.
    if source_attacker_id and not gs.pending_reaction and (lc.current_hp or 0) > 0:
        eligible = _eligible_hellish_rebuke_reactors(db_session, gs, lc)
        if eligible:
            _fire_reaction_window(
                db_session, gs, "damage_taken",
                {"target_id": lc.id, "target_name": lc.name,
                 "source_attacker_id": int(source_attacker_id),
                 "damage": taken, "damage_type": dmg_type},
                eligible,
                {"kind": "noop"},
            )
    return taken


def _heal_to(db_session: Session, gs: GameSession, lc: LiveCharacter, amount: int) -> int:
    """Healing helper: revives PCs from 0 HP and resets death-save counters
    (failures persist if the campaign uses persistent death saves)."""
    if lc.is_dead:
        return 0
    persistent = _persistent_death(db_session, gs)
    pre_hp = lc.current_hp or 0
    new_hp, healed = combat.apply_healing(pre_hp, lc.max_hp, amount)
    lc.current_hp = new_hp
    if pre_hp == 0 and new_hp > 0 and not lc.is_enemy:
        lc.death_save_successes = 0
        if not persistent:
            lc.death_save_failures = 0
        lc.is_stable = False
        suffix = f" (carries {lc.death_save_failures} death-save failures)" if persistent and lc.death_save_failures else ""
        _log(db_session, gs, f"  {lc.name} regains consciousness{suffix}")
    db_session.add(lc)
    return healed


def _roll_death_save(db_session: Session, gs: GameSession, lc: LiveCharacter) -> None:
    """Auto-roll a death save for a downed PC at the start of their turn.
    Skips if dead, stable, or already conscious.
    """
    if lc.is_enemy or lc.is_dead or lc.is_stable:
        return
    if (lc.current_hp or 0) > 0:
        return
    persistent = _persistent_death(db_session, gs)
    threshold = _death_failure_threshold(persistent)
    roll = dice.roll_d20(0)
    if roll.total == 20:
        lc.current_hp = 1
        lc.death_save_successes = 0
        if not persistent:
            lc.death_save_failures = 0
        _log(db_session, gs, f"{lc.name} death save: nat 20 — regains 1 HP and consciousness")
    elif roll.total == 1:
        lc.death_save_failures += 2
        _log(db_session, gs, f"{lc.name} death save: nat 1 — 2 failures ({lc.death_save_failures}/{threshold})")
    elif roll.total >= 10:
        lc.death_save_successes += 1
        _log(db_session, gs, f"{lc.name} death save: {roll.total} — success ({lc.death_save_successes}/3)")
    else:
        lc.death_save_failures += 1
        _log(db_session, gs, f"{lc.name} death save: {roll.total} — failure ({lc.death_save_failures}/{threshold})")
    if lc.death_save_successes >= 3:
        lc.is_stable = True
        _log(db_session, gs, f"  {lc.name} stabilizes")
    if lc.death_save_failures >= threshold:
        lc.is_dead = True
        _log(db_session, gs, f"  {lc.name} dies")
    db_session.add(lc)


def _process_save_each_turn(db_session: Session, gs: GameSession, lc: LiveCharacter) -> None:
    """For each effect on `lc` with save_each_turn, roll the save at end-of-turn.
    Successful save with on_success='end' removes the effect.
    """
    from db import ActiveEffect
    rows = db_session.exec(
        select(ActiveEffect).where(
            ActiveEffect.session_id == gs.id,
            ActiveEffect.target_live_id == lc.id,
        )
    ).all()
    for eff in rows:
        sval = eff.save_each_turn or {}
        if not sval:
            continue
        ability = _ability_full_name(sval.get("ability", "wisdom"))
        dc = int(sval.get("dc", 13))
        score = getattr(lc, ability, 10)
        mod = _ability_mod(score)
        if ability in (lc.saving_throw_profs or []):
            mod += _proficiency_bonus(lc.level or 1)
        sv_mods = effects_mod.collect_save_modifiers(db_session, gs.id, lc.id, ability)
        sv = combat.make_save(
            lc.name, ability, mod, dc,
            [c["name"] for c in (lc.conditions or [])],
            extra_dice=list(sv_mods.extra_dice),
            subtract_dice=list(sv_mods.subtract_dice),
            extra_advantage=sv_mods.advantage,
            extra_disadvantage=sv_mods.disadvantage,
            bonus=sv_mods.bonus,
        )
        outcome = "PASS" if sv.success else "FAIL"
        _log(db_session, gs, f"  {lc.name} {ability[:3].upper()} save vs {eff.name}: {sv.roll.total} vs DC {dc} ({outcome})")
        if sv.success and sval.get("on_success", "end") == "end":
            ctx = effects_mod.EffectContext(db=db_session, session_id=gs.id)
            effects_mod.remove_effect(db_session, eff, ctx)
            _log(db_session, gs, f"  effect ends: {eff.name}")


def _check_concentration_on_damage(db_session: Session, gs: GameSession,
                                   lc: LiveCharacter, damage_taken: int) -> None:
    """Standard 5e concentration check after damage. DC = max(10, damage // 2).
    Failure drops the caster's concentration effect.
    """
    if damage_taken <= 0:
        return
    eff = effects_mod.caster_concentration(db_session, gs.id, lc.id)
    if not eff:
        return
    dc = max(10, damage_taken // 2)
    con_mod = _ability_mod(lc.constitution or 10)
    if "constitution" in (lc.saving_throw_profs or []):
        con_mod += _proficiency_bonus(lc.level or 1)
    sv_mods = effects_mod.collect_save_modifiers(db_session, gs.id, lc.id, "constitution")
    sv = combat.make_save(
        lc.name, "constitution", con_mod, dc,
        [c["name"] for c in (lc.conditions or [])],
        extra_dice=list(sv_mods.extra_dice),
        extra_advantage=sv_mods.advantage,
        extra_disadvantage=sv_mods.disadvantage,
        bonus=sv_mods.bonus,
    )
    outcome = "OK" if sv.success else "BROKEN"
    _log(db_session, gs, f"  {lc.name} CON concentration save vs DC {dc}: {sv.roll.total} ({outcome})")
    if not sv.success:
        ctx = effects_mod.EffectContext(db=db_session, session_id=gs.id)
        dropped = effects_mod.break_concentration(db_session, gs.id, lc.id, ctx)
        if dropped:
            _log(db_session, gs, f"  concentration broken: {dropped}")


REACTION_TIMEOUT_SECONDS = 60


def _has_slot_at_or_above(lc: LiveCharacter, level: int) -> bool:
    slots = lc.spell_slots or {}
    for k, v in slots.items():
        try:
            if int(k) >= level and int(v) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _eligible_counterspell_reactors(db_session: Session, gs: GameSession,
                                     caster: LiveCharacter, spell_level: int) -> list[LiveCharacter]:
    """All non-caster PCs with reaction available + 3rd-level slot. Counterspell
    range is 60ft; we don't enforce visibility here (DM/players coordinate)."""
    if spell_level <= 0:  # cantrips can't be counterspelled
        return []
    rows = db_session.exec(
        select(LiveCharacter).where(
            LiveCharacter.session_id == gs.id,
            LiveCharacter.is_active == True,
            LiveCharacter.id != caster.id,
        )
    ).all()
    out = []
    for lc in rows:
        if lc.is_dead or (lc.current_hp or 0) <= 0:
            continue
        if lc.reaction_used:
            continue
        if lc.position_x is not None and caster.position_x is not None:
            if _dist_ft_between(lc, caster) > 60:
                continue
        if not _has_slot_at_or_above(lc, 3):
            continue
        out.append(lc)
    return out


def _eligible_shield_reactors(db_session: Session, gs: GameSession,
                              target: LiveCharacter) -> list[LiveCharacter]:
    """Shield is self-only. Eligible iff target is the only reactor and has a 1st+
    slot, reaction available, conscious, and is a PC. Enemies don't auto-Shield."""
    if target.is_enemy or target.is_dead or (target.current_hp or 0) <= 0:
        return []
    if target.reaction_used:
        return []
    if not _has_slot_at_or_above(target, 1):
        return []
    return [target]


def _eligible_oa_reactors(db_session: Session, gs: GameSession,
                          mover: LiveCharacter,
                          prev_xy: tuple, new_xy: tuple) -> list[LiveCharacter]:
    """Find creatures whose reach the mover leaves on this step.
    Filtered by: reaction available, conscious, hostile (different is_enemy flag),
    on the map, mover not Disengaging.
    """
    if gs.is_disengaging:
        return []
    rows = db_session.exec(
        select(LiveCharacter).where(
            LiveCharacter.session_id == gs.id,
            LiveCharacter.is_active == True,
            LiveCharacter.id != mover.id,
        )
    ).all()
    out: list = []
    for t in rows:
        if t.is_dead or (t.current_hp or 0) <= 0:
            continue
        if t.reaction_used:
            continue
        if t.is_enemy == mover.is_enemy:
            continue  # only opposing-faction OAs (PC vs enemy)
        if t.position_x is None or t.position_y is None:
            continue
        reach_cells = max(1, (t.melee_reach_ft or 5) // 5)
        prev_in_reach = max(abs(t.position_x - prev_xy[0]), abs(t.position_y - prev_xy[1])) <= reach_cells
        new_in_reach = max(abs(t.position_x - new_xy[0]), abs(t.position_y - new_xy[1])) <= reach_cells
        if prev_in_reach and not new_in_reach:
            out.append(t)
    return out


def _eligible_hellish_rebuke_reactors(db_session: Session, gs: GameSession,
                                      target: LiveCharacter) -> list[LiveCharacter]:
    """Hellish Rebuke is self-only response to taking damage. Eligible iff target
    is a PC with reaction available, conscious, and has a 1st+ slot.
    """
    if target.is_enemy or target.is_dead:
        return []
    if (target.current_hp or 0) <= 0:
        # 5e RAW: Hellish Rebuke can be cast even at 0 HP via reaction trigger,
        # but practically a downed PC doesn't get the prompt — they're unconscious.
        return []
    if target.reaction_used:
        return []
    if not _has_slot_at_or_above(target, 1):
        return []
    return [target]


def _dist_ft_between(a: LiveCharacter, b: LiveCharacter) -> float:
    """Cheap chebyshev distance in feet (5 ft per square). Uses 5fps as default."""
    if a.position_x is None or b.position_x is None:
        return 9999.0
    dx = abs(a.position_x - b.position_x)
    dy = abs(a.position_y - b.position_y)
    return 5 * max(dx, dy)


def _fire_reaction_window(db_session: Session, gs: GameSession,
                          trigger_type: str, trigger_data: dict,
                          eligible_lcs: list, suspended_action: dict) -> bool:
    """Pause an in-flight action and broadcast a reaction prompt. Returns True if
    a window was opened (caller should not continue resolution); False if no
    eligible reactors (caller continues immediately).
    """
    if not eligible_lcs:
        return False
    import uuid, time
    gs.pending_reaction = {
        "id": str(uuid.uuid4()),
        "trigger_type": trigger_type,
        "trigger_data": trigger_data,
        "eligible_reactor_ids": [lc.id for lc in eligible_lcs],
        "responses": {str(lc.id): None for lc in eligible_lcs},
        "deadline_unix": time.time() + REACTION_TIMEOUT_SECONDS,
        "suspended_action": suspended_action,
        "created_at": time.time(),
    }
    db_session.add(gs)
    names = ", ".join(lc.name for lc in eligible_lcs)
    _log(db_session, gs, f"reaction window: {trigger_type} (eligible: {names})")
    return True


def _resume_suspended_action(db_session: Session, gs: GameSession, user: User) -> dict:
    """Resume the action stashed in gs.pending_reaction.suspended_action after
    reactors finished responding. Clears pending_reaction.
    """
    pr = dict(gs.pending_reaction or {})
    sa = pr.get("suspended_action") or {}
    kind = sa.get("kind")
    gs.pending_reaction = {}
    db_session.add(gs)

    # Apply any "use" responses' side effects first.
    rxn_outcome = pr.get("outcome", {})
    cancelled = bool(rxn_outcome.get("cancel_action"))
    ac_bonus = int(rxn_outcome.get("target_ac_bonus", 0))

    if kind == "cast":
        if cancelled:
            _log(db_session, gs, "  original spell countered — no resolution")
            return {"ok": True, "cancelled": True}
        # Resume the cast with the original params.
        params = sa.get("params") or {}
        _do_cast(db_session, gs, params, user, set(sa.get("bypass") or []))
        return {"ok": True, "resumed": "cast"}
    if kind == "attack":
        # Recompute hit if Shield bumped AC. The original to-hit is in sa.
        if cancelled:
            _log(db_session, gs, "  attack negated by reaction")
            return {"ok": True, "cancelled": True}
        params = sa.get("params") or {}
        target_id = int(sa.get("target_id"))
        target = db_session.get(LiveCharacter, target_id)
        eff_to_hit = int(sa.get("eff_to_hit", 0))
        eff_ac_now = int(sa.get("target_ac", 10)) + ac_bonus
        crit = bool(sa.get("critical"))
        damage = int(sa.get("damage", 0))
        damage_type = sa.get("damage_type", "slashing")
        # Crit always hits regardless of AC bumps.
        still_hit = crit or eff_to_hit >= eff_ac_now
        if still_hit:
            taken = _apply_damage_to(db_session, gs, target, damage, damage_type,
                                     was_crit=crit, source_attacker_id=sa.get("attacker_id"))
            _log(db_session, gs, f"  {sa.get('attacker_name','?')} hits {target.name} for {taken} {damage_type} (HP: {target.current_hp}/{target.max_hp}){' [Shield bumped AC]' if ac_bonus else ''}")
        else:
            _log(db_session, gs, f"  Shield raises AC to {eff_ac_now} — attack now misses")
        return {"ok": True, "resumed": "attack", "hit": still_hit}
    if kind == "walk":
        actor = db_session.get(LiveCharacter, int(sa.get("actor_id")))
        if not actor or actor.is_dead or (actor.current_hp or 0) <= 0:
            _log(db_session, gs, "  walk aborted: mover incapacitated")
            return {"ok": True, "aborted": True}
        remaining = sa.get("remaining_path") or []
        if not remaining:
            return {"ok": True}
        return _do_walk(db_session, gs, actor, remaining, user)
    if kind == "noop":
        # Nothing to resume; the trigger fired purely for a reactor's response
        # (e.g. Hellish Rebuke after damage applied).
        return {"ok": True}
    return {"ok": True}


def _resolve_reaction(db_session: Session, gs: GameSession, user: User,
                      reactor: LiveCharacter, choice: str, params: dict) -> dict:
    """Record reactor's response. If 'use', apply the reaction's side effect.
    Resume semantics depend on trigger type (some resume on first use, OA waits
    for all reactors to respond since multiple may swing at the same step).
    """
    pr = dict(gs.pending_reaction or {})
    if not pr:
        raise HTTPException(400, "no pending reaction window")
    kind = pr["trigger_type"]
    if reactor.id not in pr.get("eligible_reactor_ids", []):
        raise HTTPException(400, "not eligible to react")
    if pr["responses"].get(str(reactor.id)) is not None:
        raise HTTPException(400, "already responded")
    pr["responses"][str(reactor.id)] = choice
    outcome = dict(pr.get("outcome") or {})

    if choice == "use":
        spell_key = (params or {}).get("spell_name")
        if spell_key:
            # Spell-based reaction (Counterspell, Shield, Hellish Rebuke).
            slot_level = int((params or {}).get("slot_level", 1))
            spell = spells.get_spell(spell_key, db_session, gs.campaign_id)
            if not spell or spell.get("casting_time") != "reaction":
                raise HTTPException(400, "spell missing or not a reaction-time spell")
            slots = dict(reactor.spell_slots or {})
            if slots.get(str(slot_level), 0) <= 0:
                raise HTTPException(400, f"no level {slot_level} slot")
            slots[str(slot_level)] = slots[str(slot_level)] - 1
            reactor.spell_slots = slots
            reactor.reaction_used = True
            db_session.add(reactor)
            _log(db_session, gs, f"  {reactor.name} reacts with {spell['name']} (slot {slot_level})")

            if kind == "spell_cast" and spell_key == "counterspell":
                original_level = int(pr.get("trigger_data", {}).get("spell_level", 1))
                if slot_level >= original_level:
                    outcome["cancel_action"] = True
                    _log(db_session, gs, f"  Counterspell auto-counters (slot {slot_level} ≥ spell level {original_level})")
                else:
                    src_char = db_session.get(Character, reactor.source_character_id) if reactor.source_character_id else None
                    _, _, scm = _spellcasting_modifiers(reactor, src_char)
                    dc = 10 + original_level
                    check = dice.roll_d20(scm)
                    if check.total >= dc:
                        outcome["cancel_action"] = True
                        _log(db_session, gs, f"  Counterspell ability check: {check.total} ≥ DC {dc} — countered")
                    else:
                        _log(db_session, gs, f"  Counterspell ability check: {check.total} < DC {dc} — fails, spell proceeds")
            elif kind == "attack_hit" and spell_key == "shield":
                outcome["target_ac_bonus"] = (outcome.get("target_ac_bonus", 0) + 5)
                _apply_spell_effects(db_session, gs, spell, reactor, [reactor],
                                     None, None, slot_level, 13)
            elif kind == "damage_taken" and spell_key == "hellish_rebuke":
                _resolve_hellish_rebuke(db_session, gs, reactor, pr["trigger_data"], slot_level)
        else:
            # Non-spell reaction (Opportunity Attack).
            reactor.reaction_used = True
            db_session.add(reactor)
            if kind == "movement_oa":
                _resolve_oa_attack(db_session, gs, reactor, pr["trigger_data"])

    pr["outcome"] = outcome
    gs.pending_reaction = pr
    db_session.add(gs)

    any_used = any(v == "use" for v in pr["responses"].values())
    all_responded = all(v is not None for v in pr["responses"].values())
    if kind == "spell_cast":
        # Counterspell: any "use" cancels; otherwise wait for all to skip.
        should_resume = any_used or all_responded
    elif kind in ("attack_hit", "damage_taken"):
        # Single eligible reactor by construction; first response resumes.
        should_resume = all_responded
    elif kind == "movement_oa":
        # Multiple OAs may resolve in parallel; wait for everyone to respond.
        should_resume = all_responded
    else:
        should_resume = all_responded

    if should_resume:
        return _resume_suspended_action(db_session, gs, user)
    return {"ok": True, "waiting_on": [
        rid for rid, v in pr["responses"].items() if v is None]}


def _resolve_oa_attack(db_session: Session, gs: GameSession,
                       reactor: LiveCharacter, trigger_data: dict) -> None:
    """Reactor swings at the mover with their basic melee profile."""
    mover_id = int(trigger_data.get("mover_id"))
    mover = db_session.get(LiveCharacter, mover_id)
    if not mover or mover.is_dead or (mover.current_hp or 0) <= 0:
        return
    to_hit, damage_dice, damage_type = _basic_attack_profile(reactor)
    attacker_conds = [c["name"] for c in (reactor.conditions or [])]
    target_conds = [c["name"] for c in (mover.conditions or [])]
    mods = effects_mod.collect_attack_modifiers(db_session, gs.id, reactor.id, mover.id)
    if mods.image_log:
        _log(db_session, gs, f"  {mods.image_log}")
    result = combat.make_attack(
        reactor.name, mover.name, to_hit + mods.bonus, mover.armor_class,
        damage_dice, damage_type, attacker_conds, target_conds, distance_ft=5,
        extra_attack_dice=list(mods.extra_dice),
        subtract_attack_dice=list(mods.subtract_dice),
        extra_advantage=mods.advantage, extra_disadvantage=mods.disadvantage,
        damage_bonus=mods.damage_bonus,
        extra_damage_on_hit=list(mods.extra_damage_dice),
        target_ac_bonus=mods.target_ac_bonus,
        image_redirect_ac=mods.image_ac if mods.redirect_to_image else None,
    )
    _log(db_session, gs, f"  OA: {reactor.name} → {mover.name}")
    if result.image_hit:
        _consume_mirror_image(db_session, gs, mover)
        _log(db_session, gs, f"  {result.description}")
    elif result.hit:
        taken = _apply_damage_to(db_session, gs, mover, result.total_damage,
                                 damage_type, was_crit=result.critical)
        _log(db_session, gs, f"  {result.description} (HP: {mover.current_hp}/{mover.max_hp})")
    else:
        _log(db_session, gs, f"  {result.description}")


def _resolve_hellish_rebuke(db_session: Session, gs: GameSession,
                            caster: LiveCharacter, trigger_data: dict,
                            slot_level: int) -> None:
    """Source attacker makes DEX save vs caster's spell save DC; takes 2d10
    fire (or +1d10 per slot above 1st), half on success. Slot was already
    consumed by the caller.
    """
    src_id = trigger_data.get("source_attacker_id")
    if src_id is None:
        return
    target = db_session.get(LiveCharacter, int(src_id))
    if not target or target.is_dead:
        return
    char = db_session.get(Character, caster.source_character_id) if caster.source_character_id else None
    save_dc, _, _ = _spellcasting_modifiers(caster, char)
    dex_mod = _ability_mod(target.dexterity or 10)
    if "dexterity" in (target.saving_throw_profs or []):
        dex_mod += _proficiency_bonus(target.level or 1)
    sv_mods = effects_mod.collect_save_modifiers(db_session, gs.id, target.id, "dexterity")
    sv = combat.make_save(target.name, "dexterity", dex_mod, save_dc,
        [c["name"] for c in (target.conditions or [])],
        extra_dice=list(sv_mods.extra_dice), subtract_dice=list(sv_mods.subtract_dice),
        extra_advantage=sv_mods.advantage, extra_disadvantage=sv_mods.disadvantage,
        bonus=sv_mods.bonus,
    )
    extra_levels = max(0, slot_level - 1)
    dice_count = 2 + extra_levels
    roll = dice.roll(f"{dice_count}d10")
    amount = roll.total // 2 if sv.success else roll.total
    _log(db_session, gs, f"  Hellish Rebuke vs {target.name}: {sv.description}")
    _log(db_session, gs, f"    {roll.total} fire, {'half' if sv.success else 'full'} → {amount}")
    _apply_damage_to(db_session, gs, target, amount, "fire")


def _check_pending_reaction_or_block(db_session: Session, gs: GameSession) -> None:
    """Reject mutating actions while a reaction window is open."""
    if gs.pending_reaction:
        raise HTTPException(409, "waiting on reaction prompt — resolve it first")


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


def _all_condition_names(lc: LiveCharacter) -> list[str]:
    """Combined list of condition names + auto-derived exhaustion tier."""
    names = [c["name"] for c in (lc.conditions or [])]
    lvl = int(getattr(lc, "exhaustion_level", 0) or 0)
    if lvl > 0:
        names.append(f"exhaustion_{min(lvl, 6)}")
    return names


_INCAPACITATING_CONDITIONS = {"incapacitated", "paralyzed", "stunned", "unconscious", "petrified"}


def _is_incapacitated(lc: LiveCharacter) -> bool:
    return any(n in _INCAPACITATING_CONDITIONS for n in _all_condition_names(lc))


def _consume_attack_action(db_session: Session, gs: GameSession, lc: LiveCharacter) -> list[str]:
    """Action economy for attacks. Multi-attack-aware: the first attack of a turn
    consumes the Action and seeds `attacks_remaining_this_action` from the LC's
    `attacks_per_action`. Subsequent attacks within the same action just decrement
    the counter without re-consuming Action. Returns [] on success or [flag] if
    blocked. Off-turn (OAs, etc.) is unaffected — those go through reactions.
    """
    c = db_session.get(Campaign, gs.campaign_id)
    if not rules_mod.get_rule(c, "action_economy") or not gs.in_combat:
        return []
    cur_name = _current_initiative_name(gs)
    if cur_name != lc.name:
        return []  # off-turn
    if (lc.attacks_remaining_this_action or 0) > 0:
        lc.attacks_remaining_this_action = lc.attacks_remaining_this_action - 1
        db_session.add(lc)
        return []
    if gs.action_used:
        return ["action_used"]
    gs.action_used = True
    extra = max(0, (lc.attacks_per_action or 1) - 1)
    lc.attacks_remaining_this_action = extra
    db_session.add(lc)
    return []


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
            saving_throw_profs=list(char.saving_throw_profs or []),
            damage_resistances=list(getattr(char, "damage_resistances", []) or []),
            damage_immunities=list(getattr(char, "damage_immunities", []) or []),
            damage_vulnerabilities=list(getattr(char, "damage_vulnerabilities", []) or []),
            class_features=list(getattr(char, "class_features", []) or []),
            melee_reach_ft=getattr(char, "melee_reach_ft", 5) or 5,
            attacks_per_action=getattr(char, "attacks_per_action", 1) or 1,
            sneak_attack_dice=getattr(char, "sneak_attack_dice", 0) or 0,
            hit_die=(getattr(char, "hit_die", "d8") or "d8"),
            hit_dice_used=getattr(char, "hit_dice_used", 0) or 0,
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


@router.post("/sessions/{session_id}/short-rest")
def short_rest(
    session_id: int,
    dice_per_pc: int = Form(1),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """DM triggers a short rest. Each PC spends up to dice_per_pc hit dice;
    each die heals (rolled die + CON mod). Warlocks recover spell slots
    (class_features contains 'warlock_pact_magic').
    """
    gs = _require_session(db, session_id, user, dm_only=True)
    if gs.in_combat:
        raise HTTPException(400, "cannot short rest during combat")
    pcs = db.exec(
        select(LiveCharacter).where(
            LiveCharacter.session_id == session_id,
            LiveCharacter.is_active == True,
            LiveCharacter.is_enemy == False,
        )
    ).all()
    _push_undo(db, gs, "short rest")
    _log(db, gs, f"Short rest ({dice_per_pc} hit dice each)")
    for lc in pcs:
        if lc.is_dead:
            continue
        max_dice = lc.level or 1
        remaining = max(0, max_dice - (lc.hit_dice_used or 0))
        spend = min(dice_per_pc, remaining)
        if spend <= 0:
            _log(db, gs, f"  {lc.name}: no hit dice remaining")
            continue
        con_mod = ((lc.constitution or 10) - 10) // 2
        die = lc.hit_die or "d8"
        try:
            rolled = dice.roll(f"{spend}{die}")
        except Exception:
            rolled = dice.roll(f"{spend}d8")
        healed_total = rolled.total + con_mod * spend
        gained = _heal_to(db, gs, lc, max(0, healed_total))
        lc.hit_dice_used = (lc.hit_dice_used or 0) + spend
        if "warlock_pact_magic" in (lc.class_features or []):
            lc.spell_slots = _default_spell_slots(lc.level or 1)
            _log(db, gs, f"  {lc.name}: pact magic slots restored")
        # Restore short-rest resources to max.
        res = dict(lc.resources or {})
        for key, entry in list(res.items()):
            if (entry or {}).get("recharge") == "short_rest":
                entry = dict(entry)
                entry["current"] = entry.get("max", 0)
                res[key] = entry
        if res != (lc.resources or {}):
            lc.resources = res
            _log(db, gs, f"  {lc.name}: short-rest resources restored")
        db.add(lc)
        _log(db, gs, f"  {lc.name}: spent {spend}{die} ({rolled.total}+{con_mod*spend} CON), healed {gained} (HP {lc.current_hp}/{lc.max_hp})")
    db.commit()
    events.publish(session_id)
    return {"ok": True}


@router.post("/sessions/{session_id}/long-rest")
def long_rest(
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """DM triggers a long rest. PCs heal to full, spell slots restore, downed PCs
    wake up. In persistent-death-save mode, each PC's death_save_failures is
    decremented by 1 (floor 0). Successes always reset to 0.
    """
    gs = _require_session(db, session_id, user, dm_only=True)
    if gs.in_combat:
        raise HTTPException(400, "cannot long rest during combat")
    persistent = _persistent_death(db, gs)
    pcs = db.exec(
        select(LiveCharacter).where(
            LiveCharacter.session_id == session_id,
            LiveCharacter.is_active == True,
            LiveCharacter.is_enemy == False,
        )
    ).all()
    _push_undo(db, gs, "long rest")
    _log(db, gs, "Long rest")
    for lc in pcs:
        if lc.is_dead:
            _log(db, gs, f"  {lc.name}: still dead — no rest restores them")
            continue
        lc.current_hp = lc.max_hp
        lc.temp_hp = 0
        lc.is_stable = False
        lc.death_save_successes = 0
        if persistent and lc.death_save_failures > 0:
            lc.death_save_failures = max(0, lc.death_save_failures - 1)
            note = f" (death-save failures now {lc.death_save_failures})"
        else:
            lc.death_save_failures = 0
            note = ""
        lc.spell_slots = _default_spell_slots(lc.level or 1)
        # Long rest restores all resources (short and long).
        res = dict(lc.resources or {})
        for key, entry in list(res.items()):
            entry = dict(entry or {})
            entry["current"] = entry.get("max", 0)
            res[key] = entry
        lc.resources = res
        # Hit dice: regain up to half of total on long rest (RAW).
        max_hd = lc.level or 1
        regained_hd = min((lc.hit_dice_used or 0), max(1, max_hd // 2))
        lc.hit_dice_used = max(0, (lc.hit_dice_used or 0) - regained_hd)
        db.add(lc)
        _log(db, gs, f"  {lc.name}: full HP, slots restored, all resources refilled, regained {regained_hd} hit dice{note}")
    db.commit()
    events.publish(session_id)
    return {"ok": True}


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
    from game.monsters import list_monsters, cr_to_xp, party_thresholds, encounter_multiplier
    monsters_list = list_monsters() if is_dm else []
    # Encounter XP calculator: sum CR-derived XP across active enemies, compute party thresholds.
    encounter_data = None
    if is_dm:
        enemies = [lc for lc in lcs if lc.is_enemy and lc.is_active and not lc.is_dead]
        pcs = [lc for lc in lcs if not lc.is_enemy and lc.is_active]
        raw_xp = sum(cr_to_xp(lc.challenge_rating or "") for lc in enemies)
        mult = encounter_multiplier(len(enemies))
        adjusted_xp = int(raw_xp * mult)
        e, m, h, d = party_thresholds([lc.level or 1 for lc in pcs])
        if raw_xp > 0 or pcs:
            difficulty = "trivial"
            if adjusted_xp >= d and d > 0:
                difficulty = "deadly"
            elif adjusted_xp >= h and h > 0:
                difficulty = "hard"
            elif adjusted_xp >= m and m > 0:
                difficulty = "medium"
            elif adjusted_xp >= e and e > 0:
                difficulty = "easy"
            encounter_data = {
                "enemy_count": len(enemies), "raw_xp": raw_xp,
                "multiplier": mult, "adjusted_xp": adjusted_xp,
                "thresholds": {"easy": e, "medium": m, "hard": h, "deadly": d},
                "difficulty": difficulty,
            }
    # Objects catalog for the spawn-object form.
    objects_list: list = []
    if is_dm:
        try:
            import json as _j
            from pathlib import Path as _P
            obj_data = _j.loads((_P(__file__).parent / "data" / "objects.json").read_text())
            objects_list = sorted(
                ({"key": k, **v} for k, v in obj_data.items()),
                key=lambda o: o.get("name", "")
            )
        except Exception:
            objects_list = []
    all_effs = _all_active_effects_for_session(db, gs.id)
    effects_by_target: dict = {}
    for eff in all_effs:
        effects_by_target.setdefault(eff.target_live_id, []).append({
            "id": eff.id, "name": eff.name, "description": eff.description,
            "is_concentration": eff.is_concentration, "duration_rounds": eff.duration_rounds,
            "handler_key": eff.handler_key, "caster_live_id": eff.caster_live_id,
        })
    my_lc_ids = [lc.id for lc in lcs if lc.owner_id == user.id]
    # Per-LC spellcasting modifiers for cast form auto-fill (DC / +atk / +mod).
    casting_mods_by_lc: dict = {}
    for lc in lcs:
        if lc.is_enemy:
            continue
        src_char = db.get(Character, lc.source_character_id) if lc.source_character_id else None
        save_dc, atk_mod, scm = _spellcasting_modifiers(lc, src_char)
        casting_mods_by_lc[lc.id] = {"dc": save_dc, "atk_mod": atk_mod, "scm": scm}
    # Per-LC concentration: which effect is each LC currently concentrating on?
    concentration_by_lc: dict = {}
    for eff in all_effs:
        if eff.is_concentration and eff.caster_live_id:
            concentration_by_lc[eff.caster_live_id] = eff.name
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
        "effects_by_target": effects_by_target,
        "my_lc_ids": my_lc_ids,
        "casting_mods_by_lc": casting_mods_by_lc,
        "concentration_by_lc": concentration_by_lc,
        "monsters": monsters_list,
        "objects_catalog": objects_list,
        "encounter_data": encounter_data,
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
    ending_name = gs.initiative_order[gs.current_turn_index].get("name")
    ending_lc = db.exec(
        select(LiveCharacter).where(
            LiveCharacter.session_id == gs.id, LiveCharacter.name == ending_name
        )
    ).first()
    if ending_lc:
        # Save-each-turn for ongoing concentration spells (Hold Person etc.)
        _process_save_each_turn(db, gs, ending_lc)
        # Tick effects ending with this creature's turn.
        for nm in effects_mod.tick_durations(db, gs.id, "caster_end_of_turn", ending_lc.id):
            _log(db, gs, f"  effect expired: {nm}")
        for nm in effects_mod.tick_durations(db, gs.id, "target_end_of_turn", ending_lc.id):
            _log(db, gs, f"  effect expired: {nm}")
    gs.current_turn_index = (gs.current_turn_index + 1) % len(gs.initiative_order)
    if gs.current_turn_index == 0:
        gs.round_number += 1
        _log(db, gs, f"-- Round {gs.round_number} --")
    _reset_turn_state(gs)
    name = gs.initiative_order[gs.current_turn_index]["name"]
    _log(db, gs, f"{name}'s turn")
    # Start-of-turn: downed PC rolls a death save automatically.
    starting_lc = db.exec(
        select(LiveCharacter).where(
            LiveCharacter.session_id == gs.id, LiveCharacter.name == name
        )
    ).first()
    if starting_lc:
        starting_lc.reaction_used = False
        starting_lc.attacks_remaining_this_action = 0
        starting_lc.sneak_attack_used_this_turn = False
        db.add(starting_lc)
        _roll_death_save(db, gs, starting_lc)
        _aura_check_turn_start(db, gs, starting_lc)
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


COVER_AC_BONUS = {"none": 0, "half": 2, "three_quarters": 5, "full": 0}


def _is_flanking(db_session: Session, gs: GameSession,
                 attacker: LiveCharacter, target: LiveCharacter) -> bool:
    """5e DMG flanking variant: attacker and an ally are on opposite cells of
    the target on a square grid, both adjacent. Skip for incapacitated/downed
    allies. Hex grid: opposite-vertex check works the same way.
    """
    if not gs.in_combat:
        return False
    if attacker.position_x is None or target.position_x is None:
        return False
    ax, ay = attacker.position_x, attacker.position_y
    tx, ty = target.position_x, target.position_y
    dx, dy = ax - tx, ay - ty
    if max(abs(dx), abs(dy)) != 1:
        return False
    opp_x, opp_y = tx - dx, ty - dy
    allies = db_session.exec(
        select(LiveCharacter).where(
            LiveCharacter.session_id == gs.id,
            LiveCharacter.is_active == True,
            LiveCharacter.id != attacker.id,
            LiveCharacter.is_enemy == attacker.is_enemy,
        )
    ).all()
    for ally in allies:
        if ally.id == target.id:
            continue
        if (ally.current_hp or 0) <= 0:
            continue
        if ally.position_x == opp_x and ally.position_y == opp_y:
            return True
    return False


def _compute_cover_from_geometry(db_session: Session, gs: GameSession,
                                  attacker: LiveCharacter, target: LiveCharacter) -> str:
    """Auto-detect cover from map walls + block-walls (Wall of Force/Stone).
    DMG corner method: from each attacker corner, trace 4 lines to target corners;
    fewest-blocked count wins (attacker picks best angle).
        0 blocked → none, 1-2 → half, 3 → three-quarters, 4 from every corner → full.
    """
    if not gs.active_map_id:
        return "none"
    m = db_session.get(Map, gs.active_map_id)
    if not m or attacker.position_x is None or target.position_x is None:
        return "none"
    grid_type = m.grid_type or "square"
    walls = list(m.walls or [])
    for eff in effects_mod.list_area_effects(db_session, gs.id):
        if not eff.area or eff.area.get("shape") != "wall":
            continue
        pts = eff.area.get("points") or []
        if len(pts) >= 2 and (eff.payload or {}).get("blocks_movement"):
            walls.append({"x1": pts[0][0], "y1": pts[0][1], "x2": pts[1][0], "y2": pts[1][1]})
    if not walls:
        return "none"
    if grid_type == "hex":
        af = movement._cell_center_xy(attacker.position_x, attacker.position_y, "hex")
        tc = movement._cell_center_xy(target.position_x, target.position_y, "hex")
        for w in walls:
            try:
                if movement._segments_cross(af, tc, (w["x1"], w["y1"]), (w["x2"], w["y2"])):
                    return "three_quarters"
            except (KeyError, TypeError):
                continue
        return "none"
    ax, ay = attacker.position_x, attacker.position_y
    tx, ty = target.position_x, target.position_y
    a_corners = [(ax + dx, ay + dy) for dx in (0, 1) for dy in (0, 1)]
    t_corners = [(tx + dx, ty + dy) for dx in (0, 1) for dy in (0, 1)]
    best_blocked = 5
    for ac in a_corners:
        blocked = 0
        for tc in t_corners:
            for w in walls:
                try:
                    if movement._segments_cross(ac, tc, (w["x1"], w["y1"]), (w["x2"], w["y2"])):
                        blocked += 1
                        break
                except (KeyError, TypeError):
                    continue
        if blocked < best_blocked:
            best_blocked = blocked
    if best_blocked == 0:
        return "none"
    if best_blocked <= 2:
        return "half"
    if best_blocked <= 3:
        return "three_quarters"
    return "full"


FUMBLE_TABLE = [
    "slips and falls prone",
    "fumbles their weapon — drop it at their feet",
    "swings wide — narrowly misses an adjacent ally (DM judges)",
    "loses balance — disadvantage on their next attack roll",
    "weapon strike sticks — lose next action freeing it",
    "just a regular miss",
]


def _maybe_roll_fumble(db_session: Session, gs: GameSession, attacker: LiveCharacter) -> None:
    """When crit_fumble_table is enabled, roll a d6 against the fumble table and log."""
    c = db_session.get(Campaign, gs.campaign_id)
    if not rules_mod.get_rule(c, "crit_fumble_table"):
        return
    roll = dice.roll("1d6")
    consequence = FUMBLE_TABLE[(roll.total - 1) % len(FUMBLE_TABLE)]
    _log(db_session, gs, f"  fumble (d6={roll.total}): {attacker.name} {consequence}")


def _do_attack(db_session: Session, gs: GameSession, params: dict, user: User, bypass: set[str]) -> None:
    attacker = _require_live(db_session, gs.id, int(params["attacker_id"]))
    target = _require_live(db_session, gs.id, int(params["target_id"]))
    if _is_incapacitated(attacker):
        cond_list = ", ".join(n for n in _all_condition_names(attacker) if n in _INCAPACITATING_CONDITIONS)
        _log(db_session, gs, f"{attacker.name} is incapacitated ({cond_list}) — attack denied")
        return
    _push_undo(db_session, gs, f"{attacker.name} attacks {target.name}")
    cover = (params.get("cover") or "none").lower()
    # Auto-detect when caller didn't override (cover == "none").
    if cover == "none":
        auto = _compute_cover_from_geometry(db_session, gs, attacker, target)
        if auto != "none":
            cover = auto
    if cover == "full":
        _log(db_session, gs, f"{attacker.name} can't target {target.name} (full cover)")
        return
    effective_bypass = bypass | _campaign_bypass(db_session, gs)
    flags = _validate_attack(db_session, gs, attacker, target, effective_bypass, distance_ft=int(params.get("distance_ft", 5)))
    # Action economy: first attack of the turn consumes Action and seeds the
    # multi-attack counter; follow-up attacks just decrement. _consume_attack_action
    # handles both branches. Two-weapon off-hand attacks consume Bonus Action instead.
    if params.get("off_hand"):
        flags = flags + _consume_turn_resource(db_session, gs, attacker, "bonus_action")
    else:
        flags = flags + _consume_attack_action(db_session, gs, attacker)
    if flags:
        if _is_pc_action(attacker):
            summary = f"{attacker.name} attacks {target.name}"
            _queue_pending(db_session, gs, "attack", attacker, params, flags, user, summary)
            label = ", ".join(FLAG_LABELS.get(f, f) for f in flags)
            _log(db_session, gs, f"pending DM approval: {summary} ({label})")
            return
        label = ", ".join(FLAG_LABELS.get(f, f) for f in flags)
        _log(db_session, gs, f"warning: {attacker.name} attacks {target.name}: {label}")
    attacker_conds = _all_condition_names(attacker)
    target_conds = _all_condition_names(target)
    mods = effects_mod.collect_attack_modifiers(db_session, gs.id, attacker.id, target.id)
    if mods.image_log:
        _log(db_session, gs, f"  {mods.image_log}")
    cover_bonus = COVER_AC_BONUS.get(cover, 0)
    if cover_bonus:
        mods.target_ac_bonus = (mods.target_ac_bonus or 0) + cover_bonus
        _log(db_session, gs, f"  {target.name} has {cover.replace('_', '-')} cover (+{cover_bonus} AC)")
    # Ranged range bands: if both short/long range filled, beyond short = disadvantage,
    # beyond long = auto-fail.
    short_range = int(params.get("short_range_ft", 0) or 0)
    long_range = int(params.get("long_range_ft", 0) or 0)
    distance_for_range = int(params.get("distance_ft", 5))
    if short_range > 0 and long_range > 0:
        if distance_for_range > long_range:
            _log(db_session, gs, f"{attacker.name}'s attack is beyond max range ({distance_for_range}ft > {long_range}ft) — auto-fails")
            return
        if distance_for_range > short_range:
            mods.disadvantage = True
            _log(db_session, gs, f"  long-range attack ({distance_for_range}ft > {short_range}ft) — disadvantage")
    c = db_session.get(Campaign, gs.campaign_id)
    crit_rule = rules_mod.get_rule(c, "crit_rule")
    # Flanking: melee attacker with an ally on the opposite cell of the target gets advantage.
    if rules_mod.get_rule(c, "flanking") == "advantage":
        distance_ft = int(params.get("distance_ft", 5))
        if distance_ft <= 5 and _is_flanking(db_session, gs, attacker, target):
            mods.advantage = True
            _log(db_session, gs, f"  {attacker.name} is flanking {target.name} (advantage)")
    # Pack Tactics: creature with the feature gets advantage when an ally is adjacent to target.
    if (rules_mod.get_rule(c, "pack_tactics_auto")
            and "pack_tactics" in (attacker.class_features or [])):
        allies = db_session.exec(
            select(LiveCharacter).where(
                LiveCharacter.session_id == gs.id,
                LiveCharacter.is_active == True,
                LiveCharacter.id != attacker.id,
                LiveCharacter.is_enemy == attacker.is_enemy,
            )
        ).all()
        for ally in allies:
            if ally.id == target.id:
                continue
            if (ally.current_hp or 0) <= 0 or ally.position_x is None or target.position_x is None:
                continue
            if max(abs(ally.position_x - target.position_x), abs(ally.position_y - target.position_y)) == 1:
                mods.advantage = True
                _log(db_session, gs, f"  Pack Tactics: {ally.name} is adjacent to {target.name} (advantage)")
                break
    # Sneak Attack: rogue with advantage (auto-eligible) or manual flag adds Nd6.
    # Once per turn; only fires on hit (extra_damage_on_hit is rolled only on hit).
    sneak_applied = False
    sneak_n = int(attacker.sneak_attack_dice or 0)
    if sneak_n > 0 and not attacker.sneak_attack_used_this_turn:
        manual_flag = bool(params.get("sneak_attack"))
        eligible = (mods.advantage and not mods.disadvantage) or manual_flag
        if eligible:
            mods.extra_damage_dice.append((f"{sneak_n}d6", params.get("damage_type", "slashing")))
            sneak_applied = True
    # Primed smites: collect attacker's smite_on_hit effects; append their damage
    # to this attack and queue them for consumption if it hits in melee.
    distance_ft = int(params.get("distance_ft", 5))
    smite_effects_to_consume: list = []
    if distance_ft <= (attacker.melee_reach_ft or 5):
        for eff in effects_mod.list_effects_on(db_session, gs.id, attacker.id):
            if eff.handler_key == "smite_on_hit":
                p = eff.payload or {}
                mods.extra_damage_dice.append((p.get("dice", "1d6"), p.get("type", "radiant")))
                smite_effects_to_consume.append(eff)
                _log(db_session, gs, f"  {eff.name} primed: +{p.get('dice','1d6')} {p.get('type','radiant')}")
    result = combat.make_attack(
        attacker.name, target.name,
        int(params.get("to_hit_modifier", 0)) + mods.bonus, target.armor_class,
        params.get("damage_dice", "1d6"), params.get("damage_type", "slashing"),
        attacker_conds, target_conds,
        int(params.get("distance_ft", 5)),
        extra_attack_dice=list(mods.extra_dice),
        subtract_attack_dice=list(mods.subtract_dice),
        extra_advantage=mods.advantage, extra_disadvantage=mods.disadvantage,
        damage_bonus=mods.damage_bonus,
        extra_damage_on_hit=list(mods.extra_damage_dice),
        target_ac_bonus=mods.target_ac_bonus,
        image_redirect_ac=mods.image_ac if mods.redirect_to_image else None,
        crit_rule=crit_rule,
    )
    if result.fumble:
        _maybe_roll_fumble(db_session, gs, attacker)
    if result.image_hit:
        _consume_mirror_image(db_session, gs, target)
        msg = result.description
        _log(db_session, gs, msg)
        return
    if sneak_applied and result.hit:
        attacker.sneak_attack_used_this_turn = True
        db_session.add(attacker)
        _log(db_session, gs, f"  Sneak Attack +{sneak_n}d6 (used this turn)")
    if result.hit and not result.image_hit and smite_effects_to_consume:
        ctx = effects_mod.EffectContext(db=db_session, session_id=gs.id)
        for eff in smite_effects_to_consume:
            _log(db_session, gs, f"  {eff.name} consumed")
            effects_mod.remove_effect(db_session, eff, ctx)
    # Divine Smite: paladin spends a slot on a successful melee hit for extra
    # radiant. 2d8 at slot 1, +1d8 per slot above (cap 5d8). Slot consumed only
    # on hit. Resolved here so the smite damage is included in this attack's
    # damage application (concentration check, instakill threshold, etc.).
    smite_slot = int(params.get("smite_slot_level", 0) or 0)
    if smite_slot > 0 and result.hit and not result.image_hit:
        distance_ft = int(params.get("distance_ft", 5))
        if distance_ft <= (attacker.melee_reach_ft or 5):
            slots = dict(attacker.spell_slots or {})
            if slots.get(str(smite_slot), 0) > 0:
                slots[str(smite_slot)] = slots[str(smite_slot)] - 1
                attacker.spell_slots = slots
                db_session.add(attacker)
                n = min(2 + max(0, smite_slot - 1), 5)
                smite_roll = dice.roll(f"{n}d8")
                result.total_damage += smite_roll.total
                result.damage_rolls.append(smite_roll)
                result.damage_types.append("radiant")
                _log(db_session, gs, f"  Divine Smite: +{smite_roll.total} radiant ({n}d8, slot {smite_slot})")
            else:
                _log(db_session, gs, f"  Divine Smite skipped: no slot {smite_slot}")
        else:
            _log(db_session, gs, f"  Divine Smite skipped: target out of melee reach")
    if result.hit:
        # Shield trigger: suspend before applying damage so target can opt to react.
        eligible = _eligible_shield_reactors(db_session, gs, target)
        if eligible:
            opened = _fire_reaction_window(
                db_session, gs, "attack_hit",
                {"attacker_name": attacker.name, "target_id": target.id, "target_name": target.name,
                 "to_hit_total": result.effective_total, "target_ac": result.target_ac,
                 "damage": result.total_damage, "damage_type": params.get("damage_type", "slashing"),
                 "critical": result.critical, "description": result.description},
                eligible,
                {"kind": "attack", "params": params, "target_id": target.id,
                 "attacker_id": attacker.id, "attacker_name": attacker.name,
                 "target_ac": result.target_ac,
                 "eff_to_hit": result.effective_total, "critical": result.critical,
                 "damage": result.total_damage, "damage_type": params.get("damage_type", "slashing")},
            )
            if opened:
                _log(db_session, gs, f"  {result.description} — pending Shield reaction")
                return
        taken = _apply_damage_to(db_session, gs, target, result.total_damage,
                                 params.get("damage_type", "slashing"),
                                 was_crit=result.critical,
                                 source_attacker_id=attacker.id)
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
    sneak_attack: bool = Form(False),
    smite_slot_level: int = Form(0),
    cover: str = Form("none"),
    off_hand: bool = Form(False),
    short_range_ft: int = Form(0),
    long_range_ft: int = Form(0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user)
    _check_pending_reaction_or_block(db, gs)
    _do_attack(db, gs, {
        "attacker_id": attacker_id, "target_id": target_id,
        "to_hit_modifier": to_hit_modifier, "damage_dice": damage_dice,
        "damage_type": damage_type, "distance_ft": distance_ft,
        "sneak_attack": sneak_attack, "smite_slot_level": smite_slot_level,
        "cover": cover, "off_hand": off_hand,
        "short_range_ft": short_range_ft, "long_range_ft": long_range_ft,
    }, user, bypass=set())
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


def _athletics_acrobatics_contest_roll(db_session: Session, lc: LiveCharacter, allow_acrobatics: bool = True) -> tuple:
    """Returns (roll_total, label) — best of Athletics or (optionally) Acrobatics."""
    char = db_session.get(Character, lc.source_character_id) if lc.source_character_id else None
    pb = _proficiency_bonus(lc.level or 1)
    str_mod = ((lc.strength or 10) - 10) // 2
    dex_mod = ((lc.dexterity or 10) - 10) // 2
    a_bonus = pb if (char and "Athletics" in (char.skill_profs or [])) else 0
    ac_bonus = pb if (char and "Acrobatics" in (char.skill_profs or [])) else 0
    athletics_total_mod = str_mod + a_bonus
    acrobatics_total_mod = dex_mod + ac_bonus
    if allow_acrobatics and acrobatics_total_mod > athletics_total_mod:
        roll = dice.roll_d20(acrobatics_total_mod)
        return roll.total, "Acrobatics"
    roll = dice.roll_d20(athletics_total_mod)
    return roll.total, "Athletics"


@router.post("/sessions/{session_id}/grapple")
def grapple(
    session_id: int,
    attacker_id: int = Form(),
    target_id: int = Form(),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user)
    attacker = _require_live(db, session_id, attacker_id)
    target = _require_live(db, session_id, target_id)
    if _is_incapacitated(attacker):
        _log(db, gs, f"{attacker.name} can't grapple — incapacitated")
        return RedirectResponse(f"/sessions/{session_id}", status_code=303)
    _push_undo(db, gs, f"{attacker.name} grapples {target.name}")
    a_total, a_label = _athletics_acrobatics_contest_roll(db, attacker, allow_acrobatics=False)
    t_total, t_label = _athletics_acrobatics_contest_roll(db, target, allow_acrobatics=True)
    _log(db, gs, f"Grapple: {attacker.name} ({a_label}) {a_total} vs {target.name} ({t_label}) {t_total}")
    if a_total > t_total:
        existing = list(target.conditions or [])
        if not any(c.get("name") == "grappled" for c in existing):
            existing.append({"name": "grappled", "source_lc_id": attacker.id})
            target.conditions = existing
            db.add(target)
        _log(db, gs, f"  {target.name} is grappled by {attacker.name}")
    else:
        _log(db, gs, f"  {target.name} resists the grapple")
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/shove")
def shove(
    session_id: int,
    attacker_id: int = Form(),
    target_id: int = Form(),
    mode: str = Form("prone"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user)
    attacker = _require_live(db, session_id, attacker_id)
    target = _require_live(db, session_id, target_id)
    if _is_incapacitated(attacker):
        _log(db, gs, f"{attacker.name} can't shove — incapacitated")
        return RedirectResponse(f"/sessions/{session_id}", status_code=303)
    _push_undo(db, gs, f"{attacker.name} shoves {target.name}")
    a_total, a_label = _athletics_acrobatics_contest_roll(db, attacker, allow_acrobatics=False)
    t_total, t_label = _athletics_acrobatics_contest_roll(db, target, allow_acrobatics=True)
    _log(db, gs, f"Shove ({mode}): {attacker.name} ({a_label}) {a_total} vs {target.name} ({t_label}) {t_total}")
    if a_total > t_total:
        if mode == "prone":
            existing = list(target.conditions or [])
            if not any(c.get("name") == "prone" for c in existing):
                existing.append({"name": "prone"})
                target.conditions = existing
                db.add(target)
            _log(db, gs, f"  {target.name} knocked prone")
        else:
            if attacker.position_x is not None and target.position_x is not None:
                dx = target.position_x - attacker.position_x
                dy = target.position_y - attacker.position_y
                mag = max(abs(dx), abs(dy)) or 1
                step_x = dx // mag if dx else 0
                step_y = dy // mag if dy else 0
                target.position_x = (target.position_x or 0) + step_x
                target.position_y = (target.position_y or 0) + step_y
                db.add(target)
                _log(db, gs, f"  {target.name} pushed 5ft to ({target.position_x}, {target.position_y})")
            else:
                _log(db, gs, f"  {target.name} pushed (positions unknown)")
    else:
        _log(db, gs, f"  {target.name} resists the shove")
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


POTION_TABLE = {
    "healing": ("2d4+2", "healing"),
    "greater_healing": ("4d4+4", "greater healing"),
    "superior_healing": ("8d4+8", "superior healing"),
    "supreme_healing": ("10d4+20", "supreme healing"),
}


@router.post("/sessions/{session_id}/use-potion")
def use_potion(
    session_id: int,
    actor_id: int = Form(),
    potion_type: str = Form("healing"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user)
    lc = _require_live(db, session_id, actor_id)
    pt = POTION_TABLE.get(potion_type.lower())
    if not pt:
        raise HTTPException(400, f"unknown potion: {potion_type}")
    dice_expr, label = pt
    _push_undo(db, gs, f"{lc.name} drinks {label} potion")
    rolled = dice.roll(dice_expr)
    healed = _heal_to(db, gs, lc, rolled.total)
    _log(db, gs, f"{lc.name} drinks potion of {label} ({dice_expr}={rolled.total}); heals {healed} (HP {lc.current_hp}/{lc.max_hp})")
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/use-scroll")
def use_scroll(
    session_id: int,
    actor_id: int = Form(),
    spell_name: str = Form(),
    target_ids: str = Form(""),
    aoe_x: Optional[int] = Form(None),
    aoe_y: Optional[int] = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """Cast a spell from a scroll without consuming a slot. If the spell level
    exceeds what the actor can naturally cast, ability check 10 + spell level.
    """
    gs = _require_session(db, session_id, user)
    lc = _require_live(db, session_id, actor_id)
    spell = spells.get_spell(spell_name, db, gs.campaign_id)
    if not spell:
        raise HTTPException(400, f"unknown spell: {spell_name}")
    spell_level = int(spell.get("level", 0))
    # Ability check: if scroll level exceeds caster's max known slot level
    max_slot_known = max((int(k) for k, v in (lc.spell_slots or {}).items() if v >= 0), default=0)
    if spell_level > max_slot_known:
        char = db.get(Character, lc.source_character_id) if lc.source_character_id else None
        _, _, scm = _spellcasting_modifiers(lc, char)
        dc = 10 + spell_level
        check = dice.roll_d20(scm)
        if check.total < dc:
            _log(db, gs, f"{lc.name} fumbles scroll of {spell['name']}: ability check {check.total} < DC {dc} — scroll consumed without effect")
            db.commit()
            events.publish(session_id)
            return RedirectResponse(f"/sessions/{session_id}", status_code=303)
        _log(db, gs, f"{lc.name} masters scroll of {spell['name']}: ability check {check.total} ≥ DC {dc}")
    # Resolve cast as if normal, but skip slot consumption (slot_level 0 maps to spell's level for resolution).
    cast_params = {
        "spell_name": spell_name, "caster_id": actor_id, "slot_level": max(1, spell_level),
        "target_ids": target_ids, "aoe_x": aoe_x, "aoe_y": aoe_y,
        "aoe_dx": 1, "aoe_dy": 0, "wall_x2": None, "wall_y2": None,
        "cover": "none",
        "spell_save_dc": _spellcasting_modifiers(lc, db.get(Character, lc.source_character_id) if lc.source_character_id else None)[0],
        "spell_attack_modifier": _spellcasting_modifiers(lc, db.get(Character, lc.source_character_id) if lc.source_character_id else None)[1],
        "spellcasting_modifier": _spellcasting_modifiers(lc, db.get(Character, lc.source_character_id) if lc.source_character_id else None)[2],
        "enforce_slots": False, "scroll": True,
    }
    _log(db, gs, f"{lc.name} reads a scroll of {spell['name']}")
    _do_cast(db, gs, cast_params, user, bypass=set())
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/lc/{lc_id}/resource/use")
def use_resource(
    session_id: int,
    lc_id: int,
    key: str = Form(),
    amount: int = Form(1),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """Generic class-resource consume: decrement resources[key].current by amount.
    DM/owner only. Useful for ki points, sorcery points, lay-on-hands pool, channel
    divinity, action surge uses, second wind uses, bardic inspiration dice, etc.
    """
    gs = _require_session(db, session_id, user)
    lc = _require_live(db, session_id, lc_id)
    if not (gs.campaign_id and (db.get(Campaign, gs.campaign_id).dm_id == user.id) or lc.owner_id == user.id):
        raise HTTPException(403)
    res = dict(lc.resources or {})
    entry = dict(res.get(key) or {"current": 0, "max": 0, "label": key})
    if entry.get("current", 0) < amount:
        raise HTTPException(400, f"insufficient {key} ({entry.get('current', 0)} < {amount})")
    entry["current"] = entry["current"] - amount
    res[key] = entry
    lc.resources = res
    db.add(lc)
    _log(db, gs, f"{lc.name} spends {amount} {entry.get('label', key)} ({entry['current']}/{entry.get('max', '?')} remaining)")
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/lc/{lc_id}/resource/set")
def set_resource(
    session_id: int,
    lc_id: int,
    key: str = Form(),
    label: str = Form(""),
    current: int = Form(0),
    max_value: int = Form(0),
    recharge: str = Form("long_rest"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """DM-only: define or update a class resource on an LC."""
    gs = _require_session(db, session_id, user, dm_only=True)
    lc = _require_live(db, session_id, lc_id)
    res = dict(lc.resources or {})
    if max_value <= 0 and key in res:
        # Setting max to 0: remove the resource
        del res[key]
        _log(db, gs, f"DM clears resource '{key}' on {lc.name}")
    else:
        res[key] = {
            "label": label or key,
            "current": max(0, min(current, max_value)),
            "max": max_value,
            "recharge": recharge,
        }
        _log(db, gs, f"DM sets {lc.name} {label or key} = {res[key]['current']}/{max_value} ({recharge})")
    lc.resources = res
    db.add(lc)
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/action-surge")
def action_surge(
    session_id: int,
    actor_id: int = Form(),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """Fighter Action Surge: consume one use of resources['action_surge'], reset
    gs.action_used so the actor can take another Action this turn.
    """
    gs = _require_session(db, session_id, user)
    lc = _require_live(db, session_id, actor_id)
    if not _can_act(gs, lc, user):
        raise HTTPException(403)
    res = dict(lc.resources or {})
    entry = dict(res.get("action_surge") or {"current": 1, "max": 1, "label": "Action Surge", "recharge": "short_rest"})
    if entry.get("current", 0) <= 0:
        raise HTTPException(400, "no Action Surge uses remaining")
    entry["current"] -= 1
    res["action_surge"] = entry
    lc.resources = res
    db.add(lc)
    gs.action_used = False
    lc.attacks_remaining_this_action = 0  # fresh Action seeds new attack chain
    db.add(gs)
    _log(db, gs, f"{lc.name} uses Action Surge — gains an extra Action this turn ({entry['current']}/{entry['max']} remaining)")
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/second-wind")
def second_wind(
    session_id: int,
    actor_id: int = Form(),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """Fighter Second Wind: consume one use, heal 1d10 + fighter level."""
    gs = _require_session(db, session_id, user)
    lc = _require_live(db, session_id, actor_id)
    if not _can_act(gs, lc, user):
        raise HTTPException(403)
    res = dict(lc.resources or {})
    entry = dict(res.get("second_wind") or {"current": 1, "max": 1, "label": "Second Wind", "recharge": "short_rest"})
    if entry.get("current", 0) <= 0:
        raise HTTPException(400, "no Second Wind uses remaining")
    entry["current"] -= 1
    res["second_wind"] = entry
    lc.resources = res
    rolled = dice.roll("1d10")
    healed = _heal_to(db, gs, lc, rolled.total + (lc.level or 1))
    db.add(lc)
    flags = _consume_turn_resource(db, gs, lc, "bonus_action")
    if flags:
        _log(db, gs, f"  warning: {flags[0]}")
    _log(db, gs, f"{lc.name} uses Second Wind: heals {healed} (1d10={rolled.total} + level {lc.level or 1}). HP {lc.current_hp}/{lc.max_hp}")
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/lay-on-hands")
def lay_on_hands(
    session_id: int,
    actor_id: int = Form(),
    target_id: int = Form(),
    amount: int = Form(),
    cure_poison: bool = Form(False),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """Paladin Lay on Hands: spend N points from pool to heal N HP, or 5 points
    to cure one disease/poison.
    """
    gs = _require_session(db, session_id, user)
    lc = _require_live(db, session_id, actor_id)
    target = _require_live(db, session_id, target_id)
    if not _can_act(gs, lc, user):
        raise HTTPException(403)
    res = dict(lc.resources or {})
    entry = dict(res.get("lay_on_hands") or {"current": (lc.level or 1) * 5, "max": (lc.level or 1) * 5, "label": "Lay on Hands", "recharge": "long_rest"})
    cost = 5 if cure_poison else max(1, amount)
    if entry.get("current", 0) < cost:
        raise HTTPException(400, f"insufficient Lay on Hands pool ({entry.get('current', 0)} < {cost})")
    entry["current"] -= cost
    res["lay_on_hands"] = entry
    lc.resources = res
    db.add(lc)
    if cure_poison:
        # Remove poisoned condition
        existing = list(target.conditions or [])
        new = [c for c in existing if c.get("name") != "poisoned"]
        target.conditions = new
        db.add(target)
        _log(db, gs, f"{lc.name} cures {target.name} of poison via Lay on Hands ({entry['current']}/{entry['max']} pool)")
    else:
        healed = _heal_to(db, gs, target, cost)
        _log(db, gs, f"{lc.name} heals {target.name} for {healed} via Lay on Hands ({entry['current']}/{entry['max']} pool, HP {target.current_hp}/{target.max_hp})")
    flags = _consume_turn_resource(db, gs, lc, "action")
    if flags:
        _log(db, gs, f"  warning: {flags[0]}")
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/inspire")
def toggle_inspiration(
    session_id: int,
    actor_id: int = Form(),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """DM-only: toggle inspiration on a PC. Use the spend_inspiration form param
    on attack/save/check to consume it for advantage.
    """
    gs = _require_session(db, session_id, user, dm_only=True)
    lc = _require_live(db, session_id, actor_id)
    lc.is_inspired = not lc.is_inspired
    db.add(lc)
    _log(db, gs, f"{lc.name} {'GAINS' if lc.is_inspired else 'loses'} inspiration")
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/skill-check")
def skill_check(
    session_id: int,
    actor_id: int = Form(),
    skill: str = Form(),
    dc: int = Form(10),
    advantage: bool = Form(False),
    disadvantage: bool = Form(False),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """1d20 + ability_mod + (proficiency or expertise bonus) vs DC. Reads
    skill_profs / skill_expertises from the source Character; LC stats drive ability mod.
    """
    import character_data as cd
    gs = _require_session(db, session_id, user)
    lc = _require_live(db, session_id, actor_id)
    ability = cd.SKILLS.get(skill) or cd.SKILLS.get(skill.title())
    if not ability:
        raise HTTPException(400, f"unknown skill: {skill}")
    score = getattr(lc, ability, 10)
    mod = (score - 10) // 2
    char = db.get(Character, lc.source_character_id) if lc.source_character_id else None
    pb = _proficiency_bonus(lc.level or 1)
    bonus = 0
    if char:
        if skill in (char.skill_expertises or []):
            bonus = pb * 2
        elif skill in (char.skill_profs or []):
            bonus = pb
    total_mod = mod + bonus
    roll = dice.roll_d20(total_mod, advantage=advantage, disadvantage=disadvantage)
    outcome = "PASS" if roll.total >= dc else "FAIL"
    note = " adv" if advantage and not disadvantage else (" dis" if disadvantage and not advantage else "")
    _log(db, gs, f"{lc.name} {skill} ({ability[:3]}) check{note}: {roll.total} vs DC {dc} ({outcome})")
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
    taken = _apply_damage_to(db, gs, target, amount, damage_type)
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
    healed = _heal_to(db, gs, target, amount)
    _log(db, gs, f"{target.name} heals {healed} (HP: {target.current_hp}/{target.max_hp})")
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


def _parse_csv_list(s: str) -> list:
    """Parse a comma-separated string into a clean list of stripped lowercase tokens."""
    if not s:
        return []
    return [tok.strip().lower() for tok in s.split(",") if tok.strip()]


@router.post("/sessions/{session_id}/lc/{lc_id}/edit")
async def edit_live_character(
    session_id: int,
    lc_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """DM-only: in-session edits to a LiveCharacter. Affects this session only;
    library Character is untouched. Pass list fields as comma-separated strings.
    """
    gs = _require_session(db, session_id, user, dm_only=True)
    lc = _require_live(db, session_id, lc_id)
    form = await request.form()
    _push_undo(db, gs, f"edit {lc.name}")

    int_fields = (
        "max_hp", "current_hp", "temp_hp", "armor_class", "speed_ft",
        "strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma",
        "melee_reach_ft", "attacks_per_action", "sneak_attack_dice",
        "darkvision_ft", "vision_normal_ft", "light_emission_ft",
        "exhaustion_level", "level",
    )
    for f in int_fields:
        v = form.get(f)
        if v is None or v == "":
            continue
        try:
            setattr(lc, f, int(v))
        except (TypeError, ValueError):
            continue

    csv_fields = (
        "damage_resistances", "damage_immunities", "damage_vulnerabilities",
        "saving_throw_profs", "class_features",
    )
    for f in csv_fields:
        if f in form:
            setattr(lc, f, _parse_csv_list(str(form.get(f) or "")))

    name = (form.get("name") or "").strip()
    if name:
        lc.name = name

    # Clamp current_hp to max_hp.
    if (lc.current_hp or 0) > (lc.max_hp or 0):
        lc.current_hp = lc.max_hp

    db.add(lc)
    _log(db, gs, f"DM edited {lc.name}")
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
    wall_x2: Optional[int] = Form(None),
    wall_y2: Optional[int] = Form(None),
    cover: str = Form("none"),
    spell_save_dc: int = Form(13),
    spell_attack_modifier: int = Form(5),
    spellcasting_modifier: int = Form(3),
    enforce_slots: bool = Form(False),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    gs = _require_session(db, session_id, user)
    _check_pending_reaction_or_block(db, gs)
    cast_params = {
        "spell_name": spell_name, "caster_id": caster_id, "slot_level": slot_level,
        "target_ids": target_ids, "aoe_x": aoe_x, "aoe_y": aoe_y,
        "aoe_dx": aoe_dx, "aoe_dy": aoe_dy,
        "wall_x2": wall_x2, "wall_y2": wall_y2,
        "cover": cover,
        "spell_save_dc": spell_save_dc, "spell_attack_modifier": spell_attack_modifier,
        "spellcasting_modifier": spellcasting_modifier,
        "enforce_slots": enforce_slots,
    }
    # Counterspell trigger: only for non-reaction spells of level 1+. The reactor
    # window suspends the cast; /reactions/use|skip resumes via _resume_suspended_action.
    spell_obj = spells.get_spell(spell_name, db, gs.campaign_id)
    if spell_obj and spell_obj.get("casting_time") != "reaction" and spell_obj.get("level", 0) > 0:
        caster_lc = _require_live(db, session_id, caster_id)
        eligible = _eligible_counterspell_reactors(db, gs, caster_lc, spell_obj["level"])
        if eligible:
            opened = _fire_reaction_window(
                db, gs, "spell_cast",
                {"caster_id": caster_lc.id, "caster_name": caster_lc.name,
                 "spell_name": spell_obj.get("name", spell_name),
                 "spell_level": spell_obj["level"]},
                eligible,
                {"kind": "cast", "params": cast_params, "bypass": []},
            )
            if opened:
                db.commit()
                events.publish(session_id)
                return RedirectResponse(f"/sessions/{session_id}", status_code=303)

    _do_cast(db, gs, cast_params, user, bypass=set())
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


def _do_cast(db_session: Session, gs: GameSession, params: dict, user: User, bypass: set[str]) -> None:
    spell_name = params["spell_name"]
    caster = _require_live(db_session, gs.id, int(params["caster_id"]))
    if _is_incapacitated(caster):
        cond_list = ", ".join(n for n in _all_condition_names(caster) if n in _INCAPACITATING_CONDITIONS)
        _log(db_session, gs, f"{caster.name} is incapacitated ({cond_list}) — cast denied")
        return
    _push_undo(db_session, gs, f"{caster.name} casts {spell_name}")
    spell = spells.get_spell(spell_name, db_session, gs.campaign_id)
    if not spell:
        raise HTTPException(400, f"unknown spell: {spell_name}")

    slot_level = int(params.get("slot_level", 1))
    aoe_x = params.get("aoe_x")
    aoe_y = params.get("aoe_y")
    aoe_x = int(aoe_x) if aoe_x is not None and aoe_x != "" else None
    aoe_y = int(aoe_y) if aoe_y is not None and aoe_y != "" else None

    # Self-centered AoE: caster's position is the origin (Burning Hands, Cone of Cold,
    # Thunderclap, Thunderwave, etc.). UI doesn't need a click target — we fill it here.
    if (aoe_x is None or aoe_y is None) and spell.get("area") and \
            spell.get("target_type") in ("self", "area_self"):
        aoe_x = caster.position_x if caster.position_x is not None else aoe_x
        aoe_y = caster.position_y if caster.position_y is not None else aoe_y

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
    is_scroll = bool(params.get("scroll"))
    if is_scroll:
        pass  # scrolls don't consume slots
    elif spell["level"] > 0 and enforce_slots:
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
        if ability in (lc.saving_throw_profs or []):
            mod += _proficiency_bonus(lc.level or 1)
        sv_mods = effects_mod.collect_save_modifiers(db_session, gs.id, lc.id, ability)
        # DEX save cover: half/three-quarters cover adds to DEX save vs AoE.
        # For now, a single cover param applies uniformly across targets; the
        # cover auto-detection pass refines this per-target where possible.
        cover_bonus = 0
        if ability == "dexterity":
            cover_bonus = COVER_AC_BONUS.get(str(params.get("cover", "none")).lower(), 0)
        return combat.make_save(
            lc.name, ability, mod, save_dc,
            [c["name"] for c in (lc.conditions or [])],
            extra_dice=list(sv_mods.extra_dice),
            subtract_dice=list(sv_mods.subtract_dice),
            extra_advantage=sv_mods.advantage,
            extra_disadvantage=sv_mods.disadvantage,
            bonus=sv_mods.bonus + cover_bonus,
        )

    def _hit(lc: LiveCharacter, amount: int, dmg_type: str):
        return _apply_damage_to(db_session, gs, lc, amount, dmg_type,
                                source_attacker_id=caster.id)

    applies_to: list = list(target_lcs)  # default: original targets get any applies_effects entries

    if effect_type == "save_for_half":
        save_info = spell.get("save", {})
        save_ability = save_info.get("ability", "DEX")
        on_success = save_info.get("on_success", "half_damage")
        rolled = result.damage_rolls[0].total if result.damage_rolls else 0
        dmg_type = (spell.get("damage", [{}])[0]).get("type", "force")
        _log(db_session, gs, f"  damage rolled: {rolled} {dmg_type}")
        applies_to = []  # damage-only
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
            atk_mods = effects_mod.collect_attack_modifiers(db_session, gs.id, caster.id, lc.id)
            if atk_mods.image_log:
                _log(db_session, gs, f"    {atk_mods.image_log}")
            att = combat.make_attack(
                caster.name, lc.name, spell_atk_mod + atk_mods.bonus, lc.armor_class,
                damage_dice_for_target, dmg_type,
                attacker_conds, target_conds, distance_ft=30,
                extra_attack_dice=list(atk_mods.extra_dice),
                subtract_attack_dice=list(atk_mods.subtract_dice),
                extra_advantage=atk_mods.advantage, extra_disadvantage=atk_mods.disadvantage,
                damage_bonus=atk_mods.damage_bonus,
                extra_damage_on_hit=list(atk_mods.extra_damage_dice),
                target_ac_bonus=atk_mods.target_ac_bonus,
                image_redirect_ac=atk_mods.image_ac if atk_mods.redirect_to_image else None,
            )
            label = "beam " + str(i + 1) if "beams" in spell else "attack"
            if att.image_hit:
                _consume_mirror_image(db_session, gs, lc)
                _log(db_session, gs, f"  -> {label} → {lc.name}: hits image (no damage)")
            elif att.hit:
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
                healed = _heal_to(db_session, gs, lc, heal_per_target)
                _log(db_session, gs, f"  -> {lc.name}: +{healed} HP (HP: {lc.current_hp}/{lc.max_hp})")
        applies_to = []  # healing-only

    elif effect_type in ("save_or_condition", "save_or_debuff"):
        save_info = spell.get("save", {})
        save_ability = save_info.get("ability", "WIS")
        applies_to = []
        for st in result.targets:
            lc = name_map.get(st.name)
            if not lc:
                continue
            sv = _roll_save(lc, save_ability)
            outcome = "save" if sv.success else "fail"
            tail = ""
            if not sv.success:
                applies_to.append(lc)
                tail = "; effect applies"
            _log(db_session, gs, f"  -> {lc.name}: {save_ability} save {sv.roll.total} vs DC {save_dc} ({outcome}){tail}")

    elif effect_type == "buff":
        for st in result.targets:
            lc = name_map.get(st.name)
            if lc:
                _log(db_session, gs, f"  -> {lc.name}: gains buff")
        # applies_to defaults to original target_lcs

    elif effect_type == "hp_threshold":
        # Sleep et al: targets identified by spell logic (from result.targets).
        applies_to = [name_map[st.name] for st in result.targets if st.name in name_map]
        for tgt in result.targets:
            _log(db_session, gs, f"  -> {tgt.name}: {tgt.notes}")

    else:
        # manual or anything novel: keep target_lcs for any applies_effects block.
        for tgt in result.targets:
            _log(db_session, gs, f"  -> {tgt.name}: {tgt.notes}")

    # Spawn ActiveEffect rows from the spell's applies_effects block (if any).
    _apply_spell_effects(db_session, gs, spell, caster, applies_to, aoe_x, aoe_y,
                         slot_level, save_dc, params=params)


def _apply_spell_effects(db_session: Session, gs: GameSession, spell: dict,
                         caster: LiveCharacter, target_lcs: list,
                         aoe_x, aoe_y, slot_level: int, save_dc: int = 13,
                         params: Optional[dict] = None) -> None:
    """Read spell['applies_effects'] and create ActiveEffect rows accordingly.
    Concentration spells drop the caster's prior concentration first.
    For wall-shape blocks with no points, fills from params.aoe_x/y + wall_x2/y2.
    """
    from db import ActiveEffect
    blocks = spell.get("applies_effects") or []
    if not blocks:
        return
    params = params or {}
    for blk in blocks:
        scope = blk.get("target_scope", "each_target")
        is_conc = bool(blk.get("is_concentration"))
        if is_conc:
            dropped = effects_mod.break_concentration(db_session, gs.id, caster.id)
            if dropped:
                _log(db_session, gs, f"  concentration broken: {dropped}")

        scope_targets: list = []
        if scope == "self":
            scope_targets = [caster]
        elif scope == "each_target":
            scope_targets = list(target_lcs or [])
        elif scope == "area":
            scope_targets = [None]  # area effect (no target_live_id)
        else:
            scope_targets = list(target_lcs or [])

        # Inject the cast's save DC into save_each_turn if not already specified.
        sxt = dict(blk.get("save_each_turn") or {})
        if sxt and "dc" not in sxt:
            sxt["dc"] = save_dc
        # For aura_damage handlers, also inject save_dc into payload if missing.
        blk_payload = dict(blk.get("payload") or {})
        if blk.get("handler_key") == "aura_damage" and blk_payload.get("save_ability") and "save_dc" not in blk_payload:
            blk_payload["save_dc"] = save_dc

        # Compose the area dict. Walls without baked-in points fill from cast params.
        area_dict = dict(blk.get("area") or {})
        if area_dict.get("shape") == "wall" and not area_dict.get("points"):
            wx2 = params.get("wall_x2"); wy2 = params.get("wall_y2")
            if aoe_x is not None and aoe_y is not None and wx2 is not None and wy2 is not None:
                area_dict["points"] = [[int(aoe_x), int(aoe_y)], [int(wx2), int(wy2)]]
        if not area_dict and aoe_x is not None:
            area_dict = {"x": aoe_x, "y": aoe_y}

        for t in scope_targets:
            eff = ActiveEffect(
                session_id=gs.id,
                target_live_id=(t.id if t is not None else None),
                caster_live_id=caster.id,
                spell_key=spell.get("key", ""),
                name=blk.get("name", spell.get("name", "")),
                description=blk.get("description", ""),
                handler_key=blk.get("handler_key", ""),
                is_concentration=is_conc,
                duration_rounds=blk.get("duration_rounds"),
                duration_basis=blk.get("duration_basis", "caster_end_of_turn"),
                save_each_turn=sxt,
                area=area_dict,
                payload=blk_payload,
                started_round=gs.round_number or 0,
                started_turn_index=gs.current_turn_index or 0,
            )
            db_session.add(eff)
            db_session.flush()  # so eff.id is available to the on_apply hook
            handler = effects_mod.get_handler(eff.handler_key)
            if handler:
                ctx = effects_mod.EffectContext(db=db_session, session_id=gs.id)
                handler.on_apply(ctx, eff)
            who = t.name if t is not None else "(area)"
            _log(db_session, gs, f"  effect '{eff.name}' applied to {who}{' [conc]' if is_conc else ''}")


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


@router.post("/sessions/{session_id}/spawn-object")
def spawn_object(
    session_id: int,
    object_key: str = Form(),
    custom_name: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """DM-only: spawn an inanimate object as a targetable LiveCharacter.
    Marked is_active=False so it doesn't roll initiative; still attackable for HP/AC.
    """
    import json as _json
    from pathlib import Path as _Path
    gs = _require_session(db, session_id, user, dm_only=True)
    catalog_path = _Path(__file__).parent / "data" / "objects.json"
    try:
        catalog = _json.loads(catalog_path.read_text())
    except FileNotFoundError:
        raise HTTPException(500, "objects catalog missing")
    obj = catalog.get(object_key)
    if not obj:
        raise HTTPException(400, f"unknown object: {object_key}")
    map_obj = db.get(Map, gs.active_map_id) if gs.active_map_id else None
    cols = map_obj.grid_cols if map_obj else 60
    enemy_count = len(db.exec(select(LiveCharacter).where(
        LiveCharacter.session_id == session_id, LiveCharacter.is_enemy == True
    )).all())
    name = (custom_name or obj.get("name", object_key)).strip()
    px = max(0, cols - 5 - enemy_count)
    lc = LiveCharacter(
        session_id=session_id,
        owner_id=user.id,
        name=name,
        max_hp=obj["max_hp"],
        current_hp=obj["max_hp"],
        armor_class=obj["armor_class"],
        speed_ft=0,
        strength=10, dexterity=0, constitution=10,
        intelligence=0, wisdom=0, charisma=0,
        is_enemy=True,
        is_active=False,  # excluded from initiative
        position_x=px,
        position_y=4,
        damage_immunities=list(obj.get("damage_immunities", []) or []),
        damage_vulnerabilities=list(obj.get("damage_vulnerabilities", []) or []),
        damage_resistances=list(obj.get("damage_resistances", []) or []),
    )
    db.add(lc)
    _log(db, gs, f"DM spawns {name} (AC {obj['armor_class']}, HP {obj['max_hp']})")
    if obj.get("notes"):
        _log(db, gs, f"  {obj['notes']}")
    db.commit()
    events.publish(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/spawn-monster")
def spawn_monster(
    session_id: int,
    monster_key: str = Form(),
    custom_name: str = Form(""),
    count: int = Form(1),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """DM-only: spawn one or more enemies from the monster catalog. The template's
    full stat block is copied to each LiveCharacter; resistances/immunities, reach,
    multi-attack, etc. all carry over.
    """
    from game.monsters import get_monster
    gs = _require_session(db, session_id, user, dm_only=True)
    monster = get_monster(monster_key)
    if not monster:
        raise HTTPException(400, f"unknown monster: {monster_key}")
    map_obj = db.get(Map, gs.active_map_id) if gs.active_map_id else None
    cols = map_obj.grid_cols if map_obj else 60
    base_name = (custom_name or monster.get("name", monster_key)).strip()
    enemy_count = len(db.exec(select(LiveCharacter).where(
        LiveCharacter.session_id == session_id, LiveCharacter.is_enemy == True
    )).all())
    _push_undo(db, gs, f"spawn {count}x {base_name}")
    spawned = 0
    for i in range(max(1, min(count, 12))):
        suffix = f" {i + 1}" if count > 1 else ""
        lc_name = f"{base_name}{suffix}"
        px = max(0, cols - 3 - enemy_count - spawned)
        lc = LiveCharacter(
            session_id=session_id,
            owner_id=user.id,
            name=lc_name,
            max_hp=monster["max_hp"],
            current_hp=monster["max_hp"],
            armor_class=monster["armor_class"],
            speed_ft=monster.get("speed_ft", 30),
            strength=monster.get("strength", 10),
            dexterity=monster.get("dexterity", 10),
            constitution=monster.get("constitution", 10),
            intelligence=monster.get("intelligence", 10),
            wisdom=monster.get("wisdom", 10),
            charisma=monster.get("charisma", 10),
            is_enemy=True,
            position_x=px,
            position_y=2,
            saving_throw_profs=list(monster.get("saving_throw_profs", []) or []),
            damage_resistances=list(monster.get("damage_resistances", []) or []),
            damage_immunities=list(monster.get("damage_immunities", []) or []),
            damage_vulnerabilities=list(monster.get("damage_vulnerabilities", []) or []),
            class_features=list(monster.get("class_features", []) or []),
            melee_reach_ft=monster.get("melee_reach_ft", 5),
            attacks_per_action=monster.get("attacks_per_action", 1),
            darkvision_ft=monster.get("darkvision_ft", 0),
            challenge_rating=str(monster.get("challenge_rating", "")),
        )
        db.add(lc)
        spawned += 1
    cr = monster.get("challenge_rating", "?")
    _log(db, gs, f"Spawned {spawned}x {base_name} (CR {cr}, HP {monster['max_hp']}, AC {monster['armor_class']})")
    if monster.get("notes"):
        _log(db, gs, f"  notes: {monster['notes']}")
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
        "pending_reaction": gs.pending_reaction or {},
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

    # Area effects: extract difficult-terrain zones, block walls, and damage walls.
    # - difficult_terrain: feeds extra zones (Spike Growth, Web)
    # - shape=wall + blocks_movement: feeds extra walls (Wall of Force, Wall of Stone)
    # - shape=wall + handler: damage fires when a step crosses the segment (Wall of Fire)
    area_effects = effects_mod.list_area_effects(db_session, gs.id)
    extra_zones = []
    extra_walls = []
    for eff in area_effects:
        if not eff.area:
            continue
        if eff.area.get("shape") == "wall":
            pts = eff.area.get("points") or []
            if len(pts) >= 2 and (eff.payload or {}).get("blocks_movement"):
                extra_walls.append({"x1": pts[0][0], "y1": pts[0][1],
                                    "x2": pts[1][0], "y2": pts[1][1]})
            continue
        if (eff.payload or {}).get("difficult_terrain"):
            zone = dict(eff.area)
            zone.setdefault("type", "difficult")
            zone["type"] = "difficult"
            if "radius_ft" in zone and "r" not in zone:
                zone["r"] = zone["radius_ft"] / fps
            extra_zones.append(zone)
    all_zones = (m.zones or []) + extra_zones
    all_walls = (m.walls or []) + extra_walls

    result = movement.validate_path(
        (lc.position_x, lc.position_y), parsed,
        all_walls, all_zones, fps, grid_type,
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

    # Step-by-step iteration: detect Opportunity Attacks at each step before
    # applying it; collect movement-step damage (Spike Growth, walls) per step.
    # If an OA fires, commit the partial walk (mover at last completed cell,
    # accumulated movement & damage), open a reaction window, and return without
    # finishing the rest of the path. Resume picks up from the suspended cell.
    step_damage: list = []  # [(amount, type, source_caster_id)]
    ctx = effects_mod.EffectContext(db=db_session, session_id=gs.id)
    _push_undo(db_session, gs, f"walk {lc.name}")

    def _commit_partial(steps_committed: int) -> None:
        if steps_committed <= 0:
            # Mover stays put; no movement_used_ft, no fog refresh.
            return
        last = result["steps"][steps_committed - 1]
        lc.position_x = last["to"][0]
        lc.position_y = last["to"][1]
        if enforce:
            gs.movement_used_ft = (gs.movement_used_ft or 0) + sum(s["cost"] for s in result["steps"][:steps_committed])
        db_session.add(lc)
        if not lc.is_enemy:
            _refresh_explored_fog(db_session, gs)
        _log(db_session, gs, f"{lc.name} pauses at ({last['to'][0]}, {last['to'][1]})")
        for amount, dmg_type, _src in step_damage:
            taken = _apply_damage_to(db_session, gs, lc, amount, dmg_type,
                                     source_attacker_id=_src)
            _log(db_session, gs, f"  {lc.name} takes {taken} {dmg_type} from movement effect (HP: {lc.current_hp}/{lc.max_hp})")

    for step_idx, step in enumerate(result["steps"]):
        prev_xy = (step["from"][0], step["from"][1])
        new_xy = (step["to"][0], step["to"][1])

        # Aura entry trigger (Spirit Guardians etc.): fires when crossing into a radius.
        _aura_check_step(db_session, gs, lc, prev_xy, new_xy)
        if lc.is_dead or (lc.current_hp or 0) <= 0:
            # Aura damage incapacitated the mover; abort the rest of the walk.
            _commit_partial(step_idx)
            return {**result, "committed": False, "reason": "incapacitated_in_aura"}

        # Opportunity Attack detection: who's losing reach this step?
        oa_eligible = _eligible_oa_reactors(db_session, gs, lc, prev_xy, new_xy)
        if oa_eligible:
            remaining = [list(s["to"]) for s in result["steps"][step_idx:]]
            opened = _fire_reaction_window(
                db_session, gs, "movement_oa",
                {"mover_id": lc.id, "mover_name": lc.name,
                 "from_xy": list(prev_xy), "to_xy": list(new_xy)},
                oa_eligible,
                {"kind": "walk", "actor_id": lc.id, "remaining_path": remaining},
            )
            if opened:
                _commit_partial(step_idx)
                return {**result, "committed": False,
                        "suspended_for_reaction": "movement_oa",
                        "movement_used_ft": gs.movement_used_ft if enforce else None}

        # Movement-step damage check (Spike Growth zones, Wall of Fire crossings).
        sc_from = movement._cell_center_xy(prev_xy[0], prev_xy[1], grid_type)
        sc_to = movement._cell_center_xy(new_xy[0], new_xy[1], grid_type)
        for eff in area_effects:
            if not eff.area:
                continue
            handler = effects_mod.get_handler(eff.handler_key)
            if not handler:
                continue
            shape = eff.area.get("shape", "")
            triggered = False
            if shape == "wall":
                pts = eff.area.get("points") or []
                if len(pts) >= 2 and movement._segments_cross(
                        sc_from, sc_to, (pts[0][0], pts[0][1]), (pts[1][0], pts[1][1])):
                    triggered = True
            else:
                zone = dict(eff.area)
                if "radius_ft" in zone and "r" not in zone:
                    zone["r"] = zone["radius_ft"] / fps
                if movement.cell_in_zone(zone, new_xy[0], new_xy[1], grid_type):
                    triggered = True
            if not triggered:
                continue
            ms = effects_mod.MovementStep(
                mover_id=lc.id,
                from_xy=tuple(step["from"]),
                to_xy=tuple(step["to"]),
                cost_ft=step["cost"],
            )
            handler.on_movement_step(ctx, eff, ms)
            step_damage.extend(ms.extra_damage)

    # All steps completed without suspension: commit final state.
    final_x, final_y = result["final"]
    lc.position_x = final_x
    lc.position_y = final_y
    if enforce:
        gs.movement_used_ft = (gs.movement_used_ft or 0) + result["total_cost_ft"]
    db_session.add(lc)
    if not lc.is_enemy:
        _refresh_explored_fog(db_session, gs)
    _log(db_session, gs, f"{lc.name} moves {result['total_cost_ft']}ft to ({final_x}, {final_y})")

    total_step_damage = 0
    for amount, dmg_type, _src in step_damage:
        taken = _apply_damage_to(db_session, gs, lc, amount, dmg_type)
        total_step_damage += taken
        _log(db_session, gs, f"  {lc.name} takes {taken} {dmg_type} from movement effect (HP: {lc.current_hp}/{lc.max_hp})")

    return {**result, "committed": True,
            "movement_used_ft": gs.movement_used_ft if enforce else None,
            "step_damage_taken": total_step_damage}


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
        _process_save_each_turn(db, gs, lc)
        for basis in ("caster_end_of_turn", "target_end_of_turn"):
            for nm in effects_mod.tick_durations(db, gs.id, basis, lc.id):
                _log(db, gs, f"  effect expired: {nm}")
        gs.current_turn_index = (gs.current_turn_index + 1) % len(gs.initiative_order)
        if gs.current_turn_index == 0:
            gs.round_number += 1
            _log(db, gs, f"-- Round {gs.round_number} --")
        _reset_turn_state(gs)
        nxt = gs.initiative_order[gs.current_turn_index].get("name")
        _log(db, gs, f"{lc.name} ends turn -> {nxt}")
        starting_lc = db.exec(
            select(LiveCharacter).where(
                LiveCharacter.session_id == gs.id, LiveCharacter.name == nxt
            )
        ).first()
        if starting_lc:
            starting_lc.reaction_used = False
            starting_lc.attacks_remaining_this_action = 0
            starting_lc.sneak_attack_used_this_turn = False
            db.add(starting_lc)
            _roll_death_save(db, gs, starting_lc)
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


@router.post("/sessions/{session_id}/reactions/use")
async def react_use(
    session_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """Reactor uses a reaction. body = {reactor_id, spell_name, slot_level}."""
    gs = _require_session(db, session_id, user)
    body = await request.json()
    reactor_id = int(body.get("reactor_id"))
    reactor = _require_live(db, session_id, reactor_id)
    if not _can_act(gs, reactor, user):
        raise HTTPException(403, "not your character")
    result = _resolve_reaction(db, gs, user, reactor, "use", body)
    db.commit()
    events.publish(session_id)
    return result


@router.post("/sessions/{session_id}/reactions/skip")
async def react_skip(
    session_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """Reactor skips. body = {reactor_id}."""
    gs = _require_session(db, session_id, user)
    body = await request.json()
    reactor_id = int(body.get("reactor_id"))
    reactor = _require_live(db, session_id, reactor_id)
    if not _can_act(gs, reactor, user):
        raise HTTPException(403, "not your character")
    result = _resolve_reaction(db, gs, user, reactor, "skip", {})
    db.commit()
    events.publish(session_id)
    return result


@router.post("/sessions/{session_id}/reactions/timeout")
async def react_timeout(
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """DM force-resolves an expired reaction window (auto-skip remaining reactors)."""
    gs = _require_session(db, session_id, user, dm_only=True)
    pr = dict(gs.pending_reaction or {})
    if not pr:
        raise HTTPException(400, "no pending window")
    for rid, v in list(pr.get("responses", {}).items()):
        if v is None:
            pr["responses"][rid] = "skip"
    gs.pending_reaction = pr
    db.add(gs)
    result = _resume_suspended_action(db, gs, user)
    db.commit()
    events.publish(session_id)
    return result


@router.post("/sessions/{session_id}/effects/add")
async def add_custom_effect(
    session_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """DM-only: apply a freeform (or handler-keyed) ActiveEffect to a target.
    body = {target_id, name, description?, duration_rounds?, is_concentration?, handler_key?}
    """
    from db import ActiveEffect
    body = await request.json()
    gs = _require_session(db, session_id, user, dm_only=True)
    target_id = body.get("target_id")
    target_lc = None
    if target_id is not None:
        target_lc = db.get(LiveCharacter, int(target_id))
        if not target_lc or target_lc.session_id != session_id:
            raise HTTPException(400, "invalid target")
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    eff = ActiveEffect(
        session_id=session_id,
        target_live_id=target_lc.id if target_lc else None,
        caster_live_id=body.get("caster_live_id"),
        spell_key="",
        name=name,
        description=body.get("description") or "",
        handler_key=body.get("handler_key") or "",
        is_concentration=bool(body.get("is_concentration")),
        duration_rounds=body.get("duration_rounds"),
        duration_basis=body.get("duration_basis") or "caster_end_of_turn",
        area=body.get("area") or {},
        payload=body.get("payload") or {},
        started_round=gs.round_number or 0,
        started_turn_index=gs.current_turn_index or 0,
    )
    db.add(eff)
    db.flush()
    handler = effects_mod.get_handler(eff.handler_key)
    if handler:
        ctx = effects_mod.EffectContext(db=db, session_id=gs.id)
        handler.on_apply(ctx, eff)
    who = target_lc.name if target_lc else (f"wall ({eff.area['points']})" if eff.area.get('shape') == 'wall' else "(area)")
    _log(db, gs, f"DM applies '{name}' to {who}{' [conc]' if eff.is_concentration else ''}")
    db.commit()
    events.publish(session_id)
    return {"ok": True, "id": eff.id}


@router.post("/sessions/{session_id}/effects/{effect_id}/remove")
async def remove_custom_effect(
    session_id: int,
    effect_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """DM-only: delete an active effect."""
    from db import ActiveEffect
    gs = _require_session(db, session_id, user, dm_only=True)
    eff = db.get(ActiveEffect, effect_id)
    if not eff or eff.session_id != session_id:
        raise HTTPException(404, "effect not found")
    name = eff.name
    ctx = effects_mod.EffectContext(db=db, session_id=session_id)
    effects_mod.remove_effect(db, eff, ctx)
    _log(db, gs, f"DM removes effect '{name}'")
    db.commit()
    events.publish(session_id)
    return {"ok": True}


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
    _check_pending_reaction_or_block(db, gs)
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
    _check_pending_reaction_or_block(db, gs)
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
    _check_pending_reaction_or_block(db, gs)
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

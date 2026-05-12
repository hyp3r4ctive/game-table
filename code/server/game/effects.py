"""Active effect framework: handler registry + hook orchestration.

An ActiveEffect row is the *state* (who/what/how-long). Per-effect
*behavior* lives here as a handler class registered under handler_key.

Hook points (called from sessions.py / combat.py once integration lands):
  on_apply              effect first lands
  on_remove             expired or dispelled
  on_target_start_turn  start of the target's turn
  on_target_end_turn    end of the target's turn
  on_caster_end_turn    end of the caster's turn (most spell durations tick here)
  on_attack_roll        modify advantage / disadvantage / bonus / extra dice
  on_save_roll          same, for saving throws
  on_damage_taken       react to damage (e.g. concentration check)
  on_movement_step      damage/save when entering a square (Spike Growth, etc.)

Freeform effect = handler_key blank or unregistered. Engine ticks its
duration and shows it in the UI but skips behavior hooks. This is how
DMs apply "shimmering and weird, end of next round" without writing code.
"""

from dataclasses import dataclass, field
from typing import Optional, Any


HANDLERS: dict[str, type] = {}


def register(key: str):
    def deco(cls):
        HANDLERS[key] = cls
        return cls
    return deco


@dataclass
class EffectContext:
    """Shared scratch passed through every hook in one resolution. The caller
    commits the db session at the end; handlers append to `log` for the event log.
    """
    db: Any
    session_id: Optional[int] = None
    log: list[str] = field(default_factory=list)


@dataclass
class AttackRoll:
    attacker_id: int
    target_id: int
    advantage: bool = False
    disadvantage: bool = False
    bonus: int = 0  # to-hit bonus
    extra_dice: list[str] = field(default_factory=list)  # added to the d20 roll (Bless: 1d4)
    subtract_dice: list[str] = field(default_factory=list)  # subtracted from the d20 roll (Bane: 1d4)
    damage_bonus: int = 0
    extra_damage_dice: list = field(default_factory=list)  # [(dice_str, damage_type), ...] applied on hit
    target_ac_bonus: int = 0  # added to target AC for hit threshold (Haste +2, Shield +5, etc.)
    redirect_to_image: bool = False  # Mirror Image: attack targets an illusory duplicate
    image_ac: Optional[int] = None  # AC of the redirected-to image (replaces target AC)
    image_log: str = ""  # human-readable deflection note for the log


@dataclass
class SaveRoll:
    saver_id: int
    ability: str
    advantage: bool = False
    disadvantage: bool = False
    bonus: int = 0
    extra_dice: list[str] = field(default_factory=list)
    subtract_dice: list[str] = field(default_factory=list)


@dataclass
class DamageEvent:
    target_id: int
    amount: int
    type: str
    source_caster_id: Optional[int] = None
    triggered_concentration_check: bool = False


@dataclass
class MovementStep:
    mover_id: int
    from_xy: tuple[int, int]
    to_xy: tuple[int, int]
    cost_ft: int
    blocked: bool = False
    extra_damage: list = field(default_factory=list)


class EffectHandler:
    """Base class. Override only the hooks you care about; defaults are no-ops."""

    def on_apply(self, ctx: EffectContext, effect): pass
    def on_remove(self, ctx: EffectContext, effect): pass
    def on_target_start_turn(self, ctx: EffectContext, effect): pass
    def on_target_end_turn(self, ctx: EffectContext, effect): pass
    def on_caster_end_turn(self, ctx: EffectContext, effect): pass
    def on_attack_roll(self, ctx: EffectContext, effect, roll: AttackRoll): pass
    def on_save_roll(self, ctx: EffectContext, effect, roll: SaveRoll): pass
    def on_damage_taken(self, ctx: EffectContext, effect, dmg: DamageEvent): pass
    def on_movement_step(self, ctx: EffectContext, effect, step: MovementStep): pass


def get_handler(handler_key: str) -> Optional[EffectHandler]:
    cls = HANDLERS.get(handler_key)
    return cls() if cls else None


def is_freeform(effect) -> bool:
    return not effect.handler_key or effect.handler_key not in HANDLERS


# ---- queries ----

def list_effects_on(db, session_id: int, target_live_id: int) -> list:
    from db import ActiveEffect
    from sqlmodel import select
    return list(db.exec(
        select(ActiveEffect).where(
            ActiveEffect.session_id == session_id,
            ActiveEffect.target_live_id == target_live_id,
        )
    ).all())


def list_area_effects(db, session_id: int) -> list:
    from db import ActiveEffect
    from sqlmodel import select
    return list(db.exec(
        select(ActiveEffect).where(
            ActiveEffect.session_id == session_id,
            ActiveEffect.target_live_id.is_(None),
        )
    ).all())


def caster_concentration(db, session_id: int, caster_live_id: int):
    from db import ActiveEffect
    from sqlmodel import select
    return db.exec(
        select(ActiveEffect).where(
            ActiveEffect.session_id == session_id,
            ActiveEffect.caster_live_id == caster_live_id,
            ActiveEffect.is_concentration == True,
        )
    ).first()


# ---- lifecycle ----

def remove_effect(db, effect, ctx: Optional[EffectContext] = None) -> None:
    if ctx is None:
        ctx = EffectContext(db=db, session_id=effect.session_id)
    h = get_handler(effect.handler_key)
    if h:
        h.on_remove(ctx, effect)
    db.delete(effect)


def break_concentration(db, session_id: int, caster_live_id: int, ctx: Optional[EffectContext] = None) -> Optional[str]:
    """Drop the caster's concentrated effect, if any. Returns the dropped name for logging."""
    eff = caster_concentration(db, session_id, caster_live_id)
    if not eff:
        return None
    name = eff.name
    remove_effect(db, eff, ctx)
    return name


def tick_durations(db, session_id: int, basis: str, current_actor_live_id: Optional[int] = None) -> list[str]:
    """Decrement duration_rounds on effects whose tick basis fires now; remove
    those that hit zero. `basis` examples: "caster_end_of_turn", "target_end_of_turn".

    Returns a list of names of effects that expired this tick (for the event log).
    """
    from db import ActiveEffect
    from sqlmodel import select
    q = select(ActiveEffect).where(
        ActiveEffect.session_id == session_id,
        ActiveEffect.duration_basis == basis,
        ActiveEffect.duration_rounds.is_not(None),
    )
    if basis == "caster_end_of_turn" and current_actor_live_id is not None:
        q = q.where(ActiveEffect.caster_live_id == current_actor_live_id)
    if basis == "target_end_of_turn" and current_actor_live_id is not None:
        q = q.where(ActiveEffect.target_live_id == current_actor_live_id)
    expired: list[str] = []
    ctx = EffectContext(db=db, session_id=session_id)
    for eff in db.exec(q).all():
        eff.duration_rounds = (eff.duration_rounds or 0) - 1
        if eff.duration_rounds <= 0:
            expired.append(eff.name)
            remove_effect(db, eff, ctx)
        else:
            db.add(eff)
    return expired


# ---- aggregation: collect bonuses/advantages from all relevant effects ----

def collect_attack_modifiers(db, session_id: int, attacker_id: int, target_id: int) -> AttackRoll:
    """Run on_attack_roll across every effect on attacker AND target, returning the
    accumulated AttackRoll. Pure read pass — handlers should not mutate db state here.
    """
    roll = AttackRoll(attacker_id=attacker_id, target_id=target_id)
    ctx = EffectContext(db=db, session_id=session_id)
    for eff in list_effects_on(db, session_id, attacker_id) + list_effects_on(db, session_id, target_id):
        h = get_handler(eff.handler_key)
        if h:
            h.on_attack_roll(ctx, eff, roll)
    return roll


def collect_save_modifiers(db, session_id: int, saver_id: int, ability: str) -> SaveRoll:
    roll = SaveRoll(saver_id=saver_id, ability=ability)
    ctx = EffectContext(db=db, session_id=session_id)
    for eff in list_effects_on(db, session_id, saver_id):
        h = get_handler(eff.handler_key)
        if h:
            h.on_save_roll(ctx, eff, roll)
    return roll

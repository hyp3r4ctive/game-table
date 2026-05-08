"""Concrete EffectHandler subclasses for spell mechanics.

Importing this module registers the handlers with `effects.HANDLERS` via the
@register decorator. Import once at app startup (main.py).

Naming convention: handler_key matches the spell's normalized key
(e.g. "bless" for the Bless spell). Spells reference these keys in their
applies_effects block in data/spells.json or the Spell DB row.
"""

from . import effects


@effects.register("bless")
class BlessHandler(effects.EffectHandler):
    """+1d4 to attack rolls and saving throws of the blessed creature."""

    def on_attack_roll(self, ctx, effect, roll: effects.AttackRoll):
        if roll.attacker_id == effect.target_live_id:
            roll.extra_dice.append("1d4")

    def on_save_roll(self, ctx, effect, roll: effects.SaveRoll):
        if roll.saver_id == effect.target_live_id:
            roll.extra_dice.append("1d4")


@effects.register("hunters_mark")
class HuntersMarkHandler(effects.EffectHandler):
    """Caster of the mark deals +1d6 damage when attacking the marked target.

    The ActiveEffect row sits on the marked creature (target_live_id), with
    caster_live_id pointing at the mark caster.
    """

    def on_attack_roll(self, ctx, effect, roll: effects.AttackRoll):
        if roll.attacker_id == effect.caster_live_id and roll.target_id == effect.target_live_id:
            roll.extra_damage_dice.append(("1d6", "force"))


@effects.register("haste")
class HasteHandler(effects.EffectHandler):
    """+2 AC, advantage on Dex saves, doubled speed (read by movement budget).

    Speed multiplier is applied via the effect's payload (speed_multiplier=2.0)
    which sessions._movement_budget_ft already reads. The ActiveEffect sits on
    the hasted creature (target_live_id).
    """

    def on_attack_roll(self, ctx, effect, roll: effects.AttackRoll):
        if roll.target_id == effect.target_live_id:
            roll.target_ac_bonus += 2

    def on_save_roll(self, ctx, effect, roll: effects.SaveRoll):
        if roll.saver_id == effect.target_live_id and roll.ability == "dexterity":
            roll.advantage = True

    def on_remove(self, ctx, effect):
        # Lethargy: noted-only effect on the same target for one round; DM/players
        # see it on the sheet. Mechanical "can't act/move" enforcement is a TODO.
        from db import ActiveEffect
        from datetime import datetime  # noqa: F401  (kept for parity if needed)
        if effect.target_live_id is None:
            return
        lethargy = ActiveEffect(
            session_id=effect.session_id,
            target_live_id=effect.target_live_id,
            caster_live_id=effect.caster_live_id,
            spell_key="haste_lethargy",
            name="Lethargic (Haste end)",
            description="Cannot move or take actions until end of next turn.",
            handler_key="",
            is_concentration=False,
            duration_rounds=1,
            duration_basis="target_end_of_turn",
            payload={},
        )
        ctx.db.add(lethargy)


@effects.register("bane")
class BaneHandler(effects.EffectHandler):
    """Inverse of Bless: -1d4 to attack rolls and saving throws."""

    def on_attack_roll(self, ctx, effect, roll: effects.AttackRoll):
        if roll.attacker_id == effect.target_live_id:
            roll.subtract_dice.append("1d4")

    def on_save_roll(self, ctx, effect, roll: effects.SaveRoll):
        if roll.saver_id == effect.target_live_id:
            roll.subtract_dice.append("1d4")


@effects.register("ac_bonus")
class AcBonusHandler(effects.EffectHandler):
    """Adds payload.ac_bonus to AC when this creature is the target of attacks."""

    def on_attack_roll(self, ctx, effect, roll: effects.AttackRoll):
        if roll.target_id == effect.target_live_id:
            roll.target_ac_bonus += int((effect.payload or {}).get("ac_bonus", 0))


@effects.register("apply_condition")
class ApplyConditionHandler(effects.EffectHandler):
    """Bridge handler: toggles a 5e condition on lc.conditions for the duration.

    Reads payload.condition_name (e.g. "paralyzed") and adds/removes a tagged
    entry on the live character's conditions list. Combat code reads conditions
    via target.conditions[*].name, so this keeps existing condition-driven
    advantage/auto-fail logic working alongside the new effect framework.
    """

    def on_apply(self, ctx, effect):
        from db import LiveCharacter
        if effect.target_live_id is None:
            return
        lc = ctx.db.get(LiveCharacter, effect.target_live_id)
        if not lc:
            return
        cond_name = (effect.payload or {}).get("condition_name")
        if not cond_name:
            return
        existing = list(lc.conditions or [])
        if any(c.get("source_effect_id") == effect.id for c in existing):
            return
        existing.append({
            "name": cond_name,
            "source_effect_id": effect.id,
            "duration_rounds": effect.duration_rounds,
        })
        lc.conditions = existing
        ctx.db.add(lc)

    def on_remove(self, ctx, effect):
        from db import LiveCharacter
        if effect.target_live_id is None:
            return
        lc = ctx.db.get(LiveCharacter, effect.target_live_id)
        if not lc:
            return
        existing = list(lc.conditions or [])
        new = [c for c in existing if c.get("source_effect_id") != effect.id]
        if len(new) != len(existing):
            lc.conditions = new
            ctx.db.add(lc)


@effects.register("mark_damage")
class MarkDamageHandler(effects.EffectHandler):
    """Caster's attacks against the marked target deal extra damage.
    payload: {"dice": "1d6", "type": "necrotic"}.
    Used by Hex (necrotic), Eldritch Smite, etc.
    """

    def on_attack_roll(self, ctx, effect, roll: effects.AttackRoll):
        if roll.attacker_id == effect.caster_live_id and roll.target_id == effect.target_live_id:
            payload = effect.payload or {}
            roll.extra_damage_dice.append((payload.get("dice", "1d6"), payload.get("type", "force")))


@effects.register("attacker_advantage")
class AttackerAdvantageHandler(effects.EffectHandler):
    """Attacks against this creature have advantage (Faerie Fire, prone melee, etc.)."""

    def on_attack_roll(self, ctx, effect, roll: effects.AttackRoll):
        if roll.target_id == effect.target_live_id:
            roll.advantage = True


@effects.register("attacker_disadvantage")
class AttackerDisadvantageHandler(effects.EffectHandler):
    """Attacks against this creature have disadvantage (Blur, Greater Invisibility)."""

    def on_attack_roll(self, ctx, effect, roll: effects.AttackRoll):
        if roll.target_id == effect.target_live_id:
            roll.disadvantage = True


@effects.register("attacker_advantage_on_attacks")
class AttackerHasAdvantageHandler(effects.EffectHandler):
    """This creature has advantage on its OWN attack rolls (Greater Invisibility caster, etc.)."""

    def on_attack_roll(self, ctx, effect, roll: effects.AttackRoll):
        if roll.attacker_id == effect.target_live_id:
            roll.advantage = True


@effects.register("mirror_image")
class MirrorImageHandler(effects.EffectHandler):
    """Three illusory duplicates deflect attacks. Per RAW: roll a d20 each time the
    protected creature is attacked. With 3 images, 6+ deflects; 2 images = 8+;
    1 image = 11+. Image AC = 10 + caster DEX mod (set on apply). A hit that
    landed on an image consumes the image; a miss vs image AC is a complete miss.
    The deflection roll happens here in the modifier-collection phase; sessions
    consumes the image after make_attack returns image_hit=True.
    """

    DEFLECT_THRESHOLD = {3: 6, 2: 8, 1: 11}

    def on_apply(self, ctx, effect):
        from db import LiveCharacter
        payload = dict(effect.payload or {})
        if "images" not in payload:
            payload["images"] = 3
        if "image_ac" not in payload and effect.caster_live_id is not None:
            caster = ctx.db.get(LiveCharacter, effect.caster_live_id)
            if caster:
                dex_mod = ((caster.dexterity or 10) - 10) // 2
                payload["image_ac"] = 10 + dex_mod
        effect.payload = payload
        ctx.db.add(effect)

    def on_attack_roll(self, ctx, effect, roll: effects.AttackRoll):
        if roll.target_id != effect.target_live_id:
            return
        from . import dice
        payload = effect.payload or {}
        images = int(payload.get("images", 0))
        if images <= 0:
            return
        threshold = self.DEFLECT_THRESHOLD.get(images, 11)
        d20 = dice.roll("1d20")
        if d20.total >= threshold:
            roll.redirect_to_image = True
            roll.image_ac = int(payload.get("image_ac", 13))
            roll.image_log = f"image deflection: d20={d20.total} ≥ {threshold} (images: {images})"
        else:
            roll.image_log = f"image deflection: d20={d20.total} < {threshold} — attack stays on caster (images: {images})"


@effects.register("hp_buff")
class HpBuffHandler(effects.EffectHandler):
    """Adds payload.amount to max HP and current HP on apply; reverts on remove.

    Aid: +5 to max & current. The revert subtracts the same amount, clamped to
    prevent negative HP/max. Doesn't track exact intervening healing/damage —
    if a creature took damage past the buff and was healed back, the revert may
    drop them below 0. Standard 5e Aid behavior is to lose the buffed pool when
    the spell ends, so this matches RAW.
    """

    def on_apply(self, ctx, effect):
        from db import LiveCharacter
        if effect.target_live_id is None:
            return
        lc = ctx.db.get(LiveCharacter, effect.target_live_id)
        if not lc:
            return
        amount = int((effect.payload or {}).get("amount", 0))
        lc.max_hp = (lc.max_hp or 0) + amount
        lc.current_hp = (lc.current_hp or 0) + amount
        ctx.db.add(lc)

    def on_remove(self, ctx, effect):
        from db import LiveCharacter
        if effect.target_live_id is None:
            return
        lc = ctx.db.get(LiveCharacter, effect.target_live_id)
        if not lc:
            return
        amount = int((effect.payload or {}).get("amount", 0))
        lc.max_hp = max(1, (lc.max_hp or 0) - amount)
        lc.current_hp = max(0, min(lc.current_hp or 0, lc.max_hp))
        ctx.db.add(lc)


@effects.register("aura_damage")
class AuraDamageHandler(effects.EffectHandler):
    """Marker for damaging auras around a caster (Spirit Guardians, Sickening Radiance,
    Cloud of Daggers, etc.). The actual triggers fire from sessions: per-step entry
    into the aura via _do_walk, and per-turn-start via the turn-advance hooks.
    payload: {radius_ft, dice, type, save_ability?, save_dc?, hostile_only?, on_apply_dc?}.
    """
    def on_apply(self, ctx, effect):
        # Inject the caster's spell save DC into payload.save_dc if not set.
        payload = dict(effect.payload or {})
        if payload.get("save_ability") and "save_dc" not in payload:
            payload["save_dc"] = payload.get("on_apply_dc", 13)
        effect.payload = payload
        ctx.db.add(effect)


@effects.register("damage_resistance")
class DamageResistanceHandler(effects.EffectHandler):
    """Adds payload.types to lc.damage_resistances on apply; removes on remove.
    Used by Stoneskin, Resistance (cantrip), etc.
    """

    def on_apply(self, ctx, effect):
        from db import LiveCharacter
        if effect.target_live_id is None:
            return
        lc = ctx.db.get(LiveCharacter, effect.target_live_id)
        if not lc:
            return
        types = (effect.payload or {}).get("types") or []
        existing = list(lc.damage_resistances or [])
        added = [t for t in types if t not in existing]
        if added:
            lc.damage_resistances = existing + added
            ctx.db.add(lc)

    def on_remove(self, ctx, effect):
        from db import LiveCharacter
        if effect.target_live_id is None:
            return
        lc = ctx.db.get(LiveCharacter, effect.target_live_id)
        if not lc:
            return
        types = (effect.payload or {}).get("types") or []
        if not types:
            return
        existing = list(lc.damage_resistances or [])
        new = [t for t in existing if t not in types]
        if len(new) != len(existing):
            lc.damage_resistances = new
            ctx.db.add(lc)


@effects.register("death_ward")
class DeathWardHandler(effects.EffectHandler):
    """Marker handler. The actual preempt-on-zero logic lives in sessions
    ._apply_damage_to: it checks for death_ward on the target before applying
    a fatal hit and clamps to 1 HP, consuming the effect.
    """
    pass


@effects.register("smite_on_hit")
class SmiteOnHitHandler(effects.EffectHandler):
    """Marker for primed smites (Searing/Wrathful/Thunderous/Branding/Banishing).
    Mechanics live in sessions._do_attack: it picks up the attacker's smite_on_hit
    effects before the attack, appends the payload's damage dice to extra_damage_on_hit,
    and consumes the effect after the next melee hit lands.
    """

    def on_attack_roll(self, ctx, effect, roll: effects.AttackRoll):
        # No-op here: the caller in sessions handles dice append + consumption,
        # because the "consume on hit" semantics need post-resolution access.
        pass


@effects.register("wall_damage")
class WallDamageHandler(effects.EffectHandler):
    """Damage when crossing a wall segment (Wall of Fire, Wall of Thorns, Blade Barrier).

    payload: {"dice": "5d8", "type": "fire", "save"?: {"ability": "dex", "dc": int}}.
    Effect's area: {shape: "wall", points: [[x1,y1],[x2,y2]]}.
    Walls don't block movement unless payload.blocks_movement=True (see _do_walk).
    Damage fires once per step that crosses the wall segment. If a save is set,
    success halves the damage.
    """

    def on_movement_step(self, ctx, effect, step: effects.MovementStep):
        from . import dice, combat
        from db import LiveCharacter
        payload = effect.payload or {}
        roll = dice.roll(payload.get("dice", "5d8"))
        amount = roll.total
        save = payload.get("save")
        if save and isinstance(save, dict):
            mover = ctx.db.get(LiveCharacter, step.mover_id) if step.mover_id else None
            if mover:
                ability = (save.get("ability") or "dexterity").lower()
                if ability in ("dex", "str", "con", "int", "wis", "cha"):
                    map_ab = {"dex": "dexterity", "str": "strength", "con": "constitution",
                              "int": "intelligence", "wis": "wisdom", "cha": "charisma"}
                    ability = map_ab[ability]
                dc = int(save.get("dc", 13))
                score = getattr(mover, ability, 10)
                mod = (score - 10) // 2
                if ability in (mover.saving_throw_profs or []):
                    pb = 2 + max(0, ((mover.level or 1) - 1) // 4)
                    mod += pb
                sv = combat.make_save(mover.name, ability, mod, dc,
                                       [c["name"] for c in (mover.conditions or [])])
                if sv.success:
                    amount = amount // 2
        step.extra_damage.append((amount, payload.get("type", "fire"), effect.caster_live_id))


@effects.register("spike_growth")
class SpikeGrowthHandler(effects.EffectHandler):
    """20-ft radius area: 2d4 piercing per cell of movement through it.

    The area becomes difficult terrain too — that's handled in sessions._do_walk
    by reading payload.difficult_terrain and feeding the area as a difficult zone
    into movement.validate_path.
    """

    def on_movement_step(self, ctx, effect, step: effects.MovementStep):
        from . import dice
        roll = dice.roll("2d4")
        step.extra_damage.append((roll.total, "piercing", effect.caster_live_id))

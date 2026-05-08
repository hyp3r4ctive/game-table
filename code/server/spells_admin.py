"""DM CRUD for campaign-scoped custom spells. Reads/writes the Spell table."""

import json
import re
from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from db import get_session, User, Campaign, Spell
from auth import get_current_user
from game import spells as spells_mod

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _normalize_key(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", name.lower().replace(" ", "_").replace("-", "_"))


def _require_dm(db_session: Session, campaign_id: int, user: User) -> Campaign:
    c = db_session.get(Campaign, campaign_id)
    if not c:
        raise HTTPException(404)
    if c.dm_id != user.id:
        raise HTTPException(403, "DM only")
    return c


@router.get("/campaigns/{campaign_id}/spells", response_class=HTMLResponse)
def list_custom_spells(
    campaign_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    campaign = _require_dm(db, campaign_id, user)
    custom = db.exec(
        select(Spell).where(Spell.campaign_id == campaign_id).order_by(Spell.level, Spell.name)
    ).all()
    json_only = [s for s in spells_mod.SPELLS.keys()]
    return templates.TemplateResponse(request, "spells_admin.html", {
        "user": user, "campaign": campaign,
        "custom": custom, "json_only_count": len(json_only),
    })


def _parse_json_field(raw: str, label: str) -> dict | list | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"{label} is not valid JSON: {e}")


@router.post("/campaigns/{campaign_id}/spells")
async def create_custom_spell(
    campaign_id: int,
    request: Request,
    name: str = Form(),
    level: int = Form(0),
    school: str = Form(""),
    casting_time: str = Form("action"),
    range_ft: int = Form(0),
    duration: str = Form("instantaneous"),
    concentration: bool = Form(False),
    effect_type: str = Form("manual"),
    requires_sight: bool = Form(True),
    target_type: str = Form("creature_seen"),
    components_v: bool = Form(False),
    components_s: bool = Form(False),
    components_m: bool = Form(False),
    material_component: str = Form(""),
    description: str = Form(""),
    damage_json: str = Form(""),
    save_json: str = Form(""),
    area_json: str = Form(""),
    healing_json: str = Form(""),
    conditions_applied_json: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    _require_dm(db, campaign_id, user)
    key = _normalize_key(name)
    existing = db.exec(
        select(Spell).where(Spell.campaign_id == campaign_id, Spell.key == key)
    ).first()
    if existing:
        raise HTTPException(400, f"a custom spell with key '{key}' already exists in this campaign")

    components = []
    if components_v: components.append("V")
    if components_s: components.append("S")
    if components_m: components.append("M")

    s = Spell(
        campaign_id=campaign_id,
        key=key,
        name=name,
        level=level,
        school=school,
        casting_time=casting_time,
        range_ft=range_ft,
        duration=duration,
        concentration=concentration,
        effect_type=effect_type,
        requires_sight=requires_sight,
        target_type=target_type,
        components=components,
        material_component=material_component,
        description=description,
        is_homebrew=True,
        created_by_user_id=user.id,
    )
    dmg = _parse_json_field(damage_json, "damage_json")
    if dmg is not None:
        s.damage = dmg if isinstance(dmg, list) else [dmg]
    save = _parse_json_field(save_json, "save_json")
    if save is not None:
        s.save = save
    area = _parse_json_field(area_json, "area_json")
    if area is not None:
        s.area = area
    healing = _parse_json_field(healing_json, "healing_json")
    if healing is not None:
        s.healing = healing
    conds = _parse_json_field(conditions_applied_json, "conditions_applied_json")
    if conds is not None:
        s.conditions_applied = conds if isinstance(conds, list) else [conds]
    db.add(s)
    db.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}/spells", status_code=303)


@router.post("/campaigns/{campaign_id}/spells/{spell_id}/delete")
def delete_custom_spell(
    campaign_id: int,
    spell_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    _require_dm(db, campaign_id, user)
    s = db.get(Spell, spell_id)
    if not s or s.campaign_id != campaign_id:
        raise HTTPException(404)
    db.delete(s)
    db.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}/spells", status_code=303)

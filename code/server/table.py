"""Pi kiosk routes. No auth — these run on physical devices at the table."""

from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from db import get_session, GameSession, LiveCharacter, Character, Campaign, ActiveEffect

router = APIRouter()
templates = Jinja2Templates(directory="templates")

VALID_SEATS = {1, 2, 3, 4, 5, 6}


def _active_pushed_session(db: Session) -> GameSession | None:
    return db.exec(
        select(GameSession).where(GameSession.pushed_to_table == True, GameSession.is_active == True)
    ).first()


def _seat_override(gs: GameSession, seat: int) -> dict | None:
    overrides = gs.seat_overrides or {}
    return overrides.get(str(seat)) or overrides.get("all")


@router.get("/table/seat/{seat}", response_class=HTMLResponse)
def table_seat(seat: int, request: Request, view: str = "sheet", db: Session = Depends(get_session)):
    if seat not in VALID_SEATS:
        raise HTTPException(404, "no such seat")
    gs = _active_pushed_session(db)
    if not gs:
        return templates.TemplateResponse(request, "table_waiting.html", {"seat": seat})

    override = _seat_override(gs, seat)
    if override:
        return templates.TemplateResponse(request, "table_override.html", {
            "seat": seat, "gs": gs, "override": override, "refresh_seconds": 0, "show_tabs": False,
        })

    campaign = db.get(Campaign, gs.campaign_id)
    seat_assignments = gs.seat_assignments or {}
    claimed_id = seat_assignments.get(str(seat))
    if claimed_id:
        lc = db.get(LiveCharacter, int(claimed_id))
        if lc:
            char = db.get(Character, lc.source_character_id) if lc.source_character_id else None
            active_effects = db.exec(
                select(ActiveEffect).where(
                    ActiveEffect.session_id == gs.id,
                    ActiveEffect.target_live_id == lc.id,
                )
            ).all()
            ctx = {
                "seat": seat, "gs": gs, "campaign": campaign,
                "lc": lc, "char": char, "view": view,
                "active_effects": active_effects,
                "live_characters": db.exec(
                    select(LiveCharacter).where(LiveCharacter.session_id == gs.id)
                ).all(),
            }
            if view == "map":
                ctx["refresh_seconds"] = 0  # SSE-driven, no meta refresh
                tmpl = "table_map.html"
            else:
                tmpl = "table_sheet.html"
            return templates.TemplateResponse(request, tmpl, ctx)

    claimed_ids = {int(v) for v in seat_assignments.values()}
    candidates = db.exec(
        select(LiveCharacter).where(LiveCharacter.session_id == gs.id, LiveCharacter.is_enemy == False)
    ).all()
    available = [lc for lc in candidates if lc.id not in claimed_ids]
    return templates.TemplateResponse(request, "table_picker.html", {
        "seat": seat,
        "gs": gs,
        "campaign": campaign,
        "available": available,
    })


@router.post("/table/seat/{seat}/claim")
def table_claim(seat: int, live_character_id: int = Form(), db: Session = Depends(get_session)):
    if seat not in VALID_SEATS:
        raise HTTPException(404)
    gs = _active_pushed_session(db)
    if not gs:
        raise HTTPException(404, "no live session")
    lc = db.get(LiveCharacter, live_character_id)
    if not lc or lc.session_id != gs.id or lc.is_enemy:
        raise HTTPException(400, "invalid character")
    seats = dict(gs.seat_assignments or {})
    seats = {k: v for k, v in seats.items() if int(v) != live_character_id}
    seats[str(seat)] = live_character_id
    gs.seat_assignments = seats
    db.add(gs)
    db.commit()
    return RedirectResponse(f"/table/seat/{seat}", status_code=303)


@router.post("/table/seat/{seat}/release")
def table_release(seat: int, db: Session = Depends(get_session)):
    if seat not in VALID_SEATS:
        raise HTTPException(404)
    gs = _active_pushed_session(db)
    if not gs:
        raise HTTPException(404, "no live session")
    seats = dict(gs.seat_assignments or {})
    seats.pop(str(seat), None)
    gs.seat_assignments = seats
    db.add(gs)
    db.commit()
    return RedirectResponse(f"/table/seat/{seat}", status_code=303)

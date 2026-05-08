"""Fullscreen projector view. No auth — meant for the rear-projection screen on the table."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from db import get_session, GameSession
import projection

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/projector/{session_id}", response_class=HTMLResponse)
def projector_view(session_id: int, request: Request, db: Session = Depends(get_session)):
    gs = db.get(GameSession, session_id)
    if not gs or not gs.is_active:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "projector.html", {
        "session_id": session_id,
        "play_w": projection.PLAY_AREA_INCHES[0],
        "play_h": projection.PLAY_AREA_INCHES[1],
        "lip": projection.LIP_INCHES,
        "player_default_margin": projection.PLAYER_EDGE_DEFAULT_MARGIN_INCHES,
        "player_geometry_margin": projection.PLAYER_EDGE_GEOMETRY_MARGIN_INCHES,
        "dm_edge": projection.DEFAULT_DM_EDGE,
    })

"""Fullscreen projector view. No auth — meant for the rear-projection screen on the table."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from db import get_session, GameSession

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/projector/{session_id}", response_class=HTMLResponse)
def projector_view(session_id: int, request: Request, db: Session = Depends(get_session)):
    gs = db.get(GameSession, session_id)
    if not gs or not gs.is_active:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "projector.html", {
        "session_id": session_id,
    })

"""Battle map management: upload, list, delete, set as active."""

import re
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request, Form, UploadFile, File
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from db import get_session, User, Campaign, Map, GameSession
from auth import get_current_user

router = APIRouter()
templates = Jinja2Templates(directory="templates")

MAPS_ROOT = Path(__file__).parent / "static" / "maps"
MAPS_ROOT.mkdir(parents=True, exist_ok=True)
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _safe_filename(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name[:80] or "map"


def _require_dm(db_session: Session, campaign_id: int, user: User) -> Campaign:
    campaign = db_session.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404)
    if campaign.dm_id != user.id:
        raise HTTPException(403, "DM only")
    return campaign


@router.get("/campaigns/{campaign_id}/maps", response_class=HTMLResponse)
def list_maps(
    campaign_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    campaign = _require_dm(db, campaign_id, user)
    maps = db.exec(select(Map).where(Map.campaign_id == campaign_id)).all()
    return templates.TemplateResponse(request, "maps.html", {
        "user": user,
        "campaign": campaign,
        "maps": maps,
    })


@router.post("/campaigns/{campaign_id}/maps")
async def upload_map(
    campaign_id: int,
    request: Request,
    name: str = Form(),
    grid_cols: int = Form(60),
    grid_rows: int = Form(48),
    grid_type: str = Form("square"),
    image: UploadFile | None = File(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    _require_dm(db, campaign_id, user)
    image_path = None
    if image and image.filename:
        ext = Path(image.filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            raise HTTPException(400, f"file type {ext} not allowed")
        campaign_dir = MAPS_ROOT / str(campaign_id)
        campaign_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_filename(Path(image.filename).stem) + ext
        dest = campaign_dir / safe_name
        i = 1
        while dest.exists():
            dest = campaign_dir / f"{Path(safe_name).stem}_{i}{ext}"
            i += 1
        with open(dest, "wb") as f:
            f.write(await image.read())
        image_path = f"/static/maps/{campaign_id}/{dest.name}"
    m = Map(
        campaign_id=campaign_id,
        name=name or "Untitled Map",
        image_path=image_path,
        grid_cols=max(4, grid_cols),
        grid_rows=max(4, grid_rows),
        grid_type=grid_type if grid_type in ("square", "hex") else "square",
    )
    db.add(m)
    db.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}/maps", status_code=303)


@router.post("/campaigns/{campaign_id}/maps/{map_id}/delete")
def delete_map(
    campaign_id: int,
    map_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    _require_dm(db, campaign_id, user)
    m = db.get(Map, map_id)
    if not m or m.campaign_id != campaign_id:
        raise HTTPException(404)
    in_use = db.exec(select(GameSession).where(GameSession.active_map_id == map_id, GameSession.is_active == True)).first()
    if in_use:
        raise HTTPException(400, "map is in use by an active session")
    if m.image_path:
        try:
            (MAPS_ROOT.parent.parent / m.image_path.lstrip("/")).unlink(missing_ok=True)
        except Exception:
            pass
    db.delete(m)
    db.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}/maps", status_code=303)

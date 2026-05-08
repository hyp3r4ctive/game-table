"""Battle map management: upload, list, delete, set as active."""

import re
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request, Form, UploadFile, File
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from db import get_session, User, Campaign, Map, GameSession
from auth import get_current_user
import projection

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
    sq_default = projection.default_map_dims("square", projection.DEFAULT_INCHES_PER_CELL)
    hex_default = projection.default_map_dims("hex", projection.DEFAULT_INCHES_PER_CELL)
    sq_fill = projection.fit_grid_dims("square", projection.DEFAULT_INCHES_PER_CELL)
    hex_fill = projection.fit_grid_dims("hex", projection.DEFAULT_INCHES_PER_CELL)
    return templates.TemplateResponse(request, "maps.html", {
        "user": user,
        "campaign": campaign,
        "maps": maps,
        "sq_default_cols": sq_default[0], "sq_default_rows": sq_default[1],
        "hex_default_cols": hex_default[0], "hex_default_rows": hex_default[1],
        "sq_fill_cols": sq_fill[0], "sq_fill_rows": sq_fill[1],
        "hex_fill_cols": hex_fill[0], "hex_fill_rows": hex_fill[1],
        "default_inches_per_cell": projection.DEFAULT_INCHES_PER_CELL,
        "default_map_physical": projection.DEFAULT_MAP_PHYSICAL_INCHES,
        "default_grid_type": projection.DEFAULT_MAP_GRID_TYPE,
        "effective_area": projection.effective_area(),
    })


@router.post("/campaigns/{campaign_id}/maps")
async def upload_map(
    campaign_id: int,
    request: Request,
    name: str = Form(),
    grid_cols: int = Form(0),
    grid_rows: int = Form(0),
    grid_type: str = Form(projection.DEFAULT_MAP_GRID_TYPE),
    feet_per_square: int = Form(5),
    inches_per_square: float = Form(projection.DEFAULT_INCHES_PER_CELL),
    image: UploadFile | None = File(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    if grid_cols < 4 or grid_rows < 4:
        gt = grid_type if grid_type in ("square", "hex") else projection.DEFAULT_MAP_GRID_TYPE
        auto_cols, auto_rows = projection.default_map_dims(gt, inches_per_square or projection.DEFAULT_INCHES_PER_CELL)
        if grid_cols < 4:
            grid_cols = auto_cols
        if grid_rows < 4:
            grid_rows = auto_rows
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
        feet_per_square=max(1, feet_per_square),
        inches_per_square=max(0.25, inches_per_square),
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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlmodel import Session, select
from db import JoinRequest
import json

from db import engine, init_db, get_session, User, Character, Campaign, CampaignMember, CampaignCharacter
from auth import hash_password, verify_password, get_current_user, get_current_user_optional

app = FastAPI()

app.add_middleware(SessionMiddleware, secret_key="change-this-to-something-random")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

clients: list[WebSocket] = []


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, user: User = Depends(get_current_user_optional)):
    if user:
        return RedirectResponse("/dashboard")
    return RedirectResponse("/login")


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html", {})


@app.post("/register")
async def register(request: Request, username: str = Form(), password: str = Form(), db: Session = Depends(get_session)):
    existing = db.exec(select(User).where(User.username == username)).first()
    if existing:
        return templates.TemplateResponse(request, "register.html", {"error": "username taken"})
    user = User(username=username, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)
    request.session["user_id"] = user.id
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


@app.post("/login")
async def login(request: Request, username: str = Form(), password: str = Form(), db: Session = Depends(get_session)):
    user = db.exec(select(User).where(User.username == username)).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(request, "login.html", {"error": "bad credentials"})
    request.session["user_id"] = user.id
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_session)):
    dm_campaigns = db.exec(select(Campaign).where(Campaign.dm_id == user.id)).all()
    member_rows = db.exec(select(CampaignMember).where(CampaignMember.user_id == user.id, CampaignMember.role == "player")).all()
    player_campaigns = [db.get(Campaign, m.campaign_id) for m in member_rows]
    characters = db.exec(select(Character).where(Character.owner_id == user.id)).all()
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user,
        "dm_campaigns": dm_campaigns,
        "player_campaigns": player_campaigns,
        "characters": characters,
    })


@app.get("/campaigns/new", response_class=HTMLResponse)
async def new_campaign_page(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse(request, "new_campaign.html", {"user": user})


@app.post("/campaigns/new")
async def new_campaign(request: Request, name: str = Form(), description: str = Form(""), user: User = Depends(get_current_user), db: Session = Depends(get_session)):
    campaign = Campaign(dm_id=user.id, name=name, description=description)
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    membership = CampaignMember(campaign_id=campaign.id, user_id=user.id, role="dm")
    db.add(membership)
    db.commit()
    return RedirectResponse(f"/campaigns/{campaign.id}", status_code=303)


@app.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
async def campaign_detail(campaign_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_session)):
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404)
    members = db.exec(select(CampaignMember).where(CampaignMember.campaign_id == campaign_id)).all()
    member_users = [db.get(User, m.user_id) for m in members]
    is_dm = campaign.dm_id == user.id
    return templates.TemplateResponse(request, "campaign.html", {
        "user": user,
        "campaign": campaign,
        "members": list(zip(members, member_users)),
        "is_dm": is_dm,
    })


@app.get("/campaigns", response_class=HTMLResponse)
async def browse_campaigns(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_session)):
    campaigns = db.exec(select(Campaign).where(Campaign.is_active == True)).all()
    dm_users = {c.id: db.get(User, c.dm_id) for c in campaigns}
    user_memberships = db.exec(select(CampaignMember).where(CampaignMember.user_id == user.id)).all()
    member_campaign_ids = {m.campaign_id for m in user_memberships}
    user_requests = db.exec(select(JoinRequest).where(JoinRequest.user_id == user.id, JoinRequest.status == "pending")).all()
    pending_campaign_ids = {r.campaign_id for r in user_requests}
    return templates.TemplateResponse(request, "browse_campaigns.html", {
        "user": user,
        "campaigns": campaigns,
        "dm_users": dm_users,
        "member_campaign_ids": member_campaign_ids,
        "pending_campaign_ids": pending_campaign_ids,
    })


@app.get("/campaigns/{campaign_id}/request", response_class=HTMLResponse)
async def request_join_page(campaign_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_session)):
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404)
    user_chars = db.exec(select(Character).where(Character.owner_id == user.id, Character.is_enemy == False)).all()
    return templates.TemplateResponse(request, "request_join.html", {
        "user": user,
        "campaign": campaign,
        "characters": user_chars,
    })


@app.post("/campaigns/{campaign_id}/request")
async def submit_join_request(
    campaign_id: int,
    request: Request,
    character_id: int | None = Form(None),
    message: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404)
    existing = db.exec(select(JoinRequest).where(
        JoinRequest.campaign_id == campaign_id,
        JoinRequest.user_id == user.id,
        JoinRequest.status == "pending",
    )).first()
    if existing:
        return RedirectResponse("/campaigns", status_code=303)
    req = JoinRequest(campaign_id=campaign_id, user_id=user.id, character_id=character_id, message=message)
    db.add(req)
    db.commit()
    return RedirectResponse("/campaigns", status_code=303)


@app.get("/campaigns/{campaign_id}/requests", response_class=HTMLResponse)
async def view_join_requests(campaign_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_session)):
    campaign = db.get(Campaign, campaign_id)
    if not campaign or campaign.dm_id != user.id:
        raise HTTPException(403)
    requests_pending = db.exec(select(JoinRequest).where(
        JoinRequest.campaign_id == campaign_id,
        JoinRequest.status == "pending",
    )).all()
    enriched = []
    for r in requests_pending:
        requester = db.get(User, r.user_id)
        char = db.get(Character, r.character_id) if r.character_id else None
        enriched.append({"request": r, "user": requester, "character": char})
    return templates.TemplateResponse(request, "join_requests.html", {
        "user": user,
        "campaign": campaign,
        "requests": enriched,
    })


@app.post("/campaigns/{campaign_id}/requests/{request_id}/approve")
async def approve_request(campaign_id: int, request_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_session)):
    campaign = db.get(Campaign, campaign_id)
    if not campaign or campaign.dm_id != user.id:
        raise HTTPException(403)
    req = db.get(JoinRequest, request_id)
    if not req or req.campaign_id != campaign_id:
        raise HTTPException(404)
    req.status = "approved"
    membership = CampaignMember(campaign_id=campaign_id, user_id=req.user_id, role="player")
    db.add(membership)
    if req.character_id:
        cc = CampaignCharacter(campaign_id=campaign_id, character_id=req.character_id, role="player_character")
        db.add(cc)
    db.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}/requests", status_code=303)


@app.post("/campaigns/{campaign_id}/requests/{request_id}/deny")
async def deny_request(campaign_id: int, request_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_session)):
    campaign = db.get(Campaign, campaign_id)
    if not campaign or campaign.dm_id != user.id:
        raise HTTPException(403)
    req = db.get(JoinRequest, request_id)
    if not req or req.campaign_id != campaign_id:
        raise HTTPException(404)
    req.status = "denied"
    db.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}/requests", status_code=303)

@app.get("/characters/new", response_class=HTMLResponse)
async def new_character_page(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse(request, "new_character.html", {"user": user})


@app.post("/characters/new")
async def new_character(
    request: Request,
    name: str = Form(),
    character_class: str = Form(""),
    max_hp: int = Form(10),
    armor_class: int = Form(10),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session)
):
    character = Character(
        owner_id=user.id,
        name=name,
        character_class=character_class,
        max_hp=max_hp,
        current_hp=max_hp,
        armor_class=armor_class,
    )
    db.add(character)
    db.commit()
    return RedirectResponse("/dashboard", status_code=303)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        clients.remove(websocket)

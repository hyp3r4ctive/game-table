from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlmodel import Session, select
from db import JoinRequest
import json

from db import engine, init_db, get_session, User, Character, Campaign, CampaignMember, CampaignCharacter, GameSession, Map
from auth import hash_password, verify_password, get_current_user, get_current_user_optional
from sessions import router as sessions_router
from table import router as table_router
from maps import router as maps_router
from projector import router as projector_router

app = FastAPI()

app.add_middleware(SessionMiddleware, secret_key="change-this-to-something-random")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
app.include_router(sessions_router)
app.include_router(table_router)
app.include_router(maps_router)
app.include_router(projector_router)

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
    is_member = any(m.user_id == user.id for m in members)
    active_session = db.exec(
        select(GameSession).where(GameSession.campaign_id == campaign_id, GameSession.is_active == True)
    ).first()
    return templates.TemplateResponse(request, "campaign.html", {
        "user": user,
        "campaign": campaign,
        "members": list(zip(members, member_users)),
        "is_dm": is_dm,
        "is_member": is_member,
        "active_session": active_session,
    })


@app.get("/campaigns/{campaign_id}/master", response_class=HTMLResponse)
async def campaign_master_view(campaign_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_session)):
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404)
    if campaign.dm_id != user.id:
        raise HTTPException(403, "DM only")
    members = db.exec(select(CampaignMember).where(CampaignMember.campaign_id == campaign_id)).all()
    member_users = {m.user_id: db.get(User, m.user_id) for m in members}
    cc_rows = db.exec(select(CampaignCharacter).where(CampaignCharacter.campaign_id == campaign_id)).all()
    chars_with_owners = []
    for cc in cc_rows:
        ch = db.get(Character, cc.character_id)
        if ch:
            chars_with_owners.append({
                "character": ch,
                "owner": db.get(User, ch.owner_id),
                "role": cc.role,
                "is_active": cc.is_active,
            })
    sessions = sorted(
        db.exec(select(GameSession).where(GameSession.campaign_id == campaign_id)).all(),
        key=lambda s: s.started_at, reverse=True,
    )
    maps = db.exec(select(Map).where(Map.campaign_id == campaign_id)).all()
    active_session = next((s for s in sessions if s.is_active), None)
    return templates.TemplateResponse(request, "campaign_master.html", {
        "user": user,
        "campaign": campaign,
        "members": members,
        "member_users": member_users,
        "characters": chars_with_owners,
        "sessions": sessions,
        "active_session": active_session,
        "maps": maps,
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
    import character_data as cd
    return templates.TemplateResponse(request, "character_form.html", {
        "user": user,
        "character": None,
        "races": cd.list_races(),
        "classes": cd.list_classes(),
        "backgrounds": cd.list_backgrounds(),
        "skills": cd.SKILLS,
        "abilities": cd.ABILITIES,
        "races_json": cd.RACES,
        "classes_json": {k: v for k, v in cd.CLASSES.items() if not k.startswith("_")},
        "spells_json": __import__("game.spells", fromlist=["SPELLS"]).SPELLS,
    })


def _character_from_form(form_data: dict, owner_id: int, existing: Character | None = None) -> Character:
    import character_data as cd

    def _list(key):
        v = form_data.getlist(key) if hasattr(form_data, "getlist") else form_data.get(key, [])
        return [x for x in v if x]

    def _get(key, default=""):
        v = form_data.get(key, default)
        return v if v is not None else default

    def _int(key, default=0):
        try:
            return int(form_data.get(key, default) or default)
        except (ValueError, TypeError):
            return default

    def _bool(key):
        return form_data.get(key) in ("on", "true", "1", True)

    def _split_lines(key):
        raw = _get(key, "")
        return [line.strip() for line in raw.splitlines() if line.strip()]

    char = existing or Character(owner_id=owner_id, name="")
    char.name = _get("name") or "Unnamed"
    char.player_name = _get("player_name")
    char.race = _get("race")
    char.subrace = _get("subrace")
    char.character_class = _get("character_class")
    char.subclass = _get("subclass")
    char.background = _get("background")
    char.alignment = _get("alignment")
    char.level = max(1, _int("level", 1))
    char.experience_points = _int("experience_points", 0)
    char.max_hp = _int("max_hp", 10)
    char.current_hp = _int("current_hp", char.max_hp)
    char.temp_hp = _int("temp_hp", 0)
    char.armor_class = _int("armor_class", 10)
    char.speed_ft = _int("speed_ft", 30)
    char.initiative_bonus = _int("initiative_bonus", 0)
    char.hit_die = _get("hit_die", "d8")
    char.hit_dice_used = _int("hit_dice_used", 0)
    for ab in cd.ABILITIES:
        setattr(char, ab, _int(ab, 10))
    char.spellcasting_ability = _get("spellcasting_ability")
    char.inspiration = _bool("inspiration")
    char.darkvision_ft = _int("darkvision_ft", 0)
    char.vision_normal_ft = _int("vision_normal_ft", 0)
    char.light_emission_ft = _int("light_emission_ft", 0)
    char.saving_throw_profs = _list("saving_throw_profs")
    char.skill_profs = _list("skill_profs")
    char.skill_expertises = _list("skill_expertises")
    char.languages = [s.strip() for s in _get("languages_csv").split(",") if s.strip()]
    char.features = [{"name": ln.split(":", 1)[0].strip(), "description": ln.split(":", 1)[1].strip() if ":" in ln else ""} for ln in _split_lines("features_text")]
    char.equipment = [{"name": ln} for ln in _split_lines("equipment_text")]
    char.money = {coin: _int(f"money_{coin}", 0) for coin in ("cp", "sp", "ep", "gp", "pp")}
    char.spells_known = _list("spells_known")
    slots_max = {}
    slots_used = {}
    for lvl in range(1, 10):
        m = _int(f"slot_max_{lvl}", -1)
        u = _int(f"slot_used_{lvl}", -1)
        if m >= 0:
            slots_max[str(lvl)] = m
        if u >= 0:
            slots_used[str(lvl)] = u
    char.spell_slots_max = slots_max
    char.spell_slots_used = slots_used
    char.personality_traits = _get("personality_traits")
    char.ideals = _get("ideals")
    char.bonds = _get("bonds")
    char.flaws = _get("flaws")
    char.backstory = _get("backstory")
    char.notes = _get("notes")
    return char


@app.post("/characters/new")
async def new_character(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_session)):
    form_data = await request.form()
    character = _character_from_form(form_data, user.id)
    db.add(character)
    db.commit()
    db.refresh(character)
    return RedirectResponse(f"/characters/{character.id}", status_code=303)


@app.get("/characters/{character_id}", response_class=HTMLResponse)
async def view_character(character_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_session)):
    import character_data as cd
    character = db.get(Character, character_id)
    if not character:
        raise HTTPException(404)
    is_owner = character.owner_id == user.id
    return templates.TemplateResponse(request, "character_sheet.html", {
        "user": user,
        "character": character,
        "is_owner": is_owner,
        "skills": cd.SKILLS,
        "abilities": cd.ABILITIES,
        "race_data": cd.get_race(character.race),
        "class_data": cd.get_class(character.character_class),
        "background_data": cd.get_background(character.background),
        "ability_modifier": cd.ability_modifier,
        "proficiency_bonus": cd.proficiency_bonus(character.level),
    })


@app.get("/characters/{character_id}/edit", response_class=HTMLResponse)
async def edit_character_page(character_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_session)):
    import character_data as cd
    character = db.get(Character, character_id)
    if not character:
        raise HTTPException(404)
    if character.owner_id != user.id:
        raise HTTPException(403)
    return templates.TemplateResponse(request, "character_form.html", {
        "user": user,
        "character": character,
        "races": cd.list_races(),
        "classes": cd.list_classes(),
        "backgrounds": cd.list_backgrounds(),
        "skills": cd.SKILLS,
        "abilities": cd.ABILITIES,
        "races_json": cd.RACES,
        "classes_json": {k: v for k, v in cd.CLASSES.items() if not k.startswith("_")},
        "spells_json": __import__("game.spells", fromlist=["SPELLS"]).SPELLS,
    })


@app.post("/characters/{character_id}/edit")
async def edit_character(character_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_session)):
    character = db.get(Character, character_id)
    if not character:
        raise HTTPException(404)
    if character.owner_id != user.id:
        raise HTTPException(403)
    form_data = await request.form()
    _character_from_form(form_data, user.id, existing=character)
    db.add(character)
    db.commit()
    return RedirectResponse(f"/characters/{character_id}", status_code=303)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        clients.remove(websocket)

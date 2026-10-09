"""Cohors — compagnon de guilde World of Warcraft (web app FastAPI).

Accounts: invitation-only registration (admin-generated links), login sessions
(signed random token in an HttpOnly cookie), admin panel (invites + users).
Simulations run in the official SimulationCraft Docker image via
`worker/simworker.py` — the app drops restricted jobs to the worker; the worker
(the sole component that mounts `/var/run/docker.sock`) launches the SimulationCraft containers.

v2026.09.003: accounts + admin.
"""
from __future__ import annotations

import logging
import asyncio
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote

import httpx
import websockets
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

import httpx

from app import bnet, discord_bot, mailer, wcl
from app.security import check_profile, hash_password as _hash_password, real_client_ip, verify_password as _verify_password

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration, database and auth helpers live in app/core (re-exported here).
# ---------------------------------------------------------------------------
from app.core.config import (
    DATA_DIR, BRAND_DIR, REPORTS_DIR, SIMC_IMAGE, QUEUE_MAX, PER_IP_ACTIVE,
    PER_IP_COOLDOWN_S, PER_USER_ACTIVE, ITER_CHOICES, DEFAULT_ITERATIONS, MAX_INPUT_CHARS,
    SESSION_COOKIE, SESSION_DAYS, PUBLIC_BASE_URL, COOKIE_SECURE, COOKIE_DOMAIN,
    SNAP_KEEP_DAYS, VERSION, STATIC_DIR,
)
from app.core.db import _db, _db_lock  # noqa: F401
from app.core.util import _int_any  # noqa: F401

_login_attempts: dict[str, list[float]] = defaultdict(list)


from app.core.schema import _init_db  # noqa: E402  (app/core/schema.py)


def _reject_blocked_profile(text: str) -> None:
    """Refuse les profils contenant des directives à effet fichier (SimC les honore)."""
    blocked = check_profile(text)
    if blocked:
        raise HTTPException(400, f"Ligne « {blocked}= » non autorisée dans un profil — colle uniquement"
                                 " ton export /simc (cette option touche aux fichiers du moteur).")


# Hachage des mots de passe (scrypt) et garde-fous des profils SimulationCraft :
# module app/security.py (testé par tests/test_security.py).


def _bootstrap_admin() -> None:
    """Create the first admin account from ADMIN_EMAIL/ADMIN_PASSWORD if none exists."""
    with _db_lock, _db() as conn:
        has_admin = conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_admin=1").fetchone()["c"]
        if has_admin:
            return
        email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
        pwd = os.environ.get("ADMIN_PASSWORD", "")
        if email and pwd:
            conn.execute(
                "INSERT INTO users (email, name, pwd, is_admin, role, active, created) VALUES (?,?,?,1,'admin',1,?)",
                (email, "Admin", _hash_password(pwd), time.time()),
            )
            print(f"[bootstrap] compte admin créé : {email}")
        else:
            print("[bootstrap] aucun admin et ADMIN_EMAIL/ADMIN_PASSWORD absents — /admin inaccessible")


from app.core.auth import (
    _session_key, _new_session, _set_session_cookie, _get_session_user, _require_user, _user_role,
    _user_lang, _user_locale, _require_admin, _require_officer,
)


# ---------------------------------------------------------------------------
# Identité de la guilde (v2026.09.111) — logo, nom et fond personnalisables.
# ---------------------------------------------------------------------------
from app.core.brand import _img_type  # noqa: E402


_IMG_MIMES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


from app.core.brand import _brand_identity, _brand_row  # noqa: E402,F401  (app/core/brand.py)


def _brand_files() -> dict:
    """Fichiers personnalisés présents : {'logo': Path, 'bg': Path}."""
    out: dict = {}
    if BRAND_DIR.is_dir():
        for p in sorted(BRAND_DIR.iterdir()):
            if not p.is_file():
                continue
            if p.name.startswith("logo."):
                out["logo"] = p
            elif p.name.startswith("bg."):
                out["bg"] = p
    return out


def _client_ip(request: Request) -> str:
    # Voir app/security.py:real_client_ip — on lit l'IP à TRUSTED_PROXY_HOPS positions de la fin
    # d'X-Forwarded-For (défaut 1 : notre proxy) ; le reste est fourni par le client et forgeable.
    try:
        hops = max(1, int(os.environ.get("TRUSTED_PROXY_HOPS", "1")))
    except ValueError:
        hops = 1
    return real_client_ip(request.headers.get("x-forwarded-for"),
                          request.client.host if request.client else None, hops=hops)


# ---------------------------------------------------------------------------
# Worker (single simulation at a time — one sim saturates every core) — app/services/sims.py
# ---------------------------------------------------------------------------
from app.services.sims import _recover_stale_sims, _worker_loop  # noqa: E402


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _init_db()
    _recover_stale_sims()
    _apply_api_keys()
    try:
        _apply_guild_config()
    except sqlite3.Error as exc:
        print(f"[guild] config: {exc}")
    _bootstrap_admin()
    try:
        _apply_mail_config()
    except sqlite3.Error as exc:
        print(f"[mail] config: {exc}")
    threading.Thread(target=_worker_loop, daemon=True, name="sim-worker").start()
    threading.Thread(target=_bot_loop, daemon=True, name="discord-bot").start()
    threading.Thread(target=_snap_loop, daemon=True, name="char-snap").start()
    threading.Thread(target=_game_recipes_loop, daemon=True, name="game-recipes").start()
    threading.Thread(target=_loot_loop, daemon=True, name="loot-sync").start()
    threading.Thread(target=_update_loop, daemon=True, name="updates").start()
    yield


app = FastAPI(title="Cohors", version=VERSION, lifespan=_lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------- sauvegarde & restauration
# app/routers/admin_backup.py
from app.routers import admin_backup as _admin_backup_router  # noqa: E402
from app.routers.admin_backup import _build_backup, _swap_file  # noqa: E402,F401  (tests)

app.include_router(_admin_backup_router.router)


@app.get("/api/branding")
def api_branding(request: Request):
    """Identité publique (page de connexion incluse) : noms, logo et fond effectifs."""
    with _db_lock, _db() as conn:
        row = _brand_row(conn)
    d = dict(row)
    files = _brand_files()
    short = (d.get("guild_short") or "Cohors").strip()[:24] or "Cohors"
    name = (d.get("guild_name") or "").strip()[:60]
    v = int(d.get("updated") or 0)
    return {
        "name": name, "short": short,
        "logo": "/branding/logo?v=" + str(v),
        "bg": ("/branding/bg?v=" + str(v)) if "bg" in files else "",
        "bg_color": (d.get("bg_color") or "").strip(),
        "custom_logo": "logo" in files, "custom_bg": "bg" in files,
        "raw": {"name": d.get("guild_name") or "", "short": d.get("guild_short") or "",
                "color": d.get("bg_color") or ""},
    }


@app.get("/branding/logo")
def branding_logo():
    p = _brand_files().get("logo")
    if p is not None:
        return FileResponse(p, media_type=_IMG_MIMES.get(p.suffix.lower().lstrip("."), "image/png"),
                            headers={"Cache-Control": "no-cache"})
    return FileResponse(STATIC_DIR / "logo.png", media_type="image/png",
                        headers={"Cache-Control": "no-cache"})


@app.get("/branding/bg")
def branding_bg():
    p = _brand_files().get("bg")
    if p is None:
        raise HTTPException(404, "Pas de fond personnalisé")
    return FileResponse(p, media_type=_IMG_MIMES.get(p.suffix.lower().lstrip("."), "image/png"),
                        headers={"Cache-Control": "no-cache"})


@app.post("/api/admin/branding")
async def api_admin_branding(
        request: Request,
        guild_name: str = Form(""), guild_short: str = Form(""), bg_color: str = Form(""),
        reset_logo: int = Form(0), reset_bg: int = Form(0), reset_color: int = Form(0),
        reset_names: int = Form(0), reset_all: int = Form(0),
        logo: UploadFile = File(None), bg: UploadFile = File(None)):
    """Met à jour l'identité de la guilde (administrateur)."""
    user = _require_admin(request)
    if reset_all:
        reset_logo = reset_bg = reset_color = reset_names = 1
    gn = guild_name.strip()[:60]
    gs = guild_short.strip()[:24]
    col = bg_color.strip().lower()
    if col and not re.match(r"^#([0-9a-f]{3}|[0-9a-f]{6})$", col):
        raise HTTPException(400, "Couleur de fond invalide (ex. #0b0f17).")
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    for uf, key in ((logo, "logo"), (bg, "bg")):
        if uf is None:
            continue
        raw = await uf.read()
        if not raw:
            continue
        if len(raw) > 2 * 1024 * 1024:
            raise HTTPException(413, "Image trop lourde (2 Mo max).")
        ext, _mime = _img_type(raw)
        if not ext:
            raise HTTPException(400, "Format d'image non reconnu (PNG, JPEG, GIF ou WebP).")
        for old in BRAND_DIR.glob(key + ".*"):
            old.unlink(missing_ok=True)
        (BRAND_DIR / f"{key}.{ext}").write_bytes(raw)
    if reset_logo:
        for old in BRAND_DIR.glob("logo.*"):
            old.unlink(missing_ok=True)
    if reset_bg:
        for old in BRAND_DIR.glob("bg.*"):
            old.unlink(missing_ok=True)
    with _db_lock, _db() as conn:
        _brand_row(conn)
        conn.execute(
            "UPDATE branding SET guild_name=?, guild_short=?, bg_color=?, updated=?, updated_by=? WHERE id=1",
            ("" if reset_names else gn, "" if reset_names else gs,
             "" if reset_color else col, time.time(), user["email"]))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.api_route("/", methods=["GET", "HEAD"])
def index(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "index.html")


@app.api_route("/login", methods=["GET", "HEAD"])
def login_page(request: Request):
    if _get_session_user(request) is not None:
        return RedirectResponse("/dashboard", status_code=302)
    return FileResponse(STATIC_DIR / "login.html")


@app.api_route("/invite/{token}", methods=["GET", "HEAD"])
def invite_page(token: str):
    return FileResponse(STATIC_DIR / "register.html")


@app.api_route("/admin", methods=["GET", "HEAD"])
def admin_page(request: Request):
    """L'administration vit désormais dans ⚙️ Paramètres (v2026.09.112)."""
    return RedirectResponse("/settings", status_code=302)


@app.api_route("/characters", methods=["GET", "HEAD"])
def characters_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "characters.html")

@app.api_route("/craft", methods=["GET", "HEAD"])
def craft_page(request: Request):
    """Page 🔨 Artisanat — annuaire des métiers de la guilde."""
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "craft.html")


_MOI_PAGES = {
    "mespersos": "mespersos.html",
    "mesrecettes": "mesrecettes.html",
    "messtats": "messtats.html",
    "alertes": "alertes.html",
    "mesindispos": "mesindispos.html",
}


@app.api_route("/mespersos", methods=["GET", "HEAD"])
@app.api_route("/mesrecettes", methods=["GET", "HEAD"])
@app.api_route("/messtats", methods=["GET", "HEAD"])
@app.api_route("/alertes", methods=["GET", "HEAD"])
@app.api_route("/mesindispos", methods=["GET", "HEAD"])
def moi_pages(request: Request):
    """Pages 🙋 Moi — personnages, recettes, statistiques, alertes MM+ (une par sujet)."""
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    name = request.url.path.strip("/")
    return FileResponse(STATIC_DIR / _MOI_PAGES.get(name, "mespersos.html"))


@app.api_route("/moi", methods=["GET", "HEAD"])
def moi_redirect(request: Request):
    return RedirectResponse("/mespersos", status_code=302)


@app.api_route("/prep", methods=["GET", "HEAD"])
def prep_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "prep.html")
@app.api_route("/mplus", methods=["GET", "HEAD"])
def mplus_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "mplus.html")


@app.api_route("/mains", methods=["GET", "HEAD"])
def mains_page(request: Request):
    """Page ⭐ Mains & alts — tous les personnages liés, groupés sous leur main."""
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "mains.html")

@app.api_route("/char", methods=["GET", "HEAD"])
def char_redirect():
    """Sans personnage précisé → retour à la liste Mains & alts."""
    return RedirectResponse("/mains", status_code=302)


@app.api_route("/char/{realm}/{name}", methods=["GET", "HEAD"])
def char_detail_page(realm: str, name: str, request: Request):
    """Fiche personnage — détails du perso + bascule entre les persos du compte."""
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "char.html")



@app.api_route("/raids", methods=["GET", "HEAD"])
def raids_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "raids.html")


@app.api_route("/compare", methods=["GET", "HEAD"])
def compare_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "compare.html")


# ---------------------------------------------------------------------------
# Auth API
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    email: str = Field(..., max_length=200)
    password: str = Field(..., max_length=200)


class RegisterRequest(BaseModel):
    token: str = Field(..., max_length=100)
    name: str = Field("", max_length=60)
    email: str = Field("", max_length=200)
    password: str = Field(..., min_length=8, max_length=200)
    lang: str = Field("", max_length=5)


@app.api_route("/gear", methods=["GET", "HEAD"])
def gear_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "gear.html")


@app.api_route("/stuff", methods=["GET", "HEAD"])
def stuff_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    response = FileResponse(STATIC_DIR / "stuff.html")
    return response


@app.api_route("/dashboard", methods=["GET", "HEAD"])
def dashboard_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.api_route("/calendar", methods=["GET", "HEAD"])
def calendar_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "calendar.html")


@app.api_route("/start", methods=["GET", "HEAD"])
def start_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "start.html")


@app.api_route("/guild", methods=["GET", "HEAD"])
def guild_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "guild.html")


@app.api_route("/help", methods=["GET", "HEAD"])
def help_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "help.html")


@app.api_route("/rankings", methods=["GET", "HEAD"])
def rankings_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "rankings.html")


@app.post("/api/login")
def login(payload: LoginRequest, request: Request, response: Response):
    ip = _client_ip(request)
    now = time.time()
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < 300]
    if len(_login_attempts[ip]) >= 15:
        raise HTTPException(429, "Trop de tentatives — réessaie dans quelques minutes.")
    email = payload.email.strip().lower()
    with _db_lock, _db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user is None or not _verify_password(payload.password, user["pwd"]):
            _login_attempts[ip].append(now)
            raise HTTPException(401, "E-mail ou mot de passe incorrect.")
        if not user["active"]:
            raise HTTPException(403, "Ce compte est désactivé.")
        token = _new_session(conn, user["id"])
        conn.execute("UPDATE users SET last_login=? WHERE id=?", (now, user["id"]))
    _login_attempts.pop(ip, None)
    _set_session_cookie(response, token)
    return {"ok": True, "name": user["name"], "is_admin": bool(user["is_admin"]),
            "role": _user_role(user), "lang": _user_lang(user)}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with _db_lock, _db() as conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (_session_key(token),))
    response.delete_cookie(SESSION_COOKIE, path="/", domain=COOKIE_DOMAIN)
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    user = _get_session_user(request)
    if user is None:
        raise HTTPException(401, "Non connecté")
    with _db_lock, _db() as conn:
        main = conn.execute(
            "SELECT display, name FROM char_links WHERE user_email=? AND is_main=1 LIMIT 1",
            (user["email"],),
        ).fetchone()
    voice_default = ""
    if main is not None:
        voice_default = (main["display"] or main["name"] or "").strip()
    if not voice_default:
        voice_default = (user["name"] or user["email"].split("@")[0]).strip()
    voice_raw = (user["voice_nick"] or "").strip()
    return {"email": user["email"], "name": user["name"], "is_admin": bool(user["is_admin"]),
            "role": _user_role(user), "lang": _user_lang(user),
            "voice_nick": voice_raw or voice_default, "voice_nick_raw": voice_raw,
            "voice_default": voice_default}


@app.get("/api/invite/{token}")
def invite_info(token: str):
    with _db_lock, _db() as conn:
        inv = conn.execute("SELECT * FROM invites WHERE token=?", (token,)).fetchone()
    if inv is None or inv["used"] is not None or inv["expires"] < time.time():
        return {"valid": False}
    return {"valid": True, "email": inv["email"], "note": inv["note"]}


@app.post("/api/register")
def register(payload: RegisterRequest, request: Request, response: Response):
    now = time.time()
    with _db_lock, _db() as conn:
        inv = conn.execute("SELECT * FROM invites WHERE token=?", (payload.token,)).fetchone()
        if inv is None or inv["used"] is not None or inv["expires"] < now:
            raise HTTPException(400, "Lien d'invitation invalide ou expiré.")
        email = (inv["email"] or payload.email or "").strip().lower()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            raise HTTPException(400, "Adresse e-mail invalide.")
        existing = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if existing is not None:
            if not inv["email"]:
                raise HTTPException(400, "Un compte existe déjà avec cet e-mail — connecte-toi, ou demande un lien de réinitialisation.")
            name = payload.name.strip()[:60] or existing["name"] or email.split("@")[0]
            conn.execute("UPDATE users SET pwd=?, name=?, active=1 WHERE id=?",
                         (_hash_password(payload.password), name, existing["id"]))
            user_id = existing["id"]
        else:
            name = payload.name.strip()[:60] or email.split("@")[0]
            lang_val = payload.lang.strip().lower()
            if lang_val not in ("fr", "en"):
                lang_val = ""
            cur = conn.execute(
                "INSERT INTO users (email, name, pwd, is_admin, role, active, created, lang) VALUES (?,?,?,0,'member',1,?,?)",
                (email, name, _hash_password(payload.password), now, lang_val),
            )
            user_id = int(cur.lastrowid or 0)
        conn.execute("DELETE FROM invites WHERE token=?", (payload.token,))  # code consommé = supprimé (fin de vie)
        token = _new_session(conn, user_id)
    _set_session_cookie(response, token)
    return {"ok": True, "name": name}


# ---------------------------------------------------------------------------
# Sim API
# ---------------------------------------------------------------------------
GEAR_MAX_ITEMS = 15
from app.core.util import ITEM_REF_RE  # noqa: E402


def _build_gear_input(profile_text: str, items_text: str, locale: str | None = None) -> tuple[str, list[str]]:
    """Ajoute les profilesets « Top Stuff » au profil — renvoie (input, avertissements)."""
    refs: list[int] = []
    seen: set[int] = set()
    for m in ITEM_REF_RE.finditer(items_text or ""):
        iid = int(m.group(1))
        if iid not in seen:
            seen.add(iid)
            refs.append(iid)
    if not refs:
        raise HTTPException(400, "Indique au moins une pièce (lien Wowhead ou identifiant).")
    warnings: list[str] = []
    if len(refs) > GEAR_MAX_ITEMS:
        warnings.append(f"{len(refs) - GEAR_MAX_ITEMS} pièce(s) ignorée(s) — maximum {GEAR_MAX_ITEMS} par comparaison.")
        refs = refs[:GEAR_MAX_ITEMS]
    lines: list[str] = []
    for iid in refs:
        try:
            it = bnet.item(iid, locale=locale)
        except bnet.BnetError as exc:
            warnings.append(f"{iid} : pièce ignorée ({exc}).")
            continue
        slots = bnet.INV_TO_SLOTS.get(it["inv_type"])
        if not slots:
            warnings.append(f"{it['name']} : emplacement non géré ({it['inv_type_fr'] or it['inv_type'] or '?'}).")
            continue
        clean = it["name"].replace('"', "'")[:48]
        for slot in slots:
            lines.append(f'profileset."{clean} · {bnet.slot_label(slot, locale)} [{slot}:{iid}]"={slot}=,id={iid}')
    if not lines:
        raise HTTPException(400, "Aucune pièce exploitable parmi celles fournies.")
    return profile_text + "\n\n" + "\n".join(lines) + "\n", warnings


# « Stuff conseillé » (export SimC, guide BIS, artisanat, classement) — app/services/stuff.py
from app.services.stuff import (  # noqa: E402,F401
    BIS_CONTENT_MAP, BIS_CONTENTS, HEAL_SPECS, STUFF_CONTENTS, STUFF_HEAL_PRIO, STUFF_ITERATIONS, STUFF_SLOTS,
    _stuff_best_crafted, _stuff_bis_filter, _stuff_bis_list, _stuff_parse_export, _stuff_sim_input,
)


class SimRequest(BaseModel):
    input: str = Field(..., min_length=30, max_length=MAX_INPUT_CHARS)
    iterations: int = DEFAULT_ITERATIONS
    label: str = ""
    kind: str = "dps"
    items: str = Field("", max_length=2000)


@app.post("/api/sim")
def submit_sim(payload: SimRequest, request: Request):
    user = _require_user(request)
    if payload.iterations not in ITER_CHOICES:
        raise HTTPException(400, f"Valeurs d'itérations acceptées : {', '.join(map(str, ITER_CHOICES))}")
    kind = payload.kind if payload.kind in ("dps", "weights", "gear") else None
    if kind is None:
        raise HTTPException(400, "Type de simulation invalide.")
    text = payload.input.replace("\r\n", "\n").strip()
    label = payload.label.strip()[:60]
    warnings: list[str] = []
    if kind == "gear":
        text, warnings = _build_gear_input(text, payload.items, locale=_user_locale(request))
    _reject_blocked_profile(text)
    ip = _client_ip(request)
    now = time.time()

    with _db_lock, _db() as conn:
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM sims WHERE ip=? AND status IN ('queued','running')", (ip,)
        ).fetchone()["c"]
        if active >= PER_IP_ACTIVE:
            raise HTTPException(429, f"Tu as déjà {active} simulation(s) en attente — patiente un peu.")
        user_active = conn.execute(
            "SELECT COUNT(*) AS c FROM sims WHERE user_email=? AND status IN ('queued','running')", (user["email"],)
        ).fetchone()["c"]
        if user_active >= PER_USER_ACTIVE:
            raise HTTPException(429, f"Tu as déjà {user_active} simulation(s) en attente — patiente un peu.")
        last_ts = conn.execute("SELECT MAX(created) AS m FROM sims WHERE ip=?", (ip,)).fetchone()["m"]
        if last_ts and now - last_ts < PER_IP_COOLDOWN_S:
            wait = int(PER_IP_COOLDOWN_S - (now - last_ts)) + 1
            raise HTTPException(429, f"Doucement ! Réessaie dans {wait} s.")
        queue_len = conn.execute("SELECT COUNT(*) AS c FROM sims WHERE status IN ('queued','running')").fetchone()["c"]
        if queue_len >= QUEUE_MAX:
            raise HTTPException(503, "La file est pleine, réessaie dans quelques minutes.")

        input_hash = hashlib.sha256(f"{kind}\n{payload.iterations}\n{text}".encode()).hexdigest()
        cached = conn.execute(
            "SELECT * FROM sims WHERE input_hash=? AND status='done' ORDER BY finished DESC LIMIT 1", (input_hash,)
        ).fetchone()

        sim_id = uuid.uuid4().hex[:20]
        sim_dir = REPORTS_DIR / sim_id
        sim_dir.mkdir(parents=True, exist_ok=True)
        input_file = sim_dir / "input.simc"
        input_file.write_text(text)

        if cached:
            conn.execute(
                """INSERT INTO sims (id, created, ip, label, iterations, status, input_hash, input_file, cached_from,
                                     dps, dps_error_pct, wall_s, report_html, report_json, started, finished,
                                     user_email, user_name, kind, weights)
                   VALUES (?,?,?,?,?, 'done', ?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sim_id, now, ip, label, payload.iterations, input_hash, str(input_file), cached["id"],
                 cached["dps"], cached["dps_error_pct"], cached["wall_s"], cached["report_html"], cached["report_json"],
                 now, now, user["email"], user["name"], kind, cached["weights"]),
            )
            return {"id": sim_id, "status": "done", "cached": True, "warnings": warnings}

        conn.execute(
            """INSERT INTO sims (id, created, ip, label, iterations, status, input_hash, input_file, user_email, user_name, kind)
               VALUES (?,?,?,?,?, 'queued', ?, ?, ?, ?, ?)""",
            (sim_id, now, ip, label, payload.iterations, input_hash, str(input_file), user["email"], user["name"], kind),
        )
        return {"id": sim_id, "status": "queued", "cached": False, "position": queue_len + 1, "warnings": warnings}


BLIZZ_LOADOUT = "Talents actuels (Blizzard)"


def _spec_token(name: str) -> str:
    """« Beast Mastery » -> « beast_mastery » (nom de spé anglais -> jeton SimC)."""
    return re.sub(r"[^a-z]+", "_", (name or "").lower()).strip("_")


def _blizz_talents_for(parsed: dict) -> str | None:
    """Code de talents en jeu (API Blizzard) pour la spé de l'export, ou None."""
    name = (parsed.get("name") or "").strip()
    realm = (parsed.get("server") or bnet.GUILD_REALM or "").strip().lower().replace("_", "-")
    if not name or not realm or not parsed.get("spec"):
        return None
    try:
        t, _ts = bnet.talents(realm, name, locale="en_US")   # noms de spé anglais = jetons SimC
    except bnet.BnetError:
        return None
    for lo in t.get("loadouts") or []:
        if _spec_token(lo.get("spec") or "") == parsed["spec"] and lo.get("code"):
            return lo["code"]
    return None


@app.get("/api/stuff/profile/{pid}")
def stuff_profile(pid: int, request: Request):
    """Résumé d'un export : classe/spé, builds détectés, nb de pièces."""
    _require_user(request)
    with _db_lock, _db() as conn:
        prof = conn.execute("SELECT id, name, input FROM profiles WHERE id=?", (pid,)).fetchone()
    if prof is None:
        raise HTTPException(404, "Profil introuvable.")
    parsed = _stuff_parse_export(prof["input"])
    blizz = _blizz_talents_for(parsed)
    loadouts = [l["name"] for l in parsed["loadouts"]] + ([BLIZZ_LOADOUT] if blizz else [])
    return {"id": prof["id"], "name": prof["name"], "cls": parsed["cls"], "spec": parsed["spec"],
            "level": parsed["level"], "loadouts": loadouts,
            "talents_changed": bool(blizz and parsed["talents"] and blizz != parsed["talents"]),
            "equipped": len(parsed["equipped"]), "bags": len(parsed["bags"]),
            "heal": parsed["spec"] in HEAL_SPECS,
            "prio_known": f'{parsed["cls"]}/{parsed["spec"]}' in STUFF_HEAL_PRIO,
            "prio_note": (STUFF_HEAL_PRIO.get(f'{parsed["cls"]}/{parsed["spec"]}') or {}).get("note_fr", "")}


class StuffRequest(BaseModel):
    profile_id: int
    loadout: str = ""
    content: str = "raid"                # raid | mplus | delves
    mode: str = "cur"                    # cur | max | bis
    bis: bool = False                    # si vrai, lance le guide BIS
    bis_content: list[str] = Field(default_factory=list, max_length=10)  # contenus filtrés quand mode=bis
    max_rank: bool = False               # compat : équivaut à mode="max" (obsolète)

    @field_validator("bis_content")
    @classmethod
    def _validate_bis_content(cls, v: list[str]) -> list[str]:
        """Valider les valeurs de bis_content : inconnu → 400, dédoublonner, max 5."""
        if not v:
            return v
        # Dédoublonner tout en préservant l'ordre
        seen: set[str] = set()
        unique: list[str] = []
        for item in v:
            if item not in seen:
                seen.add(item)
                unique.append(item)
        # Limite de taille
        if len(unique) > 5:
            raise ValueError("bis_content ne doit pas dépasser 5 valeurs.")
        # Chaque valeur doit être dans BIS_CONTENTS
        invalid = [c for c in unique if c not in BIS_CONTENTS]
        if invalid:
            raise ValueError(f"Valeur(s) inconnue(s) dans bis_content : {invalid}")
        return unique

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, v: dict) -> dict:
        """Normaliser mode/max_rank et valider mode connu."""
        if not isinstance(v, dict):
            return v
        # max_rank=True sans mode → mode="max" (avant de vérifier le mode)
        if "mode" not in v and v.get("max_rank"):
            v["mode"] = "max"
            v["max_rank"] = True
        else:
            mode = v.get("mode", "cur")
            if mode in ("cur", "max"):
                v["mode"] = mode
                v["max_rank"] = mode == "max"
            elif mode == "bis":
                v["mode"] = "bis"
                v["bis"] = True
            else:
                v["mode"] = "cur"
                v["max_rank"] = False
        return v


@app.post("/api/stuff")
def submit_stuff(payload: StuffRequest, request: Request):
    """« Stuff conseillé » : que porter, avec ce qu'on possède, pour un ou plusieurs contenus."""
    user = _require_user(request)
    with _db_lock, _db() as conn:
        prof = conn.execute("SELECT id, name, input FROM profiles WHERE id=?", (payload.profile_id,)).fetchone()
    if prof is None:
        raise HTTPException(404, "Profil introuvable.")
    parsed = _stuff_parse_export(prof["input"])
    if not parsed["bags"]:
        raise HTTPException(400, "Cet export ne contient pas les pièces des sacs — réexporte ton personnage avec l'addon (les sacs sont inclus automatiquement).")
    loadout = next((l for l in parsed["loadouts"] if l["name"] == payload.loadout), None)
    if payload.loadout == BLIZZ_LOADOUT and loadout is None:
        code = _blizz_talents_for(parsed)
        if not code:
            raise HTTPException(400, "Talents Blizzard indisponibles pour ce personnage et cette spé.")
        loadout = {"name": BLIZZ_LOADOUT, "talents": code}
    if payload.loadout and loadout is None:
        raise HTTPException(400, "Build introuvable dans cet export.")
    ip = _client_ip(request)
    now = time.time()

    # Valider les contenus
    if payload.bis:
        # BIS : validation de bis_content (liste) — réutilisée plus bas
        sim_contents = []
    else:
        # Non-BIS : payload.content est une chaîne unique
        if payload.content not in STUFF_CONTENTS:
            raise HTTPException(400, f"Contenu invalide : {payload.content}")
        sim_contents = [payload.content]

    with _db_lock, _db() as conn:
        active = conn.execute("SELECT COUNT(*) AS c FROM sims WHERE user_email=? AND status IN ('queued','running')",
                              (user["email"],)).fetchone()["c"]

    # BIS : une seule simulation combinée (tous les contenus cochés fusionnés)
    if payload.bis:
        if active >= PER_USER_ACTIVE:
            raise HTTPException(429, f"Tu as déjà {active} calcul(s) en attente — patiente un peu.")
        if _stuff_bis_list(parsed["cls"], parsed["spec"]) is None:
            raise HTTPException(400, "Liste BIS pas encore disponible pour cette spécialisation.")
        bis_contents = payload.bis_content if payload.bis_content else ["raid"]
        valid_bis = [c for c in bis_contents if c in BIS_CONTENTS]
        sim_id = uuid.uuid4().hex[:20]
        sim_dir = REPORTS_DIR / sim_id
        sim_dir.mkdir(parents=True, exist_ok=True)
        input_file = sim_dir / "input.simc"
        input_file.write_text("")
        label_parts = [f'{prof["name"]} · BIS']
        for c in valid_bis:
            label_parts.append(BIS_CONTENTS[c]["label_fr"])
        label = " / ".join(label_parts) + \
                (f' · {loadout["name"]}' if loadout else "")
        plan = {"profile_id": prof["id"], "profile_name": prof["name"], "cls": parsed["cls"],
                "spec": parsed["spec"], "loadout": loadout or {}, "content": valid_bis,
                "mode": "bis", "max_rank": False, "bis": True}
        with _db_lock, _db() as conn:
            conn.execute(
                """INSERT INTO sims (id, created, ip, label, iterations, status, input_hash, input_file,
                                     user_email, user_name, kind, plan)
                   VALUES (?,?,?,?,?, 'queued', ?, ?, ?, ?, 'stuff', ?)""",
                (sim_id, now, ip, label[:60], STUFF_ITERATIONS, f"stuff:{sim_id}", str(input_file),
                 user["email"], user["name"], json.dumps(plan)))
        return {"id": sim_id, "status": "queued", "sim_ids": [sim_id],
                "heal": parsed["spec"] in HEAL_SPECS,
                "loadouts": [l["name"] for l in parsed["loadouts"]]}

    # Non-BIS (mode cur ou max) : simuler le contenu sélectionné
    if not sim_contents:
        raise HTTPException(400, "Aucun contenu valide sélectionné.")
    sim_ids = []
    for c in sim_contents:
        with _db_lock, _db() as conn:
            active = conn.execute("SELECT COUNT(*) AS c FROM sims WHERE user_email=? AND status IN ('queued','running')",
                                  (user["email"],)).fetchone()["c"]
            if active >= PER_USER_ACTIVE:
                raise HTTPException(429, f"Tu as déjà {active} calcul(s) en attente — patiente un peu.")
            sim_id = uuid.uuid4().hex[:20]
            sim_dir = REPORTS_DIR / sim_id
            sim_dir.mkdir(parents=True, exist_ok=True)
            input_file = sim_dir / "input.simc"
            input_file.write_text("")
            plan = {"profile_id": prof["id"], "profile_name": prof["name"], "cls": parsed["cls"],
                    "spec": parsed["spec"], "loadout": loadout or {}, "content": c,
                    "mode": payload.mode, "max_rank": payload.max_rank}
            label = f'{prof["name"]} · {STUFF_CONTENTS[c]["label_fr"]}' + \
                    ("" if payload.mode != "max" else " · rang max") + \
                    (f' · {loadout["name"]}' if loadout else "")
            conn.execute(
                """INSERT INTO sims (id, created, ip, label, iterations, status, input_hash, input_file,
                                     user_email, user_name, kind, plan)
                   VALUES (?,?,?,?,?, 'queued', ?, ?, ?, ?, 'stuff', ?)""",
                (sim_id, now, ip, label[:60], STUFF_ITERATIONS, f"stuff:{sim_id}", str(input_file),
                 user["email"], user["name"], json.dumps(plan)))
            sim_ids.append(sim_id)

    return {"id": sim_ids[0] if sim_ids else None, "status": "queued", "sim_ids": sim_ids,
            "heal": parsed["spec"] in HEAL_SPECS,
            "loadouts": [l["name"] for l in parsed["loadouts"]]}


def _parse_weights_json(raw: str | None) -> list | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def _public_row(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "created": r["created"],
        "label": r["label"],
        "user_name": r["user_name"],
        "iterations": r["iterations"],
        "status": r["status"],
        "cached": bool(r["cached_from"]),
        "dps": r["dps"],
        "dps_error_pct": r["dps_error_pct"],
        "wall_s": r["wall_s"],
        "has_report": r["status"] == "done" and bool(r["report_html"]),
        "error": (r["error"] or "")[:300] if r["status"] == "failed" else None,
        "note": ((r["error"] or "")[:300] or None) if r["status"] == "done" else None,
        "kind": (r["kind"] or "dps"),
        "weights": _parse_weights_json(r["weights"]) if r["kind"] == "weights" else None,
        "gear": _parse_weights_json(r["gear"]) if r["kind"] == "gear" else None,
        "group": _parse_weights_json(r["gear"]) if r["kind"] == "group" else None,
    }


@app.get("/api/sims")
def list_sims(request: Request):
    _require_user(request)
    with _db_lock, _db() as conn:
        rows = conn.execute("SELECT * FROM sims ORDER BY created DESC LIMIT 50").fetchall()
    return {"sims": [_public_row(r) for r in rows], "version": VERSION}


@app.get("/api/sims/{sim_id}")
def get_sim(sim_id: str, request: Request):
    _require_user(request)
    loc = _user_locale(request)
    with _db_lock, _db() as conn:
        r = conn.execute("SELECT * FROM sims WHERE id=?", (sim_id,)).fetchone()
    if r is None:
        raise HTTPException(404, "Simulation inconnue")
    d = _public_row(r)
    d["cached_from"] = r["cached_from"]
    if r["kind"] == "gear" and d.get("gear"):
        base = r["dps"] or 0.0
        enriched = []
        for g in d["gear"]:
            m = re.search(r"\[(\w+):(\d+)\]$", g.get("name") or "")
            slot, item_id = (m.group(1), int(m.group(2))) if m else (None, None)
            label = re.sub(r"\s*\[[^\]]*\]\s*$", "", g.get("name") or "")
            info = {}
            if item_id:
                try:
                    info = bnet.item(item_id, locale=loc)
                except bnet.BnetError:
                    info = {}
            dps = float(g.get("dps") or 0.0)
            enriched.append({
                "label": label,
                "slot": slot,
                "slot_fr": bnet.slot_label(slot, loc),
                "item_id": item_id,
                "name": info.get("name") or label,
                "icon": info.get("icon"),
                "quality": info.get("quality") or "COMMON",
                "dps": dps,
                "err_pct": round(100 * (float(g.get("err") or 0.0)) / dps, 2) if dps else None,
                "delta": (dps - base) if base else None,
                "delta_pct": round(100 * (dps - base) / base, 2) if base else None,
            })
        enriched.sort(key=lambda e: e["dps"], reverse=True)
        d["gear"] = enriched
        d["gear_base_dps"] = base
    if r["kind"] == "stuff":
        st = _parse_weights_json(r["gear"])
        if st:
            for e in st.get("slots") or []:
                e["slot_fr"] = bnet.slot_label(e.get("slot"), loc)
            for e in st.get("items") or []:
                e["slot_fr"] = bnet.slot_label(e.get("slot"), loc)
        d["stuff"] = st
        if st and st.get("mode") == "bis":
            d["stuff"]["slot_fr"] = {k: bnet.slot_label(k, loc) for k in STUFF_SLOTS}
    return d


@app.get("/reports/{sim_id}/report.html")
def report_html(sim_id: str):
    with _db_lock, _db() as conn:
        r = conn.execute("SELECT report_html FROM sims WHERE id=?", (sim_id,)).fetchone()
    if r is None or not r["report_html"] or not Path(r["report_html"]).exists():
        raise HTTPException(404, "Rapport introuvable")
    return FileResponse(r["report_html"], media_type="text/html")


@app.get("/reports/{sim_id}/report.json")
def report_json(sim_id: str):
    with _db_lock, _db() as conn:
        r = conn.execute("SELECT report_json FROM sims WHERE id=?", (sim_id,)).fetchone()
    if r is None or not r["report_json"] or not Path(r["report_json"]).exists():
        raise HTTPException(404, "Rapport introuvable")
    return FileResponse(r["report_json"], media_type="application/json")


# ---------------------------------------------------------------------------
# Battle.net API — roster de guilde & personnages (cache serveur 30 min)
# ---------------------------------------------------------------------------
from app.core.util import _valid_char  # noqa: E402  (app/core/util.py)


def _bnet_call(fn, realm: str, name: str, refresh: int = 0, locale: str | None = None) -> dict:
    _valid_char(realm, name)
    try:
        data, ts = fn(realm, name, force=bool(refresh), locale=locale)
    except bnet.BnetError as exc:
        raise HTTPException(exc.status if exc.status in (400, 404) else 502, str(exc))
    data = dict(data)
    if data.get("class"):
        data["class_key"] = CLASS_KEY_FR.get(data["class"]) or re.sub(r"\s+", "", data["class"])
    data["fetched_at"] = ts
    return data


@app.get("/api/roster")
def api_roster(request: Request, refresh: int = 0):
    _require_user(request)
    try:
        data, ts = bnet.roster(force=bool(refresh))
    except bnet.BnetError as exc:
        raise HTTPException(exc.status if exc.status in (400, 404) else 502, str(exc))
    return {
        "guild": data["guild"],
        "realm": data["realm"],
        "region": data["region"],
        "members": data["members"],
        "count": len(data["members"]),
        "fetched_at": ts,
    }


@app.get("/api/char/{realm}/{name}/summary")
def api_char_summary(realm: str, name: str, request: Request, refresh: int = 0):
    _require_user(request)
    return _bnet_call(bnet.character, realm, name, refresh, locale=_user_locale(request))


@app.get("/api/char/{realm}/{name}/extras")
def api_char_extras(realm: str, name: str, request: Request, refresh: int = 0):
    _require_user(request)
    return _bnet_call(bnet.extras, realm, name, refresh, locale=_user_locale(request))


@app.get("/api/char/{realm}/{name}/talents")
def api_char_talents(realm: str, name: str, request: Request, refresh: int = 0):
    _require_user(request)
    return _bnet_call(bnet.talents, realm, name, refresh, locale=_user_locale(request))


@app.get("/api/char/{realm}/{name}/raids-progress")
def api_char_raids_progress(realm: str, name: str, request: Request, refresh: int = 0):
    _require_user(request)
    return _bnet_call(bnet.raid_progress, realm, name, refresh, locale=_user_locale(request))


@app.get("/api/char/{realm}/{name}/dungeons-progress")
def api_char_dungeons_progress(realm: str, name: str, request: Request, refresh: int = 0):
    _require_user(request)
    return _bnet_call(bnet.dungeon_progress, realm, name, refresh, locale=_user_locale(request))


# ---- Progression de la guilde (v2026.09.154) ----
_GPROG: dict = {}
_GPROG_TTL = 900.0
_gprog_lock = threading.Lock()


def _guild_progress_aggregate(results: list, names: dict, mains: set) -> dict:
    """[(membre roster, progression bnet|None)] -> boss × difficulté × membres, et résumé par membre.

    Blizzard ne liste que les boss tués : l'ordre et les boss manquants viennent du journal (`names`).
    """
    order = (names or {}).get("order") or {}
    enc_names = (names or {}).get("enc") or {}
    insts: dict = {}
    diffs: dict = {}
    members = []
    expansion = ""
    for m, prog in results:
        if not prog:
            continue
        expansion = expansion or prog.get("expansion") or ""
        done: dict = {}
        for ins in prog.get("instances") or []:
            iid = ins.get("id")
            it = insts.setdefault(iid, {"id": iid, "name": ins.get("name") or "?", "total": 0, "bosses": {}})
            for mo in ins.get("modes") or []:
                d = mo.get("difficulty") or ""
                diffs.setdefault(d, mo.get("label") or d)
                it["total"] = max(it["total"], int(mo.get("total") or 0))
                done[d] = done.get(d, 0) + int(mo.get("done") or 0)
                for b in mo.get("bosses") or []:
                    bb = it["bosses"].setdefault(b.get("id"), {"id": b.get("id"), "name": b.get("name") or "?",
                                                               "kills": {}})
                    bb["kills"].setdefault(d, []).append(m["name"])
        members.append({"name": m["name"], "realm": m.get("realm") or "",
                        "main": (m["name"] or "").lower() in mains, "done": done})
    rank = {d: i for i, d in enumerate(bnet.DIFF_ORDER)}
    out_insts = []
    for iid, it in insts.items():
        ids = list((order.get(iid) or order.get(str(iid)) or []))
        ids += [bid for bid in it["bosses"] if bid not in ids]
        bosses = [it["bosses"].get(bid) or {"id": bid, "name": enc_names.get(bid) or "?", "kills": {}}
                  for bid in ids]
        out_insts.append({"id": iid, "name": it["name"], "total": max(it["total"], len(ids)),
                          "bosses": bosses})
    grand = sum(i["total"] for i in out_insts)
    for mm in members:
        best = None
        for d, n in mm["done"].items():
            if n > 0 and (best is None or rank.get(d, 99) > rank.get(best, 99)):
                best = d
        mm["best"] = ({"difficulty": best, "label": diffs.get(best, best), "done": mm["done"][best],
                       "total": grand} if best else None)
    members.sort(key=lambda x: (-(rank.get((x["best"] or {}).get("difficulty"), -1)),
                                -((x["best"] or {}).get("done") or 0), x["name"].lower()))
    diff_list = [{"difficulty": d, "label": diffs[d]}
                 for d in sorted(diffs, key=lambda d: rank.get(d, 99)) if d]
    return {"expansion": expansion, "diffs": diff_list, "instances": out_insts, "members": members}


def _guild_progress(kind: str, locale: str, force: bool = False) -> dict:
    """Agrège la progression des membres de niveau max du roster (cache 15 min)."""
    key = f"{kind}/{locale}"
    with _gprog_lock:
        hit = _GPROG.get(key)
    if hit and not force and time.time() - hit["ts"] < _GPROG_TTL:
        return hit["data"]
    roster, _ts = bnet.roster()
    mem = [m for m in roster.get("members") or [] if m.get("name")]
    top = max((int(m.get("level") or 0) for m in mem), default=0)
    mem = [m for m in mem if int(m.get("level") or 0) == top]
    with _db_lock, _db() as conn:
        mains = {str(r["name"] or "").lower()
                 for r in conn.execute("SELECT name FROM char_links WHERE is_main=1").fetchall()}
    fn = bnet.raid_progress if kind == "raid" else bnet.dungeon_progress

    def one(m):
        try:
            return m, fn(m.get("realm") or bnet.GUILD_REALM, m["name"], locale=locale)[0]
        except bnet.BnetError:
            return m, None

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, mem))
    # donjons : Blizzard ne suit que le dernier boss -> pas de liste de boss du journal
    names = bnet.journal_names(locale) if kind == "raid" else {}
    data = _guild_progress_aggregate(results, names, mains)
    data.update({"kind": kind, "level": top, "scanned": len(mem), "fetched_at": time.time()})
    with _gprog_lock:
        _GPROG[key] = {"ts": time.time(), "data": data}
    return data


@app.get("/api/guild/progress")
def api_guild_progress(request: Request, kind: str = "raid", refresh: int = 0):
    """Progression de la guilde (membres de niveau max) : boss × difficulté, meilleure progression par membre."""
    _require_user(request)
    if kind not in ("raid", "dungeon"):
        raise HTTPException(400, "kind doit valoir raid ou dungeon.")
    try:
        return _guild_progress(kind, _user_locale(request), force=bool(refresh))
    except bnet.BnetError as exc:
        raise HTTPException(exc.status if exc.status in (400, 404) else 502, str(exc))


@app.get("/api/char/{realm}/{name}/equipment")
def api_char_equipment(realm: str, name: str, request: Request, refresh: int = 0):
    _require_user(request)
    return _bnet_call(bnet.equipment, realm, name, refresh, locale=_user_locale(request))


# ---------------------------------------------------------------------------
# WCL API — rapports de raid (cache serveur)
# ---------------------------------------------------------------------------
_WCL_CODE_RE = re.compile(r"^[A-Za-z0-9]{12,24}$")


@app.get("/api/wcl/reports")
def api_wcl_reports(request: Request, refresh: int = 0, limit: int = 30):
    _require_user(request)
    try:
        data, ts = wcl.reports(limit=min(max(limit, 5), 50), force=bool(refresh))
    except wcl.WclError as exc:
        raise HTTPException(exc.status if exc.status in (400, 404) else 502, str(exc))
    return {"reports": data, "fetched_at": ts}


@app.get("/api/wcl/report/{code}")
def api_wcl_report(code: str, request: Request, refresh: int = 0):
    _require_user(request)
    if not _WCL_CODE_RE.match(code):
        raise HTTPException(400, "Code de rapport invalide.")
    try:
        data, ts = wcl.report_full(code, force=bool(refresh))
    except wcl.WclError as exc:
        raise HTTPException(exc.status if exc.status in (400, 404) else 502, str(exc))
    data = dict(data)
    data["fetched_at"] = ts
    return data


@app.get("/api/compare")
def api_compare(request: Request, chars: str = "", refresh: int = 0):
    _require_user(request)
    items = [c.strip() for c in chars.split(",") if c.strip()][:6]
    out = []
    for item in items:
        realm, _, name = item.partition(":")
        realm, name = realm.strip(), name.strip()
        if not realm or not name:
            continue
        _valid_char(realm, name)
        entry: dict = {"realm": realm.lower(), "name": name}
        try:
            summary, _ts = bnet.character(realm, name, force=bool(refresh), locale=_user_locale(request))
            if summary.get("class"):
                summary = dict(summary)
                summary["class_key"] = CLASS_KEY_FR.get(summary["class"]) or re.sub(r"\s+", "", summary["class"])
            entry["summary"] = summary
        except bnet.BnetError as exc:
            entry["summary"] = None
            entry["bnet_error"] = str(exc)
        try:
            zr, _ts = wcl.character_rankings(realm, name, force=bool(refresh))
            entry["wcl"] = zr
        except wcl.WclError as exc:
            entry["wcl"] = None
            entry["wcl_error"] = str(exc)
        out.append(entry)
    return {"chars": out, "zone_id": wcl.RAID_ZONE_ID, "zone_label": wcl.zone_label()}


# ---------------------------------------------------------------------------
# Profils de simulation (exports /simc sauvegardés, partageables guilde)
# ---------------------------------------------------------------------------
MAX_PROFILES = 20


class ProfileRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    input: str = Field(..., min_length=30, max_length=MAX_INPUT_CHARS)
    shared: bool = False


class ProfilePatch(BaseModel):
    name: str | None = Field(None, max_length=60)
    input: str | None = Field(None, min_length=30, max_length=MAX_INPUT_CHARS)
    shared: bool | None = None


def _profile_public(r: sqlite3.Row, mine: bool) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "user_name": r["user_name"],
        "shared": bool(r["shared"]),
        "updated": r["updated"],
        "size": len(r["input"] or ""),
        "mine": mine,
    }


@app.get("/api/profiles")
def list_profiles(request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        mine = conn.execute(
            "SELECT * FROM profiles WHERE user_email=? ORDER BY updated DESC LIMIT 100", (user["email"],)
        ).fetchall()
        shared = conn.execute(
            "SELECT * FROM profiles WHERE shared=1 AND user_email<>? ORDER BY updated DESC LIMIT 100", (user["email"],)
        ).fetchall()
    return {"mine": [_profile_public(r, True) for r in mine], "shared": [_profile_public(r, False) for r in shared]}


@app.get("/api/profiles/{pid}")
def get_profile(pid: int, request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        r = conn.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
    if r is None:
        raise HTTPException(404, "Profil inconnu.")
    mine = r["user_email"] == user["email"]
    if not mine and not r["shared"]:
        raise HTTPException(403, "Ce profil n'est pas partagé.")
    d = _profile_public(r, mine)
    d["input"] = r["input"]
    return d


@app.post("/api/profiles")
def create_profile(payload: ProfileRequest, request: Request):
    user = _require_user(request)
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Nom de profil requis.")
    text = payload.input.replace("\r\n", "\n").strip()
    _reject_blocked_profile(text)
    now = time.time()
    with _db_lock, _db() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM profiles WHERE user_email=?", (user["email"],)).fetchone()["c"]
        if count >= MAX_PROFILES:
            raise HTTPException(400, f"Limite de {MAX_PROFILES} profils atteinte — supprime-en un d'abord.")
        cur = conn.execute(
            "INSERT INTO profiles (user_email, user_name, name, input, shared, created, updated) VALUES (?,?,?,?,?,?,?)",
            (user["email"], user["name"], name, text, 1 if payload.shared else 0, now, now),
        )
        pid = int(cur.lastrowid or 0)
    return {"id": pid, "ok": True}


@app.patch("/api/profiles/{pid}")
def update_profile(pid: int, payload: ProfilePatch, request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        r = conn.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
        if r is None:
            raise HTTPException(404, "Profil inconnu.")
        if r["user_email"] != user["email"]:
            raise HTTPException(403, "Ce profil n'est pas à toi.")
        name = payload.name.strip() if payload.name is not None else r["name"]
        if not name:
            raise HTTPException(400, "Nom de profil requis.")
        text = payload.input.replace("\r\n", "\n").strip() if payload.input is not None else r["input"]
        if payload.input is not None:
            _reject_blocked_profile(text)
        shared = (1 if payload.shared else 0) if payload.shared is not None else r["shared"]
        conn.execute(
            "UPDATE profiles SET name=?, input=?, shared=?, updated=? WHERE id=?",
            (name, text, shared, time.time(), pid),
        )
    return {"ok": True}


@app.delete("/api/profiles/{pid}")
def delete_profile(pid: int, request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        r = conn.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
        if r is None:
            raise HTTPException(404, "Profil inconnu.")
        if r["user_email"] != user["email"] and not user["is_admin"]:
            raise HTTPException(403, "Ce profil n'est pas à toi.")
        conn.execute("DELETE FROM profiles WHERE id=?", (pid,))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Sim de groupe (profils /simc combinés en une seule simulation multi-acteurs)
# ---------------------------------------------------------------------------
_ACTOR_RE = re.compile(r'^[a-z_]+="[^"]+"\s*$')


def _profile_actor(text: str) -> str | None:
    """Nom du personnage (1re ligne acteur hors commentaires) d'un export /simc, ou None."""
    for ln in (text or "").replace("\r\n", "\n").splitlines():
        st = ln.strip()
        if not st or st.startswith("#"):
            continue
        if _ACTOR_RE.match(st):
            return st.split('"')[1]
        return None
    return None


def _build_group_input(rows: list) -> tuple[str, list[str]]:
    """Combine N exports /simc en un seul fichier multi-acteurs (sim de groupe)."""
    warnings: list[str] = []
    blocks: list[str] = []
    seen: set[str] = set()
    for r in rows:
        text = (r["input"] or "").replace("\r\n", "\n")
        actor = _profile_actor(text)
        if not actor:
            warnings.append(f"{r['name']} : format /simc non reconnu — ignoré.")
            continue
        if actor.lower() in seen:
            warnings.append(f"{r['name']} : {actor} est déjà inclus — doublon ignoré.")
            continue
        seen.add(actor.lower())
        lines: list[str] = []
        started = False
        for ln in text.splitlines():
            st = ln.strip()
            if not started:
                if _ACTOR_RE.match(st):
                    started = True
                else:
                    continue
            lines.append(ln.rstrip())
        blocks.append("\n".join(lines))
    if not blocks:
        raise HTTPException(400, "Aucun profil exploitable dans la sélection.")
    header = (
        "# Sim de groupe — exports /simc combinés\n"
        "fight_style=Patchwerk\n"
        "max_time=300\n"
        "calculate_scale_factors=0\n"
    )
    return header + "\n".join(blocks) + "\n", warnings


class GroupSimRequest(BaseModel):
    ids: list[int]
    iterations: int = 5000
    label: str = ""


@app.post("/api/group/sim")
def submit_group_sim(payload: GroupSimRequest, request: Request):
    user = _require_user(request)
    ids = [int(i) for i in payload.ids][:40]
    if len(ids) < 2:
        raise HTTPException(400, "Sélectionne au moins 2 profils.")
    if payload.iterations not in ITER_CHOICES:
        raise HTTPException(400, f"Valeurs d'itérations acceptées : {', '.join(map(str, ITER_CHOICES))}")
    ip = _client_ip(request)
    now = time.time()
    with _db_lock, _db() as conn:
        ph = ",".join("?" * len(ids))
        rows = conn.execute(f"SELECT * FROM profiles WHERE id IN ({ph})", ids).fetchall()
        allowed = [r for r in rows if r["user_email"] == user["email"] or r["shared"]]
        if len(allowed) != len(rows):
            raise HTTPException(403, "Un des profils n'est pas accessible.")
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM sims WHERE ip=? AND status IN ('queued','running')", (ip,)
        ).fetchone()["c"]
        if active >= PER_IP_ACTIVE:
            raise HTTPException(429, f"Tu as déjà {active} simulation(s) en attente — patiente un peu.")
        user_active = conn.execute(
            "SELECT COUNT(*) AS c FROM sims WHERE user_email=? AND status IN ('queued','running')",
            (user["email"],),
        ).fetchone()["c"]
        if user_active >= PER_USER_ACTIVE:
            raise HTTPException(429, f"Tu as déjà {user_active} simulation(s) en attente — patiente un peu.")
        last_ts = conn.execute("SELECT MAX(created) AS m FROM sims WHERE ip=?", (ip,)).fetchone()["m"]
        if last_ts and now - last_ts < PER_IP_COOLDOWN_S:
            wait = int(PER_IP_COOLDOWN_S - (now - last_ts)) + 1
            raise HTTPException(429, f"Doucement ! Réessaie dans {wait} s.")
        queue_len = conn.execute("SELECT COUNT(*) AS c FROM sims WHERE status IN ('queued','running')").fetchone()["c"]
        if queue_len >= QUEUE_MAX:
            raise HTTPException(503, "La file est pleine, réessaie dans quelques minutes.")
        text, warnings = _build_group_input(allowed)
        if len(text) > MAX_INPUT_CHARS:
            raise HTTPException(400, "Profils trop volumineux pour une sim combinée — retire quelques profils.")
        _reject_blocked_profile(text)
        input_hash = hashlib.sha256(f"group\n{payload.iterations}\n{text}".encode()).hexdigest()
        cached = conn.execute(
            "SELECT * FROM sims WHERE input_hash=? AND status='done' ORDER BY finished DESC LIMIT 1", (input_hash,)
        ).fetchone()
        sim_id = uuid.uuid4().hex[:20]
        sim_dir = REPORTS_DIR / sim_id
        sim_dir.mkdir(parents=True, exist_ok=True)
        input_file = sim_dir / "input.simc"
        input_file.write_text(text)
        label = payload.label.strip()[:60] or f"Sim de groupe ({len(allowed)} profils)"
        if cached:
            conn.execute(
                """INSERT INTO sims (id, created, ip, label, iterations, status, input_hash, input_file, cached_from,
                                     dps, dps_error_pct, wall_s, report_html, report_json, started, finished,
                                     user_email, user_name, kind, gear)
                   VALUES (?,?,?,?,?, 'done', ?,?,?,?,?,?,?,?,?,?,?,?,'group',?)""",
                (sim_id, now, ip, label, payload.iterations, input_hash, str(input_file), cached["id"],
                 None, None, cached["wall_s"], cached["report_html"], cached["report_json"],
                 now, now, user["email"], user["name"], cached["gear"]),
            )
            return {"id": sim_id, "status": "done", "cached": True, "warnings": warnings, "count": len(allowed)}
        conn.execute(
            "INSERT INTO sims (id, created, ip, label, iterations, status, input_hash, input_file, user_email, user_name, kind) "
            "VALUES (?,?,?,?,?, 'queued', ?,?,?,?, 'group')",
            (sim_id, now, ip, label, payload.iterations, input_hash, str(input_file), user["email"], user["name"]),
        )
    return {"id": sim_id, "status": "queued", "position": queue_len + 1, "warnings": warnings, "count": len(allowed)}


# ---------------------------------------------------------------------------
# Informations de guilde (page 🛡️ Guilde — éditable par les administrateurs)
# ---------------------------------------------------------------------------
GUILD_INFO_KEYS = ("intro", "discord_url", "discord_note", "ts_host", "ts_password", "ts_note", "web_url", "web_note")


@app.get("/api/guild/info")
def api_guild_info(request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        rows = conn.execute("SELECT key, value, updated FROM guild_info").fetchall()
    items = {r["key"]: r["value"] for r in rows}
    updated = max(((r["updated"] or 0.0) for r in rows), default=0.0)
    return {"items": items, "updated": updated, "can_edit": _user_role(user) == "admin"}


class GuildInfoRequest(BaseModel):
    items: dict[str, str]


@app.post("/api/guild/info")
def update_guild_info(payload: GuildInfoRequest, request: Request):
    user = _require_admin(request)
    now = time.time()
    saved = 0
    with _db_lock, _db() as conn:
        for k, v in (payload.items or {}).items():
            if k not in GUILD_INFO_KEYS or not isinstance(v, str):
                continue
            conn.execute(
                "UPDATE guild_info SET value=?, updated=?, updated_by=? WHERE key=?",
                (v.strip()[:2000], now, user["name"] or user["email"], k),
            )
            saved += 1
    return {"ok": True, "saved": saved}


# ---------------------------------------------------------------------------
# Classements de guilde (parses récents + clés M+ des mains liés)
# ---------------------------------------------------------------------------
_LB_CACHE: dict = {"ts": 0.0, "data": None}
LB_TTL = 1800.0


def _build_leaderboard() -> dict:
    """Agrège les parses des derniers rapports WCL + le rating M+ des mains liés."""
    data: dict = {"parses": [], "mplus": [], "reports": 0, "built": time.time()}
    try:
        rl, _ts = wcl.reports(limit=8)
        rows: list[dict] = []
        count = 0
        for rep in (rl.get("data") or []):
            code = rep.get("code")
            try:
                full, _t = wcl.report_full(code)
            except wcl.WclError:
                continue
            ranks = full.get("rankings") or {}
            if not ranks:
                continue
            count += 1
            fights = {str(f.get("id")): f for f in (full["report"].get("fights") or [])}
            for fid, entry in ranks.items():
                if not entry.get("kill"):
                    continue
                f = fights.get(str(fid)) or {}
                boss = (entry.get("encounter") or {}).get("name") or f.get("name") or "?"
                diff = entry.get("difficulty") or f.get("difficulty") or 0
                for role in ("dps", "tanks", "healers"):
                    for c in (((entry.get("roles") or {}).get(role) or {}).get("characters") or []):
                        if c.get("rankPercent") is None or not c.get("name"):
                            continue
                        rows.append({
                            "name": c.get("name"), "class": c.get("class"), "spec": c.get("spec"),
                            "amount": c.get("amount"), "percent": c.get("rankPercent"),
                            "boss": boss, "role": role, "difficulty": diff,
                            "report": code, "date": full["report"].get("startTime"),
                        })
        rows.sort(key=lambda r: (r.get("percent") or 0), reverse=True)
        data["parses"] = rows[:500]
        data["reports"] = count
    except wcl.WclError as exc:
        data["error"] = str(exc)
    try:
        with _db_lock, _db() as conn:
            mains = conn.execute(
                "SELECT realm, name, display FROM char_links WHERE is_main=1"
            ).fetchall()
        mplus = []
        seen_names: set = set()
        for m in mains[:40]:
            key = (m["name"] or "").lower()
            if not key or key in seen_names:
                continue
            seen_names.add(key)
            try:
                mk, _t = bnet.mystic_rating(m["realm"], m["name"])
            except bnet.BnetError:
                continue
            if mk.get("rating"):
                mplus.append({"name": m["display"] or m["name"], "rating": mk["rating"]})
        mplus.sort(key=lambda r: r["rating"], reverse=True)
        data["mplus"] = mplus
    except Exception:  # noqa: BLE001
        pass
    return data


def _progression_data(days: int = 30, locale: str = "fr_FR") -> dict:
    """Classement des progressions (relevés quotidiens) + courbe iLvl moyen.

    Fenêtre glissante de « days » jours (7 ou 30). Les gains comparent le premier et
    le dernier relevé de chaque personnage dans la fenêtre.
    """
    days = 7 if int(days) == 7 else 30
    want_en = locale.startswith("en")
    cutoff = _snap_day(time.time() - (days - 1) * 86400)
    with _db_lock, _db() as conn:
        rows = conn.execute(
            "SELECT realm, name, day, data FROM char_snapshots WHERE day >= ? ORDER BY name, day",
            (cutoff,),
        ).fetchall()
    per: dict[tuple, list] = {}
    for r in rows:
        try:
            d = json.loads(r["data"])
        except (ValueError, TypeError):
            continue
        per.setdefault((r["realm"], r["name"]), []).append((r["day"], d))

    def gain(f: dict, l: dict, key: str):
        a, b = f.get(key), l.get(key)
        return (b - a) if (a is not None and b is not None) else None

    out_rows, measured = [], 0
    day_ilvl: dict[str, dict] = {}      # jour -> {(royaume, nom): iLvl} (persos niveau 90)
    for (realm, name), snaps in per.items():
        snaps.sort(key=lambda t: t[0])
        if len(snaps) >= 2:
            measured += 1
        for day, d in snaps:
            lvl = d.get("level")
            if d.get("ilvl") is not None and (lvl is None or lvl >= 90):
                day_ilvl.setdefault(day, {})[(realm, name)] = d["ilvl"]
        if len(snaps) < 2:
            continue
        fd, f = snaps[0]
        ld, l = snaps[-1]
        gains = {k: gain(f, l, k) for k in ("ilvl", "achv", "mounts", "pets", "mplus")}
        if not any(v is not None and v != 0 for v in gains.values()):
            continue
        out_rows.append({
            "realm": realm, "name": name,
            "class": _pick(l, "class", want_en) or _pick(f, "class", want_en),
            "spec": _pick(l, "spec", want_en) or _pick(f, "spec", want_en),
            "class_key": CLASS_KEY_FR.get((l.get("class") or f.get("class")) or ""),
            "first_day": fd, "last_day": ld,
            "ilvl0": f.get("ilvl"), "ilvl1": l.get("ilvl"),
            "d_ilvl": gains["ilvl"], "d_achv": gains["achv"], "d_mounts": gains["mounts"],
            "d_pets": gains["pets"], "d_mplus": gains["mplus"],
        })
    out_rows.sort(key=lambda r: (r["d_ilvl"] if r["d_ilvl"] is not None else -10**6,
                                 r["d_achv"] if r["d_achv"] is not None else -10**6), reverse=True)

    # courbe : population constante (présente au 1er ET au dernier jour éligibles) — moyenne comparable
    days_sorted = sorted(day_ilvl)
    curve: list = []
    pop: set = set()
    base = next((d for d in days_sorted if len(day_ilvl[d]) >= 5), None)
    if base:
        end = days_sorted[-1]
        pop = set(day_ilvl[base]) & set(day_ilvl[end])
        if len(pop) >= 3:
            for day in days_sorted:
                vals = [v for k, v in day_ilvl[day].items() if k in pop]
                if len(vals) >= 3:
                    curve.append({"day": day, "avg": round(sum(vals) / len(vals), 1), "n": len(vals)})
        else:
            pop = set()
    return {"days": days, "built": time.time(), "rows": out_rows, "curve": curve,
            "measured": measured, "progressed": len(out_rows), "curve_pop": len(pop)}


@app.get("/api/progression")
def api_progression(request: Request, days: int = 30):
    """Classement des progressions (relevés quotidiens) + courbe iLvl moyen (7 ou 30 j)."""
    _require_user(request)
    return _progression_data(days, _user_locale(request))


from app.core.util import CLASS_KEY_FR, _pick  # noqa: E402  (app/core/util.py)


# Assiduité aux soirées de raid (Warcraft Logs) — app/routers/attendance.py
from app.routers import attendance as _attendance_router  # noqa: E402

app.include_router(_attendance_router.router)


# Spécialisations (noms FR renvoyés par l'API) → rôle : tank / heal / dps.
SPEC_ROLE = {
    "Sang": "tank", "Vengeance": "tank", "Gardien": "tank", "Maître brasseur": "tank", "Protection": "tank",
    "Restauration": "heal", "Sacré": "heal", "Discipline": "heal", "Tisse-brume": "heal", "Préservation": "heal",
    "Givre": "dps", "Impie": "dps", "Dévastation": "dps", "Équilibre": "dps", "Farouche": "dps",
    "Augmentation": "dps", "Maîtrise des bêtes": "dps", "Précision": "dps", "Survie": "dps",
    "Arcanes": "dps", "Feu": "dps", "Marche-vent": "dps", "Vindicte": "dps", "Ombre": "dps",
    "Assassinat": "dps", "Hors-la-loi": "dps", "Finesse": "dps", "Élémentaire": "dps",
    "Amélioration": "dps", "Affliction": "dps", "Démonologie": "dps", "Destruction": "dps",
    "Armes": "dps", "Fureur": "dps", "Dévoration": "dps",
}


@app.get("/api/avail")
def api_avail(request: Request, hours: int = 24):
    """Vu dernièrement : persos niveau max vus récemment (relevé du jour), groupés par rôle."""
    _require_user(request)
    want_en = _user_locale(request).startswith("en")
    hours = hours if hours in (24, 48, 168) else 24
    cutoff = time.time() - hours * 3600
    with _db_lock, _db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT realm, name, data FROM char_snapshots WHERE day = ?", (_snap_day(),)).fetchall()]
    disp: dict[str, str] = {}
    try:
        roster, _t = bnet.roster()
        for m in (roster.get("members") or []):
            k = (m.get("name") or "").lower()
            if k:
                disp[k] = m.get("name") or k
    except bnet.BnetError:
        pass
    out = []
    for r in rows:
        try:
            d = json.loads(r["data"]) or {}
        except (ValueError, TypeError):
            continue
        lvl = d.get("level")
        if lvl is not None and lvl < 90:
            continue
        if d.get("ilvl") is None:
            continue
        seen = d.get("last_login")
        seen_s = (seen / 1000) if seen else None
        if seen_s is not None and seen_s < cutoff:
            continue
        k = r["name"]
        out.append({
            "name": disp.get(k) or k, "key": k, "realm": r["realm"],
            "class_key": CLASS_KEY_FR.get(d.get("class") or ""),
            "spec": _pick(d, "spec", want_en), "role": SPEC_ROLE.get(d.get("spec") or ""),
            "ilvl": d.get("ilvl"), "level": lvl, "seen": seen_s,
        })
    out.sort(key=lambda x: -(x.get("ilvl") or 0))
    return {"hours": hours, "built": time.time(), "rows": out}


PROF_ORDER = ["Alchimie", "Calligraphie", "Couture", "Dépeçage", "Enchantement", "Forge",
              "Herboristerie", "Ingénierie", "Joaillerie", "Minéralogie", "Travail du cuir",
              "Archéologie", "Cuisine", "Pêche"]
from app.services.crafting import PROF_EN, _prof_store  # noqa: E402  (app/services/crafting.py)


@app.get("/api/craft")
def api_craft(request: Request):
    """Annuaire d'artisanat : qui peut crafter quoi (métiers de tout le roster)."""
    _require_user(request)
    want_en = _user_locale(request).startswith("en")
    with _db_lock, _db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT realm, name, ts, data FROM char_professions").fetchall()]
        cls_by_name: dict[str, str] = {}
        for row in conn.execute("SELECT name, data FROM char_snapshots WHERE day = ?", (_snap_day(),)).fetchall():
            try:
                c = (json.loads(row["data"]) or {}).get("class")
            except (ValueError, TypeError):
                c = None
            if c:
                cls_by_name[row["name"]] = c
    disp: dict[str, str] = {}
    realms: dict[str, str] = {}
    try:
        roster, _t = bnet.roster()
        for m in (roster.get("members") or []):
            k = (m.get("name") or "").lower()
            if k:
                disp[k] = m.get("name") or k
                realms[k] = m.get("realm") or bnet.GUILD_REALM
    except bnet.BnetError:
        pass
    groups: dict[str, list] = {}
    with_profs = 0
    for r in rows:
        try:
            d = json.loads(r["data"]) or {}
        except (ValueError, TypeError):
            continue
        profs = d.get("profs") or []
        if not profs:
            continue
        with_profs += 1
        k = r["name"]
        for p in profs:
            nm = (_pick(p, "name", want_en)) or "?"
            if want_en:
                nm = PROF_EN.get(nm, nm)  # lignes pas encore rafraîchies : libellé FR → nom anglais
            groups.setdefault(nm, []).append({
                "name": disp.get(k) or k, "key": k,
                "realm": realms.get(k) or r["realm"],
                "class_key": CLASS_KEY_FR.get(cls_by_name.get(k) or ""),
                "points": p.get("points"), "max": p.get("max"), "tier": p.get("tier"),
            })
    order = {n: i for i, n in enumerate(
        [PROF_EN.get(x, x) if want_en else x for x in PROF_ORDER])}
    profs_out = []
    for nm, lst in groups.items():
        lst.sort(key=lambda x: (-(x.get("points") or 0), (x["name"] or "").lower()))
        profs_out.append({"name": nm, "members": lst})
    profs_out.sort(key=lambda g: (order.get(g["name"], 99), g["name"]))
    return {"built": time.time(), "chars": len(rows), "with_profs": with_profs, "professions": profs_out}


@app.get("/api/leaderboard")
def api_leaderboard(request: Request, refresh: int = 0):
    _require_user(request)
    now = time.time()
    with _db_lock:
        cached = _LB_CACHE["data"]
        age = now - _LB_CACHE["ts"]
    if cached is not None and ((not refresh and age < LB_TTL) or (refresh and age < 60)):
        return cached
    data = _build_leaderboard()
    with _db_lock:
        _LB_CACHE["ts"] = time.time()
        _LB_CACHE["data"] = data
    return data


# ---------------------------------------------------------------------------
# Wishlist (pièces à obtenir + gains)
# ---------------------------------------------------------------------------
@app.api_route("/manifest.webmanifest", methods=["GET", "HEAD"])
def pwa_manifest(request: Request):
    """Manifeste PWA dynamique : nom, nom court et icône suivent l'identité de la guilde."""
    short, name = "Cohors", ""
    try:
        with _db_lock, _db() as conn:
            row = _brand_row(conn)
        short = (row["guild_short"] or "Cohors").strip()[:24] or "Cohors"
        name = (row["guild_name"] or "").strip()[:60]
    except sqlite3.Error:
        pass
    icons = []
    if _brand_files().get("logo"):
        icons.append({"src": "/branding/logo", "sizes": "any", "type": "image/png", "purpose": "any"})
    icons += [
        {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ]
    man = {
        "name": f"{short} — {name}" if name else "Cohors — Compagnon de guilde",
        "short_name": short,
        "description": "Compagnon de guilde World of Warcraft — simulations, roster, raids, artisanat et suivi.",
        "lang": "fr",
        "start_url": "/dashboard",
        "scope": "/",
        "display": "standalone",
        "background_color": "#0b0f17",
        "theme_color": "#b1002e",
        "icons": icons,
    }
    return Response(json.dumps(man, ensure_ascii=False), media_type="application/manifest+json")


@app.api_route("/sw.js", methods=["GET", "HEAD"])
def pwa_sw(request: Request):
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.api_route("/offline.html", methods=["GET", "HEAD"])
def pwa_offline(request: Request):
    return FileResponse(STATIC_DIR / "offline.html")


@app.api_route("/voice", methods=["GET", "HEAD"])
def voice_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "voice.html")


@app.api_route("/fun", methods=["GET", "HEAD"])
def fun_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "fun.html")


@app.api_route("/wishlist", methods=["GET", "HEAD"])
def wishlist_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "wishlist.html")


# Wishlist — app/services/wishlist.py (helpers partagés) et app/routers/wishlist.py (routes)
from app.services.wishlist import _recipe_wish_key  # noqa: E402


from app.routers import wishlist as _wishlist_router  # noqa: E402

app.include_router(_wishlist_router.router)


# ---------------------------------------------------------------------------
# Succès fun (palmarès rigolo de la guilde)
# ---------------------------------------------------------------------------
_FUN_CACHE: dict = {"ts": 0.0, "data": None}
FUN_TTL = 1800.0


def _build_fun() -> dict:
    """Agrège le palmarès fun : morts WCL + stats internes (sims, présences, partage)."""
    out: dict = {
        "cemetery": [], "massacre": None, "first_blood": [], "cause": None,
        "intouchables": [], "scholars": [], "pillars": [], "collectors": [], "hearts": [],
        "built": time.time(),
    }
    try:
        rl, _ts = wcl.reports(limit=6)
        cemetery: dict = {}
        firsts: dict = {}
        participation: dict = {}
        causes: dict = {}
        massacre = None
        for rep in (rl.get("data") or []):
            code = rep.get("code")
            try:
                full, _t = wcl.report_full(code)
            except wcl.WclError:
                continue
            fights = {f["id"]: f for f in (full["report"].get("fights") or [])}
            if not fights:
                continue
            try:
                death_rows, _t2 = wcl.deaths(code)
            except wcl.WclError:
                death_rows = []
            per_fight: dict = {}
            first_ts: dict = {}
            for de in death_rows:
                nm = de.get("name")
                if not nm:
                    continue
                row = cemetery.setdefault(nm, {"name": nm, "class": de.get("class"),
                                               "spec": de.get("spec"), "deaths": 0})
                row["deaths"] += 1
                fid = de.get("fight")
                per_fight[fid] = per_fight.get(fid, 0) + 1
                ts = de.get("timestamp")
                if ts is not None and (fid not in first_ts or ts < first_ts[fid][1]):
                    first_ts[fid] = (nm, ts)
                killer = de.get("killer")
                if killer:
                    causes[killer] = causes.get(killer, 0) + 1
            for nm, _t3 in first_ts.values():
                firsts[nm] = firsts.get(nm, 0) + 1
            for fid, n in per_fight.items():
                if not massacre or n > massacre["deaths"]:
                    f = fights.get(fid) or {}
                    massacre = {"boss": f.get("name") or "?", "deaths": n, "kill": bool(f.get("kill")),
                                "report": code, "date": full["report"].get("startTime")}
            for _fid, entry in (full.get("rankings") or {}).items():
                if not entry.get("kill"):
                    continue
                for role in ("dps", "tanks", "healers"):
                    for c in (((entry.get("roles") or {}).get(role) or {}).get("characters") or []):
                        nm = c.get("name")
                        if nm:
                            participation[nm] = participation.get(nm, 0) + 1
        out["cemetery"] = sorted(cemetery.values(), key=lambda r: -r["deaths"])[:10]
        out["massacre"] = massacre
        out["first_blood"] = sorted(
            ({"name": k, "count": v} for k, v in firsts.items()), key=lambda r: -r["count"]
        )[:5]
        if causes:
            top_cause = max(causes.items(), key=lambda kv: kv[1])
            out["cause"] = {"name": top_cause[0], "count": top_cause[1]}
        else:
            out["cause"] = None
        tomb = set(cemetery)
        out["intouchables"] = sorted(
            ({"name": k, "fights": v} for k, v in participation.items() if v >= 5 and k not in tomb),
            key=lambda r: -r["fights"],
        )[:5]
    except wcl.WclError as exc:
        out["error"] = str(exc)
    try:
        with _db_lock, _db() as conn:
            out["scholars"] = [dict(r) for r in conn.execute(
                "SELECT user_name AS name, COUNT(*) AS n FROM sims "
                "WHERE user_name IS NOT NULL AND user_name<>'' GROUP BY user_name ORDER BY n DESC LIMIT 5"
            ).fetchall()]
            out["pillars"] = [dict(r) for r in conn.execute(
                "SELECT COALESCE(u.name, s.user_email) AS name, COUNT(*) AS n FROM raid_signups s "
                "LEFT JOIN users u ON u.email = s.user_email WHERE s.status='yes' "
                "GROUP BY s.user_email ORDER BY n DESC LIMIT 5"
            ).fetchall()]
            out["hearts"] = [dict(r) for r in conn.execute(
                "SELECT COALESCE(u.name, p.user_email) AS name, COUNT(*) AS n FROM profiles p "
                "LEFT JOIN users u ON u.email = p.user_email WHERE p.shared=1 "
                "GROUP BY p.user_email ORDER BY n DESC LIMIT 5"
            ).fetchall()]
            mains = conn.execute("SELECT realm, name, display FROM char_links WHERE is_main=1").fetchall()
        coll = []
        seen: set = set()
        for m in mains[:30]:
            k = (m["name"] or "").lower()
            if not k or k in seen:
                continue
            seen.add(k)
            try:
                ex, _t = bnet.extras(m["realm"], m["name"])
            except bnet.BnetError:
                continue
            if ex.get("mounts"):
                coll.append({"name": m["display"] or m["name"], "mounts": ex["mounts"],
                             "pets": ex.get("pets") or 0, "achv": ex.get("achv_points") or 0})
        coll.sort(key=lambda r: -r["mounts"])
        out["collectors"] = coll[:5]
    except Exception:  # noqa: BLE001
        pass
    return out


@app.get("/api/fun")
def api_fun(request: Request, refresh: int = 0):
    _require_user(request)
    now = time.time()
    with _db_lock:
        cached = _FUN_CACHE["data"]
        age = now - _FUN_CACHE["ts"]
    if cached is not None and ((not refresh and age < FUN_TTL) or (refresh and age < 60)):
        return cached
    data = _build_fun()
    with _db_lock:
        _FUN_CACHE["ts"] = time.time()
        _FUN_CACHE["data"] = data
    return data


# ---------------------------------------------------------------------------
# Admin API — comptes et invitations : app/routers/admin_users.py
# ---------------------------------------------------------------------------
from app.routers import admin_users as _admin_users_router  # noqa: E402

app.include_router(_admin_users_router.router)


# ---------------------------------------------------------------------------
# Calendrier des raids (planification + présences) — app/routers/calendar.py
# ---------------------------------------------------------------------------
from app.routers import calendar as _calendar_router  # noqa: E402

app.include_router(_calendar_router.router)


# ---------------------------------------------------------------------------
# Tableau de bord (activité de la guilde) — app/routers/dashboard.py
# ---------------------------------------------------------------------------
from app.routers import dashboard as _dashboard_router  # noqa: E402

app.include_router(_dashboard_router.router)


# ---------------------------------------------------------------------------
# Mes personnages (liaison compte ↔ personnages de guilde) — app/routers/characters.py
# ---------------------------------------------------------------------------
from app.routers import characters as _characters_router  # noqa: E402

app.include_router(_characters_router.router)


# ---------------------------------------------------------------------------
# Relevés quotidiens — évolution des personnages liés (v2026.09.054)
# ---------------------------------------------------------------------------
from app.core.util import _lua_unescape, _snap_day  # noqa: E402,F401


from app.services.crafting import _known_craft_rows  # noqa: E402  (app/services/crafting.py)


def _char_changes(prev: dict, new: dict) -> dict:
    """Différences annonçables entre deux relevés : palier d'iLvl, montures, mascottes."""
    ch: dict = {}
    p_il, n_il = prev.get("ilvl"), new.get("ilvl")
    if p_il and n_il and n_il > p_il and (n_il // 5) > (p_il // 5):
        ch["ilvl_from"], ch["ilvl_to"] = p_il, n_il
    for key in ("mounts", "pets"):
        p, n = prev.get(key), new.get(key)
        if p is not None and n is not None and n > p:
            ch[key] = n - p
    return ch


def _char_alert(name: str, prev: dict, new: dict) -> None:
    """Annonce Discord (persos liés) : palier d'iLvl, nouvelles montures / mascottes."""
    ch = _char_changes(prev, new)
    if not ch:
        return
    try:
        cfg = _bot_config()
    except Exception:  # noqa: BLE001
        return
    if (cfg is None or not cfg["enabled"] or not cfg["notify_chars"]
            or not (cfg["token"] or "").strip() or not (cfg["channel_id"] or "").strip()):
        return
    try:
        discord_bot.send(cfg["token"], cfg["channel_id"], embeds=[discord_bot.char_embed(name, ch, _brand_identity()["guild_name"])])
    except Exception as exc:  # noqa: BLE001
        print(f"[snap] alerte {name}: {exc}")


def _char_snapshot(realm: str, name: str) -> dict:
    """État d'un personnage (résumé + équipement + collections) pour un relevé quotidien.

    Les noms (classe, spé, objets, emplacements) sont relevés en FR **et** en EN :
    l'historique reste lisible dans la langue du compte au moment de la consultation.
    """
    s, _ = bnet.character(realm, name, locale="fr_FR")
    g, _ = bnet.equipment(realm, name, locale="fr_FR")
    x, _ = bnet.extras(realm, name)
    s_en: dict = {}
    g_en: dict = {}
    try:
        s_en, _ = bnet.character(realm, name, locale="en_US")
        g_en, _ = bnet.equipment(realm, name, locale="en_US")
    except bnet.BnetError as exc:
        print(f"[snap] versions EN indisponibles pour {name}: {exc}")
    en_items = {it.get("item_id"): it for it in (g_en.get("items") or [])}
    items = []
    for it in (g.get("items") or []):
        e = en_items.get(it.get("item_id")) or {}
        items.append({
            "slot": it.get("slot"), "slot_en": e.get("slot"),
            "name": it.get("name"), "name_en": e.get("name"),
            "ilvl": it.get("ilvl"), "q": it.get("quality"), "id": it.get("item_id"),
        })
    return {
        "level": s.get("level"), "spec": s.get("spec"), "spec_en": s_en.get("spec"),
        "class": s.get("class"), "class_en": s_en.get("class"),
        "ilvl": s.get("ilvl_equipped"), "ilvl_avg": s.get("ilvl_avg"),
        "last_login": s.get("last_login"),
        "achv": s.get("achievement_points"),
        "mounts": x.get("mounts"), "pets": x.get("pets"), "mplus": x.get("mplus_rating"),
        "items": items,
    }


def _snap_store(realm: str, name: str, data: dict, day: str | None = None, ts: float | None = None) -> None:
    """Enregistre (ou remplace) le relevé d'un jour (défaut : aujourd'hui)."""
    with _db_lock, _db() as conn:
        conn.execute(
            "INSERT INTO char_snapshots (realm, name, day, ts, data) VALUES (?,?,?,?,?) "
            "ON CONFLICT(realm, name, day) DO UPDATE SET ts=excluded.ts, data=excluded.data",
            (realm.lower(), name.lower(), day or _snap_day(), time.time() if ts is None else ts,
             json.dumps(data, ensure_ascii=False)),
        )


def _snap_capture(realm: str, name: str) -> None:
    """Capture silencieuse (thread à la demande) — les erreurs sont seulement journalisées."""
    try:
        _snap_store(realm, name, _char_snapshot(realm, name))
    except Exception as exc:  # noqa: BLE001
        print(f"[snap] {name}: {exc}")


# Emplacements Blizzard (ordre des tableaux CombatantInfo WCL, index 0-17).
WCL_SLOTS = ["Tête", "Cou", "Épaules", "Chemise", "Torse", "Taille", "Jambes", "Pieds",
             "Poignets", "Mains", "1er anneau", "2e anneau", "1er bijou", "2e bijou",
             "Dos", "Main droite", "Main gauche", "Tabard"]
WCL_SLOTS_EN = ["Head", "Neck", "Shoulders", "Shirt", "Chest", "Waist", "Legs", "Feet",
                "Wrists", "Hands", "Ring 1", "Ring 2", "Trinket 1", "Trinket 2",
                "Back", "Main Hand", "Off Hand", "Tabard"]


def _snap_backfill(days: int = 30, force: bool = False) -> dict:
    """Rétro-remplit les relevés depuis les logs de raid (WCL) — uniquement les jours manquants.

    Blizzard ne fournit AUCUN historique : l'équipement passé ne peut venir que des
    rapports de combat (CombatantInfo), qui donnent l'état exact au moment du raid.
    """
    try:
        rep_list, _ts = wcl.reports(limit=50, force=force)
    except wcl.WclError as exc:
        return {"ok": False, "error": str(exc)}
    cutoff_ms = (time.time() - days * 86400) * 1000
    reports = sorted(
        [r for r in (rep_list.get("data") or []) if (r.get("startTime") or 0) >= cutoff_ms],
        key=lambda r: r.get("startTime") or 0,
    )
    with _db_lock, _db() as conn:
        have = {(r["realm"], r["name"], r["day"]) for r in conn.execute(
            "SELECT realm, name, day FROM char_snapshots").fetchall()}
    # cible : tout le roster de la guilde (+ les persos liés par sécurité)
    candidates: dict[str, tuple] = {}
    try:
        roster, _ts = bnet.roster()
        for m in roster.get("members") or []:
            nm = (m.get("name") or "").lower()
            if nm:
                candidates[nm] = ((m.get("realm") or bnet.GUILD_REALM), nm)
    except bnet.BnetError as exc:
        print(f"[snap] backfill : roster indisponible ({exc})")
    with _db_lock, _db() as conn:
        for r in conn.execute("SELECT DISTINCT realm, name FROM char_links").fetchall():
            candidates.setdefault(r["name"].lower(), (r["realm"], r["name"]))
    linked_by_name = candidates
    per: dict[tuple, dict] = {}
    for rep in reports:
        code = rep.get("code")
        day = _snap_day((rep.get("startTime") or 0) / 1000)
        try:
            comb, _ts2 = wcl.report_combatants(code)
        except wcl.WclError as exc:
            print(f"[snap] WCL {code}: {exc}")
            continue
        for pname, gear in (comb.get("players") or {}).items():
            link = linked_by_name.get(pname.lower())
            if link is None:
                continue
            k = (link[0], link[1], day)
            if k in have:
                continue
            items = [
                {"slot": WCL_SLOTS[i], "slot_en": WCL_SLOTS_EN[i],
                 "id": g.get("id"), "ilvl": g.get("itemLevel")}
                for i, g in enumerate(gear)
                if g and g.get("id") and i < len(WCL_SLOTS)
            ]
            if items:
                per[k] = {"ts": (rep.get("startTime") or 0) / 1000, "items": items}
    added = 0
    for (realm, name, day), entry in per.items():
        items = []
        for it in entry["items"]:
            try:
                meta = bnet.item(it["id"], locale="fr_FR")
                nm, q = meta.get("name"), meta.get("quality")
            except bnet.BnetError:
                nm, q = f"Objet {it['id']}", None
            try:
                nm_en = bnet.item(it["id"], locale="en_US").get("name")
            except bnet.BnetError:
                nm_en = None
            items.append({"slot": it["slot"], "slot_en": it.get("slot_en"), "name": nm, "name_en": nm_en,
                          "ilvl": it["ilvl"], "q": q, "id": it["id"]})
        ilvls = [it["ilvl"] for it in items if it.get("ilvl") and it["ilvl"] > 1]
        data = {
            "level": None, "spec": None, "class": None,
            "ilvl": round(sum(ilvls) / len(ilvls)) if ilvls else None, "ilvl_avg": None,
            "achv": None, "mounts": None, "pets": None, "mplus": None,
            "items": items, "src": "wcl",
        }
        _snap_store(realm, name, data, day=day, ts=entry["ts"])
        added += 1
    return {"ok": True, "added": added, "reports": len(reports)}


def _snap_tick() -> None:
    """Un passage : relevés de TOUS les personnages du roster (liés rafraîchis plus souvent) + purge."""
    with _db_lock, _db() as conn:
        linked = {r["name"] for r in conn.execute("SELECT DISTINCT name FROM char_links").fetchall()}
        latest = {
            r["k"]: r["ts"]
            for r in conn.execute(
                "SELECT realm || '|' || name AS k, MAX(ts) AS ts FROM char_snapshots GROUP BY realm, name"
            ).fetchall()
        }
        prof_latest = {
            r["k"]: r["ts"]
            for r in conn.execute(
                "SELECT realm || '|' || name AS k, MAX(ts) AS ts FROM char_professions GROUP BY realm, name"
            ).fetchall()
        }
    try:
        roster, _ts = bnet.roster()
        members = [(m.get("realm") or bnet.GUILD_REALM, m.get("name") or "")
                   for m in (roster.get("members") or [])]
        roster_ok = True
    except bnet.BnetError as exc:
        print(f"[snap] roster indisponible ({exc}) — repli sur les personnages liés")
        with _db_lock, _db() as conn:
            members = [(r["realm"], r["name"]) for r in conn.execute(
                "SELECT DISTINCT realm, name FROM char_links").fetchall()]
        roster_ok = False
    targets = sorted(((realm, name, name.lower() in linked) for realm, name in members if name),
                     key=lambda t: not t[2])  # persos liés d'abord
    now = time.time()
    done_this_tick = 0
    snapped = 0
    profs = 0
    limit_linked = _job_int("snap_linked_h") * 60
    limit_roster = _job_int("snap_roster_h") * 60
    max_tick = _job_int("snap_max_tick")
    prof_days = _job_int("prof_days")
    for realm, name, is_linked in targets:
        k = f"{realm}|{name.lower()}"
        last = latest.get(k)
        limit_min = limit_linked if is_linked else limit_roster
        need_snap = not (last and now - last < limit_min * 60)
        plast = prof_latest.get(k)
        need_prof = not (plast and now - plast < prof_days * 86400)
        if not need_snap and not need_prof:
            continue
        if done_this_tick >= max_tick:
            break  # borne le temps du passage ; le reste au tick suivant
        done_this_tick += 1
        if need_snap:
            try:
                prev = None
                if name.lower() in linked:
                    with _db_lock, _db() as conn:
                        prow = conn.execute(
                            "SELECT data FROM char_snapshots WHERE realm=? AND name=? AND day=?",
                            (realm.lower(), name.lower(), _snap_day()),
                        ).fetchone()
                        if prow is None:
                            prow = conn.execute(
                                "SELECT data FROM char_snapshots WHERE realm=? AND name=? "
                                "ORDER BY day DESC LIMIT 1",
                                (realm.lower(), name.lower()),
                            ).fetchone()
                    if prow is not None:
                        try:
                            prev = json.loads(prow["data"] or "{}")
                        except (ValueError, TypeError):
                            prev = None
                data = _char_snapshot(realm, name)
                _snap_store(realm, name, data)
                if prev:
                    _char_alert(name, prev, data)
            except bnet.BnetError as exc:
                if getattr(exc, "status", None) == 404 and roster_ok:
                    # personnage inexistant côté API : purge des relevés s'il a quitté le roster (ToU §18)
                    if name.lower() not in {n.lower() for _r, n in members}:
                        with _db_lock, _db() as conn:
                            conn.execute("DELETE FROM char_snapshots WHERE realm=? AND name=?", (realm, name.lower()))
                        print(f"[snap] {name} absent du roster → relevés supprimés")
                else:
                    print(f"[snap] {name}: {exc}")
        if need_prof:
            try:
                _prof_store(realm, name)
                profs += 1
            except bnet.BnetError as exc:
                print(f"[snap] prof {name}: {exc}")
        if need_snap:
            snapped += 1
    cutoff = _snap_day(now - (_job_int("snap_keep_days") - 1) * 86400)
    with _db_lock, _db() as conn:
        conn.execute("DELETE FROM char_snapshots WHERE day < ?", (cutoff,))
    return {"snapped": snapped, "profs": profs, "checked": done_this_tick, "cutoff": cutoff}


def _snap_loop() -> None:
    time.sleep(20)
    first = True
    while True:
        if _job_conf("snap_enabled") in ("0", "false", "no", ""):
            _job_status_set("snapshots", detail="en pause (administration)")
            time.sleep(max(30, _job_int("snap_interval_min") * 60))
            continue
        try:
            res = _snap_tick()
            _job_status_set("snapshots", detail=f'{res.get("snapped", 0)} relevé(s), '
                                                f'{res.get("profs", 0)} métier(s)')
            if first:
                first = False
                with _db_lock, _db() as conn:
                    done_row = conn.execute("SELECT value FROM meta WHERE key='snap_backfill_v1'").fetchone()
                if done_row is None:
                    res = _snap_backfill()
                    print(f"[snap] backfill WCL : {res}")
                    if res.get("ok"):
                        with _db_lock, _db() as conn:
                            conn.execute(
                                "INSERT OR REPLACE INTO meta (key, value) VALUES ('snap_backfill_v1', ?)",
                                (str(int(time.time())),),
                            )
        except Exception as exc:  # noqa: BLE001
            print(f"[snap] tick: {exc}")
            _job_status_set("snapshots", error=str(exc))
        time.sleep(max(30, _job_int("snap_interval_min") * 60))


def _is_tracked_char(realm: str, name: str) -> bool:
    """Le personnage est-il suivi (lié au compte ou membre du roster de guilde) ?"""
    with _db_lock, _db() as conn:
        row = conn.execute("SELECT 1 FROM char_links WHERE realm=? AND name=? LIMIT 1", (realm, name)).fetchone()
    if row is not None:
        return True
    try:
        roster, _ts = bnet.roster()
    except bnet.BnetError:
        return False
    return any((m.get("name") or "").lower() == name for m in (roster.get("members") or []))


def _snap_summary(day: str, ts: float, d: dict, want_en: bool = False) -> dict:
    return {"day": day, "ts": ts, "level": d.get("level"),
            "spec": _pick(d, "spec", want_en), "class": _pick(d, "class", want_en),
            "ilvl": d.get("ilvl"), "ilvl_avg": d.get("ilvl_avg"),
            "achv": d.get("achv"), "mounts": d.get("mounts"), "pets": d.get("pets"),
            "mplus": d.get("mplus"), "src": d.get("src")}


@app.get("/api/char/{realm}/{name}/history")
def api_char_history(realm: str, name: str, request: Request):
    """Relevés quotidiens (résumés) d'un personnage — du plus ancien au plus récent."""
    _require_user(request)
    want_en = _user_locale(request).startswith("en")
    realm, name = realm.lower(), name.lower()
    with _db_lock, _db() as conn:
        rows = conn.execute(
            "SELECT day, ts, data FROM char_snapshots WHERE realm=? AND name=? ORDER BY day",
            (realm, name),
        ).fetchall()
    days = []
    for r in rows:
        try:
            days.append(_snap_summary(r["day"], r["ts"], json.loads(r["data"]), want_en))
        except ValueError:
            continue
    # personnage suivi et relevé du jour manquant → capture à la volée (sans bloquer la réponse)
    if (not days or days[-1]["day"] != _snap_day()) and _is_tracked_char(realm, name):
        threading.Thread(target=_snap_capture, args=(realm, name), daemon=True).start()
    return {"ok": True, "days": days, "keep_days": SNAP_KEEP_DAYS}


@app.post("/api/admin/snap-backfill")
def admin_snap_backfill(request: Request, days: int = 30):
    """Relance manuelle du rétro-remplissage WCL (admin)."""
    _require_admin(request)
    return _snap_backfill(days=max(1, min(90, days)), force=True)


@app.get("/api/char/{realm}/{name}/snapdiff")
def api_char_snapdiff(realm: str, name: str, request: Request):
    """Différence entre deux relevés (?from=YYYY-MM-DD&to=YYYY-MM-DD ; défaut : début → fin)."""
    _require_user(request)
    want_en = _user_locale(request).startswith("en")
    realm, name = realm.lower(), name.lower()
    frm = (request.query_params.get("from") or "").strip()
    to = (request.query_params.get("to") or "").strip()
    with _db_lock, _db() as conn:
        rows = conn.execute(
            "SELECT day, ts, data FROM char_snapshots WHERE realm=? AND name=? ORDER BY day",
            (realm, name),
        ).fetchall()
    if not rows:
        raise HTTPException(404, "Aucun relevé pour ce personnage.")
    snaps = []
    for r in rows:
        try:
            snaps.append((r["day"], r["ts"], json.loads(r["data"])))
        except ValueError:
            continue
    if not snaps:
        raise HTTPException(404, "Aucun relevé pour ce personnage.")
    by_day = {d: (ts, data) for d, ts, data in snaps}
    a_day = frm if frm in by_day else snaps[0][0]
    b_day = to if to in by_day else snaps[-1][0]
    if a_day > b_day:
        a_day, b_day = b_day, a_day
    a_ts, a = by_day[a_day]
    b_ts, b = by_day[b_day]

    def delta(key: str):
        av, bv = a.get(key), b.get(key)
        if isinstance(av, (int, float)) and isinstance(bv, (int, float)):
            return round(bv - av, 2)
        return None

    a_items = {it.get("slot"): it for it in (a.get("items") or [])}
    b_items = {it.get("slot"): it for it in (b.get("items") or [])}

    def _loc_item(it):
        if not it:
            return it
        out = dict(it)
        out["slot"] = _pick(it, "slot", want_en)
        out["name"] = _pick(it, "name", want_en)
        return out

    items = []
    for slot in list(dict.fromkeys(list(a_items.keys()) + list(b_items.keys()))):
        fa, fb = a_items.get(slot), b_items.get(slot)
        slot_lbl = ((fa or {}).get("slot_en") or (fb or {}).get("slot_en") or slot) if want_en else slot
        changed = not (fa and fb and fa.get("id") == fb.get("id") and fa.get("ilvl") == fb.get("ilvl"))
        items.append({"slot": slot_lbl, "from": _loc_item(fa), "to": _loc_item(fb), "changed": changed})
    return {
        "ok": True,
        "from": _snap_summary(a_day, a_ts, a, want_en),
        "to": _snap_summary(b_day, b_ts, b, want_en),
        "deltas": {k: delta(k) for k in ("level", "ilvl", "ilvl_avg", "achv", "mounts", "pets", "mplus")},
        "items": items,
    }


# ---------------------------------------------------------------------------
# Bot Discord (annonces de guilde)
# ---------------------------------------------------------------------------
# Calendrier in-game (import de l'addon, visées, relances) — app/routers/gcal.py
from app.routers import gcal as _gcal_router  # noqa: E402

app.include_router(_gcal_router.router)


# ---------------------------------------------------------------------------
# Clés API (Battle.net, Warcraft Logs) — renseignées depuis l'administration
# ---------------------------------------------------------------------------
# app/services/api_keys.py
from app.services.api_keys import _api_effective, _api_keys_rows, _apply_api_keys  # noqa: E402,F401


# ---------------------------------------------------------------------------
# Jobs de synchronisation — réglages (administration) + état du dernier passage
# ---------------------------------------------------------------------------
# Réglages et état des tâches de fond — app/services/jobs.py
from app.services.jobs import (  # noqa: E402,F401
    JOB_BOUNDS, JOB_DEFAULTS, _job_conf, _job_int, _job_status_rows, _job_status_set,
)


# bornes de saisie (min, max) par réglage
# Mises à jour — app/services/updates.py
from app.services.updates import _update_loop  # noqa: E402


def _snap_tick_job() -> None:
    """Passage des relevés déclenché depuis l'administration."""
    try:
        res = _snap_tick()
        _job_status_set("snapshots", detail=f'{res.get("snapped", 0)} relevé(s), '
                                            f'{res.get("profs", 0)} métier(s)')
    except Exception as exc:  # noqa: BLE001
        _job_status_set("snapshots", error=str(exc))


# ---------------------------------------------------------------------------
# E-mail (SMTP) — réglages de l'administration ; prioritaires sur l'environnement
# ---------------------------------------------------------------------------
# app/services/mail_settings.py
from app.services.mail_settings import _apply_mail_config, _mail_rows  # noqa: E402,F401


# Réglages du bot Discord — app/services/bot.py
from app.services.bot import _bot_config, _bot_save  # noqa: E402,F401


def _weekly_recap_embed() -> dict | None:
    """Embed du récap hebdo : progressions (7 j), raids, mouvements de guilde."""
    fields: list[dict] = []
    try:
        prog = _progression_data(7)
        top = [r for r in (prog.get("rows") or []) if r.get("d_ilvl")][:5]
        if top:
            lines = [f"**{r['name']}** +{r['d_ilvl']} iLvl ({r.get('ilvl0')} → {r.get('ilvl1')})" for r in top]
            fields.append({"name": "🏆 Progressions de la semaine", "value": "\n".join(lines)[:1024]})
    except Exception as exc:  # noqa: BLE001
        print(f"[bot] récap progression: {exc}")
    nights = kills = 0
    try:
        rl, _ts = wcl.reports(limit=30)
        cutoff = time.time() - 7 * 86400
        for rep in rl.get("data") or []:
            if (rep.get("startTime") or 0) / 1000 < cutoff:
                continue
            full, _t = wcl.report_full(rep["code"])
            boss = [f for f in ((full.get("report") or {}).get("fights") or []) if f.get("encounterID")]
            if boss:
                nights += 1
                kills += sum(1 for f in boss if f.get("kill"))
        if nights:
            fields.append({"name": "⚔️ Raids", "value": f"{nights} soirée(s) · {kills} boss tué(s)", "inline": True})
    except wcl.WclError:
        pass
    try:
        with _db_lock, _db() as conn:
            ev = conn.execute(
                "SELECT kind, COUNT(*) AS c FROM guild_events WHERE created > ? GROUP BY kind",
                (time.time() - 7 * 86400,),
            ).fetchall()
        mov = {row["kind"]: row["c"] for row in ev}
        if mov.get("join") or mov.get("leave"):
            fields.append({"name": "👋 Mouvements",
                           "value": f"+{mov.get('join', 0)} / −{mov.get('leave', 0)}", "inline": True})
    except Exception:  # noqa: BLE001
        pass
    if not fields:
        return None
    return discord_bot.weekly_embed(fields, f"{PUBLIC_BASE_URL}/rankings" if PUBLIC_BASE_URL else "",
                                   _brand_identity()["guild_name"])


def _bot_tick() -> None:
    """Un passage : mouvements de roster (suivi continu) + annonces Discord (si actif)."""
    cfg = _bot_config()
    if cfg is None:
        return
    updates: dict = {}
    notes: list[str] = []
    errs: list[str] = []
    token, channel = (cfg["token"] or "").strip(), (cfg["channel_id"] or "").strip()
    bot_on = bool(cfg["enabled"]) and bool(token) and bool(channel)

    if bot_on and cfg["notify_reports"]:
        try:
            data, _ts = wcl.reports(limit=30)
            rows = data.get("data") or []
            newest = max((float(r.get("startTime") or 0.0) for r in rows), default=0.0)
            last = float(cfg["last_report_t"] or 0.0)
            if newest and last <= 0:
                updates["last_report_t"] = newest  # premier passage : référence, pas d'annonce rétroactive
            elif newest > last:
                fresh = sorted(
                    (r for r in rows if float(r.get("startTime") or 0.0) > last),
                    key=lambda r: float(r.get("startTime") or 0.0),
                )
                for r in fresh[:5]:
                    discord_bot.send(token, channel, embeds=[discord_bot.report_embed(r, _brand_identity()["guild_name"])])
                updates["last_report_t"] = newest
                notes.append(f"{min(len(fresh), 5)} annonce(s) « rapport »")
        except Exception as exc:  # noqa: BLE001
            errs.append(f"rapports — {exc}")

    # Raids planifiés : annonce à la création, rappel ~1 h avant (si le bot est actif).
    if bot_on:
        try:
            now = time.time()
            link = f"{PUBLIC_BASE_URL or ''}/calendar"
            with _db_lock, _db() as conn:
                to_announce = conn.execute(
                    "SELECT * FROM raids WHERE announced=0 AND starts > ? ORDER BY starts", (now,)
                ).fetchall()
                to_remind = conn.execute(
                    "SELECT * FROM raids WHERE announced=1 AND reminded=0 AND starts > ? AND starts <= ?",
                    (now, now + 3600),
                ).fetchall()
            for r in to_announce:
                discord_bot.send(token, channel, embeds=[discord_bot.raid_embed(dict(r), link, _brand_identity()["guild_name"])])
                with _db_lock, _db() as conn:
                    if float(r["starts"]) <= now + 3600:
                        conn.execute("UPDATE raids SET announced=1, reminded=1 WHERE id=?", (r["id"],))
                    else:
                        conn.execute("UPDATE raids SET announced=1 WHERE id=?", (r["id"],))
                notes.append("annonce « raid »")
            for r in to_remind:
                with _db_lock, _db() as conn:
                    su = conn.execute(
                        "SELECT status, COUNT(*) AS c FROM raid_signups WHERE raid_id=? GROUP BY status",
                        (r["id"],),
                    ).fetchall()
                counts = {x["status"]: x["c"] for x in su}
                discord_bot.send(token, channel, embeds=[discord_bot.raid_reminder_embed(dict(r), counts, link, _brand_identity()["guild_name"])])
                with _db_lock, _db() as conn:
                    conn.execute("UPDATE raids SET reminded=1 WHERE id=?", (r["id"],))
                notes.append("rappel « raid »")
        except Exception as exc:  # noqa: BLE001
            errs.append(f"raids — {exc}")

    # Mouvements de guilde : suivis en continu (tableau de bord), annoncés si le bot est actif.
    try:
        data, _ts = bnet.roster()
        members = {m["name"]: m for m in (data.get("members") or []) if m.get("name")}
        snap = set(json.loads(cfg["roster_snap"] or "[]"))
        if not snap:
            updates["roster_snap"] = json.dumps(sorted(members))
        else:
            added = sorted(set(members) - snap)
            gone = sorted(snap - set(members))
            if added or gone:
                now = time.time()
                with _db_lock, _db() as conn:
                    for n in added:
                        conn.execute(
                            "INSERT INTO guild_events (kind, member, created) VALUES ('join',?,?)", (n, now)
                        )
                    for n in gone:
                        conn.execute(
                            "INSERT INTO guild_events (kind, member, created) VALUES ('leave',?,?)", (n, now)
                        )
                updates["roster_snap"] = json.dumps(sorted(members))
                notes.append(f"roster : +{len(added)} / -{len(gone)}")
                if bot_on and cfg["notify_roster"]:
                    for n in added[:5]:
                        discord_bot.send(token, channel, embeds=[discord_bot.roster_embed("join", members[n], _brand_identity()["guild_name"])])
                    for n in gone[:5]:
                        discord_bot.send(token, channel, embeds=[discord_bot.roster_embed("leave", {"name": n}, _brand_identity()["guild_name"])])
    except Exception as exc:  # noqa: BLE001
        errs.append(f"roster — {exc}")

    # Récap hebdo (lundi matin, heure de Paris) — une fois par semaine si activé.
    if bot_on and cfg["notify_weekly"]:
        try:
            now = time.time()
            try:
                from zoneinfo import ZoneInfo
                local_now = datetime.now(ZoneInfo("Europe/Paris"))
            except Exception:  # noqa: BLE001
                local_now = datetime.now()
            if (local_now.weekday() == 0 and local_now.hour >= 9
                    and now - float(cfg["last_recap"] or 0) > 6 * 86400):
                emb = _weekly_recap_embed()
                if emb is not None:
                    discord_bot.send(token, channel, embeds=[emb])
                    updates["last_recap"] = now
                    notes.append("récap hebdo")
        except Exception as exc:  # noqa: BLE001
            errs.append(f"récap — {exc}")

    if updates or notes or errs or cfg["last_error"]:
        updates["last_message"] = " ; ".join(notes)[:300] if notes else (cfg["last_message"] or "")
        updates["last_error"] = " ; ".join(errs)[:300]
        _bot_save(updates)


def _bot_loop() -> None:
    time.sleep(15)
    while True:
        try:
            _bot_tick()
            _job_status_set("bot", detail="passage OK")
        except Exception as exc:  # noqa: BLE001
            print(f"[bot] tick: {exc}")
            _job_status_set("bot", error=str(exc))
        time.sleep(max(60, _job_int("bot_interval_min") * 60))


@app.get("/api/addon")
def api_addon(request: Request):
    """Addon WoW « Cohors » (zip) — collecte le calendrier de guilde en jeu."""
    _require_user(request)
    import io as _io
    import zipfile as _zip
    src = Path(__file__).resolve().parent.parent / "addon" / "Cohors"
    if not src.is_dir():
        raise HTTPException(404, "Addon introuvable sur le serveur.")
    buf = _io.BytesIO()
    with _zip.ZipFile(buf, "w", _zip.ZIP_DEFLATED) as z:
        for fp in sorted(src.glob("*")):
            if fp.is_file():
                z.write(fp, f"Cohors/{fp.name}")
    buf.seek(0)
    return Response(buf.read(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="Cohors-addon.zip"'})


# ---------------------------------------------------------------------------
# Préparation de raid (atelier : recettes, plan, apports des membres) — app/routers/prep.py
# ---------------------------------------------------------------------------
from app.routers import prep as _prep_router  # noqa: E402

app.include_router(_prep_router.router)


# Recettes du jeu — app/services/game_recipes.py
from app.services.game_recipes import _game_recipes_loop, _game_sync, _game_sync_state  # noqa: E402,F401


# Tableau MM+ (dispos et clés annoncées) — app/routers/mplus.py
from app.routers import mplus as _mplus_router  # noqa: E402

app.include_router(_mplus_router.router)


# Espace « Moi » (alertes MM+, notifications, Mes recettes, indisponibilités) — app/routers/me.py
from app.routers import me as _me_router  # noqa: E402

app.include_router(_me_router.router)


# Butin des raids et donjons — app/services/loot.py
from app.services.loot import _loot_loop  # noqa: E402


# E-mail (SMTP), administration — app/routers/admin_mail.py
from app.routers import admin_mail as _admin_mail_router  # noqa: E402

app.include_router(_admin_mail_router.router)


# ---------------------------------------------------------------------------
# Guilde (royaume, région, Warcraft Logs) — réglages de l'administration
# ---------------------------------------------------------------------------
GUILD_KEYS = ("region", "realm", "slug", "locale", "wcl_region", "wcl_name")
GUILD_BNET_REGIONS = ("eu", "us", "kr", "tw")
GUILD_WCL_REGIONS = ("EU", "US", "KR", "TW", "CN")
GUILD_LOCALES = ("en_US", "es_MX", "pt_BR", "en_GB", "es_ES", "fr_FR", "ru_RU",
                 "de_DE", "it_IT", "ko_KR", "zh_TW", "zh_CN")
_GUILD_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")


# app/services/guild_settings.py
from app.services.guild_settings import _apply_guild_config, _guild_effective, _guild_rows  # noqa: E402,F401


def _guild_normalize(values: dict) -> dict:
    """Normalise puis valide les valeurs fournies (400 avec motif si invalide)."""
    clean: dict = {}
    if values.get("region"):
        region = str(values["region"]).strip().lower()
        if region not in GUILD_BNET_REGIONS:
            raise HTTPException(400, "Région Battle.net inconnue (au choix : eu, us, kr, tw).")
        clean["region"] = region
    if values.get("wcl_region"):
        wcl_region = str(values["wcl_region"]).strip().upper()
        if wcl_region not in GUILD_WCL_REGIONS:
            raise HTTPException(400, "Région Warcraft Logs inconnue (au choix : EU, US, KR, TW, CN).")
        clean["wcl_region"] = wcl_region
    if values.get("locale"):
        locale = str(values["locale"]).strip()
        if locale not in GUILD_LOCALES:
            raise HTTPException(400, "Langue de données inconnue (ex. fr_FR, en_US, de_DE).")
        clean["locale"] = locale
    for key, label in (("realm", "royaume"), ("slug", "slug de guilde")):
        if values.get(key):
            val = str(values[key]).strip().lower()
            if not _GUILD_SLUG_RE.match(val):
                raise HTTPException(400, f"Le {label} doit être un slug en minuscules (ex. hyjal, ma-guilde).")
            clean[key] = val
    if values.get("wcl_name"):
        name = str(values["wcl_name"]).strip()
        if len(name) > 60:
            raise HTTPException(400, "Nom Warcraft Logs trop long (60 caractères maximum).")
        clean["wcl_name"] = name
    return clean


class GuildConfigRequest(BaseModel):
    values: dict[str, str] = {}
    clear: bool = False


class GuildTestRequest(BaseModel):
    values: dict[str, str] = {}


@app.get("/api/admin/guild")
def admin_guild_get(request: Request):
    _require_admin(request)
    rows = _guild_rows()
    return {"config": _guild_effective(),
            "source": {k: ("admin" if rows.get(k) else "env") for k in GUILD_KEYS}}


@app.post("/api/admin/guild")
def admin_guild_save(payload: GuildConfigRequest, request: Request):
    _require_admin(request)
    if payload.clear:
        with _db_lock, _db() as conn:
            conn.execute("DELETE FROM guild_config")
        _apply_guild_config()
        return {"ok": True, "cleared": True}
    values = {k: str(v).strip() for k, v in (payload.values or {}).items() if k in GUILD_KEYS}
    if not values:
        raise HTTPException(400, "Aucune valeur à enregistrer.")
    clean = _guild_normalize(values)
    with _db_lock, _db() as conn:
        for key in values:
            value = clean.get(key, "")
            if value:
                conn.execute("INSERT OR REPLACE INTO guild_config (key, value, updated) VALUES (?,?,?)",
                             (key, value, time.time()))
            else:
                conn.execute("DELETE FROM guild_config WHERE key=?", (key,))
    _apply_guild_config()
    return {"ok": True, "config": _guild_effective()}


@app.post("/api/admin/guild/test")
def admin_guild_test(payload: GuildTestRequest, request: Request):
    """Contrôle (des valeurs saisies, sinon de celles en vigueur) sur les deux services."""
    _require_admin(request)
    eff = _guild_effective()
    overrides = {k: str(v).strip() for k, v in (payload.values or {}).items()
                 if k in GUILD_KEYS and str(v).strip()}
    values = {k: overrides.get(k) or eff.get(k, "") for k in GUILD_KEYS}
    try:
        clean = _guild_normalize(values)
    except HTTPException as exc:
        detail = str(exc.detail)
        return {"bnet": {"ok": False, "detail": detail}, "wcl": {"ok": False, "detail": detail}}
    final = {**values, **clean}
    return {"bnet": bnet.guild_lookup(final["realm"], final["slug"], final["region"]),
            "wcl": wcl.guild_lookup(final["wcl_name"], final["realm"], final["wcl_region"])}


# Mises à jour, administration — app/routers/admin_updates.py
from app.routers import admin_updates as _admin_updates_router  # noqa: E402

app.include_router(_admin_updates_router.router)


@app.get("/api/admin/jobs")
def admin_jobs_get(request: Request):
    _require_admin(request)
    return {"config": {k: _job_conf(k) for k in JOB_DEFAULTS},
            "bounds": JOB_BOUNDS,
            "defaults": JOB_DEFAULTS,
            "status": _job_status_rows()}


class JobConfigRequest(BaseModel):
    values: dict[str, str] = {}


@app.post("/api/admin/jobs")
def admin_jobs_save(payload: JobConfigRequest, request: Request):
    _require_admin(request)
    saved = {}
    for key, value in (payload.values or {}).items():
        if key not in JOB_DEFAULTS:
            continue
        val = str(value).strip()
        if key.endswith("_enabled"):
            saved[key] = "1" if val in ("1", "true", "on", "yes") else "0"
            continue
        try:
            num = int(float(val))
        except (TypeError, ValueError):
            raise HTTPException(400, f"Valeur invalide pour {key}.")
        lo, hi = JOB_BOUNDS.get(key, (1, 100000))
        if not (lo <= num <= hi):
            raise HTTPException(400, f"{key} doit être entre {lo} et {hi}.")
        saved[key] = str(num)
    if saved:
        with _db_lock, _db() as conn:
            for k, v in saved.items():
                conn.execute("INSERT OR REPLACE INTO job_config (key, value, updated) VALUES (?,?,?)",
                             (k, v, time.time()))
    return {"ok": True, "saved": saved}


class JobRunRequest(BaseModel):
    slug: str = Field(..., max_length=30)


@app.post("/api/admin/jobs/run")
def admin_jobs_run(payload: JobRunRequest, request: Request):
    _require_admin(request)
    slug = payload.slug.strip().lower()
    if slug == "snapshots":
        threading.Thread(target=_snap_tick_job, daemon=True, name="snap-manual").start()
        return {"ok": True, "started": True}
    raise HTTPException(400, "Ce job ne peut pas être lancé à la demande.")


# ---------------------------------------------------------------------------
# Première configuration (v2026.09.146) — checklist d'installation sur /start
# ---------------------------------------------------------------------------
@app.get("/api/setup/status")
def api_setup_status(request: Request):
    """État des étapes de mise en route de la guilde (réservé aux administrateurs)."""
    _require_admin(request)
    with _db_lock, _db() as conn:
        n_admins = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE active=1 AND (is_admin=1 OR role='admin')"
        ).fetchone()["c"]
        n_users = conn.execute("SELECT COUNT(*) AS c FROM users WHERE active=1").fetchone()["c"]
        n_invites = conn.execute("SELECT COUNT(*) AS c FROM invites WHERE used IS NULL").fetchone()["c"]
        brand = dict(_brand_row(conn))
    cfg = _bot_config()
    bot = cfg
    g = _guild_effective()
    bnet_id, bnet_secret, _s1 = _api_effective("bnet")
    wcl_id, wcl_secret, _s2 = _api_effective("wcl")
    mail_host = (_mail_rows().get("host") or os.environ.get("SMTP_HOST", "")).strip()
    guild_txt = " · ".join(x for x in (str(g.get("realm") or ""), str(g.get("slug") or "")) if x)
    brand_name = (brand.get("guild_name") or "").strip()
    members_txt = f"{n_users} membre" + ("s" if n_users > 1 else "")
    if n_invites:
        members_txt += f" · {n_invites} invitation" + ("s" if n_invites > 1 else "") + " en attente"
    steps = [
        {"key": "admin", "label": "Compte administrateur", "done": n_admins > 0, "optional": False,
         "hint": "Créé au premier démarrage avec ADMIN_EMAIL / ADMIN_PASSWORD.", "detail": "",
         "href": "/settings#comptes"},
        {"key": "guild", "label": "Guilde du serveur", "done": bool(g.get("realm") and g.get("slug")),
         "optional": False,
         "hint": "Royaume, région, slug Battle.net et nom Warcraft Logs — bouton 🔎 Vérifier.",
         "detail": guild_txt, "href": "/settings#guilde"},
        {"key": "bnet", "label": "Clés API Battle.net", "done": bool(bnet_id and bnet_secret),
         "optional": False,
         "hint": "Portail développeurs Blizzard → Clients API (roster et fiches de personnages).",
         "detail": "", "href": "/settings#api"},
        {"key": "wcl", "label": "Clés API Warcraft Logs", "done": bool(wcl_id and wcl_secret),
         "optional": False,
         "hint": "Warcraft Logs → API Clients (page Rapports).", "detail": "", "href": "/settings#api"},
        {"key": "identity", "label": "Identité du site", "done": bool(brand_name or "logo" in _brand_files()),
         "optional": False,
         "hint": "Nom de guilde, nom court, logo et fond.", "detail": brand_name,
         "href": "/settings#identite"},
        {"key": "smtp", "label": "✉️ E-mail (SMTP)", "done": bool(mail_host), "optional": True,
         "hint": "Optionnel — pour envoyer les invitations par e-mail.", "detail": mail_host,
         "href": "/settings#mail"},
        {"key": "discord", "label": "Bot Discord",
         "done": bool(bot and (bot["token"] or "").strip()), "optional": True,
         "hint": "Optionnel — jeton du bot pour les annonces de rapports et de mouvements.", "detail": "",
         "href": "/settings#bot"},
        {"key": "members", "label": "Premiers membres", "done": n_users > 1 or n_invites > 0,
         "optional": False,
         "hint": "Crée une invitation, envoie le lien, ils s'inscrivent eux-mêmes.", "detail": members_txt,
         "href": "/settings#invitations"},
    ]
    required = [s for s in steps if not s["optional"]]
    return {"steps": steps, "done": sum(1 for s in steps if s["done"]), "total": len(steps),
            "required_done": sum(1 for s in required if s["done"]), "required_total": len(required),
            "optional_done": sum(1 for s in steps if s["optional"] and s["done"]),
            "optional_total": sum(1 for s in steps if s["optional"])}


# Clés API, administration — app/routers/admin_api_keys.py
from app.routers import admin_api_keys as _admin_api_keys_router  # noqa: E402

app.include_router(_admin_api_keys_router.router)


# Bot Discord (administration) — app/routers/admin_bot.py ; le récap hebdo reste ici pour l'instant.
from app.routers import admin_bot as _admin_bot_router  # noqa: E402

app.include_router(_admin_bot_router.router)


@app.post("/api/admin/bot/recap")
def admin_bot_recap(request: Request):
    """Envoie le récap hebdo à la demande (admin)."""
    _require_admin(request)
    cfg = _bot_config()
    token, channel = (cfg["token"] or "").strip(), (cfg["channel_id"] or "").strip()
    if not token or not channel:
        raise HTTPException(400, "Bot Discord non configuré.")
    emb = _weekly_recap_embed()
    if emb is None:
        raise HTTPException(400, "Rien à résumer pour le moment.")
    try:
        discord_bot.send(token, channel, embeds=[emb])
    except discord_bot.DiscordError as exc:
        raise HTTPException(400, f"Discord — {exc}")
    _bot_save({"last_recap": time.time()})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Paramètres du compte (langue, nom, mot de passe) — app/routers/account.py
# ---------------------------------------------------------------------------
from app.routers import account as _account_router  # noqa: E402

app.include_router(_account_router.router)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
@app.api_route("/api/health", methods=["GET", "HEAD"])
def health():
    with _db_lock, _db() as conn:
        queued = conn.execute("SELECT COUNT(*) AS c FROM sims WHERE status='queued'").fetchone()["c"]
        running = conn.execute("SELECT COUNT(*) AS c FROM sims WHERE status='running'").fetchone()["c"]
    return {"ok": True, "version": VERSION, "queued": queued, "running": bool(running), "simc_image": SIMC_IMAGE}


# ---------------------------------------------------------------------------
# Portail vocal (sous-domaine dédié : VOICE_PUBLIC_HOST) — réservé aux membres
# ---------------------------------------------------------------------------
# Le vhost du sous-domaine vocal pose l'en-tête VOICE_HEADER (ex. X-Cohors-Voice)
# puis proxyfie vers cette app : les requêtes marquées sont réécrites vers
# /__voice* où la session est vérifiée avant tout relais vers le client web
# interne (WebSpeak).
VOICE_BACKEND = os.environ.get("VOICE_BACKEND", "http://127.0.0.1:3040").rstrip("/")
VOICE_BACKEND_WS = VOICE_BACKEND.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
VOICE_PUBLIC_HOST = os.environ.get("VOICE_PUBLIC_HOST", "").strip()
VOICE_HEADER = os.environ.get("VOICE_HEADER", "X-Cohors-Voice").strip()
VOICE_CLIENT_ZIP = DATA_DIR / "voice" / "Cohors-TeamSpeak.zip"
_VOICE_APP_BASE = PUBLIC_BASE_URL or ""

# Script injecté dans le client web : pré-remplit le pseudo (fragment #nickname=...).
_VOICE_EXTRAS_JS = """
(function () {
  try {
    var hash = String(location.hash || "");
    var m = hash.match(/[#&]nickname=([^&]+)/);
    var nick = "";
    if (m) { try { nick = decodeURIComponent(m[1]); } catch (e) { nick = m[1]; } }
    nick = (nick || "").trim();
    var auto = /[#&]autojoin=1/.test(hash);
    if (nick) { try { localStorage.setItem("webspeak:nickname", nick); } catch (e) {} }
    if (!nick && !auto) return;
    var tries = 0;
    var nickDone = false;
    var clicks = 0;
    var lastClick = 0;
    var timer = setInterval(function () {
      tries += 1;
      var input = document.querySelector("#nickname");
      if (nick && input && !nickDone) {
        try {
          var setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
          setter.call(input, nick);
          input.dispatchEvent(new Event("input", { bubbles: true }));
          input.dispatchEvent(new Event("change", { bubbles: true }));
        } catch (e) {}
        nickDone = true;
      }
      if (auto && input && (input.value || "").trim()) {
        var btn = null;
        var buttons = document.querySelectorAll("button");
        for (var i = 0; i < buttons.length; i++) {
          var t = (buttons[i].innerText || "").trim().toLowerCase();
          if (t.indexOf("enter voice space") >= 0) { btn = buttons[i]; break; }
        }
        if (btn && !btn.disabled && clicks < 2 && (Date.now() - lastClick) > 4000) {
          clicks += 1;
          lastClick = Date.now();
          btn.click();
        }
      }
      if (tries > 120) clearInterval(timer);
    }, 300);
  } catch (e) {}
})();
"""


class VoiceGateMiddleware:
    """Réécrit les requêtes du vhost vocal (en-tête VOICE_HEADER) vers /__voice*."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") in ("http", "websocket"):
            headers = dict(scope.get("headers") or [])
            if headers.get(VOICE_HEADER.lower().encode()):
                scope = dict(scope)
                scope["voice_gate"] = True
                path = scope.get("path") or "/"
                scope["voice_orig_path"] = path
                scope["path"] = "/__voice" + path
                scope["raw_path"] = scope["path"].encode()
        await self.app(scope, receive, send)


app.add_middleware(VoiceGateMiddleware)


def _build_csp() -> str:
    # frame-src doit autoriser le client vocal embarqué (autre origine) s'il est configuré.
    voice = os.environ.get("VOICE_PUBLIC_HOST", "").strip()
    frame_src = "'self'" + (f" https://{voice}" if voice else "")
    # script-src garde 'unsafe-inline' (l'interface est faite de scripts embarqués dans les pages) :
    # le passage à des nonces fait partie du chantier découpage/refonte front. En l'état, la CSP
    # bloque déjà les scripts/objets d'origine externe, le cadrage tiers et le base-uri détourné.
    return ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; media-src 'self' blob:; "
            f"frame-src {frame_src}; object-src 'none'; base-uri 'self'; form-action 'self'; "
            "frame-ancestors 'self'; worker-src 'self'")


_CSP = _build_csp()


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", _CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


@app.middleware("http")
async def html_no_cache(request: Request, call_next):
    """Pages HTML et fichiers statiques : toujours revalidés (évite les vieilles versions en cache)."""
    response = await call_next(request)
    ctype = response.headers.get("content-type", "")
    if ctype.startswith("text/html") or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response

_VOICE_HOP_REQ = {"host", "cookie", "connection", "keep-alive", "transfer-encoding", "upgrade",
                  "proxy-connection", "te", "trailer", "expect", VOICE_HEADER.lower(), "content-length"}
_VOICE_HOP_RESP = {"connection", "keep-alive", "transfer-encoding", "upgrade",
                   "content-encoding", "content-length"}


@app.get("/api/voice/handoff")
def voice_handoff(request: Request, next: str = ""):
    """Répare la session pour le sous-domaine vocal puis renvoie vers le client.

    Le portail vocal vit sur un autre sous-domaine : les cookies host-only du site
    ne l'atteignent pas. Ici on réémet le cookie avec le domaine parent
    (COOKIE_DOMAIN) puis on renvoie vers le client — sans passage par la connexion.
    """
    user = _get_session_user(request)
    target = next if next.startswith("https://" + VOICE_PUBLIC_HOST + "/") or next == "https://" + VOICE_PUBLIC_HOST else "https://" + VOICE_PUBLIC_HOST + "/"
    if user is None:
        return RedirectResponse(f"{_VOICE_APP_BASE}/login?next={quote(_VOICE_APP_BASE + '/api/voice/handoff?next=' + quote(target, safe=''), safe='')}", status_code=302)
    if request.query_params.get("json"):
        response = Response(content='{"ok": true}', media_type="application/json")
    else:
        response = RedirectResponse(target, status_code=302)
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True,
                            samesite="lax", secure=COOKIE_SECURE, path="/", domain=COOKIE_DOMAIN)
    return response


@app.get("/api/voice/config")
def voice_config():
    """Configuration publique du portail vocal (hôte web) pour la page 🎧 Vocal."""
    return {"host": ("https://" + VOICE_PUBLIC_HOST) if VOICE_PUBLIC_HOST else ""}


@app.api_route("/__voice{rest:path}",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def voice_portal(request: Request, rest: str):
    if not request.scope.get("voice_gate"):
        raise HTTPException(404)
    if _get_session_user(request) is None:
        nxt = "https://" + VOICE_PUBLIC_HOST + request.scope.get("voice_orig_path", request.url.path)
        if request.url.query:
            nxt += "?" + request.url.query
        return RedirectResponse(f"{_VOICE_APP_BASE}/api/voice/handoff?next={quote(nxt, safe='')}", status_code=302)
    if rest == "/__cohors_extras.js":
        return Response(content=_VOICE_EXTRAS_JS, media_type="application/javascript; charset=utf-8",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    url = VOICE_BACKEND + rest
    if request.url.query:
        url += "?" + request.url.query
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _VOICE_HOP_REQ}
    headers["host"] = request.headers.get("host", VOICE_PUBLIC_HOST)
    headers["accept-encoding"] = "identity"
    try:
        body = await request.body()
        async with httpx.AsyncClient(timeout=60.0) as client:
            upstream = await client.request(request.method, url, headers=headers, content=body)
    except httpx.HTTPError as exc:
        return PlainTextResponse(f"Vocal indisponible ({exc.__class__.__name__})", status_code=502)
    content = upstream.content
    if "text/html" in (upstream.headers.get("content-type") or ""):
        html = content.decode("utf-8", "replace")
        if "__cohors_extras.js" not in html:
            tag = '<script src="/__cohors_extras.js"></script>'
            if "</head>" in html:
                html = html.replace("</head>", tag + "</head>", 1)
            elif "</body>" in html:
                html = html.replace("</body>", tag + "</body>", 1)
            else:
                html = html + tag
        content = html.encode("utf-8")
    out = Response(content=content, status_code=upstream.status_code)
    for key, value in upstream.headers.multi_items():
        lk = key.lower()
        if lk in _VOICE_HOP_RESP:
            continue
        if lk == "set-cookie":
            out.headers.append(key, value)
        else:
            out.headers[key] = value
    return out


@app.websocket("/__voice{rest:path}")
async def voice_portal_ws(websocket: WebSocket, rest: str):
    if not websocket.scope.get("voice_gate"):
        await websocket.close(code=4404)
        return
    if _get_session_user(websocket) is None:
        await websocket.close(code=4401)
        return
    subprotocols = websocket.scope.get("subprotocols") or []
    qs = websocket.scope.get("query_string", b"").decode("utf-8", "replace")
    url = VOICE_BACKEND_WS + rest + (("?" + qs) if qs else "")
    up_headers = {}
    origin = websocket.headers.get("origin")
    if origin:
        up_headers["Origin"] = origin
    user_agent = websocket.headers.get("user-agent")
    if user_agent:
        up_headers["User-Agent"] = user_agent
    host_header = websocket.headers.get("host")
    if host_header:
        up_headers["Host"] = host_header
    forwarded_for = websocket.headers.get("x-forwarded-for")
    if forwarded_for:
        up_headers["X-Forwarded-For"] = forwarded_for
    try:
        try:
            upstream_cm = websockets.connect(url, subprotocols=subprotocols or None, max_size=None,
                                             open_timeout=15, ping_interval=None, close_timeout=5,
                                             additional_headers=up_headers or None)
        except TypeError:  # websockets < 14 : autre nom du paramètre
            upstream_cm = websockets.connect(url, subprotocols=subprotocols or None, max_size=None,
                                             open_timeout=15, ping_interval=None, close_timeout=5,
                                             extra_headers=up_headers or None)
        async with upstream_cm as upstream:
            chosen = upstream.subprotocol
            await websocket.accept(subprotocol=chosen if chosen in subprotocols else None)

            async def client_to_upstream():
                while True:
                    msg = await websocket.receive()
                    if msg.get("type") == "websocket.disconnect":
                        raise WebSocketDisconnect(msg.get("code", 1000))
                    if msg.get("text") is not None:
                        await upstream.send(msg["text"])
                    elif msg.get("bytes") is not None:
                        await upstream.send(msg["bytes"])

            async def upstream_to_client():
                async for message in upstream:
                    if isinstance(message, (bytes, bytearray)):
                        await websocket.send_bytes(bytes(message))
                    else:
                        await websocket.send_text(message)

            tasks = [asyncio.create_task(client_to_upstream()),
                     asyncio.create_task(upstream_to_client())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
    except Exception as exc:  # noqa: BLE001
        print(f"[voice] session web fermée : {exc!r}")
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


@app.get("/api/voice/client")
def voice_client_download(request: Request):
    """Client TeamSpeak portable préconfiguré (réservé aux membres connectés)."""
    _require_user(request)
    if not VOICE_CLIENT_ZIP.exists():
        raise HTTPException(404, "Client portable pas encore disponible.")
    return FileResponse(VOICE_CLIENT_ZIP, filename="Cohors-TeamSpeak.zip",
                        media_type="application/zip")



# ---------------------------------------------------------------------------
# 🎵 Musique (SinusBot) — réservé aux officiers et administrateurs
# ---------------------------------------------------------------------------
SINUSBOT_URL = os.environ.get("SINUSBOT_URL", "http://127.0.0.1:8087").rstrip("/")
SINUSBOT_USER = os.environ.get("SINUSBOT_USER", "")
SINUSBOT_PASS = os.environ.get("SINUSBOT_PASS", "")
SINUSBOT_BOTID = os.environ.get("SINUSBOT_BOTID", "")
SINUSBOT_INSTANCE = os.environ.get("SINUSBOT_INSTANCE", "")
MUSIC_MEDIA_TOKEN = os.environ.get("MUSIC_MEDIA_TOKEN", "")
MUSIC_MEDIA_BASE = os.environ.get("MUSIC_MEDIA_BASE", "http://127.0.0.1:8030").rstrip("/")
MUSIC_DIR = DATA_DIR / "music"
_MUSIC_SB = {"token": "", "ts": 0.0}


def _music_login() -> str:
    if _MUSIC_SB["token"] and (time.time() - _MUSIC_SB["ts"]) < 3600:
        return _MUSIC_SB["token"]
    payload = {"username": SINUSBOT_USER, "password": SINUSBOT_PASS, "botId": SINUSBOT_BOTID}
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.post(SINUSBOT_URL + "/api/v1/bot/login", json=payload)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Bot musique injoignable ({exc.__class__.__name__}).")
    j = r.json()
    token = j.get("token", "")
    if not token:
        raise HTTPException(502, "Connexion au bot musique impossible.")
    _MUSIC_SB["token"] = token
    _MUSIC_SB["ts"] = time.time()
    return token


def _sb_call(method: str, path: str, body=None, timeout: float = 20.0):
    token = _music_login()
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.request(method, SINUSBOT_URL + path, json=body,
                               headers={"Authorization": "Bearer " + token})
            if r.status_code == 401:
                _MUSIC_SB["token"] = ""
                token = _music_login()
                r = client.request(method, SINUSBOT_URL + path, json=body,
                                   headers={"Authorization": "Bearer " + token})
        return r
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Bot musique injoignable ({exc.__class__.__name__}).")



def _music_payload(j: dict) -> dict:
    ct = j.get("currentTrack") or {}
    conn = j.get("connStatus") or {}
    return {
        "ok": True,
        "running": bool(j.get("running")),
        "connected": bool(conn.get("status") == 4),
        "playing": bool(j.get("playing")),
        "paused": bool((_music_state_load() or {}).get("paused")),
        "stopped": bool((_music_state_load() or {}).get("stopped")),
        "position": int(j.get("position") or 0),
        "volume": int(j.get("volume") or 0),
        "track": ({"uuid": ct.get("uuid", ""), "title": ct.get("title") or ct.get("filename", "")}
                  if ct.get("uuid") else None),
        "modes": {"shuffle": bool((_music_state_load() or {}).get("shuffle")), "loop": bool((_music_state_load() or {}).get("loop"))},
    }


@app.get("/music")
def music_page(request: Request):
    user = _get_session_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    if _user_role(user) not in ("admin", "officer"):
        return RedirectResponse("/dashboard", status_code=302)
    return FileResponse(STATIC_DIR / "music.html")


@app.get("/api/music/status")
def music_status(request: Request):
    _require_officer(request)
    try:
        r = _sb_call("GET", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/status")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Bot injoignable ({exc.__class__.__name__})")
    if r.status_code != 200:
        raise HTTPException(502, "Bot injoignable.")
    return _music_payload(r.json())


@app.get("/api/music/tracks")
def music_tracks(request: Request):
    _require_officer(request)
    r = _sb_call("GET", "/api/v1/bot/files")
    try:
        files = r.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(502, "Bibliothèque illisible.")
    tracks = []
    for f in (files if isinstance(files, list) else []):
        title = (f.get("title") or f.get("filename") or "").strip()
        tracks.append({"uuid": f.get("uuid", ""), "title": title[:120]})
    tracks.sort(key=lambda t: t["title"].lower())
    return {"ok": True, "tracks": tracks}


class MusicPlay(BaseModel):
    uuid: str = Field(..., max_length=80)


@app.post("/api/music/play")
def music_play(payload: MusicPlay, request: Request):
    _require_officer(request)
    if not re.match(r"^[A-Za-z0-9-]{8,80}$", payload.uuid):
        raise HTTPException(400, "Identifiant invalide.")
    r = _sb_call("POST", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/play/byId/{payload.uuid}")
    if r.status_code != 200:
        raise HTTPException(502, "Le bot a refusé la lecture.")
    # Vérifier que la lecture démarre vraiment (piste supprimée du serveur => silence trompeur).
    time.sleep(1.6)
    try:
        _j = _sb_call("GET", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/status").json()
        if not _j.get("playing") and int(_j.get("position") or 0) <= 0:
            raise HTTPException(409, "Impossible de lire ce morceau : le fichier n'existe plus sur le serveur. Choisis une piste dans la bibliothèque.")
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        pass  # statut indisponible : on ne bloque pas la lecture
    st = _music_state_load()
    if st.get("paused") or st.get("stopped"):
        st.pop("paused", None)
        st.pop("stopped", None)
        _music_state_save(st)
    return {"ok": r.status_code == 200}


@app.post("/api/music/pause")
def music_pause(request: Request):
    _require_officer(request)
    r = _sb_call("POST", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/pause")
    st = _music_state_load()
    st["paused"] = True
    _music_state_save(st)
    return {"ok": r.status_code == 200}


@app.post("/api/music/stop")
def music_stop(request: Request):
    _require_officer(request)
    r = _sb_call("POST", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/stop")
    st = _music_state_load()
    st["stopped"] = True  # Stop volontaire ≠ fin de piste : le moteur d'enchaînement doit l'ignorer
    st.pop("paused", None)
    _music_state_save(st)
    return {"ok": r.status_code == 200}


class MusicVolume(BaseModel):
    volume: int = Field(..., ge=0, le=100)


@app.post("/api/music/volume")
def music_volume(payload: MusicVolume, request: Request):
    _require_officer(request)
    r = _sb_call("POST", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/volume/set/{payload.volume}")
    return {"ok": r.status_code == 200}


class MusicUrl(BaseModel):
    url: str = Field(..., max_length=500)


@app.post("/api/music/add-url")
def music_add_url(payload: MusicUrl, request: Request):
    _require_officer(request)
    url = payload.url.strip()
    if not re.match(r"^https?://", url):
        raise HTTPException(400, "Lien invalide (http/https attendu).")
    r = _sb_call("POST", "/api/v1/bot/url", {"url": url, "parent": ""})
    ok = r.status_code == 200 and '"success":true' in r.text
    return {"ok": ok}


@app.post("/api/music/upload")
async def music_upload(request: Request, file: UploadFile = File(...)):
    _require_officer(request)
    raw = await file.read()
    if len(raw) > 60 * 1024 * 1024:
        raise HTTPException(400, "Fichier trop lourd (60 Mo max).")
    name = re.sub(r"[^A-Za-z0-9._ ()-]", "_", (file.filename or "musique.mp3")).strip()[:100] or "musique.mp3"
    if not re.search(r"\.(mp3|ogg|wav|m4a|flac|opus)$", name, re.I):
        name += ".mp3"
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    (MUSIC_DIR / name).write_bytes(raw)
    media_url = f"{MUSIC_MEDIA_BASE}/musicmedia/{MUSIC_MEDIA_TOKEN}/{quote(name)}"
    r = _sb_call("POST", "/api/v1/bot/url", {"url": media_url, "parent": ""})
    ok = r.status_code == 200 and '"success":true' in r.text
    if not ok:
        raise HTTPException(502, "Fichier enregistré mais refusé par le bot.")
    return {"ok": True, "name": name}


@app.delete("/api/music/track/{uuid}")
def music_delete(uuid: str, request: Request):
    _require_officer(request)
    if not re.match(r"^[A-Za-z0-9-]{8,80}$", uuid):
        raise HTTPException(400, "Identifiant invalide.")
    # Retrouver le fichier local correspondant AVANT de supprimer côté bot (ménage disque)
    local_name = ""
    try:
        rl = _sb_call("GET", "/api/v1/bot/files")
        files = rl.json()
        for f in (files if isinstance(files, list) else []):
            if f.get("uuid") == uuid:
                fname = str(f.get("filename") or f.get("title") or "")
                base = Path(unquote(fname.rsplit("/", 1)[-1])).name if "/" in fname else Path(unquote(fname)).name
                if base.lower().endswith((".mp3", ".ogg", ".wav", ".m4a", ".flac", ".opus")):
                    local_name = base
                break
    except Exception:  # noqa: BLE001
        pass
    r = _sb_call("DELETE", f"/api/v1/bot/files/{uuid}")
    file_deleted = False
    if local_name:
        try:
            p = MUSIC_DIR / local_name
            if p.is_file():
                p.unlink()
                file_deleted = True
        except Exception:  # noqa: BLE001
            pass
    return {"ok": r.status_code == 200, "file_deleted": file_deleted}


@app.get("/musicmedia/{token}/{name}")
def music_media(token: str, name: str):
    if not MUSIC_MEDIA_TOKEN or token != MUSIC_MEDIA_TOKEN:
        raise HTTPException(404)
    if "/" in name or chr(92) in name or ".." in name:
        raise HTTPException(404)
    path = MUSIC_DIR / name
    if not path.exists() or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="audio/mpeg")


@app.get("/api/music/channels")
def music_channels(request: Request):
    _require_officer(request)
    r = _sb_call("GET", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/channels")
    try:
        raw = r.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(502, "Liste des salons illisible.")
    by_id = {c.get("id"): c.get("name", "") for c in raw}
    out = []
    for c in raw:
        parent = c.get("parent") or 0
        path = (by_id.get(parent, "") + "/" + c.get("name", "")) if parent else c.get("name", "")
        out.append({"id": c.get("id"), "name": c.get("name", ""), "path": path, "hasPassword": bool(c.get("pw"))})
    out.sort(key=lambda x: (x["path"] or "").lower())
    return {"ok": True, "channels": out}


@app.get("/api/music/bot")
def music_bot(request: Request):
    _require_officer(request)
    r = _sb_call("GET", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/settings")
    j = r.json()
    return {"ok": True, "nick": j.get("nick", ""), "channel": j.get("channelName", "")}


class MusicBotConfig(BaseModel):
    nick: str | None = Field(default=None, max_length=40)
    channel: str | None = Field(default=None, max_length=120)


@app.post("/api/music/bot")
def music_bot_set(payload: MusicBotConfig, request: Request):
    _require_officer(request)
    patch = {}
    if payload.nick is not None:
        nick = payload.nick.strip()
        if not (2 <= len(nick) <= 30):
            raise HTTPException(400, "Nom du bot : 2 à 30 caractères.")
        if any(ord(ch) < 32 for ch in nick):
            raise HTTPException(400, "Nom du bot invalide.")
        patch["nick"] = nick
    if payload.channel is not None:
        rc = _sb_call("GET", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/channels")
        try:
            raw = rc.json()
        except Exception:  # noqa: BLE001
            raise HTTPException(502, "Liste des salons illisible.")
        by_id = {c.get("id"): c.get("name", "") for c in raw}
        valid = {}
        for c in raw:
            parent = c.get("parent") or 0
            path = (by_id.get(parent, "") + "/" + c.get("name", "")) if parent else c.get("name", "")
            valid[path] = bool(c.get("pw"))
        want = payload.channel.strip()
        if want not in valid:
            raise HTTPException(400, "Salon inconnu.")
        if valid[want]:
            raise HTTPException(400, "Ce salon a un mot de passe — pas encore géré.")
        patch["channelName"] = want
    if not patch:
        raise HTTPException(400, "Rien à modifier.")
    r = _sb_call("POST", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/settings", patch)
    if r.status_code != 200 or '"success":true' not in r.text:
        raise HTTPException(502, "Réglage refusé par le bot.")
    _sb_call("POST", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/kill")
    time.sleep(2)
    _sb_call("POST", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/spawn")
    return {"ok": True}


# --- 🎵 Modes de lecture : aléatoire + boucle (moteur côté app) ---
MUSIC_STATE_FILE = MUSIC_DIR / "state.json"


def _music_state_load() -> dict:
    try:
        j = json.loads(MUSIC_STATE_FILE.read_text())
        if isinstance(j, dict):
            return j
    except Exception:  # noqa: BLE001
        pass
    return {}


def _music_state_save(st: dict) -> None:
    try:
        MUSIC_DIR.mkdir(parents=True, exist_ok=True)
        MUSIC_STATE_FILE.write_text(json.dumps(st))
    except Exception:  # noqa: BLE001
        pass


def _music_pick_random(current_uuid: str = "") -> str:
    import random
    r = _sb_call("GET", "/api/v1/bot/files")
    try:
        files = r.json()
    except Exception:  # noqa: BLE001
        return ""
    uuids = [f.get("uuid") for f in (files if isinstance(files, list) else []) if f.get("uuid")]
    if not uuids:
        return ""
    pool = [u for u in uuids if u != current_uuid] or uuids
    return random.choice(pool)


@app.get("/api/music/modes")
def music_modes(request: Request):
    _require_officer(request)
    st = _music_state_load()
    return {"ok": True, "shuffle": bool(st.get("shuffle")), "loop": bool(st.get("loop"))}


class MusicModes(BaseModel):
    shuffle: bool | None = None
    loop: bool | None = None


@app.post("/api/music/modes")
def music_modes_set(payload: MusicModes, request: Request):
    _require_officer(request)
    st = _music_state_load()
    if payload.shuffle is not None:
        st["shuffle"] = bool(payload.shuffle)
    if payload.loop is not None:
        st["loop"] = bool(payload.loop)
    st.pop("paused", None)
    st.pop("stopped", None)
    _music_state_save(st)
    started = ""
    if payload.shuffle:
        try:
            r = _sb_call("GET", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/status")
            j = r.json()
            if not j.get("playing"):
                uid = _music_pick_random()
                if uid:
                    _sb_call("POST", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/play/byId/{uid}")
                    started = uid
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "shuffle": bool(st.get("shuffle")), "loop": bool(st.get("loop")), "started": started}


_MUSIC_WATCH = {"prev_playing": False, "prev_uuid": "", "prev_pos": 0, "max_pos": 0,
                "ticks": 0, "last_tick": 0.0, "last_ended": "", "last_error": ""}


def _music_watch_tick():
    st = _music_state_load()
    r = _sb_call("GET", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/status")
    j = r.json()
    playing = bool(j.get("playing"))
    pos = int(j.get("position") or 0)
    uuid = ((j.get("currentTrack") or {}).get("uuid") or "")
    P = _MUSIC_WATCH
    if playing and uuid:
        P["max_pos"] = max(P["max_pos"], pos) if P["prev_uuid"] == uuid else pos
    ended = False
    if P["prev_playing"] and not playing and uuid and uuid == P["prev_uuid"] and not st.get("paused") and not st.get("stopped"):
        if pos > 0 and pos >= P["prev_pos"] and pos > 2500:
            dur = (st.get("durations") or {}).get(uuid)
            if dur:
                ended = pos >= int(dur) - 2500
            else:
                ended = pos >= P["max_pos"] - 1000
    if ended:
        if not st.get("durations"):
            st["durations"] = {}
        st["durations"][uuid] = max(pos, P["max_pos"])
        _music_state_save(st)
        mode = "shuffle" if st.get("shuffle") else ("loop" if st.get("loop") else "none")
        P["last_ended"] = f"{uuid[:8]}@{pos}ms ({mode})"
        print(f"[music] fin de piste detectee : {P['last_ended']}", flush=True)
        if st.get("shuffle"):
            nxt = _music_pick_random(uuid)
            if nxt:
                _sb_call("POST", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/play/byId/{nxt}")
        elif st.get("loop"):
            _sb_call("POST", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/play/byId/{uuid}")
    P["prev_playing"] = playing
    P["prev_uuid"] = uuid if uuid else P["prev_uuid"]
    P["prev_pos"] = pos


def _music_watcher_loop():
    time.sleep(20)
    while True:
        try:
            _music_watch_tick()
            _MUSIC_WATCH["ticks"] += 1
            _MUSIC_WATCH["last_tick"] = time.time()
            _MUSIC_WATCH["last_error"] = ""
        except Exception as exc:  # noqa: BLE001
            _MUSIC_WATCH["last_error"] = f"{exc.__class__.__name__}: {exc}"[:200]
        time.sleep(3)


threading.Thread(target=_music_watcher_loop, daemon=True).start()


@app.get("/api/music/watch")
def music_watch_state(request: Request):
    """Diagnostic du moteur d'enchaînement (ticks, dernier événement)."""
    _require_officer(request)
    st = _music_state_load()
    keys = ("ticks", "last_tick", "last_ended", "last_error", "prev_playing", "prev_uuid", "prev_pos", "max_pos")
    return {"ok": True, "watch": {k: _MUSIC_WATCH.get(k) for k in keys},
            "state": {"stopped": bool(st.get("stopped")), "paused": bool(st.get("paused")),
                      "shuffle": bool(st.get("shuffle")), "loop": bool(st.get("loop")),
                      "durations": len(st.get("durations") or {})}}


# --- 🎧 Compteur de connectés TeamSpeak (hors bot) ---
MUSIC_BOT_UID = os.environ.get("SINUSBOT_CLIENT_UID", "qr2Ym8lUukm/XbbkStGGt+o4g9Q=")


@app.get("/api/voice/count")
def voice_count(request: Request):
    if _get_session_user(request) is None:
        raise HTTPException(401, "Connexion requise.")
    try:
        r = _sb_call("GET", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/channels")
        raw = r.json()
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        raise HTTPException(502, "Vocal indisponible.")
    count = 0
    groups = []
    for ch in (raw if isinstance(raw, list) else []):
        names = []
        for cl in (ch.get("clients") or []):
            if cl.get("uid") == MUSIC_BOT_UID:
                continue
            count += 1
            nm = cl.get("nick") or ""
            if nm:
                names.append(nm)
        if names:
            groups.append({"channel": ch.get("name", ""), "names": names})
    return {"ok": True, "count": count, "groups": groups}

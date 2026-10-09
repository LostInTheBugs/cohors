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
import hmac
import json
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from urllib.parse import quote, unquote

import httpx
import websockets
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import httpx

from app import bnet, discord_bot, mailer, wcl
from app.security import hash_password as _hash_password, verify_password as _verify_password

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration, database and auth helpers live in app/core (re-exported here).
# ---------------------------------------------------------------------------
from app.core.config import (
    DATA_DIR, SIMC_IMAGE,
    SESSION_COOKIE, SESSION_DAYS, PUBLIC_BASE_URL, COOKIE_SECURE, COOKIE_DOMAIN,
    VERSION, STATIC_DIR,
)
from app.core.db import _db, _db_lock  # noqa: F401
from app.core.util import _int_any  # noqa: F401

_login_attempts: dict[str, list[float]] = defaultdict(list)


from app.core.schema import _init_db  # noqa: E402  (app/core/schema.py)


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
    _user_lang, _require_officer,
)


# ---------------------------------------------------------------------------
# Identité de la guilde (v2026.09.111) — logo, nom et fond personnalisables.
# ---------------------------------------------------------------------------
from app.core.brand import _brand_identity, _brand_row  # noqa: E402,F401  (app/core/brand.py)


from app.core.auth import _client_ip  # noqa: E402  (app/core/auth.py)


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


# Identité de la guilde (noms, logo, fond) et son administration — app/routers/branding.py
from app.routers import branding as _branding_router  # noqa: E402

app.include_router(_branding_router.router)


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
# Sim API — app/routers/sims.py
# ---------------------------------------------------------------------------
# « Stuff conseillé » : réexportés pour les tests (code dans app/services/stuff.py)
from app.services.stuff import (  # noqa: E402,F401
    BIS_CONTENT_MAP, _stuff_best_crafted, _stuff_bis_filter, _stuff_parse_export, _stuff_sim_input,
)
from app.routers import sims as _sims_router  # noqa: E402
from app.routers.sims import BLIZZ_LOADOUT, _blizz_talents_for, _spec_token  # noqa: E402,F401  (tests)

app.include_router(_sims_router.router)


# ---------------------------------------------------------------------------
# Battle.net API — roster de guilde & personnages (cache serveur 30 min) — app/routers/bnet_wcl.py
# ---------------------------------------------------------------------------
# (rapports Warcraft Logs et comparateur dans le même module)
from app.routers import bnet_wcl as _bnet_wcl_router  # noqa: E402
from app.routers.bnet_wcl import _GPROG, _guild_progress_aggregate  # noqa: E402,F401  (tests)

app.include_router(_bnet_wcl_router.router)


# ---------------------------------------------------------------------------
# Profils de simulation (exports /simc sauvegardés, partageables guilde) — app/routers/profiles.py
# ---------------------------------------------------------------------------
from app.routers import profiles as _profiles_router  # noqa: E402

app.include_router(_profiles_router.router)


# ---------------------------------------------------------------------------
# Informations de guilde (page 🛡️ Guilde — éditable par les administrateurs) — app/routers/guild.py
# ---------------------------------------------------------------------------
from app.routers import guild as _guild_router  # noqa: E402

app.include_router(_guild_router.router)


# Assiduité aux soirées de raid (Warcraft Logs) — app/routers/attendance.py
from app.routers import attendance as _attendance_router  # noqa: E402

app.include_router(_attendance_router.router)


# ---------------------------------------------------------------------------
# Wishlist (pièces à obtenir + gains)
# ---------------------------------------------------------------------------
# PWA : manifeste, service worker, page hors-ligne — app/routers/pwa.py
from app.routers import pwa as _pwa_router  # noqa: E402

app.include_router(_pwa_router.router)


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
# Relevés quotidiens — évolution des personnages liés (v2026.09.054) — app/services/snapshots.py, app/routers/snapshots.py
# ---------------------------------------------------------------------------
from app.core.util import _lua_unescape, _snap_day  # noqa: E402,F401


from app.services.crafting import _known_craft_rows  # noqa: E402  (tests)
from app.services.snapshots import _snap_loop  # noqa: E402  (démarrage)
from app.routers import snapshots as _snapshots_router  # noqa: E402

app.include_router(_snapshots_router.router)


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


# ---------------------------------------------------------------------------
# E-mail (SMTP) — réglages de l'administration ; prioritaires sur l'environnement
# ---------------------------------------------------------------------------
# app/services/mail_settings.py
from app.services.mail_settings import _apply_mail_config, _mail_rows  # noqa: E402,F401


# Réglages du bot Discord — app/services/bot.py
from app.services.bot import _bot_config, _bot_save  # noqa: E402,F401


# Boucle du bot (annonces, mouvements de roster, récap hebdo) — app/services/bot_loop.py
from app.services.bot_loop import _bot_loop  # noqa: E402


# Addon WoW « Cohors » (zip) — app/routers/addon.py
from app.routers import addon as _addon_router  # noqa: E402

app.include_router(_addon_router.router)


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
# Guilde (royaume, région, Warcraft Logs) — réglages de l'administration — app/routers/admin_guild.py
# ---------------------------------------------------------------------------
# app/services/guild_settings.py
from app.services.guild_settings import _apply_guild_config, _guild_effective, _guild_rows  # noqa: E402,F401
from app.routers import admin_guild as _admin_guild_router  # noqa: E402

app.include_router(_admin_guild_router.router)


# Mises à jour, administration — app/routers/admin_updates.py
from app.routers import admin_updates as _admin_updates_router  # noqa: E402

app.include_router(_admin_updates_router.router)


# Tâches de fond, administration (réglages, état, relevés à la demande) — app/routers/admin_jobs.py
from app.routers import admin_jobs as _admin_jobs_router  # noqa: E402

app.include_router(_admin_jobs_router.router)


# ---------------------------------------------------------------------------
# Première configuration (v2026.09.146) — checklist d'installation sur /start — app/routers/setup.py
# ---------------------------------------------------------------------------
from app.routers import setup as _setup_router  # noqa: E402

app.include_router(_setup_router.router)


# Clés API, administration — app/routers/admin_api_keys.py
from app.routers import admin_api_keys as _admin_api_keys_router  # noqa: E402

app.include_router(_admin_api_keys_router.router)


# Bot Discord (administration, récap hebdo à la demande) — app/routers/admin_bot.py
from app.routers import admin_bot as _admin_bot_router  # noqa: E402

app.include_router(_admin_bot_router.router)


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

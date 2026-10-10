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
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import bnet, discord_bot, mailer, wcl  # noqa: F401  (les tests les remplacent via app.main)
from app.security import hash_password as _hash_password, verify_password as _verify_password

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration, database and auth helpers live in app/core (re-exported here).
# ---------------------------------------------------------------------------
from app.core.config import (
    DATA_DIR, SIMC_IMAGE,
    SESSION_COOKIE, COOKIE_DOMAIN,
    VERSION, STATIC_DIR,
)
from app.core.db import _db, _db_lock  # noqa: F401

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
    _session_key, _new_session, _set_session_cookie, _get_session_user, _user_role,
    _user_lang,
)


# ---------------------------------------------------------------------------
# Identité de la guilde (v2026.09.111) — logo, nom et fond personnalisables.
# ---------------------------------------------------------------------------


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


@app.api_route("/readiness", methods=["GET", "HEAD"])
def readiness_page(request: Request):
    """Bilan des joueurs — iLvl, cote M+, enchantements manquants, châsses vides."""
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "readiness.html")


@app.api_route("/item/{iid}", methods=["GET", "HEAD"])
def item_page(iid: int, request: Request):
    """Fiche objet — où il tombe, qui le veut, qui l'a, qui peut le fabriquer."""
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "item.html")


_MOI_PAGES = {
    "mespersos": "mespersos.html",
    "mesrecettes": "mesrecettes.html",
    "messtats": "messtats.html",
    "alertes": "alertes.html",
    "mesindispos": "mesindispos.html",
    "commandes": "commandes.html",
}


@app.api_route("/mespersos", methods=["GET", "HEAD"])
@app.api_route("/mesrecettes", methods=["GET", "HEAD"])
@app.api_route("/messtats", methods=["GET", "HEAD"])
@app.api_route("/alertes", methods=["GET", "HEAD"])
@app.api_route("/mesindispos", methods=["GET", "HEAD"])
@app.api_route("/commandes", methods=["GET", "HEAD"])
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
        n_chars = conn.execute("SELECT COUNT(*) AS n FROM char_links WHERE user_email=?",
                               (user["email"],)).fetchone()["n"]
    voice_default = ""
    if main is not None:
        voice_default = (main["display"] or main["name"] or "").strip()
    if not voice_default:
        voice_default = (user["name"] or user["email"].split("@")[0]).strip()
    voice_raw = (user["voice_nick"] or "").strip()
    return {"email": user["email"], "name": user["name"], "is_admin": bool(user["is_admin"]),
            "role": _user_role(user), "lang": _user_lang(user),
            "voice_nick": voice_raw or voice_default, "voice_nick_raw": voice_raw,
            "voice_default": voice_default, "chars": int(n_chars or 0)}


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
from app.routers import sims as _sims_router  # noqa: E402

app.include_router(_sims_router.router)


# ---------------------------------------------------------------------------
# Battle.net API — roster de guilde & personnages (cache serveur 30 min) — app/routers/bnet_wcl.py
# ---------------------------------------------------------------------------
# (rapports Warcraft Logs et comparateur dans le même module)
from app.routers import bnet_wcl as _bnet_wcl_router  # noqa: E402

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


# Fiche objet (butin, wishlists, porteurs, artisans) — app/routers/items.py
from app.routers import items as _items_router  # noqa: E402

app.include_router(_items_router.router)


# Recherche globale (barre de navigation, Ctrl+K) — app/routers/search.py
from app.routers import search as _search_router  # noqa: E402

app.include_router(_search_router.router)


# Bilan des joueurs (préparation : enchantements, châsses, iLvl, M+) — app/routers/readiness.py
from app.routers import readiness as _readiness_router  # noqa: E402

app.include_router(_readiness_router.router)


# Commandes d'artisanat (demande à un artisan, suivi, notifications) — app/routers/craft_orders.py
from app.routers import craft_orders as _craft_orders_router  # noqa: E402

app.include_router(_craft_orders_router.router)


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
from app.services.api_keys import _apply_api_keys  # noqa: E402


# ---------------------------------------------------------------------------
# Jobs de synchronisation — réglages (administration) + état du dernier passage
# ---------------------------------------------------------------------------
# Réglages et état des tâches de fond — app/services/jobs.py


# bornes de saisie (min, max) par réglage
# Mises à jour — app/services/updates.py
from app.services.updates import _update_loop  # noqa: E402


# ---------------------------------------------------------------------------
# E-mail (SMTP) — réglages de l'administration ; prioritaires sur l'environnement
# ---------------------------------------------------------------------------
# app/services/mail_settings.py
from app.services.mail_settings import _apply_mail_config  # noqa: E402


# Réglages du bot Discord — app/services/bot.py


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
from app.services.game_recipes import _game_recipes_loop  # noqa: E402


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
from app.services.guild_settings import _apply_guild_config  # noqa: E402
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
# Portail vocal (sous-domaine dédié : VOICE_PUBLIC_HOST) — réservé aux membres — app/routers/voice.py
# ---------------------------------------------------------------------------
from app.routers.voice import VoiceGateMiddleware  # noqa: E402  (app/routers/voice.py)

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


# Portail vocal : routes (relais HTTP/WebSocket, passage de session, client) — app/routers/voice.py
from app.routers import voice as _voice_router  # noqa: E402

app.include_router(_voice_router.router)


# ---------------------------------------------------------------------------
# 🎵 Musique (SinusBot) — réservé aux officiers et administrateurs — app/routers/music.py
# ---------------------------------------------------------------------------
from app.routers import music as _music_router  # noqa: E402

app.include_router(_music_router.router)

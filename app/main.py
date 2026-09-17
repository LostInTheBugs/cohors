"""LOTP Simulateur — web app (FastAPI).

Accounts: invitation-only registration (admin-generated links), login sessions
(signed random token in an HttpOnly cookie), admin panel (invites + users).
Simulations run in the official SimulationCraft Docker image via
`worker/simrun.py` (the app container mounts the host Docker socket).

v2026.09.003: accounts + admin.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
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

from worker.simrun import run_sim

from app import bnet, discord_bot, mailer, wcl

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DB_PATH = DATA_DIR / "wow.sqlite"
REPORTS_DIR = DATA_DIR / "reports"

SIMC_IMAGE = os.environ.get("SIMC_IMAGE", "simulationcraftorg/simc:latest")
SIM_TIMEOUT = int(os.environ.get("SIM_TIMEOUT", "900"))
QUEUE_MAX = int(os.environ.get("QUEUE_MAX", "20"))
PER_IP_ACTIVE = int(os.environ.get("PER_IP_ACTIVE", "3"))
PER_IP_COOLDOWN_S = int(os.environ.get("PER_IP_COOLDOWN_S", "15"))
PER_USER_ACTIVE = int(os.environ.get("PER_USER_ACTIVE", "3"))
ITER_CHOICES = (1000, 5000, 10000, 25000, 50000)
DEFAULT_ITERATIONS = 10000
MAX_INPUT_CHARS = 200_000

SESSION_COOKIE = "lotp_session"
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "30"))
INVITE_TTL_DAYS = int(os.environ.get("INVITE_TTL_DAYS", "7"))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") not in ("0", "false", "no", "")
COOKIE_DOMAIN = os.environ.get("COOKIE_DOMAIN", "").strip() or None
BOT_POLL_S = int(os.environ.get("BOT_POLL_S", "300"))  # intervalle du bot Discord (secondes)

DATA_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

_version_file = Path(__file__).resolve().parent.parent / "VERSION"
VERSION = _version_file.read_text().strip() if _version_file.exists() else os.environ.get("APP_VERSION", "dev")

_db_lock = threading.Lock()
STATIC_DIR = Path(__file__).resolve().parent / "static"
_login_attempts: dict[str, list[float]] = defaultdict(list)


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_db() -> None:
    with _db_lock, _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sims (
                id            TEXT PRIMARY KEY,
                created       REAL NOT NULL,
                ip            TEXT NOT NULL,
                label         TEXT NOT NULL DEFAULT '',
                iterations    INTEGER NOT NULL,
                status        TEXT NOT NULL,
                error         TEXT,
                input_hash    TEXT NOT NULL,
                input_file    TEXT NOT NULL,
                cached_from   TEXT,
                dps           REAL,
                dps_error_pct REAL,
                wall_s        REAL,
                report_html   TEXT,
                report_json   TEXT,
                started       REAL,
                finished      REAL,
                user_email    TEXT,
                user_name     TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                email      TEXT UNIQUE NOT NULL,
                name       TEXT NOT NULL DEFAULT '',
                pwd        TEXT NOT NULL,
                is_admin   INTEGER NOT NULL DEFAULT 0,
                role       TEXT NOT NULL DEFAULT 'member',
                lang       TEXT NOT NULL DEFAULT '',
                active     INTEGER NOT NULL DEFAULT 1,
                created    REAL NOT NULL,
                last_login REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token     TEXT PRIMARY KEY,
                user_id   INTEGER NOT NULL,
                created   REAL NOT NULL,
                last_seen REAL NOT NULL,
                expires   REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS invites (
                token   TEXT PRIMARY KEY,
                email   TEXT,
                note    TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL,
                expires REAL NOT NULL,
                used    REAL,
                used_by INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email TEXT NOT NULL,
                user_name  TEXT NOT NULL,
                name       TEXT NOT NULL,
                input      TEXT NOT NULL,
                shared     INTEGER NOT NULL DEFAULT 0,
                created    REAL NOT NULL,
                updated    REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_config (
                id             INTEGER PRIMARY KEY CHECK (id = 1),
                enabled        INTEGER NOT NULL DEFAULT 0,
                token          TEXT NOT NULL DEFAULT '',
                app_id         TEXT NOT NULL DEFAULT '',
                channel_id     TEXT NOT NULL DEFAULT '',
                channel_name   TEXT NOT NULL DEFAULT '',
                notify_reports INTEGER NOT NULL DEFAULT 1,
                notify_roster  INTEGER NOT NULL DEFAULT 1,
                last_report_t  REAL NOT NULL DEFAULT 0,
                roster_snap    TEXT NOT NULL DEFAULT '[]',
                last_message   TEXT NOT NULL DEFAULT '',
                last_error     TEXT NOT NULL DEFAULT '',
                updated        REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("INSERT OR IGNORE INTO bot_config (id, updated) VALUES (1, 0)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS char_links (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email TEXT NOT NULL,
                realm      TEXT NOT NULL,
                name       TEXT NOT NULL,
                display    TEXT NOT NULL,
                is_main    INTEGER NOT NULL DEFAULT 0,
                created    REAL NOT NULL
            )
            """
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_char_links_uniq ON char_links(user_email, realm, name)")
        # v2026.09.018 — UN SEUL « main » par compte : normalise les doublons éventuels
        # (on garde le plus récent) puis verrouille par index partiel unique.
        rows = conn.execute(
            "SELECT user_email, id FROM char_links WHERE is_main=1 ORDER BY created DESC, id DESC"
        ).fetchall()
        seen_main: set = set()
        for r in rows:
            if r["user_email"] in seen_main:
                conn.execute("UPDATE char_links SET is_main=0 WHERE id=?", (r["id"],))
            else:
                seen_main.add(r["user_email"])
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_char_links_one_main ON char_links(user_email) WHERE is_main=1"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wishlist (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email TEXT NOT NULL,
                item_id    INTEGER NOT NULL,
                name       TEXT NOT NULL,
                slot       TEXT NOT NULL DEFAULT '',
                inv_type   TEXT NOT NULL DEFAULT '',
                quality    TEXT NOT NULL DEFAULT 'COMMON',
                icon       TEXT,
                added      REAL NOT NULL
            )
            """
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_wishlist_uniq ON wishlist(user_email, item_id)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_events (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                kind    TEXT NOT NULL,
                member  TEXT NOT NULL,
                created REAL NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_guild_events_created ON guild_events(created)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS raids (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                title        TEXT NOT NULL DEFAULT '',
                starts       REAL NOT NULL,
                duration_min INTEGER NOT NULL DEFAULT 180,
                note         TEXT NOT NULL DEFAULT '',
                created_by   TEXT NOT NULL DEFAULT '',
                created      REAL NOT NULL,
                announced    INTEGER NOT NULL DEFAULT 0,
                reminded     INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_raids_starts ON raids(starts)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS raid_signups (
                raid_id    INTEGER NOT NULL,
                user_email TEXT NOT NULL,
                status     TEXT NOT NULL,
                updated    REAL NOT NULL,
                PRIMARY KEY (raid_id, user_email)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_info (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL DEFAULT '',
                updated    REAL NOT NULL DEFAULT 0,
                updated_by TEXT NOT NULL DEFAULT ''
            )
            """
        )
        for k in ("intro", "discord_url", "discord_note", "ts_host", "ts_password", "ts_note", "web_url", "web_note"):
            conn.execute("INSERT OR IGNORE INTO guild_info (key, value, updated) VALUES (?, '', 0)", (k,))
        # v2026.09.015 — rôles (membre / officier / administrateur).
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "role" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'member'")
        conn.execute("UPDATE users SET role='admin' WHERE is_admin=1 AND role != 'admin'")
        if "lang" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN lang TEXT NOT NULL DEFAULT ''")
        if "voice_nick" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN voice_nick TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sims_status ON sims(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sims_hash ON sims(input_hash)")
        # migrations (idempotent)
        for table, col in (("sims", "user_email"), ("sims", "user_name"),
                           ("sims", "kind"), ("sims", "weights"), ("sims", "gear")):
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    n, r, p = 2 ** 14, 8, 1
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_hex, dk_hex = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:  # noqa: BLE001
        return False


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


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _new_session(conn: sqlite3.Connection, user_id: int) -> str:
    now = time.time()
    token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))
    conn.execute(
        "INSERT INTO sessions (token, user_id, created, last_seen, expires) VALUES (?,?,?,?,?)",
        (token, user_id, now, now, now + SESSION_DAYS * 86400),
    )
    return token


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True,
                        samesite="lax", secure=COOKIE_SECURE, path="/", domain=COOKIE_DOMAIN)


def _get_session_user(request: Request) -> sqlite3.Row | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    with _db_lock, _db() as conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token=? AND s.expires > ?",
            (token, time.time()),
        ).fetchone()
        if row is not None:
            conn.execute("UPDATE sessions SET last_seen=? WHERE token=?", (time.time(), token))
    if row is not None and not row["active"]:
        return None
    return row


def _require_user(request: Request) -> sqlite3.Row:
    user = _get_session_user(request)
    if user is None:
        raise HTTPException(401, "Connexion requise")
    return user


def _user_role(user: sqlite3.Row) -> str:
    """Rôle effectif du compte (tolérant aux bases sans colonne role)."""
    try:
        role = (user["role"] or "").strip()
    except (IndexError, KeyError):
        role = ""
    if role in ("member", "officer", "admin"):
        return role
    return "admin" if user["is_admin"] else "member"


def _user_lang(user: sqlite3.Row) -> str:
    """Langue préférée du compte (« fr » / « en », sinon vide = auto)."""
    try:
        lang = (user["lang"] or "").strip()
    except (IndexError, KeyError):
        lang = ""
    return lang if lang in ("fr", "en") else ""


def _require_admin(request: Request) -> sqlite3.Row:
    user = _require_user(request)
    if _user_role(user) != "admin":
        raise HTTPException(403, "Réservé à l'administrateur")
    return user


def _require_officer(request: Request) -> sqlite3.Row:
    """Officier ou administrateur."""
    user = _require_user(request)
    if _user_role(user) not in ("officer", "admin"):
        raise HTTPException(403, "Réservé aux officiers et administrateurs")
    return user


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


# ---------------------------------------------------------------------------
# Worker (single simulation at a time — one sim saturates every core)
# ---------------------------------------------------------------------------
def _next_queued() -> str | None:
    with _db_lock, _db() as conn:
        running = conn.execute("SELECT COUNT(*) AS c FROM sims WHERE status='running'").fetchone()["c"]
        if running:
            return None
        row = conn.execute("SELECT id FROM sims WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
        if row is None:
            return None
        conn.execute("UPDATE sims SET status='running', started=? WHERE id=?", (time.time(), row["id"]))
        return row["id"]


# Certains objets font planter SimulationCraft (segfault) — retirés automatiquement et signalés.
CRASH_ITEM_IDS = {"270162": "Réceptacle rituel de l'Entortillâme"}


def _strip_crash_items(text: str) -> tuple[str, list[str]]:
    kept, removed = [], []
    for line in text.splitlines():
        m = re.match(r"^\s*(head|neck|shoulder|back|chest|shirt|tabard|wrist|hands|waist|legs|feet|finger1|finger2|trinket1|trinket2|main_hand|off_hand)\s*=", line)
        if m:
            ids = re.findall(r"\bid=(\d+)", line)
            hit = next((i for i in ids if i in CRASH_ITEM_IDS), None)
            if hit:
                removed.append(CRASH_ITEM_IDS[hit] + f" (id {hit})")
                continue
        kept.append(line)
    return "\n".join(kept), removed


def _run_one(sim_id: str) -> None:
    with _db_lock, _db() as conn:
        row = conn.execute("SELECT * FROM sims WHERE id=?", (sim_id,)).fetchone()
        if row is None or row["status"] != "running":
            return
        input_file = Path(row["input_file"])
        iterations = int(row["iterations"])

    try:
        kind = row["kind"] or "dps"
        extra = ["calculate_scale_factors=1"] if kind == "weights" else None
        if kind == "group":
            extra = ["calculate_scale_factors=0", "fight_style=Patchwerk", "max_time=300"]
        res = run_sim(profile_path=input_file, iterations=iterations, outdir=input_file.parent, timeout=SIM_TIMEOUT, extra=extra)
        if not res.get("ok") and res.get("rc") == 139:
            # Segfault du moteur : réessayer sans les objets connus comme faisant planter SimC.
            try:
                stripped, removed = _strip_crash_items(input_file.read_text())
                if removed:
                    input_file.write_text(stripped)
                    res = run_sim(profile_path=input_file, iterations=iterations, outdir=input_file.parent, timeout=SIM_TIMEOUT, extra=extra)
                    if res.get("ok"):
                        res["note"] = "Sim lancée SANS " + ", ".join(removed) + " — cet objet fait planter le moteur SimulationCraft (bug du moteur, pas de l'export)."
            except Exception:  # noqa: BLE001
                pass
        if not res.get("ok") and res.get("rc") == 139 and not res.get("note"):
            res["log_tail"] = ("Le moteur a planté (segfault) sur cet export — c'est un bug du moteur SimC (souvent un objet précis, connu : Réceptacle rituel de l'Entortillâme). " + (res.get("log_tail") or ""))[:2000]
        ok = bool(res.get("ok"))
        weights = json.dumps(res.get("scale_factors")) if res.get("scale_factors") else None
        gear = json.dumps(res.get("gear")) if res.get("gear") else None
        if kind == "group" and res.get("group"):
            gear = json.dumps(res.get("group"))
        if kind == "group":
            res["dps"] = None  # DPS multi-acteurs : pas de valeur globale
        with _db_lock, _db() as conn:
            conn.execute(
                """UPDATE sims SET status=?, dps=?, dps_error_pct=?, wall_s=?, report_html=?, report_json=?,
                                    error=?, finished=?, weights=?, gear=? WHERE id=?""",
                (
                    "done" if ok else "failed",
                    res.get("dps"), res.get("dps_error_pct"), res.get("wall_s"),
                    res.get("html"), res.get("json"),
                    (res.get("note") or None) if ok else (res.get("log_tail") or "échec de la simulation")[-2000:],
                    time.time(), weights, gear, sim_id,
                ),
            )
    except Exception as exc:  # noqa: BLE001
        with _db_lock, _db() as conn:
            conn.execute(
                "UPDATE sims SET status='failed', error=?, finished=? WHERE id=?",
                (str(exc)[-2000:], time.time(), sim_id),
            )


def _worker_loop() -> None:
    while True:
        sim_id = _next_queued()
        if sim_id is None:
            time.sleep(1.0)
            continue
        _run_one(sim_id)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _init_db()
    _bootstrap_admin()
    threading.Thread(target=_worker_loop, daemon=True, name="sim-worker").start()
    threading.Thread(target=_bot_loop, daemon=True, name="discord-bot").start()
    yield


app = FastAPI(title="LOTP Simulateur", version=VERSION, lifespan=_lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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
        return RedirectResponse("/", status_code=302)
    return FileResponse(STATIC_DIR / "login.html")


@app.api_route("/invite/{token}", methods=["GET", "HEAD"])
def invite_page(token: str):
    return FileResponse(STATIC_DIR / "register.html")


@app.api_route("/admin", methods=["GET", "HEAD"])
def admin_page(request: Request):
    user = _get_session_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    if _user_role(user) == "member":
        return RedirectResponse("/", status_code=302)
    return FileResponse(STATIC_DIR / "admin.html")


@app.api_route("/characters", methods=["GET", "HEAD"])
def characters_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "characters.html")

@app.api_route("/mains", methods=["GET", "HEAD"])
def mains_page(request: Request):
    """Page ⭐ Mains & alts — tous les personnages liés, groupés sous leur main."""
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "mains.html")



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
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))
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
ITEM_REF_RE = re.compile(r"(?<!\d)(\d{4,7})(?!\d)")


def _build_gear_input(profile_text: str, items_text: str) -> tuple[str, list[str]]:
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
            it = bnet.item(iid)
        except bnet.BnetError as exc:
            warnings.append(f"{iid} : pièce ignorée ({exc}).")
            continue
        slots = bnet.INV_TO_SLOTS.get(it["inv_type"])
        if not slots:
            warnings.append(f"{it['name']} : emplacement non géré ({it['inv_type_fr'] or it['inv_type'] or '?'}).")
            continue
        clean = it["name"].replace('"', "'")[:48]
        for slot in slots:
            lines.append(f'profileset."{clean} · {bnet.SLOT_FR[slot]} [{slot}:{iid}]"={slot}=,id={iid}')
    if not lines:
        raise HTTPException(400, "Aucune pièce exploitable parmi celles fournies.")
    return profile_text + "\n\n" + "\n".join(lines) + "\n", warnings


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
        text, warnings = _build_gear_input(text, payload.items)
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

        sim_id = uuid.uuid4().hex[:12]
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
                    info = bnet.item(item_id)
                except bnet.BnetError:
                    info = {}
            dps = float(g.get("dps") or 0.0)
            enriched.append({
                "label": label,
                "slot": slot,
                "slot_fr": bnet.SLOT_FR.get(slot, slot or ""),
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
_REALM_RE = re.compile(r"^[a-z0-9-]{2,40}$")
_CHARNAME_RE = re.compile(r"^[A-Za-z\u00c0-\u00ff][A-Za-z\u00c0-\u00ff'\-]{1,23}$")


def _bnet_call(fn, realm: str, name: str, refresh: int = 0) -> dict:
    _valid_char(realm, name)
    try:
        data, ts = fn(realm, name, force=bool(refresh))
    except bnet.BnetError as exc:
        raise HTTPException(exc.status if exc.status in (400, 404) else 502, str(exc))
    data = dict(data)
    data["fetched_at"] = ts
    return data


def _valid_char(realm: str, name: str) -> None:
    if not _REALM_RE.match(realm.lower()) or not _CHARNAME_RE.match(name):
        raise HTTPException(400, "Nom de personnage ou royaume invalide.")


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
    return _bnet_call(bnet.character, realm, name, refresh)


@app.get("/api/char/{realm}/{name}/extras")
def api_char_extras(realm: str, name: str, request: Request, refresh: int = 0):
    _require_user(request)
    return _bnet_call(bnet.extras, realm, name, refresh)


@app.get("/api/char/{realm}/{name}/equipment")
def api_char_equipment(realm: str, name: str, request: Request, refresh: int = 0):
    _require_user(request)
    return _bnet_call(bnet.equipment, realm, name, refresh)


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
            summary, _ts = bnet.character(realm, name, force=bool(refresh))
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
        input_hash = hashlib.sha256(f"group\n{payload.iterations}\n{text}".encode()).hexdigest()
        cached = conn.execute(
            "SELECT * FROM sims WHERE input_hash=? AND status='done' ORDER BY finished DESC LIMIT 1", (input_hash,)
        ).fetchone()
        sim_id = uuid.uuid4().hex[:12]
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
class WishlistAdd(BaseModel):
    item: str = Field(..., min_length=4, max_length=300)


@app.api_route("/manifest.webmanifest", methods=["GET", "HEAD"])
def pwa_manifest(request: Request):
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


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


@app.get("/api/wishlist")
def api_wishlist(request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        rows = conn.execute(
            "SELECT item_id, name, slot, quality, icon, added FROM wishlist WHERE user_email=? ORDER BY added DESC",
            (user["email"],),
        ).fetchall()
        chars = conn.execute(
            "SELECT realm, name, display, is_main FROM char_links WHERE user_email=? ORDER BY is_main DESC, name",
            (user["email"],),
        ).fetchall()
    items = [
        {
            "item_id": r["item_id"], "name": r["name"],
            "slot": r["slot"], "slot_fr": bnet.SLOT_FR.get(r["slot"], r["slot"] or ""),
            "quality": r["quality"], "icon": r["icon"], "added": r["added"],
        }
        for r in rows
    ]
    chars_out = [dict(c) for c in chars]
    for ch in chars_out:
        try:
            eq, _t = bnet.equipment(ch["realm"], ch["name"])
        except bnet.BnetError:
            ch["_ids"] = set()
            continue
        ch["_ids"] = {it.get("item_id") for it in (eq.get("items") or []) if it.get("item_id")}
    for it in items:
        it["owned"] = {ch["name"]: (it["item_id"] in ch["_ids"]) for ch in chars_out}
    for ch in chars_out:
        ch.pop("_ids", None)
    return {"items": items, "chars": chars_out}


@app.post("/api/wishlist")
def api_wishlist_add(payload: WishlistAdd, request: Request):
    user = _require_user(request)
    m = ITEM_REF_RE.search(payload.item or "")
    if not m:
        raise HTTPException(400, "Indique une pièce (identifiant ou lien Wowhead).")
    iid = int(m.group(1))
    try:
        it = bnet.item(iid)
    except bnet.BnetError as exc:
        raise HTTPException(400, str(exc))
    slot = (bnet.INV_TO_SLOTS.get(it["inv_type"]) or [""])[0]
    with _db_lock, _db() as conn:
        exists = conn.execute(
            "SELECT 1 AS x FROM wishlist WHERE user_email=? AND item_id=?", (user["email"], iid)
        ).fetchone()
        if exists:
            return {"ok": True, "already": True, "name": it["name"]}
        conn.execute(
            "INSERT INTO wishlist (user_email, item_id, name, slot, inv_type, quality, icon, added) VALUES (?,?,?,?,?,?,?,?)",
            (user["email"], iid, it["name"], slot, it["inv_type"], it["quality"], it.get("icon"), time.time()),
        )
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM wishlist WHERE user_email=?", (user["email"],)
        ).fetchone()["c"]
    return {"ok": True, "name": it["name"], "slot": slot, "count": count}


@app.delete("/api/wishlist/{item_id}")
def api_wishlist_del(item_id: int, request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        conn.execute("DELETE FROM wishlist WHERE user_email=? AND item_id=?", (user["email"], item_id))
    return {"ok": True}


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
# Admin API
# ---------------------------------------------------------------------------
class InviteRequest(BaseModel):
    email: str = Field("", max_length=200)
    note: str = Field("", max_length=120)
    send_email: bool = False


class ActiveRequest(BaseModel):
    active: bool


def _invite_link(token: str) -> str:
    base = PUBLIC_BASE_URL or ""
    return f"{base}/invite/{token}"


@app.get("/api/admin/users")
def admin_users(request: Request):
    _require_admin(request)
    with _db_lock, _db() as conn:
        rows = conn.execute(
            """SELECT u.id, u.email, u.name, u.is_admin, u.role, u.active, u.created, u.last_login,
                      (SELECT COUNT(*) FROM sims s WHERE s.user_email = u.email) AS sims_count,
                      (SELECT c.display FROM char_links c WHERE c.user_email = u.email AND c.is_main = 1 LIMIT 1) AS main_char,
                      (SELECT COUNT(*) FROM char_links c WHERE c.user_email = u.email) AS chars_count
               FROM users u ORDER BY u.created""",
        ).fetchall()
    return {"users": [dict(r) for r in rows]}


@app.post("/api/admin/users/{uid}/active")
def admin_set_active(uid: int, payload: ActiveRequest, request: Request):
    me_row = _require_admin(request)
    if uid == me_row["id"]:
        raise HTTPException(400, "Impossible de modifier ton propre compte.")
    with _db_lock, _db() as conn:
        if conn.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone() is None:
            raise HTTPException(404, "Compte inconnu")
        conn.execute("UPDATE users SET active=? WHERE id=?", (1 if payload.active else 0, uid))
        if not payload.active:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    return {"ok": True}


@app.delete("/api/admin/users/{uid}")
def admin_delete_user(uid: int, request: Request):
    me_row = _require_admin(request)
    if uid == me_row["id"]:
        raise HTTPException(400, "Impossible de supprimer ton propre compte.")
    with _db_lock, _db() as conn:
        u = conn.execute("SELECT email FROM users WHERE id=?", (uid,)).fetchone()
        if u is None:
            raise HTTPException(404, "Compte inconnu")
        conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM char_links WHERE user_email=?", (u["email"],))
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
    return {"ok": True}


@app.post("/api/admin/users/{uid}/reset-link")
def admin_reset_link(uid: int, request: Request):
    _require_admin(request)
    now = time.time()
    with _db_lock, _db() as conn:
        u = conn.execute("SELECT email, name FROM users WHERE id=?", (uid,)).fetchone()
        if u is None:
            raise HTTPException(404, "Compte inconnu")
        token = secrets.token_urlsafe(24)
        conn.execute(
            "INSERT INTO invites (token, email, note, created, expires) VALUES (?,?,?,?,?)",
            (token, u["email"], f"réinitialisation — {u['name']}", now, now + INVITE_TTL_DAYS * 86400),
        )
    return {"link": _invite_link(token), "expires_in_days": INVITE_TTL_DAYS}


class RoleRequest(BaseModel):
    role: str = Field(..., max_length=20)


@app.post("/api/admin/users/{uid}/role")
def admin_set_role(uid: int, payload: RoleRequest, request: Request):
    """Change le rôle d'un compte (réservé aux administrateurs)."""
    me_row = _require_admin(request)
    role = payload.role.strip().lower()
    if role not in ("member", "officer", "admin"):
        raise HTTPException(400, "Rôle inconnu (membre, officier ou administrateur).")
    if uid == me_row["id"]:
        raise HTTPException(400, "Impossible de modifier ton propre rôle.")
    with _db_lock, _db() as conn:
        if conn.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone() is None:
            raise HTTPException(404, "Compte inconnu")
        conn.execute(
            "UPDATE users SET role=?, is_admin=? WHERE id=?",
            (role, 1 if role == "admin" else 0, uid),
        )
    return {"ok": True, "role": role}


@app.get("/api/admin/invites")
def admin_invites(request: Request):
    _require_officer(request)
    now = time.time()
    with _db_lock, _db() as conn:
        rows = conn.execute("SELECT * FROM invites ORDER BY created DESC LIMIT 50").fetchall()
    out = []
    for r in rows:
        if r["used"] is not None:
            status = "used"
        elif r["expires"] < now:
            status = "expired"
        else:
            status = "pending"
        out.append({
            "token": r["token"], "email": r["email"], "note": r["note"], "created": r["created"],
            "expires": r["expires"], "status": status, "used_by": r["used_by"],
            "link": _invite_link(r["token"]),
        })
    return {"invites": out, "smtp_configured": mailer.smtp_configured()}


@app.post("/api/admin/invites")
def admin_create_invite(payload: InviteRequest, request: Request):
    _require_officer(request)
    now = time.time()
    token = secrets.token_urlsafe(24)
    email = payload.email.strip().lower() or None
    with _db_lock, _db() as conn:
        conn.execute(
            "INSERT INTO invites (token, email, note, created, expires) VALUES (?,?,?,?,?)",
            (token, email, payload.note.strip()[:120], now, now + INVITE_TTL_DAYS * 86400),
        )
    mail_result = None
    if payload.send_email and email:
        try:
            text, html = mailer.invite_mail(_invite_link(token), INVITE_TTL_DAYS)
            mailer.send_mail(email, "Invitation — LOTP Simulateur", text, html)
            mail_result = {"sent": True, "to": email}
        except mailer.MailError as exc:
            mail_result = {"sent": False, "error": str(exc)}
    return {"token": token, "link": _invite_link(token), "expires_in_days": INVITE_TTL_DAYS, "mail": mail_result}


@app.post("/api/admin/invites/{token}/send")
def admin_send_invite(token: str, request: Request):
    _require_officer(request)
    with _db_lock, _db() as conn:
        r = conn.execute("SELECT * FROM invites WHERE token=?", (token,)).fetchone()
    if r is None:
        raise HTTPException(404, "Invitation inconnue.")
    if r["used"] is not None or r["expires"] < time.time():
        raise HTTPException(400, "Invitation déjà utilisée ou expirée.")
    if not r["email"]:
        raise HTTPException(400, "Cette invitation est un lien libre (sans e-mail).")
    try:
        text, html = mailer.invite_mail(_invite_link(token), INVITE_TTL_DAYS)
        mailer.send_mail(r["email"], "Invitation — LOTP Simulateur", text, html)
    except mailer.MailError as exc:
        raise HTTPException(502, str(exc))
    return {"ok": True, "sent_to": r["email"]}


@app.delete("/api/admin/invites/{token}")
def admin_revoke_invite(token: str, request: Request):
    _require_officer(request)
    with _db_lock, _db() as conn:
        conn.execute("DELETE FROM invites WHERE token=? AND used IS NULL", (token,))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Calendrier des raids (planification + présences)
# ---------------------------------------------------------------------------
class RaidRequest(BaseModel):
    title: str = Field("", max_length=120)
    starts: float
    duration_min: int = Field(180, ge=15, le=720)
    note: str = Field("", max_length=300)


class SignupRequest(BaseModel):
    status: str = Field(..., max_length=10)


@app.get("/api/raids")
def api_raids(request: Request):
    user = _require_user(request)
    now = time.time()
    with _db_lock, _db() as conn:
        rows = conn.execute(
            "SELECT * FROM raids WHERE starts >= ? ORDER BY starts LIMIT 20", (now - 7200,)
        ).fetchall()
        past = conn.execute(
            "SELECT id, title, starts FROM raids WHERE starts < ? ORDER BY starts DESC LIMIT 5",
            (now - 7200,),
        ).fetchall()
        su_rows = conn.execute(
            "SELECT s.*, u.name AS user_name FROM raid_signups s LEFT JOIN users u ON u.email = s.user_email"
        ).fetchall()
    by_raid: dict = {}
    for r in su_rows:
        by_raid.setdefault(r["raid_id"], []).append(r)

    def pack(row) -> dict:
        counts = {"yes": 0, "maybe": 0, "no": 0}
        names = {"yes": [], "maybe": [], "no": []}
        mine = ""
        for s in by_raid.get(row["id"], []):
            st = s["status"]
            if st in counts:
                counts[st] += 1
                names[st].append(s["user_name"] or (s["user_email"] or "").split("@")[0])
            if s["user_email"] == user["email"]:
                mine = st
        return {
            "id": row["id"], "title": row["title"], "starts": row["starts"],
            "duration_min": row["duration_min"], "note": row["note"],
            "created_by": row["created_by"], "counts": counts, "names": names, "mine": mine,
        }

    return {
        "raids": [pack(r) for r in rows],
        "past": [dict(r) for r in past],
        "can_plan": _user_role(user) in ("officer", "admin"),
    }


@app.post("/api/raids")
def create_raid(payload: RaidRequest, request: Request):
    user = _require_officer(request)
    starts = float(payload.starts)
    if starts < time.time() - 3600:
        raise HTTPException(400, "La date du raid est déjà passée.")
    title = payload.title.strip()[:120] or "Raid de guilde"
    with _db_lock, _db() as conn:
        cur = conn.execute(
            "INSERT INTO raids (title, starts, duration_min, note, created_by, created) VALUES (?,?,?,?,?,?)",
            (title, starts, int(payload.duration_min), payload.note.strip()[:300],
             user["name"] or user["email"], time.time()),
        )
        rid = int(cur.lastrowid or 0)
    return {"ok": True, "id": rid}


@app.delete("/api/raids/{rid}")
def delete_raid(rid: int, request: Request):
    _require_officer(request)
    with _db_lock, _db() as conn:
        conn.execute("DELETE FROM raids WHERE id=?", (rid,))
        conn.execute("DELETE FROM raid_signups WHERE raid_id=?", (rid,))
    return {"ok": True}


@app.post("/api/raids/{rid}/signup")
def raid_signup(rid: int, payload: SignupRequest, request: Request):
    user = _require_user(request)
    st = payload.status.strip().lower()
    if st not in ("yes", "no", "maybe", ""):
        raise HTTPException(400, "Réponse inconnue.")
    with _db_lock, _db() as conn:
        if conn.execute("SELECT id FROM raids WHERE id=?", (rid,)).fetchone() is None:
            raise HTTPException(404, "Raid inconnu.")
        if st == "":
            conn.execute("DELETE FROM raid_signups WHERE raid_id=? AND user_email=?", (rid, user["email"]))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO raid_signups (raid_id, user_email, status, updated) VALUES (?,?,?,?)",
                (rid, user["email"], st, time.time()),
            )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Tableau de bord (activité de la guilde)
# ---------------------------------------------------------------------------
@app.get("/api/dashboard")
def api_dashboard(request: Request):
    _require_user(request)
    with _db_lock, _db() as conn:
        rows = conn.execute(
            "SELECT kind, member, created FROM guild_events ORDER BY created DESC, id DESC LIMIT 15"
        ).fetchall()
    out: dict = {"events": [dict(r) for r in rows], "members": None}
    try:
        data, ts = bnet.roster()
        out["members"] = {"count": len(data.get("members") or []), "fetched": ts}
    except bnet.BnetError:
        pass
    return out


# ---------------------------------------------------------------------------
# Mes personnages (liaison compte ↔ personnages de guilde)
# ---------------------------------------------------------------------------
class CharLinkRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=40)
    main: bool = False


@app.get("/api/me/chars")
def my_chars(request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        rows = conn.execute(
            "SELECT id, realm, name, display, is_main, created FROM char_links WHERE user_email=? ORDER BY is_main DESC, display",
            (user["email"],),
        ).fetchall()
    return {"chars": [dict(r) for r in rows]}


@app.get("/api/mains")
def api_mains(request: Request):
    """Tous les personnages liés de la guilde, regroupés par compte/main (sans e-mails)."""
    _require_user(request)
    with _db_lock, _db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT c.user_email, c.realm, c.name, c.display, c.is_main, COALESCE(u.name, '') AS user_name "
            "FROM char_links c LEFT JOIN users u ON u.email = c.user_email "
            "ORDER BY c.is_main DESC, c.name COLLATE NOCASE"
        ).fetchall()]
    groups: dict = {}
    for r in rows:
        g = groups.setdefault(r["user_email"], {"user": r["user_name"] or "(compte)", "chars": []})
        g["chars"].append({"realm": r["realm"], "name": r["name"],
                           "display": r["display"] or r["name"], "main": bool(r["is_main"])})
    out = list(groups.values())
    for g in out:
        g["chars"].sort(key=lambda c: (not c["main"], (c["display"] or "").lower()))
    out.sort(key=lambda g: ((not any(c["main"] for c in g["chars"])), g["user"].lower()))
    return {"ok": True, "accounts": out, "total_accounts": len(out), "total_chars": len(rows)}


@app.post("/api/me/chars")
def link_char(payload: CharLinkRequest, request: Request):
    user = _require_user(request)
    name = payload.name.strip()
    try:
        data, _ts = bnet.roster()
    except bnet.BnetError as exc:
        raise HTTPException(502, f"Roster indisponible : {exc}")
    hit = next((m for m in (data.get("members") or []) if (m.get("name") or "").lower() == name.lower()), None)
    if hit is None:
        raise HTTPException(404, "Personnage introuvable dans le roster de la guilde — vérifie l'orthographe.")
    realm = (hit.get("realm") or bnet.GUILD_REALM).lower()
    display = hit.get("name") or name
    lname = display.lower()
    with _db_lock, _db() as conn:
        dup = conn.execute(
            "SELECT id FROM char_links WHERE user_email=? AND realm=? AND name=?",
            (user["email"], realm, lname),
        ).fetchone()
        if dup is not None:
            raise HTTPException(400, "Ce personnage est déjà lié à ton compte.")
        taken = conn.execute(
            "SELECT id FROM char_links WHERE realm=? AND name=? AND user_email != ? LIMIT 1",
            (realm, lname, user["email"]),
        ).fetchone() is not None
        first_char = conn.execute(
            "SELECT COUNT(*) AS c FROM char_links WHERE user_email=?", (user["email"],)
        ).fetchone()["c"] == 0
        set_main = bool(payload.main) or first_char
        if set_main:
            conn.execute("UPDATE char_links SET is_main=0 WHERE user_email=?", (user["email"],))
        cur = conn.execute(
            "INSERT INTO char_links (user_email, realm, name, display, is_main, created) VALUES (?,?,?,?,?,?)",
            (user["email"], realm, lname, display, 1 if set_main else 0, time.time()),
        )
        cid = int(cur.lastrowid or 0)
    return {"id": cid, "ok": True, "taken": taken, "display": display}


@app.delete("/api/me/chars/{cid}")
def unlink_char(cid: int, request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        r = conn.execute("SELECT * FROM char_links WHERE id=?", (cid,)).fetchone()
        if r is None or (r["user_email"] != user["email"] and not user["is_admin"]):
            raise HTTPException(404, "Personnage non lié à ton compte.")
        conn.execute("DELETE FROM char_links WHERE id=?", (cid,))
    return {"ok": True}


@app.post("/api/me/chars/{cid}/main")
def set_main_char(cid: int, request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        r = conn.execute("SELECT * FROM char_links WHERE id=?", (cid,)).fetchone()
        if r is None or r["user_email"] != user["email"]:
            raise HTTPException(404, "Personnage non lié à ton compte.")
        conn.execute("UPDATE char_links SET is_main=0 WHERE user_email=?", (user["email"],))
        conn.execute("UPDATE char_links SET is_main=1 WHERE id=?", (cid,))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Bot Discord (annonces de guilde)
# ---------------------------------------------------------------------------
class BotConfigRequest(BaseModel):
    """Mise à jour PARTIELLE : seuls les champs transmis sont modifiés."""

    enabled: bool | None = None
    token: str = Field("", max_length=200)
    app_id: str | None = Field(None, max_length=32)
    channel_id: str = Field("", max_length=32)
    channel_name: str = Field("", max_length=120)
    notify_reports: bool | None = None
    notify_roster: bool | None = None


def _bot_config() -> sqlite3.Row | None:
    with _db_lock, _db() as conn:
        return conn.execute("SELECT * FROM bot_config WHERE id=1").fetchone()


def _bot_save(updates: dict) -> None:
    if not updates:
        return
    sets = ", ".join(f"{k}=?" for k in updates)
    with _db_lock, _db() as conn:
        conn.execute(f"UPDATE bot_config SET {sets}, updated=? WHERE id=1", (*updates.values(), time.time()))


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
                    discord_bot.send(token, channel, embeds=[discord_bot.report_embed(r)])
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
                discord_bot.send(token, channel, embeds=[discord_bot.raid_embed(dict(r), link)])
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
                discord_bot.send(token, channel, embeds=[discord_bot.raid_reminder_embed(dict(r), counts, link)])
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
                        discord_bot.send(token, channel, embeds=[discord_bot.roster_embed("join", members[n])])
                    for n in gone[:5]:
                        discord_bot.send(token, channel, embeds=[discord_bot.roster_embed("leave", {"name": n})])
    except Exception as exc:  # noqa: BLE001
        errs.append(f"roster — {exc}")

    if updates or notes or errs or cfg["last_error"]:
        updates["last_message"] = " ; ".join(notes)[:300] if notes else (cfg["last_message"] or "")
        updates["last_error"] = " ; ".join(errs)[:300]
        _bot_save(updates)


def _bot_loop() -> None:
    time.sleep(15)
    while True:
        try:
            _bot_tick()
        except Exception as exc:  # noqa: BLE001
            print(f"[bot] tick: {exc}")
        time.sleep(BOT_POLL_S)


@app.get("/api/admin/bot")
def admin_bot_get(request: Request):
    _require_admin(request)
    cfg = _bot_config()
    token = (cfg["token"] or "").strip()
    out = {
        "enabled": bool(cfg["enabled"]),
        "token_set": bool(token),
        "token_hint": token[-4:] if token else "",
        "app_id": cfg["app_id"] or "",
        "channel_id": cfg["channel_id"] or "",
        "channel_name": cfg["channel_name"] or "",
        "notify_reports": bool(cfg["notify_reports"]),
        "notify_roster": bool(cfg["notify_roster"]),
        "last_message": cfg["last_message"] or "",
        "last_error": cfg["last_error"] or "",
        "last_report_t": cfg["last_report_t"],
        "updated": cfg["updated"],
        "invite_url": discord_bot.invite_url(cfg["app_id"]) if (cfg["app_id"] or "").strip() else "",
        "status": "unconfigured",
    }
    if token:
        try:
            who = discord_bot.me(token)
            out["status"] = "ok"
            out["bot_user"] = str(who.get("username") or "?")
        except discord_bot.DiscordError as exc:
            out["status"] = "error"
            out["status_error"] = str(exc)
    return out


@app.post("/api/admin/bot")
def admin_bot_save(payload: BotConfigRequest, request: Request):
    _require_admin(request)
    updates: dict = {}
    if payload.enabled is not None:
        updates["enabled"] = 1 if payload.enabled else 0
    if payload.app_id is not None:
        updates["app_id"] = payload.app_id.strip()
    if payload.notify_reports is not None:
        updates["notify_reports"] = 1 if payload.notify_reports else 0
    if payload.notify_roster is not None:
        updates["notify_roster"] = 1 if payload.notify_roster else 0
    if payload.channel_id.strip():
        updates["channel_id"] = payload.channel_id.strip()
        updates["channel_name"] = payload.channel_name.strip()[:120]
    if payload.token.strip():
        try:
            discord_bot.me(payload.token.strip())
        except discord_bot.DiscordError as exc:
            raise HTTPException(400, f"Token refusé par Discord — {exc}")
        updates["token"] = payload.token.strip()
    _bot_save(updates)
    return {"ok": True}


@app.get("/api/admin/bot/guilds")
def admin_bot_guilds(request: Request):
    _require_admin(request)
    cfg = _bot_config()
    token = (cfg["token"] or "").strip()
    if not token:
        raise HTTPException(400, "Token du bot non configuré.")
    try:
        gs = discord_bot.guilds(token)
    except discord_bot.DiscordError as exc:
        raise HTTPException(502, str(exc))
    if not gs:
        raise HTTPException(404, "Le bot n'est encore sur aucun serveur — utilise le lien d'invitation.")
    return {"guilds": [{"id": str(g.get("id")), "name": g.get("name")} for g in gs]}


@app.get("/api/admin/bot/guilds/{guild_id}/channels")
def admin_bot_channels(guild_id: str, request: Request):
    _require_admin(request)
    cfg = _bot_config()
    token = (cfg["token"] or "").strip()
    if not token:
        raise HTTPException(400, "Token du bot non configuré.")
    try:
        chans = discord_bot.channels(token, guild_id)
    except discord_bot.DiscordError as exc:
        raise HTTPException(502, str(exc))
    return {"channels": chans}


@app.post("/api/admin/bot/test")
def admin_bot_test(request: Request):
    _require_admin(request)
    cfg = _bot_config()
    token, channel = (cfg["token"] or "").strip(), (cfg["channel_id"] or "").strip()
    if not token or not channel:
        raise HTTPException(400, "Configure d'abord le token et le salon (Enregistrer).")
    try:
        discord_bot.send(token, channel, embeds=[{
            "title": "✅ LOTP Simulateur — test",
            "description": "Le bot est correctement configuré : les annonces de la guilde arriveront dans ce salon.",
            "color": 0xDFA55A,
        }])
    except discord_bot.DiscordError as exc:
        raise HTTPException(502, str(exc))
    _bot_save({"last_message": "message de test envoyé"})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Paramètres du compte (langue, nom, mot de passe)
# ---------------------------------------------------------------------------
@app.api_route("/settings", methods=["GET", "HEAD"])
def settings_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "settings.html")


class SettingsRequest(BaseModel):
    lang: str | None = Field(None, max_length=5)
    name: str | None = Field(None, max_length=60)
    voice_nick: str | None = Field(None, max_length=60)


@app.post("/api/me/settings")
def save_my_settings(payload: SettingsRequest, request: Request):
    user = _require_user(request)
    updates: dict = {}
    if payload.lang is not None:
        lang = payload.lang.strip().lower()
        if lang not in ("", "fr", "en"):
            raise HTTPException(400, "Langue inconnue.")
        updates["lang"] = lang
    if payload.name is not None:
        name = payload.name.strip()[:60]
        if not name:
            raise HTTPException(400, "Le nom ne peut pas être vide.")
        updates["name"] = name
    if payload.voice_nick is not None:
        updates["voice_nick"] = " ".join(payload.voice_nick.split())[:60]
    if updates:
        sets = ", ".join(f"{k}=?" for k in updates)
        with _db_lock, _db() as conn:
            conn.execute(f"UPDATE users SET {sets} WHERE id=?", (*updates.values(), user["id"]))
    return {"ok": True, "lang": updates.get("lang", _user_lang(user)), "name": updates.get("name", user["name"]),
            "voice_nick": updates.get("voice_nick", user["voice_nick"])}


class PasswordChangeRequest(BaseModel):
    current: str = Field(..., max_length=200)
    new: str = Field(..., min_length=8, max_length=200)


@app.post("/api/me/password")
def change_my_password(payload: PasswordChangeRequest, request: Request):
    user = _require_user(request)
    if not _verify_password(payload.current, user["pwd"]):
        raise HTTPException(400, "Mot de passe actuel incorrect.")
    token = request.cookies.get(SESSION_COOKIE) or ""
    with _db_lock, _db() as conn:
        conn.execute("UPDATE users SET pwd=? WHERE id=?", (_hash_password(payload.new), user["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id=? AND token != ?", (user["id"], token))
    return {"ok": True}


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
# Portail vocal (ts.gensbien.fr) — réservé aux membres connectés
# ---------------------------------------------------------------------------
# Le vhost Apache de ts.gensbien.fr pose l'en-tête X-LOTP-Voice puis proxyfie
# vers cette app : les requêtes marquées sont réécrites vers /__voice* où la
# session est vérifiée avant tout relais vers le client web interne (WebSpeak).
VOICE_BACKEND = os.environ.get("VOICE_BACKEND", "http://127.0.0.1:3040").rstrip("/")
VOICE_BACKEND_WS = VOICE_BACKEND.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
VOICE_PUBLIC_HOST = os.environ.get("VOICE_PUBLIC_HOST", "ts.gensbien.fr")
VOICE_CLIENT_ZIP = DATA_DIR / "voice" / "LOTP-TeamSpeak.zip"
_VOICE_APP_BASE = PUBLIC_BASE_URL or "https://lotp.gensbien.fr"

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
    """Réécrit les requêtes du vhost vocal (en-tête X-LOTP-Voice) vers /__voice*."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") in ("http", "websocket"):
            headers = dict(scope.get("headers") or [])
            if headers.get(b"x-lotp-voice"):
                scope = dict(scope)
                scope["lotp_voice"] = True
                path = scope.get("path") or "/"
                scope["lotp_voice_orig_path"] = path
                scope["path"] = "/__voice" + path
                scope["raw_path"] = scope["path"].encode()
        await self.app(scope, receive, send)


app.add_middleware(VoiceGateMiddleware)


@app.middleware("http")
async def html_no_cache(request: Request, call_next):
    """Pages HTML et fichiers statiques : toujours revalidés (évite les vieilles versions en cache)."""
    response = await call_next(request)
    ctype = response.headers.get("content-type", "")
    if ctype.startswith("text/html") or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response

_VOICE_HOP_REQ = {"host", "cookie", "connection", "keep-alive", "transfer-encoding", "upgrade",
                  "proxy-connection", "te", "trailer", "expect", "x-lotp-voice", "content-length"}
_VOICE_HOP_RESP = {"connection", "keep-alive", "transfer-encoding", "upgrade",
                   "content-encoding", "content-length"}


@app.get("/api/voice/handoff")
def voice_handoff(request: Request, next: str = ""):
    """Répare la session pour le sous-domaine vocal puis renvoie vers le client.

    Les cookies créés avant la v031 sont host-only (lotp.gensbien.fr) : le portail
    vocal ne les voit pas. Ici on réémet le cookie avec Domain=.gensbien.fr puis on
    renvoie vers ts.gensbien.fr — sans passage par la page de connexion.
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


@app.api_route("/__voice{rest:path}",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def voice_portal(request: Request, rest: str):
    if not request.scope.get("lotp_voice"):
        raise HTTPException(404)
    if _get_session_user(request) is None:
        nxt = "https://" + VOICE_PUBLIC_HOST + request.scope.get("lotp_voice_orig_path", request.url.path)
        if request.url.query:
            nxt += "?" + request.url.query
        return RedirectResponse(f"{_VOICE_APP_BASE}/api/voice/handoff?next={quote(nxt, safe='')}", status_code=302)
    if rest == "/__lotp_extras.js":
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
        if "__lotp_extras.js" not in html:
            tag = '<script src="/__lotp_extras.js"></script>'
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
    if not websocket.scope.get("lotp_voice"):
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
    return FileResponse(VOICE_CLIENT_ZIP, filename="LOTP-TeamSpeak.zip",
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


def _require_officer(request: Request):
    user = _require_user(request)
    if _user_role(user) not in ("admin", "officer"):
        raise HTTPException(403, "Réservé aux officiers et aux administrateurs.")
    return user


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

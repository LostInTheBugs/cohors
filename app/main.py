"""LOTP Simulateur — web app (FastAPI).

Accounts: invitation-only registration (admin-generated links), login sessions
(signed random token in an HttpOnly cookie), admin panel (invites + users).
Simulations run in the official SimulationCraft Docker image via
`worker/simrun.py` (the app container mounts the host Docker socket).

v2026.09.003: accounts + admin.
"""
from __future__ import annotations

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

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
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
                "INSERT INTO users (email, name, pwd, is_admin, active, created) VALUES (?,?,?,1,1,?)",
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
                        samesite="lax", secure=COOKIE_SECURE, path="/")


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


def _require_admin(request: Request) -> sqlite3.Row:
    user = _require_user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "Réservé à l'administrateur")
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
        res = run_sim(profile_path=input_file, iterations=iterations, outdir=input_file.parent, timeout=SIM_TIMEOUT, extra=extra)
        ok = bool(res.get("ok"))
        weights = json.dumps(res.get("scale_factors")) if res.get("scale_factors") else None
        gear = json.dumps(res.get("gear")) if res.get("gear") else None
        with _db_lock, _db() as conn:
            conn.execute(
                """UPDATE sims SET status=?, dps=?, dps_error_pct=?, wall_s=?, report_html=?, report_json=?,
                                    error=?, finished=?, weights=?, gear=? WHERE id=?""",
                (
                    "done" if ok else "failed",
                    res.get("dps"), res.get("dps_error_pct"), res.get("wall_s"),
                    res.get("html"), res.get("json"),
                    None if ok else (res.get("log_tail") or "échec de la simulation")[-2000:],
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
    if not user["is_admin"]:
        return RedirectResponse("/", status_code=302)
    return FileResponse(STATIC_DIR / "admin.html")


@app.api_route("/characters", methods=["GET", "HEAD"])
def characters_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "characters.html")


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


@app.api_route("/gear", methods=["GET", "HEAD"])
def gear_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "gear.html")


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
    return {"ok": True, "name": user["name"], "is_admin": bool(user["is_admin"])}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with _db_lock, _db() as conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    user = _get_session_user(request)
    if user is None:
        raise HTTPException(401, "Non connecté")
    return {"email": user["email"], "name": user["name"], "is_admin": bool(user["is_admin"])}


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
            cur = conn.execute(
                "INSERT INTO users (email, name, pwd, is_admin, active, created) VALUES (?,?,?,0,1,?)",
                (email, name, _hash_password(payload.password), now),
            )
            user_id = int(cur.lastrowid or 0)
        conn.execute("UPDATE invites SET used=?, used_by=? WHERE token=?", (now, user_id, payload.token))
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
        "kind": (r["kind"] or "dps"),
        "weights": _parse_weights_json(r["weights"]) if r["kind"] == "weights" else None,
        "gear": _parse_weights_json(r["gear"]) if r["kind"] == "gear" else None,
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
            """SELECT u.id, u.email, u.name, u.is_admin, u.active, u.created, u.last_login,
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


@app.get("/api/admin/invites")
def admin_invites(request: Request):
    _require_admin(request)
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
    _require_admin(request)
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
    _require_admin(request)
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
    _require_admin(request)
    with _db_lock, _db() as conn:
        conn.execute("DELETE FROM invites WHERE token=? AND used IS NULL", (token,))
    return {"ok": True}


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
        if payload.main:
            conn.execute("UPDATE char_links SET is_main=0 WHERE user_email=?", (user["email"],))
        cur = conn.execute(
            "INSERT INTO char_links (user_email, realm, name, display, is_main, created) VALUES (?,?,?,?,?,?)",
            (user["email"], realm, lname, display, 1 if payload.main else 0, time.time()),
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
    enabled: bool = False
    token: str = Field("", max_length=200)
    app_id: str = Field("", max_length=32)
    channel_id: str = Field("", max_length=32)
    channel_name: str = Field("", max_length=120)
    notify_reports: bool = True
    notify_roster: bool = True


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
    """Un passage d'annonces : nouveaux rapports WCL + mouvements de roster."""
    cfg = _bot_config()
    if cfg is None or not cfg["enabled"]:
        return
    token, channel = (cfg["token"] or "").strip(), (cfg["channel_id"] or "").strip()
    if not token or not channel:
        return
    updates: dict = {}
    notes: list[str] = []
    errs: list[str] = []

    if cfg["notify_reports"]:
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

    if cfg["notify_roster"]:
        try:
            data, _ts = bnet.roster()
            members = {m["name"]: m for m in (data.get("members") or []) if m.get("name")}
            snap = set(json.loads(cfg["roster_snap"] or "[]"))
            if not snap:
                updates["roster_snap"] = json.dumps(sorted(members))
            else:
                added = sorted(set(members) - snap)
                gone = sorted(snap - set(members))
                for n in added[:5]:
                    discord_bot.send(token, channel, embeds=[discord_bot.roster_embed("join", members[n])])
                for n in gone[:5]:
                    discord_bot.send(token, channel, embeds=[discord_bot.roster_embed("leave", {"name": n})])
                if added or gone:
                    updates["roster_snap"] = json.dumps(sorted(members))
                    notes.append(f"roster : +{len(added)} / -{len(gone)}")
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
    updates: dict = {
        "enabled": 1 if payload.enabled else 0,
        "app_id": payload.app_id.strip(),
        "notify_reports": 1 if payload.notify_reports else 0,
        "notify_roster": 1 if payload.notify_roster else 0,
    }
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
# Misc
# ---------------------------------------------------------------------------
@app.api_route("/api/health", methods=["GET", "HEAD"])
def health():
    with _db_lock, _db() as conn:
        queued = conn.execute("SELECT COUNT(*) AS c FROM sims WHERE status='queued'").fetchone()["c"]
        running = conn.execute("SELECT COUNT(*) AS c FROM sims WHERE status='running'").fetchone()["c"]
    return {"ok": True, "version": VERSION, "queued": queued, "running": bool(running), "simc_image": SIMC_IMAGE}

"""Cohors — compagnon de guilde World of Warcraft (web app FastAPI).

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
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote

import httpx
import websockets
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from worker.simrun import run_sim
import httpx

from app import bnet, discord_bot, mailer, wcl
from app.security import check_profile, hash_password as _hash_password, real_client_ip, verify_password as _verify_password

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
BRAND_DIR = DATA_DIR / "branding"
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

SESSION_COOKIE = "cohors_session"
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "30"))
INVITE_TTL_DAYS = int(os.environ.get("INVITE_TTL_DAYS", "7"))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") not in ("0", "false", "no", "")
COOKIE_DOMAIN = os.environ.get("COOKIE_DOMAIN", "").strip() or None
BOT_POLL_S = int(os.environ.get("BOT_POLL_S", "300"))  # intervalle du bot Discord (secondes)
# Relevés quotidiens (évolution des personnages liés) — v2026.09.054.
# TTL max 30 jours : Blizzard Developer API ToU §18 (« retain data ... no longer than 30 days »).
SNAP_POLL_S = float(os.environ.get("SNAPSHOT_POLL_S", "900"))       # tick de la boucle (s)
SNAP_REFRESH_MIN = float(os.environ.get("SNAPSHOT_REFRESH_MIN", "360"))  # re-relevé si dernier > 6 h
SNAP_KEEP_DAYS = min(30, max(2, int(os.environ.get("SNAPSHOT_KEEP_DAYS", "30"))))
SNAP_REFRESH_MIN_OTHER = float(os.environ.get("SNAPSHOT_REFRESH_MIN_OTHER", "1200"))  # roster : 20 h
SNAP_MAX_PER_TICK = int(os.environ.get("SNAPSHOT_MAX_PER_TICK", "60"))  # borne le temps du passage
PROF_REFRESH_DAYS = float(os.environ.get("PROFESSIONS_REFRESH_DAYS", "7"))  # métiers : 7 j

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
            CREATE TABLE IF NOT EXISTS job_config (
                key     TEXT PRIMARY KEY,
                value   TEXT NOT NULL DEFAULT '',
                updated REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mail_config (
                key     TEXT PRIMARY KEY,
                value   TEXT NOT NULL DEFAULT '',
                updated REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_config (
                key     TEXT PRIMARY KEY,
                value   TEXT NOT NULL DEFAULT '',
                updated REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_status (
                slug     TEXT PRIMARY KEY,
                last_run REAL NOT NULL DEFAULT 0,
                detail   TEXT NOT NULL DEFAULT '',
                error    TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                provider      TEXT PRIMARY KEY,
                client_id     TEXT NOT NULL DEFAULT '',
                client_secret TEXT NOT NULL DEFAULT '',
                updated       REAL NOT NULL DEFAULT 0
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS char_snapshots (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                realm  TEXT NOT NULL,
                name   TEXT NOT NULL,
                day    TEXT NOT NULL,
                ts     REAL NOT NULL,
                data   TEXT NOT NULL,
                UNIQUE (realm, name, day)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_char_snapshots_lookup ON char_snapshots(realm, name, day)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS char_professions (
                realm TEXT NOT NULL,
                name  TEXT NOT NULL,
                ts    REAL NOT NULL,
                data  TEXT NOT NULL,
                PRIMARY KEY (realm, name)
            )
            """
        )
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
                           ("sims", "kind"), ("sims", "weights"), ("sims", "gear"),
                           ("sims", "plan")):
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
        # v2026.09.062 — alertes « paliers » + récap hebdo du bot.
        bcols = [r["name"] for r in conn.execute("PRAGMA table_info(bot_config)").fetchall()]
        for bcol in ("notify_chars", "notify_weekly"):
            if bcol not in bcols:
                conn.execute(f"ALTER TABLE bot_config ADD COLUMN {bcol} INTEGER NOT NULL DEFAULT 1")
        # v2026.09.109 — wishlist : pièces prioritaires (⭐) + BIS déduits des sims « Top Stuff ».
        wcols = [r["name"] for r in conn.execute("PRAGMA table_info(wishlist)").fetchall()]
        if "prio" not in wcols:
            conn.execute("ALTER TABLE wishlist ADD COLUMN prio INTEGER NOT NULL DEFAULT 0")
        # v2026.09.111 — identité de la guilde (logo, nom, fond) pour réutiliser le site avec une autre guilde.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS branding ("
            " id INTEGER PRIMARY KEY CHECK (id = 1),"
            " guild_name TEXT NOT NULL DEFAULT '',"
            " guild_short TEXT NOT NULL DEFAULT '',"
            " logo TEXT NOT NULL DEFAULT '',"
            " bg TEXT NOT NULL DEFAULT '',"
            " bg_color TEXT NOT NULL DEFAULT '',"
            " updated REAL NOT NULL DEFAULT 0,"
            " updated_by TEXT NOT NULL DEFAULT '')")
        if "last_recap" not in bcols:
            conn.execute("ALTER TABLE bot_config ADD COLUMN last_recap REAL NOT NULL DEFAULT 0")
        # v2026.09.064 — import du calendrier in-game (addon Cohors).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gcal_import (
                id     INTEGER PRIMARY KEY CHECK (id = 1),
                ts     REAL NOT NULL,
                player TEXT NOT NULL DEFAULT '',
                data   TEXT NOT NULL
            )
            """
        )
        # v2026.09.079 — préparation de raid (recettes d'objets, plan, apports des membres).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prep_recipes (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                name    TEXT NOT NULL,
                mats    TEXT NOT NULL DEFAULT '[]',
                created REAL NOT NULL DEFAULT 0,
                updated REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prep_plan (
                id         INTEGER PRIMARY KEY CHECK (id = 1),
                title      TEXT NOT NULL DEFAULT '',
                event_ts   REAL NOT NULL DEFAULT 0,
                items      TEXT NOT NULL DEFAULT '[]',
                updated    REAL NOT NULL DEFAULT 0,
                updated_by TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prep_claims (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                mat     TEXT NOT NULL,
                qty     REAL NOT NULL DEFAULT 0,
                user    TEXT NOT NULL,
                name    TEXT NOT NULL DEFAULT '',
                updated REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_prep_claims ON prep_claims(mat, user)")
        # v2026.09.093 — stock en banque de guilde par compos (préparation de raid).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prep_bank (
                mat        TEXT PRIMARY KEY,
                qty        REAL NOT NULL DEFAULT 0,
                updated    REAL NOT NULL DEFAULT 0,
                updated_by TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # v2026.09.080 — recettes des artisans (export addon /cohors recettes).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS craft_recipes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                crafter    TEXT NOT NULL,
                realm      TEXT NOT NULL DEFAULT '',
                profession TEXT NOT NULL DEFAULT '',
                item       TEXT NOT NULL,
                item_id    INTEGER NOT NULL DEFAULT 0,
                mats       TEXT NOT NULL DEFAULT '[]',
                updated    REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_craft_recipes ON craft_recipes(crafter, item)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS game_recipes (
                id       INTEGER PRIMARY KEY,
                prof     TEXT NOT NULL,
                tier     TEXT NOT NULL DEFAULT '',
                exp_rank INTEGER NOT NULL DEFAULT 0,
                item     TEXT NOT NULL,
                item_id  INTEGER NOT NULL DEFAULT 0,
                rank_no  INTEGER NOT NULL DEFAULT 1,
                mats     TEXT NOT NULL DEFAULT '[]',
                updated  REAL NOT NULL DEFAULT 0,
                item_en  TEXT NOT NULL DEFAULT '',
                tier_en  TEXT NOT NULL DEFAULT '',
                prof_en  TEXT NOT NULL DEFAULT '',
                mats_en  TEXT NOT NULL DEFAULT '[]'
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_game_recipes_prof ON game_recipes(prof)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mplus_posts (
                user    TEXT PRIMARY KEY,
                name    TEXT NOT NULL DEFAULT '',
                roles   TEXT NOT NULL DEFAULT '[]',
                slots   TEXT NOT NULL DEFAULT '[]',
                keys    TEXT NOT NULL DEFAULT '[]',
                updated REAL NOT NULL DEFAULT 0
            )
            """
        )
        # v2026.09.090 — périodes d'indisponibilité des membres (croisées avec le raid en préparation).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS unavails (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                email    TEXT NOT NULL,
                day_from TEXT NOT NULL,
                day_to   TEXT NOT NULL,
                note     TEXT NOT NULL DEFAULT '',
                created  REAL NOT NULL DEFAULT 0
            )
            """
        )
        # v2026.09.102 — visées des officiers par événement du calendrier in-game (raids/boss/départ).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gcal_meta (
                event_key  TEXT PRIMARY KEY,
                data       TEXT NOT NULL DEFAULT '{}',
                updated    REAL NOT NULL DEFAULT 0,
                updated_by TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # v2026.09.104 — butin (journal de jeu) : où trouver quoi (raids, donjons/MM+).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS item_loot (
                item_id INTEGER NOT NULL,
                kind    TEXT NOT NULL DEFAULT '',
                inst_fr TEXT NOT NULL DEFAULT '',
                inst_en TEXT NOT NULL DEFAULT '',
                boss_fr TEXT NOT NULL DEFAULT '',
                boss_en TEXT NOT NULL DEFAULT '',
                updated REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_item_loot_item ON item_loot(item_id)")
        # v2026.09.087 — alertes MM+ (clés recherchées) & notifications personnelles.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mplus_alerts (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                email     TEXT NOT NULL,
                dungeon   TEXT NOT NULL DEFAULT '',
                min_level INTEGER NOT NULL DEFAULT 2,
                created   REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notifs (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                email   TEXT NOT NULL,
                kind    TEXT NOT NULL DEFAULT '',
                data    TEXT NOT NULL DEFAULT '{}',
                seen    INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL DEFAULT 0
            )
            """
        )
        for _stmt in (
            "ALTER TABLE craft_recipes ADD COLUMN expansion TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE craft_recipes ADD COLUMN exp_rank INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE game_recipes ADD COLUMN item_en TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE game_recipes ADD COLUMN tier_en TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE game_recipes ADD COLUMN prof_en TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE game_recipes ADD COLUMN mats_en TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE prep_plan ADD COLUMN raids TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE prep_plan ADD COLUMN bosses TEXT NOT NULL DEFAULT '[]'",
        ):
            try:
                conn.execute(_stmt)
            except Exception:
                pass
        # v2026.09.085 — données bilingues : force un re-relevé (noms EN) des métiers et recettes déjà stockés.
        for _key, _stmt in (
            ("loc_en_profs_v1", "UPDATE char_professions SET ts = 0"),
            ("loc_en_recipes_v1", "UPDATE game_recipes SET updated = 0"),
        ):
            if conn.execute("SELECT value FROM meta WHERE key=?", (_key,)).fetchone() is None:
                conn.execute(_stmt)
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (_key, str(int(time.time()))))


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


def _user_locale(request: Request) -> str:
    """Locale des données de jeu selon la langue du compte (« en » → en_US, sinon fr_FR)."""
    user = _get_session_user(request)
    return "en_US" if (user is not None and _user_lang(user) == "en") else "fr_FR"


def _owns_char(user: sqlite3.Row, name: str) -> bool:
    """Le personnage (par nom, insensible à la casse) est-il lié au compte ?"""
    with _db_lock, _db() as conn:
        row = conn.execute(
            "SELECT 1 AS x FROM char_links WHERE user_email=? AND name=?",
            (user["email"], (name or "").lower()),
        ).fetchone()
    return row is not None


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


# ---------------------------------------------------------------------------
# Identité de la guilde (v2026.09.111) — logo, nom et fond personnalisables.
# ---------------------------------------------------------------------------
_IMG_SIGS = ((b"\x89PNG\r\n\x1a\n", "png", "image/png"),
             (b"\xff\xd8\xff", "jpg", "image/jpeg"),
             (b"GIF87a", "gif", "image/gif"),
             (b"GIF89a", "gif", "image/gif"),
             (b"RIFF", "webp", "image/webp"))
_IMG_MIMES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


def _img_type(raw: bytes):
    """(extension, type MIME) d'après la signature du fichier, sinon ('', '')."""
    for sig, ext, mime in _IMG_SIGS:
        if raw.startswith(sig):
            if sig == b"RIFF" and raw[8:12] != b"WEBP":
                continue
            return ext, mime
    return "", ""


def _brand_row(conn) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM branding WHERE id=1").fetchone()
    if row is None:
        conn.execute("INSERT INTO branding (id, updated) VALUES (1, ?)", (time.time(),))
        row = conn.execute("SELECT * FROM branding WHERE id=1").fetchone()
    return row


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
    # Voir app/security.py:real_client_ip — l'IP réelle est la DERNIÈRE entrée d'X-Forwarded-For
    # (ajoutée par notre proxy) ; les précédentes sont fournies par le client et forgeables.
    return real_client_ip(request.headers.get("x-forwarded-for"),
                          request.client.host if request.client else None)


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
        if kind == "stuff":
            _run_stuff(row)
            return
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


def _run_stuff_bis(sim_id: str, parsed: dict, items: list[dict], plan: dict, t0: float) -> None:
    """Mode BIS : liste des meilleures pièces du guide, possession et comparaison."""
    blk = _stuff_bis_list(parsed["cls"], parsed["spec"]) or {}
    slot_fr = {}
    with _db_lock, _db() as conn:
        loc = "fr_FR"
        loc_en = "en_US"
        results = {"mode": "bis", "source": blk.get("source_url", ""),
                   "source_fr": blk.get("source_label_fr", ""),
                   "updated_fr": blk.get("updated_fr", ""), "slots": [],
                   "content": plan.get("content") or "raid"}
        owned_by_id, equipped_by_id = {}, set()
        for it in items:
            m = re.search(r"id=(\d+)", it.get("opts") or "")
            if not m:
                continue
            owned_by_id[int(m.group(1))] = it
            if it.get("equipped"):
                equipped_by_id.add(int(m.group(1)))
        cur_by_slot = {it["slot"]: it for it in items if it.get("equipped")}
        need_stats = []
        for e in blk.get("slots") or []:
            row = {"slot": e.get("slot"), "id": e.get("id"),
                   "src_fr": e.get("src_fr", ""), "src_en": e.get("src_en", "")}
            cur = cur_by_slot.get(e.get("slot"))
            row["current"] = ({"name": cur.get("name"), "ilvl": cur.get("ilvl")} if cur else None)
            row["equipped"] = int(e.get("id") or 0) in equipped_by_id
            row["owned"] = int(e.get("id") or 0) in owned_by_id
            if row["owned"] and not row["equipped"]:
                row["bag_ilvl"] = (owned_by_id[int(e.get("id"))] or {}).get("ilvl")
            try:
                row["name_fr"] = bnet.item(int(e.get("id")), locale=loc).get("name") or ""
                row["name_en"] = bnet.item(int(e.get("id")), locale=loc_en).get("name") or ""
            except Exception:  # noqa: BLE001
                row["name_fr"], row["name_en"] = "", ""
            need_stats.append({"opts": f',id={int(e.get("id"))}', "ilvl": 0})
            results["slots"].append(row)
        _stuff_max_levels(need_stats, conn)
        for row, st in zip(results["slots"], need_stats):
            row["max_ilvl"] = st.get("max_ilvl")
    results["missing"] = sum(1 for r in results["slots"] if not r["owned"] and not r["equipped"])
    results["have"] = len(results["slots"]) - results["missing"]
    wall = time.time() - t0
    with _db_lock, _db() as conn:
        conn.execute("UPDATE sims SET status='done', wall_s=?, gear=?, finished=? WHERE id=?",
                     (round(wall, 3), json.dumps(results), time.time(), sim_id))


def _run_stuff(row: sqlite3.Row) -> None:
    """Exécute un « Stuff conseillé » (kind='stuff') : stats des pièces puis classement."""
    sim_id = row["id"]
    workdir = Path(row["input_file"]).parent
    t0 = time.time()
    res = None
    try:
        plan = json.loads(row["plan"] or "{}")
        with _db_lock, _db() as conn:
            prof = conn.execute("SELECT id, name, input FROM profiles WHERE id=?",
                                (plan.get("profile_id"),)).fetchone()
        if prof is None:
            raise RuntimeError("Profil introuvable (il a peut-être été supprimé).")
        parsed = _stuff_parse_export(prof["input"])
        parsed["_raw"] = prof["input"]
        if not parsed["bags"]:
            raise RuntimeError("Aucune pièce dans les sacs de cet export — réexporte avec l'addon.")
        items, seen = [], set()
        for it in parsed["equipped"]:
            key = (it["slot"], it["opts"])
            if key in seen:
                continue
            seen.add(key)
            d = dict(it)
            d["equipped"] = True
            items.append(d)
        for it in parsed["bags"]:
            key = (it["slot"], it["opts"])
            if key in seen:
                continue
            seen.add(key)
            d = dict(it)
            d["equipped"] = False
            items.append(d)
        boosted, examples = 0, []
        if plan.get("max_rank"):
            with _db_lock, _db() as conn:
                boosted, examples = _stuff_max_levels(items, conn)
        if (plan.get("mode") or "cur") == "bis":
            _run_stuff_bis(sim_id, parsed, items, plan, t0)
            return
        stats, dropped, crashed = _stuff_fetch_stats(items, parsed["cls"], parsed["spec"], workdir)
        for it in items:
            it["stats"] = stats.get(it.get("actor"))
        content = plan.get("content") or "raid"
        is_heal = parsed["spec"] in HEAL_SPECS
        if is_heal:
            prio = dict(STUFF_HEAL_PRIO.get(f'{parsed["cls"]}/{parsed["spec"]}')
                        or {"orders": {}, "source": "", "source_fr": ""})
            prio["known"] = bool(prio.get("orders"))
            results = _stuff_rank_heal(parsed, items, prio, content)
            results["prio_known"] = prio["known"]
            results["note_fr"] = prio.get("note_fr") or ""
            note = None if prio["known"] else "Classé au niveau d'objet : priorités de stats pas encore répertoriées pour cette spécialisation."
        else:
            equipped_by_slot = {it["slot"]: it for it in items if it.get("equipped")}
            cands = _stuff_plausible(items, equipped_by_slot)
            if not cands:
                raise RuntimeError("Rien dans les sacs ne peut battre l'équipement actuel — bon signe !")
            text, n_cands = _stuff_sim_input(parsed, items, plan.get("loadout") or {})
            sim_file = workdir / "stuff.simc"
            sim_file.write_text(text)
            extra = list(STUFF_CONTENTS[content]["opts"])
            res = run_sim(profile_path=sim_file, iterations=STUFF_ITERATIONS, outdir=workdir,
                          timeout=SIM_TIMEOUT, extra=extra)
            note = None
            if not res.get("ok") and res.get("rc") == 139:
                stripped, removed = _strip_crash_items(text)
                if removed:
                    sim_file.write_text(stripped)
                    res = run_sim(profile_path=sim_file, iterations=STUFF_ITERATIONS, outdir=workdir,
                                  timeout=SIM_TIMEOUT, extra=extra)
                    if res.get("ok"):
                        note = "Calcul lancé sans " + ", ".join(removed) + " — cet objet fait planter le moteur SimulationCraft (bug connu)."
            if not res.get("ok"):
                raise RuntimeError("La simulation a échoué : " + ((res.get("log_tail") or "raison inconnue")[-400:]))
            base = None
            try:
                data = json.loads(Path(res["json"]).read_text())
                p0 = (data.get("sim", {}).get("players") or [{}])[0]
                base = ((p0.get("collected_data") or {}).get("dps") or {}).get("mean")
            except Exception:  # noqa: BLE001
                base = None
            items_res = []
            for g in (res.get("gear") or []):
                m = re.search(r"\[(\w+)\]$", g.get("name") or "")
                dps = float(g.get("dps") or 0.0)
                items_res.append({"name": re.sub(r"\s*\[[^\]]*\]\s*$", "", g.get("name") or ""),
                                  "slot": m.group(1) if m else "",
                                  "dps": dps,
                                  "gain": (dps - base) if base else None,
                                  "gain_pct": round(100 * (dps - base) / base, 2) if base else None})
            items_res.sort(key=lambda x: -(x["dps"] or 0))
            results = {"mode": "sim", "content": content, "baseline": base,
                       "iterations": STUFF_ITERATIONS, "items": items_res,
                       "candidates": n_cands, "report": bool(res.get("html"))}
        results["sim_settings"] = STUFF_CONTENTS.get(content, {}).get("sim_fr", "")
        results["max_rank"] = bool(plan.get("max_rank"))
        results["boosted"] = boosted
        results["boost_examples"] = examples
        results["equipped_count"] = len(parsed["equipped"])
        results["bag_count"] = len(parsed["bags"])
        results["valid_count"] = sum(1 for it in items if it.get("stats"))
        results["invalid_count"] = dropped
        if crashed:
            results["crashed"] = [c.get("name") or "?" for c in crashed]
        results["loadout"] = (plan.get("loadout") or {}).get("name") or ""
        wall = time.time() - t0
        with _db_lock, _db() as conn:
            conn.execute(
                """UPDATE sims SET status='done', dps=?, wall_s=?, gear=?, error=?, report_html=?, report_json=?, finished=? WHERE id=?""",
                (results.get("baseline"), round(wall, 3), json.dumps(results), note,
                 (res or {}).get("html"), (res or {}).get("json"), time.time(), sim_id))
    except Exception as exc:  # noqa: BLE001
        with _db_lock, _db() as conn:
            conn.execute("UPDATE sims SET status='failed', error=?, finished=? WHERE id=?",
                         (str(exc)[-2000:], time.time(), sim_id))


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
    yield


app = FastAPI(title="Cohors", version=VERSION, lifespan=_lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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
    return FileResponse(STATIC_DIR / "stuff.html")


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


# =====================================================================
# v2026.09.119 — « Stuff conseillé » : quoi porter, depuis ce qu'on possède.
#
# L'export SimC de l'addon contient l'équipement porté, les pièces des sacs
# (« ### Gear from Bags ») et les builds sauvegardés (« # Saved Loadout: »).
# On s'en sert pour répondre à : « avec ce que j'ai, que devrais-je porter
# pour tel contenu ? » — sans rien coller.
#
# Méthodes :
#  * Spés de soin → SimulationCraft ne simule pas les soins : classement par
#    niveau d'objet puis par priorité des statistiques du guide de la spé
#    (les stats de chaque pièce sont extraites du moteur, bonus inclus).
#  * Spés DPS → vraies simulations : une profileset par pièce (comme le
#    Top Stuff), sur le contenu choisi.
# =====================================================================
STUFF_SLOTS = ("head", "neck", "shoulder", "back", "chest", "wrist", "hands", "waist",
               "legs", "feet", "finger1", "finger2", "trinket1", "trinket2", "main_hand", "off_hand")
STUFF_ITERATIONS = 800
STUFF_MAX_CANDIDATES = 30
STUFF_CONTENTS = {
    "mplus": {"opts": ["fight_style=DungeonSlice"], "label_fr": "Mythique+", "label_en": "Mythic+",
              "sim_fr": "donjon (DungeonSlice)", "sim_en": "dungeon (DungeonSlice)"},
    "raid": {"opts": ["fight_style=Patchwerk", "max_time=300"], "label_fr": "Raid", "label_en": "Raid",
             "sim_fr": "combat de 5 min, buffs de raid", "sim_en": "5 min fight, raid buffs"},
    "delves": {"opts": ["fight_style=Patchwerk", "max_time=90", "optimal_raid=0"],
               "label_fr": "Gouffres", "label_en": "Delves",
               "sim_fr": "combat court (90 s), sans buffs de raid (solo)",
               "sim_en": "short fight (90 s), no raid buffs (solo)"},
}
# Spés de soin (le moteur ne les simule pas) — classement par stats pondérées.
HEAL_SPECS = {"restoration", "holy", "discipline", "mistweaver", "preservation"}
# Priorité des stats secondaires par spé et par contenu : niveau d'objet d'abord,
# puis cet ordre (les deux premières stats portées pèsent le plus).
# Sources : guides Wowhead « Stat Priority » (patch 12.1, août-sept. 2026) ;
# pour le chaman, croisé avec Icy Veins « Restoration Shaman Stat Priority » (12.1).
# Les gouffres n'ont pas de priorité publiée : on utilise celle du Mythique+ (combats courts).
STUFF_HEAL_PRIO = {
    "shaman/restoration": {
        "orders": {"raid": ["crit", "vers", "haste", "mast"],
                   "mplus": ["crit", "haste", "vers", "mast"],
                   "delves": ["crit", "haste", "vers", "mast"]},
        "source": "https://www.wowhead.com/guide/classes/shaman/restoration/stat-priority-pve-healer",
        "source_fr": "Guide Wowhead — Chaman Restauration (12.1)",
        "note_fr": "Critique d'abord ; Hâte et Polyvalence très proches ; Maîtrise en dernier. Identiques en Totémique et Farseer. La Hâte est mise en avant en Mythique+ et en gouffres (combats courts).",
    },
    "druid/restoration": {
        "orders": {"raid": ["haste", "mast", "vers", "crit"],
                   "mplus": ["haste", "mast", "vers", "crit"],
                   "delves": ["haste", "mast", "vers", "crit"]},
        "source": "https://www.wowhead.com/guide/classes/druid/restoration/stat-priority-pve-healer",
        "source_fr": "Guide Wowhead — Druide Restauration (12.1)",
        "note_fr": "Hâte d'abord, puis Maîtrise, Polyvalence, et Critique en dernier.",
    },
    "paladin/holy": {
        "orders": {"raid": ["mast", "haste", "crit", "vers"],
                   "mplus": ["mast", "haste", "crit", "vers"],
                   "delves": ["mast", "haste", "crit", "vers"]},
        "source": "https://www.wowhead.com/guide/classes/paladin/holy/stat-priority-pve-healer",
        "source_fr": "Guide Wowhead — Paladin Sacré (12.1)",
        "note_fr": "Maîtrise d'abord, puis Hâte et Critique (à égalité), Polyvalence en dernier.",
    },
    "priest/holy": {
        "orders": {"raid": ["crit", "vers", "mast", "haste"],
                   "mplus": ["vers", "crit", "haste", "mast"],
                   "delves": ["vers", "crit", "haste", "mast"]},
        "source": "https://www.wowhead.com/guide/classes/priest/holy/stat-priority-pve-healer",
        "source_fr": "Guide Wowhead — Prêtre Sacré (12.1)",
        "note_fr": "En raid : Critique puis Polyvalence/Maîtrise. En Mythique+/gouffres : Polyvalence d'abord (survie et dégâts), puis Critique.",
    },
    "priest/discipline": {
        "orders": {"raid": ["haste", "mast", "crit", "vers"],
                   "mplus": ["haste", "mast", "crit", "vers"],
                   "delves": ["haste", "mast", "crit", "vers"]},
        "source": "https://www.wowhead.com/guide/classes/priest/discipline/stat-priority-pve-healer",
        "source_fr": "Guide Wowhead — Prêtre Discipline (12.1)",
        "note_fr": "Hâte d'abord, puis Maîtrise, Critique, Polyvalence. Identiques en Oracle et Voidweaver.",
    },
    "monk/mistweaver": {
        "orders": {"raid": ["haste", "crit", "vers", "mast"],
                   "mplus": ["haste", "mast", "crit", "vers"],
                   "delves": ["haste", "mast", "crit", "vers"]},
        "source": "https://www.wowhead.com/guide/classes/monk/mistweaver/stat-priority-pve-healer",
        "source_fr": "Guide Wowhead — Moine Tisse-brume (12.1)",
        "note_fr": "Hâte d'abord. En raid : Critique puis Polyvalence. En Mythique+/gouffres : Maîtrise puis Critique (dégâts).",
    },
    "evoker/preservation": {
        "orders": {"raid": ["crit", "mast", "haste", "vers"],
                   "mplus": ["crit", "haste", "mast", "vers"],
                   "delves": ["crit", "haste", "mast", "vers"]},
        "source": "https://www.wowhead.com/guide/classes/evoker/preservation/stat-priority-pve-healer",
        "source_fr": "Guide Wowhead — Évocateur Préservation (12.1)",
        "note_fr": "Critique d'abord. En raid : Maîtrise avant Hâte. En Mythique+/gouffres : Hâte avant Maîtrise.",
    },
}


# Niveau maximum d'une pièce : Wowhead publie, pour chaque objet, la version la plus
# haute qui existe (« Item Level X · Upgrade Level: <Piste> Y/6 »). On s'en sert pour le
# mode « tout au rang maximum » (aucune table de saison inventée : les valeurs viennent
# de l'objet lui-même).
WH_TOOLTIP = "https://nether.wowhead.com/tooltip/item/{iid}?dataEnv=1&locale=0"
WH_ILVL_RE = re.compile(r"Item Level <!--ilvl-->(\d+)")
WH_UP_RE = re.compile(r"Upgrade Level: ([A-Za-z ]+) <!--uindex-->(\d+)/(\d+)")


# Liste BIS : instantané des guides embarqué (voir app/data/bis.json) — le serveur ne peut
# pas récupérer les pages de guides (Cloudflare 403), donc la liste est versionnée avec l'app.
BIS_FILE = Path(__file__).resolve().parent / "data" / "bis.json"
BIS_CACHE: dict = {"mtime": 0.0, "data": {}}


def _stuff_bis_list(cls: str, spec: str) -> dict | None:
    """Bloc BIS d'une spécialisation depuis l'instantané embarqué."""
    try:
        mtime = BIS_FILE.stat().st_mtime
    except OSError:
        return None
    if BIS_CACHE["mtime"] != mtime:
        try:
            BIS_CACHE["data"] = json.loads(BIS_FILE.read_text(encoding="utf-8"))
            BIS_CACHE["mtime"] = mtime
        except Exception:  # noqa: BLE001
            return None
    return ((BIS_CACHE["data"].get("specs") or {}).get(f"{cls}/{spec}"))


def _stuff_wh_version(iid: int) -> dict | None:
    """Version maximum publiée par Wowhead pour une pièce : {ilvl, track, rank, label}."""
    try:
        r = httpx.get(WH_TOOLTIP.format(iid=int(iid)), timeout=20,
                      headers={"User-Agent": "Cohors-Stuff/1.0"})
        r.raise_for_status()
        tip = (r.json() or {}).get("tooltip") or ""
    except Exception:  # noqa: BLE001
        return None
    mi = WH_ILVL_RE.search(tip)
    if not mi:
        return None
    out = {"ilvl": int(mi.group(1)), "track": "", "rank": 0, "label": ""}
    mu = WH_UP_RE.search(tip)
    if mu:
        out["track"] = mu.group(1).strip()
        out["rank"] = int(mu.group(2))
        out["label"] = f'{out["track"]} {mu.group(2)}/{mu.group(3)}'
    return out


def _stuff_max_levels(items: list[dict], conn) -> tuple[int, list[str]]:
    """Complète chaque pièce avec son niveau maximum (cache en base). Renvoie (nb modifiés, exemples)."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS item_max_ilvl (
               item_id   INTEGER PRIMARY KEY,
               ilvl      INTEGER NOT NULL,
               label     TEXT NOT NULL DEFAULT '',
               fetched   REAL NOT NULL DEFAULT 0)""")
    ids = sorted({int(re.search(r"id=(\d+)", it.get("opts") or "").group(1))
                  for it in items if re.search(r"id=(\d+)", it.get("opts") or "")})
    known = {r["item_id"]: {"ilvl": r["ilvl"], "label": r["label"]}
             for r in conn.execute("SELECT * FROM item_max_ilvl").fetchall()}
    missing = [i for i in ids if i not in known]
    if missing:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(_stuff_wh_version, missing))
        for iid, res in zip(missing, results):
            if res:
                conn.execute(
                    "INSERT OR REPLACE INTO item_max_ilvl (item_id, ilvl, label, fetched) VALUES (?,?,?,?)",
                    (iid, res["ilvl"], res["label"], time.time()))
                known[iid] = {"ilvl": res["ilvl"], "label": res["label"]}
        conn.commit()
    changed, examples = 0, []
    for it in items:
        m = re.search(r"id=(\d+)", it.get("opts") or "")
        if not m:
            continue
        info = known.get(int(m.group(1)))
        it["max_ilvl"] = (info or {}).get("ilvl")
        it["max_label"] = (info or {}).get("label") or ""
        cur = int(it.get("ilvl") or 0)
        if it["max_ilvl"] and it["max_ilvl"] > cur:
            it["was_ilvl"] = cur
            it["ilvl"] = it["max_ilvl"]
            it["opts"] = f'{it["opts"]},ilevel={it["max_ilvl"]}'
            changed += 1
            if len(examples) < 3:
                examples.append(f'{it.get("name") or "?"} {cur}→{it["max_ilvl"]}')
    return changed, examples


def _stuff_parse_export(txt: str) -> dict:
    """Analyse un export SimC : classe/spé, builds sauvegardés, équipé, pièces des sacs."""
    out = {"cls": "", "name": "", "spec": "", "level": 0, "loadouts": [],
           "equipped": [], "bags": []}
    pending = None
    cur_loadout = None
    in_bags = False
    for ln in txt.replace("\r\n", "\n").splitlines():
        if not out["cls"]:
            m = re.match(r'^([a-z_]+)="([^"]*)"\s*$', ln)
            if m:
                out["cls"], out["name"] = m.group(1), m.group(2)
                continue
        if ln.strip().startswith("### Gear from Bags"):
            in_bags = True
            continue
        if not out["spec"]:
            m = re.match(r"^spec=(\S+)", ln)
            if m:
                out["spec"] = m.group(1)
                continue
        if not out["level"]:
            m = re.match(r"^level=(\d+)", ln)
            if m:
                out["level"] = int(m.group(1))
                continue
        m = re.match(r"^#\s*Saved Loadout:\s*(.+?)\s*$", ln)
        if m:
            cur_loadout = m.group(1)
            continue
        m = re.match(r"^#\s*talents=(\S+)\s*$", ln)
        if m:
            if cur_loadout:
                out["loadouts"].append({"name": cur_loadout, "talents": m.group(1)})
                cur_loadout = None
            continue
        m = re.match(r"^#\s*(.+?)\s*\((\d+)\)\s*$", ln)
        if m:
            pending = {"name": m.group(1), "ilvl": int(m.group(2))}
            continue
        m = re.match(r"^(#\s*)?(" + "|".join(STUFF_SLOTS) + r")\s*=\s*(.+?)\s*$", ln)
        if m:
            item = {"slot": m.group(2), "opts": m.group(3),
                    "name": (pending or {}).get("name") or "", "ilvl": (pending or {}).get("ilvl") or 0}
            dest = out["bags"] if (in_bags or m.group(1)) else out["equipped"]
            dest.append(item)
            pending = None
    return out


def _stuff_gear_stats(json_path: str | None) -> dict:
    """Stats des pièces depuis le JSON d'une sim d'acteurs (un acteur par pièce)."""
    if not json_path:
        return {}
    try:
        data = json.loads(Path(json_path).read_text())
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, dict] = {}
    for p in (data.get("sim", {}).get("players") or []):
        nm = str(p.get("name") or "")
        if not re.match(r"^C\d+$", nm):
            continue
        for _slot, it in (p.get("gear") or {}).items():
            if it.get("name"):
                out[nm] = {"ilvl": it.get("ilevel"), "int": it.get("intellect") or it.get("agiint"),
                           "crit": it.get("crit_rating"), "haste": it.get("haste_rating"),
                           "mast": it.get("mastery_rating"), "vers": it.get("versatility_rating"),
                           "stam": it.get("stamina")}
    return out


def _stuff_fetch_stats(items: list[dict], cls: str, spec: str, workdir: Path) -> tuple[dict, int, list]:
    """Stats de chaque pièce via des acteurs SimC, par lots.

    Retire les pièces que le personnage ne peut pas porter (le moteur répond
    « Invalid type ») et isole par dichotomie celles qui font planter le moteur.
    Renvoie (stats par acteur, nb de pièces écartées, pièces qui plantent).
    """
    for k, c in enumerate(items, 1):
        c["actor"] = f"C{k:03d}"
    stats: dict[str, dict] = {}
    dropped = 0
    crashed: list[dict] = []
    queue = [list(items)]
    guard = 0
    while queue and guard < 80:
        guard += 1
        batch = queue.pop(0)
        if not batch:
            continue
        lines = []
        for c in batch:
            lines += [f'{cls or "shaman"}="{c["actor"]}"', f'level={c.get("level") or 90}',
                      "region=eu", f'spec={spec or "restoration"}', f'{c["slot"]}={c["opts"]}', ""]
        profile = workdir / "stats.simc"
        profile.write_text("\n".join(lines))
        res = run_sim(profile_path=profile, iterations=1, outdir=workdir, extra=["max_time=1"], timeout=300)
        got = _stuff_gear_stats(res.get("json"))
        if got:
            stats.update(got)
            continue
        log = res.get("log_tail") or ""
        bad = set(re.findall(r"Player '(C\d+)'", log))
        if bad:
            keep = [c for c in batch if c["actor"] not in bad]
            dropped += len(batch) - len(keep)
            if keep:
                queue.append(keep)
            continue
        if res.get("rc") == 139:
            if len(batch) > 1:
                mid = len(batch) // 2
                queue += [batch[:mid], batch[mid:]]
            else:
                crashed.append(batch[0])
            continue
        dropped += len(batch)
    return stats, dropped, crashed


def _stuff_score(item: dict, order: list[str]) -> tuple:
    """Clé de classement : niveau d'objet d'abord, puis stats dans l'ordre de priorité."""
    st = item.get("stats") or {}
    return (int(item.get("ilvl") or 0),) + tuple(int(st.get(k) or 0) for k in order)


def _stuff_item_public(item: dict) -> dict:
    st = item.get("stats") or {}
    return {"name": item.get("name") or "?", "ilvl": item.get("ilvl"),
            "int": st.get("int"), "crit": st.get("crit"), "haste": st.get("haste"),
            "mast": st.get("mast"), "vers": st.get("vers")}


def _stuff_rank_heal(parsed: dict, items: list[dict], prio: dict, content: str) -> dict:
    """Classement des pièces pour une spé de soin (niveau d'objet puis priorité des stats)."""
    orders = prio.get("orders") or {}
    order = orders.get(content) or orders.get("raid") or []
    by_slot_cur: dict[str, dict] = {}
    by_slot_bags: dict[str, list[dict]] = {}
    for it in items:
        if not it.get("stats"):
            continue
        if it.get("equipped"):
            by_slot_cur[it["slot"]] = it
        else:
            by_slot_bags.setdefault(it["slot"], []).append(it)
    slots = []
    for slot in STUFF_SLOTS:
        cur = by_slot_cur.get(slot)
        cands = by_slot_bags.get(slot) or []
        if not cur and not cands:
            continue
        best = None
        if cands:
            best = max(cands, key=lambda i: _stuff_score(i, order))
        entry = {"slot": slot,
                 "current": _stuff_item_public(cur) if cur else None,
                 "best": _stuff_item_public(best) if best else None,
                 "swap": False, "reason": None}
        if best is not None:
            if cur is None:
                entry["swap"] = True
                entry["reason"] = {"kind": "empty"}
            else:
                sb, sc = _stuff_score(best, order), _stuff_score(cur, order)
                if sb > sc:
                    entry["swap"] = True
                    if int(best.get("ilvl") or 0) != int(cur.get("ilvl") or 0):
                        entry["reason"] = {"kind": "ilvl",
                                           "diff": int(best.get("ilvl") or 0) - int(cur.get("ilvl") or 0)}
                    else:
                        bs, cs = best.get("stats") or {}, cur.get("stats") or {}
                        stat = next((k for k in order if int(bs.get(k) or 0) != int(cs.get(k) or 0)), None)
                        entry["reason"] = {"kind": "stats", "stat": stat} if stat else {"kind": "tie"}
        slots.append(entry)
    return {"mode": "heal", "content": content, "priority": order,
            "source": prio.get("source"), "source_fr": prio.get("source_fr"),
            "slots": slots}


def _stuff_sim_input(parsed: dict, items: list[dict], loadout: dict | None) -> str:
    """Profil pour les spés DPS : export nettoyé + talents du build + une profileset par pièce."""
    lines = [ln for ln in parsed.get("_raw", "").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    if loadout and loadout.get("talents"):
        lines = [ln for ln in lines if not ln.startswith("talents=")]
        lines.insert(1, "talents=" + loadout["talents"])
    cands = [it for it in items if not it.get("equipped") and it.get("stats")]
    cands.sort(key=lambda i: -int(i.get("ilvl") or 0))
    cands = cands[:STUFF_MAX_CANDIDATES]
    ps = [f'profileset."{(it.get("name") or "?")[:40]} [{it["slot"]}]"={it["slot"]}={it["opts"]}'
          for it in cands]
    return "\n".join(lines) + "\n\n" + "\n".join(ps) + "\n", len(cands)


def _stuff_plausible(items: list, equipped_by_slot: dict) -> list:
    """Ne garde que les pièces qui peuvent battre (ou presque) l'équipé en niveau d'objet."""
    out = []
    for it in items:
        if it.get("equipped") or not it.get("stats"):
            continue
        cur = equipped_by_slot.get(it["slot"])
        ilvl = int(it.get("ilvl") or 0)
        if cur is None or ilvl >= int(cur.get("ilvl") or 0) - 6:
            out.append(it)
    out.sort(key=lambda i: -int(i.get("ilvl") or 0))
    return out[:STUFF_MAX_CANDIDATES]


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


@app.get("/api/stuff/profile/{pid}")
def stuff_profile(pid: int, request: Request):
    """Résumé d'un export : classe/spé, builds détectés, nb de pièces."""
    _require_user(request)
    with _db_lock, _db() as conn:
        prof = conn.execute("SELECT id, name, input FROM profiles WHERE id=?", (pid,)).fetchone()
    if prof is None:
        raise HTTPException(404, "Profil introuvable.")
    parsed = _stuff_parse_export(prof["input"])
    return {"id": prof["id"], "name": prof["name"], "cls": parsed["cls"], "spec": parsed["spec"],
            "level": parsed["level"], "loadouts": [l["name"] for l in parsed["loadouts"]],
            "equipped": len(parsed["equipped"]), "bags": len(parsed["bags"]),
            "heal": parsed["spec"] in HEAL_SPECS,
            "prio_known": f'{parsed["cls"]}/{parsed["spec"]}' in STUFF_HEAL_PRIO,
            "prio_note": (STUFF_HEAL_PRIO.get(f'{parsed["cls"]}/{parsed["spec"]}') or {}).get("note_fr", "")}


class StuffRequest(BaseModel):
    profile_id: int
    loadout: str = ""
    content: str = "raid"
    mode: str = "cur"          # cur | max | bis
    max_rank: bool = False     # compat : équivaut à mode="max"


@app.post("/api/stuff")
def submit_stuff(payload: StuffRequest, request: Request):
    """« Stuff conseillé » : que porter, avec ce qu'on possède, pour un contenu donné."""
    user = _require_user(request)
    if payload.content not in STUFF_CONTENTS:
        raise HTTPException(400, "Contenu invalide.")
    with _db_lock, _db() as conn:
        prof = conn.execute("SELECT id, name, input FROM profiles WHERE id=?", (payload.profile_id,)).fetchone()
    if prof is None:
        raise HTTPException(404, "Profil introuvable.")
    parsed = _stuff_parse_export(prof["input"])
    if not parsed["bags"]:
        raise HTTPException(400, "Cet export ne contient pas les pièces des sacs — réexporte ton personnage avec l'addon (les sacs sont inclus automatiquement).")
    loadout = next((l for l in parsed["loadouts"] if l["name"] == payload.loadout), None)
    if payload.loadout and loadout is None:
        raise HTTPException(400, "Build introuvable dans cet export.")
    ip = _client_ip(request)
    now = time.time()
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
        mode = payload.mode if payload.mode in ("cur", "max", "bis") else ("max" if payload.max_rank else "cur")
        if mode == "bis" and _stuff_bis_list(parsed["cls"], parsed["spec"]) is None:
            raise HTTPException(400, "Liste BIS pas encore disponible pour cette spécialisation.")
        plan = {"profile_id": prof["id"], "profile_name": prof["name"], "cls": parsed["cls"],
                "spec": parsed["spec"], "loadout": loadout or {}, "content": payload.content,
                "mode": mode, "max_rank": mode == "max"}
        label = f'{prof["name"]} · {STUFF_CONTENTS[payload.content]["label_fr"]}' + \
                (f' · {loadout["name"]}' if loadout else "") + \
                ({"max": " · rang max", "bis": " · BIS"}.get(mode) or "")
        conn.execute(
            """INSERT INTO sims (id, created, ip, label, iterations, status, input_hash, input_file,
                                 user_email, user_name, kind, plan)
               VALUES (?,?,?,?,?, 'queued', ?, ?, ?, ?, 'stuff', ?)""",
            (sim_id, now, ip, label[:60], STUFF_ITERATIONS, f"stuff:{sim_id}", str(input_file),
             user["email"], user["name"], json.dumps(plan)))
    return {"id": sim_id, "status": "queued", "heal": parsed["spec"] in HEAL_SPECS,
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
_REALM_RE = re.compile(r"^[a-z0-9-]{2,40}$")
_CHARNAME_RE = re.compile(r"^[A-Za-z\u00c0-\u00ff][A-Za-z\u00c0-\u00ff'\-]{1,23}$")


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
    return _bnet_call(bnet.character, realm, name, refresh, locale=_user_locale(request))


@app.get("/api/char/{realm}/{name}/extras")
def api_char_extras(realm: str, name: str, request: Request, refresh: int = 0):
    _require_user(request)
    return _bnet_call(bnet.extras, realm, name, refresh, locale=_user_locale(request))


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


# Clés de classe anglaises (couleurs côté front) ↔ libellés Blizzard localisés (données stockées).
CLASS_KEY_FR = {
    "Chevalier de la mort": "DeathKnight", "Chasseur de démons": "DemonHunter", "Druide": "Druid",
    "Évocateur": "Evoker", "Chasseur": "Hunter", "Mage": "Mage", "Moine": "Monk", "Paladin": "Paladin",
    "Prêtre": "Priest", "Voleur": "Rogue", "Chaman": "Shaman", "Démoniste": "Warlock", "Guerrier": "Warrior",
}


def _pick(d: dict, key: str, want_en: bool):
    """Valeur d'un relevé dans la langue demandée (version EN si dispo, sinon FR)."""
    v = d.get(key)
    return (d.get(key + "_en") or v) if want_en else v


def _att_localized(data: dict, locale: str) -> dict:
    """Assiduité : sert la classe dans la langue demandée (les clés/parcours restent FR)."""
    if not locale.startswith("en"):
        return data
    out = dict(data)
    out["rows"] = [dict(r, **{"class": r.get("class_en") or r.get("class")}) for r in (data.get("rows") or [])]
    return out

_ATT_CACHE: dict = {"ts": 0.0, "days": 0, "data": None}
ATT_TTL = 900.0


@app.get("/api/attendance")
def api_attendance(request: Request, days: int = 30, refresh: int = 0):
    """Assiduité réelle aux soirées de raid (logs Warcraft Logs) sur les N derniers jours."""
    _require_user(request)
    days = days if days in (14, 30, 60) else 30
    now = time.time()
    if (not refresh and _ATT_CACHE["data"] is not None and _ATT_CACHE["days"] == days
            and now - _ATT_CACHE["ts"] < ATT_TTL):
        return _att_localized(_ATT_CACHE["data"], _user_locale(request))
    try:
        rl, _ts = wcl.reports(limit=50, force=bool(refresh))
    except wcl.WclError as exc:
        return {"error": str(exc)}
    cutoff = now - days * 86400
    cls_by_name: dict[str, str] = {}
    cls_en_by_name: dict[str, str] = {}
    with _db_lock, _db() as conn:
        for row in conn.execute(
            "SELECT name, data FROM char_snapshots WHERE day = ?", (_snap_day(),)
        ).fetchall():
            try:
                d_snap = json.loads(row["data"]) or {}
            except (ValueError, TypeError):
                d_snap = {}
            if d_snap.get("class"):
                cls_by_name[row["name"]] = d_snap["class"]
            if d_snap.get("class_en"):
                cls_en_by_name[row["name"]] = d_snap["class_en"]
    roster: dict[str, dict] = {}
    try:
        rl2, _t = bnet.roster()
        roster = {(m.get("name") or "").lower(): m for m in (rl2.get("members") or [])}
    except bnet.BnetError:
        pass
    evenings: list[dict] = []
    for r in rl.get("data") or []:
        st = (r.get("startTime") or 0) / 1000
        if st < cutoff:
            continue
        code = r.get("code")
        try:
            full, _t = wcl.report_full(code, force=bool(refresh))
            comb, _t2 = wcl.report_combatants(code, force=bool(refresh))
        except wcl.WclError as exc:
            print(f"[att] WCL {code}: {exc}")
            continue
        fights = (full.get("report") or {}).get("fights") or []
        boss = [f for f in fights if f.get("encounterID")]
        players = comb.get("players") or {}
        if not boss or not players:
            continue
        evenings.append({
            "code": code, "day": _snap_day(st), "ts": st,
            "zone": (r.get("zone") or {}).get("name") or "",
            "kills": sum(1 for f in boss if f.get("kill")),
            "bosses": len({f.get("encounterID") for f in boss}),
            "players": list(players),
        })
    evenings.sort(key=lambda e: e["ts"])
    total = len(evenings)
    seen: dict[str, dict] = {}
    for e in evenings:
        for pname in e["players"]:
            key = pname.lower()
            d = seen.setdefault(key, {"name": pname, "nights": 0, "last_day": None})
            d["nights"] += 1
            d["last_day"] = e["day"]
    rows = []
    for key, d in seen.items():
        mem = roster.get(key) or {}
        rows.append({
            "name": d["name"], "key": key,
            "realm": mem.get("realm") or bnet.GUILD_REALM,
            "class": cls_by_name.get(key), "class_en": cls_en_by_name.get(key),
            "class_key": CLASS_KEY_FR.get(cls_by_name.get(key) or ""),
            "nights": d["nights"], "pct": round(100 * d["nights"] / total) if total else 0,
            "last_day": d["last_day"], "guest": key not in roster,
        })
    rows.sort(key=lambda r: (-r["nights"], r["name"].lower()))
    data = {"days": days, "built": now, "total": total,
            "evenings": [{k: v for k, v in e.items() if k != "players"} for e in evenings],
            "rows": rows}
    _ATT_CACHE.update({"ts": now, "days": days, "data": data})
    return _att_localized(data, _user_locale(request))


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
PROF_EN = {v: k for k, v in bnet.PROF_FR.items()}  # libellé FR → nom anglais (API)


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
class WishlistAdd(BaseModel):
    item: str = Field(..., min_length=4, max_length=300)
    prio: bool = False


class PrioSet(BaseModel):
    on: bool = False


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


_SIM_ITEM_RE = re.compile(r"\[(\w+):(\d+)\]\s*$")


def _sim_char_name(path, label: str = "") -> str:
    """Nom du personnage depuis l'en-tête du profil SimC (1re ligne « classe="Nom" »)."""
    try:
        for line in Path(path).read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m2 = re.match(r'^\w+="?([^"=\n]+)"?\s*$', line)
            return (m2.group(1).strip() if m2 else "") or label or ""
    except OSError:
        pass
    return label or ""


def _bis_by_user(conn) -> dict:
    """Meilleures améliorations par emplacement déduites des sims « Top Stuff » de chaque compte.

    {email: {item_id: {char, gain}}} — gain = % de DPS vs le stuff actuel du profil simulé.
    """
    out: dict = {}
    try:
        rows = conn.execute(
            "SELECT label, user_email, dps, gear, input_file FROM sims "
            "WHERE kind='gear' AND status='done' AND gear IS NOT NULL AND dps IS NOT NULL "
            "ORDER BY created DESC LIMIT 200").fetchall()
    except sqlite3.OperationalError:
        return out
    for sr in rows:
        try:
            gear = json.loads(sr["gear"]) or []
        except (ValueError, TypeError):
            continue
        base = float(sr["dps"] or 0)
        if base <= 0 or not sr["user_email"]:
            continue
        best: dict = {}
        for g in gear:
            m2 = _SIM_ITEM_RE.search(str(g.get("name") or ""))
            if not m2:
                continue
            iid = int(m2.group(2))
            try:
                dl = 100.0 * (float(g.get("dps") or 0) - base) / base
            except (TypeError, ValueError):
                continue
            if dl > 0.05 and (m2.group(1) not in best or dl > best[m2.group(1)]["gain"]):
                best[m2.group(1)] = {"item_id": iid, "gain": round(dl, 2)}
        if not best:
            continue
        chart = _sim_char_name(sr["input_file"], sr["label"] or "")
        dset = out.setdefault(sr["user_email"], {})
        for _slot, b in best.items():
            dset.setdefault(b["item_id"], {"char": chart, "gain": b["gain"]})
    return out


@app.get("/api/wishlist")
def api_wishlist(request: Request):
    user = _require_user(request)
    loc = _user_locale(request)
    en = loc.startswith("en")
    if _loot_sync_state["state"] != "running":
        with _db_lock, _db() as conn:
            n_loot = int(conn.execute("SELECT COUNT(*) AS n FROM item_loot").fetchone()["n"] or 0)
        if not n_loot:
            threading.Thread(target=_loot_sync, daemon=True).start()
    with _db_lock, _db() as conn:
        rows = conn.execute(
            "SELECT item_id, name, slot, quality, icon, added, prio FROM wishlist "
            "WHERE user_email=? ORDER BY added DESC",
            (user["email"],),
        ).fetchall()
        chars = conn.execute(
            "SELECT realm, name, display, is_main FROM char_links WHERE user_email=? ORDER BY is_main DESC, name",
            (user["email"],),
        ).fetchall()
        loot: dict = {}
        for lr in conn.execute(
                "SELECT item_id, kind, inst_fr, inst_en, boss_fr, boss_en FROM item_loot").fetchall():
            loot.setdefault(int(lr["item_id"]), []).append(dict(lr))
        my_bis = _bis_by_user(conn).get(user["email"], {})
        mychars = [{"k": str(c["name"] or "").strip().lower(),
                    "disp": (c["display"] or c["name"])} for c in chars]
        mynames = {c["k"] for c in mychars}
        my_disp = {c["k"]: c["disp"] for c in mychars}
        my_profs: dict = {}
        my_recipes: dict = {}
        my_recipes_name: dict = {}
        for pr in conn.execute("SELECT name, data FROM char_professions").fetchall():
            nmk = str(pr["name"] or "").strip().lower()
            if nmk not in mynames:
                continue
            try:
                pd2 = json.loads(pr["data"]) or {}
            except (ValueError, TypeError):
                continue
            for pp in (pd2.get("profs") or []):
                for key in {str(pp.get("name_fr") or pp.get("name") or "").strip().lower(),
                            str(pp.get("name_en") or "").strip().lower()}:
                    if key:
                        lst = my_profs.setdefault(key, [])
                        if my_disp[nmk] not in lst:
                            lst.append(my_disp[nmk])
        for cr2 in conn.execute("SELECT crafter, item, item_id FROM craft_recipes").fetchall():
            nmk = str(cr2["crafter"] or "").strip().lower()
            if nmk not in mynames:
                continue
            disp = my_disp[nmk]
            if cr2["item_id"]:
                lst = my_recipes.setdefault(int(cr2["item_id"]), [])
                if disp not in lst:
                    lst.append(disp)
            if cr2["item"]:
                lst = my_recipes_name.setdefault(str(cr2["item"]).strip().casefold(), [])
                if disp not in lst:
                    lst.append(disp)
        prof_holders: dict = {}
        for pr in conn.execute("SELECT name, data FROM char_professions").fetchall():
            try:
                pd = json.loads(pr["data"]) or {}
            except (ValueError, TypeError):
                continue
            for pp in (pd.get("profs") or []):
                frk = str(pp.get("name_fr") or pp.get("name") or "").strip().lower()
                enk = str(pp.get("name_en") or pp.get("name") or "").strip().lower()
                ent_p = {"name": (pr["name"] or "")[:1].upper() + (pr["name"] or "")[1:],
                         "points": pp.get("points") or 0}
                if frk:
                    prof_holders.setdefault(frk, []).append(ent_p)
                if enk and enk != frk:
                    prof_holders.setdefault(enk, []).append(ent_p)
        items = []
        for r in rows:
            nm = r["name"]
            if en and r["item_id"]:
                try:
                    nm = bnet.item(r["item_id"], locale=loc).get("name") or nm
                except bnet.BnetError:
                    pass
            src, seen = [], {}
            for s in loot.get(int(r["item_id"] or 0), []):
                key = (s["kind"], s["inst_en"])
                ent = seen.get(key)
                if ent is None:
                    if len(src) >= 3:
                        continue
                    ent = {"kind": s["kind"], "inst": s["inst_en"] if en else s["inst_fr"], "bosses": []}
                    seen[key] = ent
                    src.append(ent)
                bos = s["boss_en"] if en else s["boss_fr"]
                if bos and bos not in ent["bosses"] and len(ent["bosses"]) < 3:
                    ent["bosses"].append(bos)
            gr = conn.execute(
                "SELECT item, prof, prof_en, mats, mats_en FROM game_recipes "
                "WHERE (item_id > 0 AND item_id=?) OR lower(item)=lower(?) OR lower(item_en)=lower(?) "
                "ORDER BY rank_no LIMIT 1",
                (r["item_id"] or 0, r["name"] or "", r["name"] or "")).fetchone()
            fr_name = (gr["item"] if gr else r["name"]) or ""
            decl = conn.execute(
                "SELECT profession, mats FROM craft_recipes "
                "WHERE (item_id > 0 AND item_id=?) OR lower(item)=lower(?) LIMIT 1",
                (r["item_id"] or 0, fr_name)).fetchone()
            who = [w["crafter"] for w in conn.execute(
                "SELECT DISTINCT crafter FROM craft_recipes "
                "WHERE (item_id > 0 AND item_id=?) OR lower(item)=lower(?)",
                (r["item_id"] or 0, fr_name)).fetchall()]
            craft = None
            if gr or who:
                try:
                    if gr:
                        mats = json.loads((gr["mats_en"] if en else gr["mats"]) or "[]")
                    else:
                        mats = json.loads((decl or {})["mats"] or "[]") if decl else []
                except (ValueError, TypeError):
                    mats = []
                prof_fr = ((gr["prof"] if gr else "") or (decl["profession"] if decl else "") or "")
                prof_en = ((gr["prof_en"] if gr else "") or (decl["profession"] if decl else "") or "")
                prof = (prof_en if en else prof_fr) or prof_fr
                holders = []
                hl = prof_holders.get((prof or "").strip().lower()) or []
                if hl:
                    hl = sorted(hl, key=lambda x: (-(x.get("points") or 0), (x.get("name") or "").lower()))
                    holders = [h["name"] for h in hl[:8]]
                craft = {"prof": prof, "mats": mats, "who": who[:6],
                         "holders": holders, "holders_n": len(hl)}
            me = None
            if craft:
                mchars = my_profs.get((craft["prof"] or "").strip().lower()) or []
                mrec = (my_recipes.get(int(r["item_id"] or 0))
                        or my_recipes_name.get(str(fr_name or "").strip().casefold())
                        or my_recipes_name.get(str(r["name"] or "").strip().casefold())
                        or [])
                if mchars or mrec:
                    me = {"chars": mchars[:3], "recipe": mrec[:3]}
            bis = []
            bhit = my_bis.get(int(r["item_id"] or 0))
            if bhit:
                bis = [{"char": bhit["char"], "gain": bhit["gain"]}]
            items.append({
                "item_id": r["item_id"], "name": nm,
                "slot": r["slot"], "slot_fr": bnet.slot_label(r["slot"], loc),
                "quality": r["quality"], "icon": r["icon"], "added": r["added"],
                "source": src, "craft": craft, "me": me,
                "prio": bool(r["prio"]), "bis": bis,
            })
        items.sort(key=lambda it: 0 if it["prio"] else (1 if it["bis"] else 2))
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
    loc = _user_locale(request)
    try:
        it = bnet.item(iid, locale=loc)
    except bnet.BnetError as exc:
        raise HTTPException(400, str(exc))
    it_fr = it if loc.startswith("fr") else bnet.item(iid)
    slot = (bnet.INV_TO_SLOTS.get(it["inv_type"]) or [""])[0]
    with _db_lock, _db() as conn:
        exists = conn.execute(
            "SELECT 1 AS x FROM wishlist WHERE user_email=? AND item_id=?", (user["email"], iid)
        ).fetchone()
        if exists:
            if payload.prio:
                conn.execute("UPDATE wishlist SET prio=1 WHERE user_email=? AND item_id=?",
                             (user["email"], iid))
            return {"ok": True, "already": True, "name": it["name"]}
        conn.execute(
            "INSERT INTO wishlist (user_email, item_id, name, slot, inv_type, quality, icon, added, prio) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (user["email"], iid, it_fr["name"], slot, it["inv_type"], it["quality"], it.get("icon"),
             time.time(), 1 if payload.prio else 0),
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


@app.post("/api/wishlist/{item_id}/prio")
def api_wishlist_prio(item_id: int, payload: PrioSet, request: Request):
    """Marque (ou non) une pièce de la wishlist comme prioritaire (⭐)."""
    user = _require_user(request)
    with _db_lock, _db() as conn:
        conn.execute("UPDATE wishlist SET prio=? WHERE user_email=? AND item_id=?",
                     (1 if payload.on else 0, user["email"], item_id))
    return {"ok": True, "on": bool(payload.on)}


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
            ident = _brand_identity()
            text, html = mailer.invite_mail(_invite_link(token), INVITE_TTL_DAYS, **ident)
            mailer.send_mail(email, f"Invitation — {ident['short_name'] or ident['guild_name']}", text, html)
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
        ident = _brand_identity()
        text, html = mailer.invite_mail(_invite_link(token), INVITE_TTL_DAYS, **ident)
        mailer.send_mail(r["email"], f"Invitation — {ident['short_name'] or ident['guild_name']}", text, html)
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
        gcal = conn.execute("SELECT ts, player, data FROM gcal_import WHERE id=1").fetchone()
    out: dict = {"events": [dict(r) for r in rows], "members": None, "next_raid": None}
    try:
        data, ts = bnet.roster()
        out["members"] = {"count": len(data.get("members") or []), "fetched": ts}
    except bnet.BnetError:
        pass
    # prochain raid d'apres le calendrier in-game importe (addon Cohors)
    try:
        if gcal is not None:
            evs = (json.loads(gcal["data"]) or {}).get("events") or []
            now = time.time()
            upcoming = [e for e in evs if float(e.get("ts") or 0) > now - 3600]
            upcoming.sort(key=lambda e: float(e.get("ts") or 0))
            pick = next((e for e in upcoming if _int_any(e.get("type")) == 0), None)
            if pick is None and upcoming:
                pick = upcoming[0]
            if pick is not None:
                inv = pick.get("inv") or []
                ok = sum(1 for i in inv if _int_any(i.get("s")) in (1, 3))
                maybe = sum(1 for i in inv if _int_any(i.get("s")) == 8)
                no = sum(1 for i in inv if _int_any(i.get("s")) == 2)
                out["next_raid"] = {
                    "title": str(pick.get("title") or "Raid"),
                    "date": str(pick.get("date") or ""),
                    "ts": float(pick.get("ts") or 0),
                    "ok": ok, "maybe": maybe, "no": no, "wait": len(inv) - ok - maybe - no,
                    "imported_at": gcal["ts"], "player": gcal["player"] or "",
                }
    except (ValueError, TypeError):
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
# Relevés quotidiens — évolution des personnages liés (v2026.09.054)
# ---------------------------------------------------------------------------
def _snap_day(ts: float | None = None) -> str:
    """Jour courant (Europe/Paris, DST-safe) au format YYYY-MM-DD."""
    moment = time.time() if ts is None else ts
    try:
        from zoneinfo import ZoneInfo

        return datetime.fromtimestamp(moment, ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return time.strftime("%Y-%m-%d", time.gmtime(moment))


def _prof_store(realm: str, name: str) -> None:
    """Enregistre (ou remplace) les métiers d'un personnage."""
    data, ts = bnet.professions(realm, name, locale="fr_FR")
    with _db_lock, _db() as conn:
        conn.execute(
            "INSERT INTO char_professions (realm, name, ts, data) VALUES (?,?,?,?) "
            "ON CONFLICT(realm, name) DO UPDATE SET ts=excluded.ts, data=excluded.data",
            (realm.lower(), name.lower(), ts, json.dumps(data, ensure_ascii=False)),
        )


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
class ApiKeyRequest(BaseModel):
    """Clés d'un fournisseur : secret vide = inchangé ; clear=True = retour à l'environnement."""

    provider: str = Field(..., max_length=20)
    client_id: str | None = Field(None, max_length=200)
    client_secret: str = Field("", max_length=400)
    clear: bool = False


class ApiKeyTestRequest(BaseModel):
    provider: str = Field(..., max_length=20)


class BotConfigRequest(BaseModel):
    """Mise à jour PARTIELLE : seuls les champs transmis sont modifiés."""

    enabled: bool | None = None
    token: str = Field("", max_length=200)
    app_id: str | None = Field(None, max_length=32)
    channel_id: str = Field("", max_length=32)
    channel_name: str = Field("", max_length=120)
    notify_reports: bool | None = None
    notify_roster: bool | None = None
    notify_chars: bool | None = None
    notify_weekly: bool | None = None


class GcalImportRequest(BaseModel):
    payload: str = Field("", max_length=2_000_000)


def _lua_unescape(t: str) -> str:
    """Dé-échappe une chaîne Lua écrite par le jeu dans un fichier SavedVariables."""
    out: list[str] = []
    i, n = 0, len(t)
    while i < n:
        c = t[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        nxt = t[i + 1]
        if nxt in "\\\"'":
            out.append(nxt)
            i += 2
        elif nxt == "n":
            out.append("\n")
            i += 2
        elif nxt == "r":
            out.append("\r")
            i += 2
        elif nxt == "t":
            out.append("\t")
            i += 2
        elif nxt.isdigit():
            j = i + 1
            while j < n and j < i + 4 and t[j].isdigit():
                j += 1
            out.append(chr(int(t[i + 1:j])))
            i = j
        else:
            out.append(nxt)
            i += 2
    return "".join(out)


def _int_any(v) -> int:
    """Entier depuis un nombre ou une chaîne (y compris hexadécimal « 0x… » écrit par le client WoW)."""
    try:
        if isinstance(v, str):
            s = v.strip()
            if s.lower().startswith("0x"):
                return int(s, 16)
            return int(float(s))
        return int(v if v is not None else 0)
    except (TypeError, ValueError):
        return 0


def _gcal_parse(text: str) -> dict:
    """Extrait les données d'un import : JSON brut (chaîne collée) ou fichier SavedVariables (Cohors.lua)."""
    t = (text or "").strip()
    if not t:
        raise HTTPException(400, "Contenu vide.")
    raw = None
    if t.startswith("{"):
        raw = t
    else:
        ls = re.search(r'\["export"\]\s*=\s*\[(=*)\[(.*?)\]\1\]', t, re.S)
        st = re.search(r'\["export"\]\s*=\s*"((?:[^"\\]|\\.)*)"', t, re.S)
        if ls:
            raw = ls.group(2)
        elif st:
            raw = _lua_unescape(st.group(1))
        else:
            # tolérance : chaîne d'export noyée dans du texte copié avec (résumé, etc.)
            j = t.find('{"v":')
            k = t.rfind("}")
            if j >= 0 and k > j:
                raw = t[j:k + 1]
    if raw is None:
        snippet = " ".join(t[:90].split())
        raise HTTPException(400, "Format non reconnu (reçu : %d caractères — « %s… »). Copie la chaîne qui commence par "
                                 "{\"v\":1 avec le bouton « Exporter » de l'addon (Ctrl+A puis Ctrl+C), ou choisis le "
                                 "fichier WTF/Account/<compte>/SavedVariables/Cohors.lua." % (len(t), snippet))
    # tolérance : le client WoW écrit certains ids 64 bits en hexadécimal (0x1F45…), invalide en JSON strict
    raw = re.sub(r"(\s*:\s*)0x([0-9A-Fa-f]+)", r'\1"0x\2"', raw)
    try:
        data = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Données illisibles (JSON invalide) — recopie la chaîne avec « Exporter » "
                                 "(Ctrl+A puis Ctrl+C) ou importe le fichier Cohors.lua.")
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise HTTPException(400, "Données inattendues (aucun événement).")
    return data


# ---------------------------------------------------------------------------
# Clés API (Battle.net, Warcraft Logs) — renseignées depuis l'administration
# ---------------------------------------------------------------------------
API_PROVIDERS = {
    "bnet": {"label": "Battle.net (Blizzard)",
             "help_url": "https://develop.battle.net/access/clients",
             "help_fr": "Portail développeurs Blizzard → Clients API → « Create Client » (type Client Credentials).",
             "module": "bnet"},
    "wcl": {"label": "Warcraft Logs",
            "help_url": "https://www.warcraftlogs.com/api/clients",
            "help_fr": "Warcraft Logs → ton profil → API Clients → « Create Client » (Client Credentials).",
            "module": "wcl"},
}


# ---------------------------------------------------------------------------
# Jobs de synchronisation — réglages (administration) + état du dernier passage
# ---------------------------------------------------------------------------
JOB_DEFAULTS = {
    # relevés des personnages (char_snapshots) + métiers
    "snap_enabled": "1",
    "snap_interval_min": str(max(1, int(SNAP_POLL_S // 60))),
    "snap_linked_h": str(max(1, int(SNAP_REFRESH_MIN // 60))),
    "snap_roster_h": str(max(1, int(SNAP_REFRESH_MIN_OTHER // 60))),
    "snap_max_tick": str(SNAP_MAX_PER_TICK),
    "snap_keep_days": str(SNAP_KEEP_DAYS),
    "prof_days": str(max(1, int(PROF_REFRESH_DAYS))),
    # bot Discord (rapports + mouvements de guilde)
    "bot_interval_min": str(max(1, BOT_POLL_S // 60)),
}
# bornes de saisie (min, max) par réglage
JOB_BOUNDS = {
    "snap_interval_min": (1, 1440), "snap_linked_h": (1, 168), "snap_roster_h": (1, 720),
    "snap_max_tick": (1, 500), "snap_keep_days": (2, 30), "prof_days": (1, 60),
    "bot_interval_min": (1, 1440),
}


def _job_rows() -> dict:
    with _db_lock, _db() as conn:
        return {r["key"]: r["value"] for r in conn.execute("SELECT * FROM job_config").fetchall()}


def _job_conf(key: str) -> str:
    """Réglage effectif : valeur de l'administration, sinon défaut (constante d'environnement)."""
    return _job_rows().get(key) or JOB_DEFAULTS.get(key, "")


def _job_int(key: str) -> int:
    try:
        return int(float(_job_conf(key)))
    except (TypeError, ValueError):
        return int(float(JOB_DEFAULTS.get(key, "0") or 0))


def _job_status_set(slug: str, detail: str = "", error: str = "") -> None:
    with _db_lock, _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO job_status (slug, last_run, detail, error) VALUES (?,?,?,?)",
            (slug, time.time(), detail[:200], error[:200]))


def _job_status_rows() -> dict:
    with _db_lock, _db() as conn:
        return {r["slug"]: {"last_run": r["last_run"], "detail": r["detail"], "error": r["error"]}
                for r in conn.execute("SELECT * FROM job_status").fetchall()}


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
MAIL_KEYS = ("host", "port", "mode", "user", "password", "sender", "helo")


def _mail_rows() -> dict:
    with _db_lock, _db() as conn:
        return {r["key"]: r["value"] for r in conn.execute("SELECT * FROM mail_config").fetchall()}


def _apply_mail_config() -> None:
    """Applique les réglages SMTP enregistrés (sinon retour aux variables d'environnement)."""
    rows = _mail_rows()
    mailer.set_config(rows if rows.get("host") and rows.get("user") else None)


def _brand_identity() -> dict:
    """Identité de guilde pour les e-mails et métadonnées (nom, nom court, adresse du site)."""
    name, short = "la guilde", ""
    try:
        with _db_lock, _db() as conn:
            row = _brand_row(conn)
        name = (row["guild_name"] or "").strip() or "la guilde"
        short = (row["guild_short"] or "").strip()
    except sqlite3.Error:
        pass
    return {"guild_name": name, "short_name": short, "base_url": PUBLIC_BASE_URL}


def _api_keys_rows() -> dict:
    with _db_lock, _db() as conn:
        return {r["provider"]: r for r in conn.execute("SELECT * FROM api_keys").fetchall()}


def _apply_api_keys() -> None:
    """Recopie les clés stockées vers les clients (prioritaires sur l'environnement)."""
    rows = _api_keys_rows()
    for prov, mod in (("bnet", bnet), ("wcl", wcl)):
        row = rows.get(prov)
        mod.set_credentials(row["client_id"] if row else None,
                            row["client_secret"] if row else None)


def _api_effective(prov: str) -> tuple[str, str, str]:
    """(client_id, secret, source) effectifs pour un fournisseur — source : admin / env / aucune."""
    rows = _api_keys_rows()
    row = rows.get(prov)
    if row and (row["client_id"] or row["client_secret"]):
        return (row["client_id"], row["client_secret"], "admin")
    cid, secret = (bnet.credentials() if prov == "bnet" else wcl.credentials())
    if cid or secret:
        return (cid, secret, "env")
    return ("", "", "")


def _mask(value: str, keep: int = 4) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    return ("•" * 6 + v[-keep:]) if len(v) > keep else "•" * 6


def _bot_config() -> sqlite3.Row | None:
    with _db_lock, _db() as conn:
        return conn.execute("SELECT * FROM bot_config WHERE id=1").fetchone()


def _bot_save(updates: dict) -> None:
    if not updates:
        return
    sets = ", ".join(f"{k}=?" for k in updates)
    with _db_lock, _db() as conn:
        conn.execute(f"UPDATE bot_config SET {sets}, updated=? WHERE id=1", (*updates.values(), time.time()))


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


def _gcal_key(e: dict) -> str:
    """Clé stable d'un événement du calendrier in-game (id Blizzard, sinon horodatage)."""
    try:
        if e.get("id"):
            return "id:" + str(int(e["id"]))
    except (TypeError, ValueError):
        pass
    try:
        return "ts:" + str(int(float(e.get("ts") or 0)))
    except (TypeError, ValueError):
        return "ts:0"


@app.get("/api/gcal")
def api_gcal_get(request: Request):
    """Dernier import du calendrier in-game (addon) + indispos + visées des officiers."""
    _require_user(request)
    with _db_lock, _db() as conn:
        row = conn.execute("SELECT ts, player, data FROM gcal_import WHERE id=1").fetchone()
        unavails = [dict(r) for r in conn.execute(
            "SELECT email, day_from, day_to, note FROM unavails ORDER BY day_from").fetchall()]
        links = {r["name"]: r["user_email"] for r in conn.execute(
            "SELECT user_email, name FROM char_links").fetchall()}
        unames = {(r["name"] or "").lower(): r["email"] for r in conn.execute(
            "SELECT email, name FROM users WHERE active=1").fetchall()}
        uname_by_email = {r["email"]: (r["name"] or r["email"]) for r in conn.execute(
            "SELECT email, name FROM users").fetchall()}
        metas = {r["event_key"]: dict(r) for r in conn.execute(
            "SELECT event_key, data, updated, updated_by FROM gcal_meta").fetchall()}
        wish_rows = conn.execute("SELECT item_id, name, user_email, prio FROM wishlist").fetchall()
        bis_all = _bis_by_user(conn)
        loot_rows2 = conn.execute(
            "SELECT item_id, kind, inst_fr, inst_en, boss_fr, boss_en FROM item_loot").fetchall()
        main_chars = {r["user_email"]: (r["display"] or r["name"]) for r in conn.execute(
            "SELECT user_email, display, name FROM char_links WHERE is_main=1").fetchall()}
        class_rows = conn.execute(
            "SELECT name, json_extract(data, '$.class') AS cfr, "
            "json_extract(data, '$.class_en') AS cen FROM char_snapshots "
            "WHERE id IN (SELECT MAX(id) FROM char_snapshots GROUP BY name)").fetchall()
    by_email: dict = {}
    for urow in unavails:
        by_email.setdefault(urow["email"], []).append(urow)
    if row is None:
        events = []
    else:
        try:
            data = json.loads(row["data"])
        except ValueError:
            data = {}
        events = data.get("events") or []
    loot_by_item: dict = {}
    for lr2 in loot_rows2:
        loot_by_item.setdefault(int(lr2["item_id"]), []).append(dict(lr2))
    want: dict = {}
    for wr in wish_rows:
        ent = want.setdefault(int(wr["item_id"]), {"name": wr["name"], "who": [], "prio": False})
        disp = (main_chars.get(wr["user_email"])
                or (uname_by_email.get(wr["user_email"]) or wr["user_email"]).split("@")[0])
        if disp and disp not in ent["who"]:
            ent["who"].append(disp)
        if wr["prio"] or int(wr["item_id"]) in (bis_all.get(wr["user_email"]) or {}):
            ent["prio"] = True
    en_wl = _user_locale(request).startswith("en")
    if en_wl:
        for iid2 in list(want):
            try:
                want[iid2]["name"] = (bnet.item(iid2, locale=_user_locale(request)).get("name")
                                      or want[iid2]["name"])
            except bnet.BnetError:
                pass
    status_map = {1: "ok", 3: "ok", 2: "no", 8: "maybe"}
    conflicts = 0
    for e in events:
        rows = []
        try:
            ts = float(e.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0
        key = _gcal_key(e)
        e["key"] = key
        mrow = metas.get(key)
        try:
            e["meta"] = json.loads(mrow["data"]) if mrow else None
        except (ValueError, TypeError):
            e["meta"] = None
        md2 = e.get("meta") or {}
        ev_raids = {str(x).strip().casefold() for x in (md2.get("raids") or []) if str(x).strip()}
        ev_bosses = {str(x).strip().casefold() for x in (md2.get("bosses") or []) if str(x).strip()}
        if ev_raids or ev_bosses:
            groups: dict = {}
            for iid3, went in want.items():
                for lr2 in loot_by_item.get(iid3, []):
                    in_f = str(lr2["inst_fr"] or "").strip().casefold()
                    in_e = str(lr2["inst_en"] or "").strip().casefold()
                    bo_f = str(lr2["boss_fr"] or "").strip().casefold()
                    bo_e = str(lr2["boss_en"] or "").strip().casefold()
                    if ev_raids and not (in_f in ev_raids or in_e in ev_raids):
                        continue
                    if ev_bosses and not (bo_f in ev_bosses or bo_e in ev_bosses):
                        continue
                    disp_b = None
                    for b in (md2.get("bosses") or []):
                        if str(b).strip().casefold() in (bo_f, bo_e):
                            disp_b = str(b).strip()
                            break
                    if disp_b is None:
                        disp_b = (lr2["boss_en"] if en_wl else lr2["boss_fr"]) or ""
                    g = groups.setdefault(disp_b, [])
                    ex = next((x for x in g if x["name"] == went["name"]), None)
                    if ex is not None:
                        ex["prio"] = ex["prio"] or bool(went["prio"])
                    elif len(g) < 5:
                        g.append({"name": went["name"], "who": went["who"][:3],
                                  "prio": bool(went["prio"])})
            if groups:
                e["wanted"] = [{"boss": b, "items": its} for b, its in groups.items()]
        if ts > 0:
            day = _snap_day(ts)
            ovr_map = (e.get("meta") or {}).get("ovr") or {}
            for mem in (e.get("inv") or []):
                nm = str(mem.get("n") or "").strip()
                if not nm:
                    continue
                owner = links.get(nm.lower()) or unames.get(nm.lower())
                periods = [x for x in by_email.get(owner, [])
                           if x["day_from"] <= day <= x["day_to"]] if owner else []
                if not periods:
                    continue
                st = ovr_map.get(nm) or status_map.get(mem.get("s"), "wait")
                conf = st == "ok"
                if conf:
                    conflicts += 1
                rows.append({"n": nm, "status": st, "conflict": conf,
                             "periods": [{"from": x["day_from"], "to": x["day_to"], "note": x["note"] or ""}
                                         for x in periods]})
        rows.sort(key=lambda r: (not r["conflict"], r["n"].casefold()))
        e["unav"] = rows
        e["unav_conflicts"] = sum(1 for r in rows if r["conflict"])
    today = _snap_day()
    limit = _snap_day(time.time() + 14 * 86400)
    urows = []
    for email, periods in by_email.items():
        ps = [x for x in periods if x["day_to"] >= today and x["day_from"] <= limit]
        if ps:
            urows.append({"name": uname_by_email.get(email, email),
                          "periods": [{"from": x["day_from"], "to": x["day_to"], "note": x["note"] or ""}
                                      for x in ps]})
    urows.sort(key=lambda r: r["periods"][0]["from"])
    try:
        raid_catalog, _jts = bnet.journal_raids(_user_locale(request))
    except bnet.BnetError:
        raid_catalog = {"expansion": "", "raids": []}
    en_loc = _user_locale(request).startswith("en")
    classes: dict = {}
    for cr in class_rows:
        nmk = str(cr["name"] or "").strip().lower()
        if not nmk or nmk in classes:
            continue
        cname = (cr["cen"] if en_loc else cr["cfr"]) or cr["cfr"] or ""
        if cname:
            classes[nmk] = cname
    return {"imported_at": row["ts"] if row else 0, "player": row["player"] if row else "",
            "events": events, "raid_catalog": raid_catalog, "classes": classes,
            "unavail": {"rows": urows, "counts": {"members": len(urows), "conflict": conflicts}}}


class GcalEventMetaRequest(BaseModel):
    raids: list[str] = []
    bosses: list[str] = []
    start: str = ""
    roles: dict[str, str] = {}
    ovr: dict[str, str] = {}


@app.post("/api/gcal/event/{key}")
def api_gcal_event_meta(key: str, body: GcalEventMetaRequest, request: Request):
    """Officiers : raids et boss visés pour une date (🚩 raid de départ inclus)."""
    user = _require_officer(request)
    key = (key or "").strip()[:48]
    if not (key.startswith("id:") or key.startswith("ts:")):
        raise HTTPException(400, "Clé d'événement invalide.")
    raids = [str(x).strip()[:80] for x in (body.raids or []) if str(x).strip()][:30]
    bosses = [str(x).strip()[:80] for x in (body.bosses or []) if str(x).strip()][:60]
    start = (body.start or "").strip()[:80]
    roles = {str(k).strip()[:60]: str(v).strip().lower() for k, v in (body.roles or {}).items()
             if str(k).strip() and str(v).strip().lower() in ("tank", "heal", "dps")}
    ovr = {str(k).strip()[:60]: str(v).strip().lower() for k, v in (body.ovr or {}).items()
           if str(k).strip() and str(v).strip().lower() in ("ok", "no")}
    roles, ovr = dict(list(roles.items())[:120]), dict(list(ovr.items())[:120])
    with _db_lock, _db() as conn:
        if not raids and not bosses and not start and not roles and not ovr:
            conn.execute("DELETE FROM gcal_meta WHERE event_key=?", (key,))
        else:
            conn.execute(
                "INSERT INTO gcal_meta (event_key, data, updated, updated_by) VALUES (?,?,?,?) "
                "ON CONFLICT(event_key) DO UPDATE SET data=excluded.data, updated=excluded.updated, "
                "updated_by=excluded.updated_by",
                (key, json.dumps({"raids": raids, "bosses": bosses, "start": start,
                                  "roles": roles, "ovr": ovr}, ensure_ascii=False),
                 time.time(), user["email"]))
    return {"ok": True, "key": key}


# ---------------------------------------------------------------------------
# Préparation de raid (atelier : recettes, plan, apports des membres)
# ---------------------------------------------------------------------------
class PrepPlanRequest(BaseModel):
    title: str = Field("", max_length=120)
    event_ts: float = 0
    items: list[dict] = []
    raids: list[str] = []
    bosses: list[str] = []


class PrepRecipeRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    mats: list[dict] = []


class PrepClaimRequest(BaseModel):
    mat: str = Field(..., min_length=1, max_length=120)
    qty: float = 0


def _prep_needs(plan_items: list, recipes: list) -> tuple[list[dict], list[str]]:
    """Agrège les compos nécessaires (objets du plan × quantités × recettes)."""
    rec = {}
    for r in recipes:
        rec[str(r.get("name") or "").casefold()] = r.get("mats") or []
    needs: dict = {}
    unknown: list = []
    for it in plan_items:
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        try:
            qty = max(0.0, min(9999.0, float(it.get("qty") or 0)))
        except (TypeError, ValueError):
            qty = 0.0
        mats = rec.get(name.casefold())
        if not mats:   # recette absente OU compos inconnues (données Blizzard incomplètes)
            if name not in unknown:
                unknown.append(name)
            continue
        for m in mats:
            mn = str((m or {}).get("name") or "").strip()
            if not mn:
                continue
            try:
                mq = max(0.0, min(999999.0, float((m or {}).get("qty") or 0)))
            except (TypeError, ValueError):
                mq = 0.0
            needs[mn] = needs.get(mn, 0.0) + mq * qty
    out = [{"mat": k, "need": round(v, 2)} for k, v in needs.items()]
    out.sort(key=lambda n: n["mat"].casefold())
    return out, unknown


@app.get("/api/prep")
def api_prep_get(request: Request):
    """Plan de préparation + recettes + besoins agrégés + apports des membres."""
    user = _require_user(request)
    with _db_lock, _db() as conn:
        plan = conn.execute(
            "SELECT title, event_ts, items, raids, bosses, updated, updated_by FROM prep_plan WHERE id=1").fetchone()
        recipes = [dict(r) for r in conn.execute(
            "SELECT id, name, mats, updated FROM prep_recipes ORDER BY name COLLATE NOCASE").fetchall()]
        claims = [dict(r) for r in conn.execute(
            "SELECT mat, qty, user, name FROM prep_claims").fetchall()]
        bank_rows = [dict(r) for r in conn.execute("SELECT mat, qty FROM prep_bank").fetchall()]
        crafts = [dict(r) for r in conn.execute(
            "SELECT crafter, profession, item, item_id, expansion, exp_rank, mats FROM craft_recipes").fetchall()]
        game = [dict(r) for r in conn.execute(
            "SELECT prof, tier, exp_rank, item, item_id, rank_no, mats, "
            "item_en, tier_en, prof_en, mats_en FROM game_recipes").fetchall()]
        gts_row = conn.execute("SELECT MAX(updated) AS ts FROM game_recipes").fetchone()
    for r in recipes:
        try:
            r["mats"] = json.loads(r["mats"] or "[]")
        except ValueError:
            r["mats"] = []
    p = dict(plan) if plan else {"title": "", "event_ts": 0, "items": "[]", "raids": "[]",
                                 "bosses": "[]", "updated": 0, "updated_by": ""}
    for _k in ("items", "raids", "bosses"):
        try:
            p[_k] = json.loads(p.get(_k) or "[]")
        except (ValueError, TypeError):
            p[_k] = []
    # Résolution des compos par objet : recettes maison > jeu > artisans, en ignorant les
    # entrées vides (l'API du jeu n'a pas les compos de nombreuses recettes récentes).
    cand: dict = {}
    for c in crafts:
        try:
            _m = json.loads(c["mats"] or "[]")
        except ValueError:
            _m = []
        nm = str(c.get("item") or "").strip()
        if nm:
            e = cand.setdefault(nm.casefold(), {"name": nm, "maison": [], "jeu": [], "artisans": []})
            e["artisans"] = _m
    game_best: dict = {}
    for c in game:
        try:
            g_rank = int(c.get("rank_no") or 1)
        except (TypeError, ValueError):
            g_rank = 1
        for it_nm, mats_raw in ((c.get("item"), c.get("mats")), (c.get("item_en"), c.get("mats_en"))):
            nm = str(it_nm or "").strip()
            if not nm:
                continue
            cur = game_best.get(nm.casefold())
            if cur is None or g_rank < cur[0]:
                try:
                    _gm = json.loads(mats_raw or "[]")
                except (TypeError, ValueError):
                    _gm = []
                game_best[nm.casefold()] = (g_rank, nm, _gm)
    for _k, (_rk, _nm, _gm) in game_best.items():
        e = cand.setdefault(_k, {"name": _nm, "maison": [], "jeu": [], "artisans": []})
        e["jeu"] = _gm
        e["name"] = e.get("name") or _nm
    for r in recipes:
        nm = str(r.get("name") or "").strip()
        if nm:
            e = cand.setdefault(nm.casefold(), {"name": nm, "maison": [], "jeu": [], "artisans": []})
            e["maison"] = r.get("mats") or []
    res_list = [{"name": v["name"], "mats": (v["maison"] or v["jeu"] or v["artisans"] or [])}
                for v in cand.values()]
    needs, unknown = _prep_needs(p["items"], res_list)
    by_mat: dict = {}
    for c in claims:
        by_mat.setdefault(str(c["mat"]), []).append(
            {"qty": c["qty"], "name": c["name"] or c["user"], "mine": c["user"] == user["email"]})
    known = {n["mat"] for n in needs}
    for n in needs:
        cs = by_mat.get(n["mat"], [])
        cs.sort(key=lambda x: x["name"].casefold())
        n["claims"] = cs
        n["claimed"] = round(sum(x["qty"] for x in cs), 2)
    for mat, cs in by_mat.items():
        if mat not in known:
            needs.append({"mat": mat, "need": 0, "claims": cs,
                          "claimed": round(sum(x["qty"] for x in cs), 2)})
    bank_map = {str(r["mat"]): round(float(r["qty"] or 0), 2) for r in bank_rows}
    for n in needs:
        n["bank"] = bank_map.get(str(n["mat"]), 0.0)
        n["rest"] = round(max(0.0, float(n.get("need") or 0) - float(n.get("claimed") or 0) - n["bank"]), 2)
    can = _user_role(user) in ("officer", "admin")
    catalog: dict = {}
    for c in crafts:
        key = str(c["item"]).casefold()
        ent = catalog.get(key)
        if ent is None:
            try:
                cmats = json.loads(c["mats"] or "[]")
            except ValueError:
                cmats = []
            ent = {"item": c["item"], "item_id": c["item_id"] or 0, "prof": c["profession"],
                   "exp": c["expansion"] or "", "exp_rank": c["exp_rank"] or 0,
                   "mats": cmats, "crafters": []}
            catalog[key] = ent
        else:
            # même objet dans plusieurs paliers : garder le plus récent (rang mini)
            if (c["exp_rank"] or 0) < (ent["exp_rank"] or 0):
                try:
                    ent["mats"] = json.loads(c["mats"] or "[]")
                except ValueError:
                    pass
                ent["exp"] = c["expansion"] or ""
                ent["exp_rank"] = c["exp_rank"] or 0
        if c["crafter"] not in ent["crafters"]:
            ent["crafters"].append(c["crafter"])
    cat_list = sorted(catalog.values(), key=lambda e: str(e["item"]).casefold())
    exps: dict = {}
    for c in crafts:
        nm = str(c["expansion"] or "").strip()
        rk = c["exp_rank"] or 0
        if nm and (nm not in exps or rk < exps[nm]):
            exps[nm] = rk
    exps_list = [{"name": n, "rank": r} for n, r in sorted(exps.items(), key=lambda kv: kv[1])]
    # Recettes du jeu : une entrée par objet et par métier (on garde le rang le plus bas),
    # enrichies de « qui peut la fabriquer » depuis les exports des artisans.
    known_by_item: dict = {}
    for ent_g in catalog.values():
        known_by_item.setdefault(str(ent_g["item"]).casefold(), []).extend(ent_g["crafters"])
    want_en = _user_locale(request).startswith("en")
    game_cat: dict = {}
    for c in game:
        item_nm = (c.get("item_en") or c.get("item")) if want_en else c.get("item")
        prof_nm = (c.get("prof_en") or c.get("prof")) if want_en else c.get("prof")
        exp_nm = (c.get("tier_en") or c.get("tier")) if want_en else c.get("tier")
        key = (str(item_nm).casefold(), prof_nm)
        try:
            gmats = json.loads(((c.get("mats_en") or c.get("mats")) if want_en else c.get("mats")) or "[]")
        except ValueError:
            gmats = []
        ent = game_cat.get(key)
        if ent is None or int(c["rank_no"] or 1) < int(ent["rank"] or 1):
            game_cat[key] = {"item": item_nm, "item_id": c["item_id"] or 0, "prof": prof_nm,
                             "exp": exp_nm or "", "exp_rank": c["exp_rank"] or 0,
                             "rank": c["rank_no"] or 1, "mats": gmats, "item_fr": c["item"]}
    for ent in game_cat.values():
        # les artisans sont connus par le nom FR (exports addon) — l'appariement reste FR
        ent["crafters"] = known_by_item.get(str(ent.get("item_fr") or ent["item"]).casefold(), [])
    game_list = sorted(game_cat.values(), key=lambda e: (str(e["prof"]), str(e["item"]).casefold()))
    # 🚫 Indisponibilités : croisées avec l'objectif (événement du calendrier in-game).
    unavail_block: dict = {"event": None, "mode": "upcoming", "rows": [],
                           "counts": {"members": 0, "conflict": 0}}
    with _db_lock, _db() as conn:
        unavails = [dict(r) for r in conn.execute(
            "SELECT email, day_from, day_to, note FROM unavails ORDER BY day_from").fetchall()]
        links = {r["name"]: r["user_email"] for r in conn.execute(
            "SELECT user_email, name FROM char_links").fetchall()}
        unames = {(r["name"] or "").lower(): r["email"] for r in conn.execute(
            "SELECT email, name FROM users WHERE active=1").fetchall()}
        uname_by_email = {r["email"]: (r["name"] or r["email"]) for r in conn.execute(
            "SELECT email, name FROM users").fetchall()}
        grow = conn.execute("SELECT data FROM gcal_import WHERE id=1").fetchone()
    by_email: dict = {}
    for urow in unavails:
        by_email.setdefault(urow["email"], []).append(urow)
    if p.get("event_ts"):
        try:
            events = (json.loads(grow["data"]) or {}).get("events") if grow else []
        except (ValueError, TypeError):
            events = []
        ev = None
        for e in (events or []):
            try:
                if abs(float(e.get("ts") or 0) - float(p["event_ts"])) < 60:
                    ev = e
                    break
            except (TypeError, ValueError):
                continue
        if ev is None and events:
            # objectif saisi à la main : on rapproche par jour (Paris)
            want = _snap_day(float(p["event_ts"]))
            for e in events:
                try:
                    if _snap_day(float(e.get("ts") or 0)) == want:
                        ev = e
                        break
                except (TypeError, ValueError):
                    continue
        if ev:
            day = _snap_day(float(ev.get("ts") or p["event_ts"]))
            status_map = {1: "ok", 3: "ok", 2: "no", 8: "maybe"}
            rows = []
            for m in (ev.get("inv") or []):
                nm = str(m.get("n") or "").strip()
                if not nm:
                    continue
                owner = links.get(nm.lower()) or unames.get(nm.lower())
                periods = [x for x in by_email.get(owner, [])
                           if x["day_from"] <= day <= x["day_to"]] if owner else []
                if not periods:
                    continue
                st = status_map.get(m.get("s"), "wait")
                rows.append({"name": nm, "user": uname_by_email.get(owner, "") if owner else "",
                             "status": st,
                             "periods": [{"from": x["day_from"], "to": x["day_to"], "note": x["note"] or ""}
                                         for x in periods],
                             "conflict": st == "ok"})
            rows.sort(key=lambda r: (not r["conflict"], r["name"].casefold()))
            unavail_block = {"event": {"title": ev.get("title") or "", "date": ev.get("date") or "",
                                       "ts": float(ev.get("ts") or 0)},
                             "mode": "event", "rows": rows,
                             "counts": {"members": len(rows),
                                        "conflict": sum(1 for r in rows if r["conflict"])}}
    if unavail_block["mode"] == "upcoming" and unavails:
        today = _snap_day()
        limit = _snap_day(time.time() + 14 * 86400)
        rows = []
        for email, periods in by_email.items():
            ps = [x for x in periods if x["day_to"] >= today and x["day_from"] <= limit]
            if ps:
                rows.append({"name": uname_by_email.get(email, email), "user": "", "status": "",
                             "periods": [{"from": x["day_from"], "to": x["day_to"], "note": x["note"] or ""}
                                         for x in ps],
                             "conflict": False})
        rows.sort(key=lambda r: r["periods"][0]["from"])
        unavail_block["rows"] = rows
        unavail_block["counts"] = {"members": len(rows), "conflict": 0}
    try:
        _jr, _jts = bnet.journal_raids(_user_locale(request))
        raid_catalog = _jr
    except bnet.BnetError:
        raid_catalog = {"expansion": "", "raids": []}
    return {"plan": p, "recipes": recipes, "needs": needs, "unknown": unknown,
            "unavail": unavail_block, "raid_catalog": raid_catalog, "bank": bank_map,
            "catalog": cat_list, "exps": exps_list, "crafters": sorted({c["crafter"] for c in crafts}),
            "game": game_list, "game_sync": _game_sync_report(float(gts_row["ts"] or 0)),
            "me": {"name": user["name"] if "name" in user.keys() else user["email"]},
            "can_edit": can}


@app.post("/api/prep/plan")
def api_prep_plan(body: PrepPlanRequest, request: Request):
    user = _require_officer(request)
    items = []
    for it in (body.items or [])[:60]:
        name = str((it or {}).get("name") or "").strip()[:120]
        if not name:
            continue
        try:
            qty = max(0.0, min(9999.0, float((it or {}).get("qty") or 0)))
        except (TypeError, ValueError):
            qty = 0.0
        items.append({"name": name, "qty": qty})
    raids = []
    for r in (body.raids or [])[:8]:
        nm = str(r or "").strip()[:80]
        if nm and nm not in raids:
            raids.append(nm)
    bosses = []
    for x in (body.bosses or [])[:40]:
        nm = str(x or "").strip()[:80]
        if nm and nm not in bosses:
            bosses.append(nm)
    with _db_lock, _db() as conn:
        conn.execute(
            "INSERT INTO prep_plan (id, title, event_ts, items, raids, bosses, updated, updated_by) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET title=excluded.title, event_ts=excluded.event_ts, items=excluded.items, "
            "raids=excluded.raids, bosses=excluded.bosses, updated=excluded.updated, updated_by=excluded.updated_by",
            (body.title.strip()[:120], float(body.event_ts or 0), json.dumps(items, ensure_ascii=False),
             json.dumps(raids, ensure_ascii=False), json.dumps(bosses, ensure_ascii=False),
             time.time(), user["name"] if "name" in user.keys() else user["email"]),
        )
    return {"ok": True, "items": len(items), "raids": len(raids), "bosses": len(bosses)}


@app.post("/api/prep/recipes")
def api_prep_recipe_save(body: PrepRecipeRequest, request: Request):
    _require_officer(request)
    mats = []
    for m in (body.mats or [])[:40]:
        mn = str((m or {}).get("name") or "").strip()[:120]
        if not mn:
            continue
        try:
            mq = max(0.0, min(999999.0, float((m or {}).get("qty") or 0)))
        except (TypeError, ValueError):
            mq = 0.0
        mats.append({"name": mn, "qty": mq})
    name = body.name.strip()[:120]
    now = time.time()
    with _db_lock, _db() as conn:
        row = conn.execute("SELECT id FROM prep_recipes WHERE name=? COLLATE NOCASE", (name,)).fetchone()
        if row:
            conn.execute("UPDATE prep_recipes SET mats=?, updated=? WHERE id=?",
                         (json.dumps(mats, ensure_ascii=False), now, row["id"]))
            rid = row["id"]
        else:
            cur = conn.execute("INSERT INTO prep_recipes (name, mats, created, updated) VALUES (?,?,?,?)",
                               (name, json.dumps(mats, ensure_ascii=False), now, now))
            rid = cur.lastrowid
    return {"ok": True, "id": rid, "mats": len(mats)}


@app.delete("/api/prep/recipes/{rid}")
def api_prep_recipe_del(rid: int, request: Request):
    _require_officer(request)
    with _db_lock, _db() as conn:
        conn.execute("DELETE FROM prep_recipes WHERE id=?", (rid,))
    return {"ok": True}


@app.post("/api/prep/claim")
def api_prep_claim(body: PrepClaimRequest, request: Request):
    user = _require_user(request)
    mat = body.mat.strip()[:120]
    try:
        qty = max(0.0, min(999999.0, float(body.qty or 0)))
    except (TypeError, ValueError):
        qty = 0.0
    with _db_lock, _db() as conn:
        if qty <= 0:
            conn.execute("DELETE FROM prep_claims WHERE mat=? AND user=?", (mat, user["email"]))
        else:
            conn.execute(
                "INSERT INTO prep_claims (mat, qty, user, name, updated) VALUES (?,?,?,?,?) "
                "ON CONFLICT(mat, user) DO UPDATE SET qty=excluded.qty, name=excluded.name, updated=excluded.updated",
                (mat, qty, user["email"], user["name"] if "name" in user.keys() else "", time.time()),
            )
    return {"ok": True}


class PrepBankRequest(BaseModel):
    mat: str = Field(..., min_length=1, max_length=120)
    qty: float = 0


@app.post("/api/prep/bank")
def api_prep_bank(body: PrepBankRequest, request: Request):
    """Officiers : quantité d'une compos déjà en banque de guilde (0 = retirer la ligne)."""
    user = _require_officer(request)
    mat = body.mat.strip()[:120]
    if not mat:
        raise HTTPException(400, "Compos manquante.")
    qty = max(0.0, min(999999.0, float(body.qty or 0)))
    with _db_lock, _db() as conn:
        if qty <= 0:
            conn.execute("DELETE FROM prep_bank WHERE mat=?", (mat,))
        else:
            conn.execute(
                "INSERT INTO prep_bank (mat, qty, updated, updated_by) VALUES (?,?,?,?) "
                "ON CONFLICT(mat) DO UPDATE SET qty=excluded.qty, updated=excluded.updated, "
                "updated_by=excluded.updated_by",
                (mat, qty, time.time(), user["name"] if "name" in user.keys() else user["email"]),
            )
    return {"ok": True, "mat": mat, "qty": qty}


@app.post("/api/prep/reset")
def api_prep_reset(request: Request):
    user = _require_officer(request)
    with _db_lock, _db() as conn:
        conn.execute("DELETE FROM prep_claims")
        conn.execute("UPDATE prep_plan SET items='[]', raids='[]', bosses='[]', updated=?, updated_by=? WHERE id=1",
                     (time.time(), user["name"] if "name" in user.keys() else user["email"]))
    return {"ok": True}


# ---- Recettes du jeu (API Game Data Blizzard) --------------------------------
GAME_PREP_PROFS = ((185, "Cuisine"), (171, "Alchimie"), (773, "Calligraphie"),
                   (164, "Forge"), (165, "Travail du cuir"), (202, "Ingénierie"))
GAME_SYNC_TTL = 86400.0  # resynchro auto au plus une fois par jour (boucle 6 h + au démarrage)
_game_sync_state = {"state": "idle", "prof": "", "done": 0, "total": 0, "error": "", "ts": 0.0}


class MplusPostRequest(BaseModel):
    roles: list[str] = []
    slots: list[dict] = []
    keys: list[dict] = []


def _mplus_clean_slots(slots) -> list[dict]:
    out = []
    for s in (slots or [])[:6]:
        if not isinstance(s, dict):
            continue
        days = sorted({str(d)[:10] for d in (s.get("days") or [])
                       if re.match(r"^\d{4}-\d{2}-\d{2}$", str(d))})[:20]
        fr = str(s.get("from") or "")[:5]
        to = str(s.get("to") or "")[:5]
        fr = fr if re.match(r"^\d{2}:\d{2}$", fr) else ""
        to = to if re.match(r"^\d{2}:\d{2}$", to) else ""
        if days and (fr or to):
            out.append({"days": days, "from": fr, "to": to})
    return out


def _dungeon_key(name: str) -> str:
    """Clé canonique d'un donjon MM+ (nom anglais) pour comparer FR/EN."""
    nm = (name or "").strip()
    if not nm:
        return ""
    try:
        dun, _ts = bnet.mplus_dungeons()
        for x in (dun.get("dungeons") or []):
            if nm.lower() in ((x.get("name") or "").lower(), (x.get("en") or "").lower()):
                return (x.get("en") or x.get("name") or nm).lower()
    except bnet.BnetError:
        pass
    return nm.lower()


def _notify_mplus_keys(owner_email: str, owner_name: str, keys: list[dict]) -> None:
    """Prévient les membres dont une alerte MM+ correspond aux clés annoncées."""
    now = time.time()
    with _db_lock, _db() as conn:
        alerts = [dict(r) for r in conn.execute(
            "SELECT email, dungeon, min_level FROM mplus_alerts WHERE email != ?",
            (owner_email,)).fetchall()]
        for k in keys:
            dk = _dungeon_key(k.get("dungeon"))
            for a in alerts:
                if a["dungeon"] and a["dungeon"] != dk:
                    continue
                if int(k.get("level") or 2) < int(a["min_level"] or 2):
                    continue
                payload = json.dumps({"who": owner_name, "dungeon": k.get("dungeon") or "",
                                      "level": k.get("level") or 2}, ensure_ascii=False)
                dup = conn.execute(
                    "SELECT 1 AS x FROM notifs WHERE email=? AND kind='mplus' AND data=? AND created > ?",
                    (a["email"], payload, now - 7 * 86400)).fetchone()
                if dup:
                    continue
                conn.execute("INSERT INTO notifs (email, kind, data, created) VALUES (?,?,?,?)",
                             (a["email"], "mplus", payload, now))


def _mplus_clean_keys(keys) -> list[dict]:
    out = []
    for k in (keys or [])[:12]:
        if not isinstance(k, dict):
            continue
        ch = str(k.get("char") or "").strip()[:40]
        du = str(k.get("dungeon") or "").strip()[:80]
        try:
            lv = max(2, min(40, int(k.get("level") or 2)))
        except (TypeError, ValueError):
            lv = 2
        if ch or du:
            out.append({"char": ch, "dungeon": du, "level": lv})
    return out


@app.get("/api/mplus")
def api_mplus(request: Request):
    """Tableau d'organisation MM+ : dispos de chacun + clés annoncées."""
    user = _require_user(request)
    with _db_lock, _db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT user, name, roles, slots, keys, updated FROM mplus_posts ORDER BY updated DESC").fetchall()]
        mine = [dict(r) for r in conn.execute(
            "SELECT name, display, is_main FROM char_links WHERE user_email=?", (user["email"],)).fetchall()]
    posts = []
    for r in rows:
        for f in ("roles", "slots", "keys"):
            try:
                r[f] = json.loads(r[f] or "[]")
            except ValueError:
                r[f] = []
        r["mine"] = r["user"] == user["email"]
        posts.append(r)
    try:
        dun, _ts = bnet.mplus_dungeons()
        dungeons = dun.get("dungeons") or []
    except bnet.BnetError:
        dungeons = []
    if _user_locale(request).startswith("en"):
        dungeons = [dict(x, name=(x.get("en") or x.get("name"))) for x in dungeons]
    return {"posts": posts, "dungeons": dungeons,
            "my_chars": [{"name": c["name"], "display": c["display"], "is_main": bool(c["is_main"])}
                         for c in mine],
            "me": {"name": user["name"] if "name" in user.keys() else user["email"]}}


@app.post("/api/mplus")
def api_mplus_save(body: MplusPostRequest, request: Request):
    user = _require_user(request)
    roles = [r for r in (body.roles or []) if r in ("tank", "heal", "dps")][:3]
    slots = _mplus_clean_slots(body.slots)
    keys = _mplus_clean_keys(body.keys)
    email = user["email"]
    name = (user["name"] if "name" in user.keys() else "") or email
    with _db_lock, _db() as conn:
        _old = conn.execute("SELECT keys FROM mplus_posts WHERE user=?", (email,)).fetchone()
    try:
        _old_keys = {(_dungeon_key(k.get("dungeon")), int(k.get("level") or 2))
                     for k in (json.loads(_old["keys"]) if _old else [])}
    except (ValueError, TypeError, AttributeError):
        _old_keys = set()
    with _db_lock, _db() as conn:
        if not roles and not slots and not keys:
            conn.execute("DELETE FROM mplus_posts WHERE user=?", (email,))
            return {"ok": True, "deleted": True}
        conn.execute(
            "INSERT INTO mplus_posts (user, name, roles, slots, keys, updated) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(user) DO UPDATE SET name=excluded.name, roles=excluded.roles, "
            "slots=excluded.slots, keys=excluded.keys, updated=excluded.updated",
            (email, name, json.dumps(roles),
             json.dumps(slots, ensure_ascii=False), json.dumps(keys, ensure_ascii=False), time.time()))
    _new_keys = [k for k in keys if (_dungeon_key(k.get("dungeon")), int(k.get("level") or 2)) not in _old_keys]
    if _new_keys:
        _notify_mplus_keys(email, name, _new_keys)
    return {"ok": True, "roles": roles, "slots": len(slots), "keys": len(keys)}


class AlertRequest(BaseModel):
    dungeon: str = Field("", max_length=80)
    min_level: int = 2


@app.get("/api/me/alerts")
def api_my_alerts(request: Request):
    """Mes alertes MM+ (clés recherchées)."""
    user = _require_user(request)
    with _db_lock, _db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, dungeon, min_level, created FROM mplus_alerts WHERE email=? ORDER BY created DESC",
            (user["email"],)).fetchall()]
    return {"alerts": rows}


@app.post("/api/me/alerts")
def api_my_alerts_add(body: AlertRequest, request: Request):
    user = _require_user(request)
    dun = _dungeon_key(body.dungeon)
    lv = max(2, min(40, int(body.min_level or 2)))
    with _db_lock, _db() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM mplus_alerts WHERE email=?",
                         (user["email"],)).fetchone()["n"]
        if n >= 20:
            raise HTTPException(400, "Trop d'alertes (20 maximum).")
        if conn.execute("SELECT 1 AS x FROM mplus_alerts WHERE email=? AND dungeon=? AND min_level=?",
                        (user["email"], dun, lv)).fetchone() is None:
            conn.execute("INSERT INTO mplus_alerts (email, dungeon, min_level, created) VALUES (?,?,?,?)",
                         (user["email"], dun, lv, time.time()))
    return {"ok": True}


@app.delete("/api/me/alerts/{aid}")
def api_my_alerts_del(aid: int, request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        conn.execute("DELETE FROM mplus_alerts WHERE id=? AND email=?", (aid, user["email"]))
    return {"ok": True}


@app.get("/api/me/notifs")
def api_my_notifs(request: Request):
    """Mes notifications (20 dernières) + compteur non lues."""
    user = _require_user(request)
    with _db_lock, _db() as conn:
        unread = conn.execute("SELECT COUNT(*) AS n FROM notifs WHERE email=? AND seen=0",
                              (user["email"],)).fetchone()["n"]
        items = [dict(r) for r in conn.execute(
            "SELECT id, kind, data, seen, created FROM notifs WHERE email=? ORDER BY created DESC LIMIT 20",
            (user["email"],)).fetchall()]
    for it in items:
        try:
            it["data"] = json.loads(it["data"] or "{}")
        except ValueError:
            it["data"] = {}
    return {"unread": unread, "items": items}


@app.post("/api/me/notifs/read")
def api_my_notifs_read(request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        conn.execute("UPDATE notifs SET seen=1 WHERE email=? AND seen=0", (user["email"],))
    return {"ok": True}


@app.get("/api/me/overview")
def api_me_overview(request: Request):
    """Page 🙋 Moi : mes personnages + stats rapides (ilvl, présence, recettes) et mes clés."""
    user = _require_user(request)
    locale = _user_locale(request)
    with _db_lock, _db() as conn:
        links = [dict(r) for r in conn.execute(
            "SELECT id, realm, name, display, is_main FROM char_links WHERE user_email=? "
            "ORDER BY is_main DESC, display COLLATE NOCASE", (user["email"],)).fetchall()]
        craft = {(r["c"] or ""): r["n"] for r in conn.execute(
            "SELECT lower(crafter) AS c, COUNT(*) AS n FROM craft_recipes GROUP BY lower(crafter)").fetchall()}
        post = conn.execute("SELECT keys FROM mplus_posts WHERE user=?", (user["email"],)).fetchone()
    try:
        my_keys = json.loads(post["keys"]) if post else []
    except (ValueError, TypeError):
        my_keys = []
    try:
        att = api_attendance(request, days=30, refresh=0)
        att_rows = {(r.get("name") or "").lower(): r for r in (att.get("rows") or [])}
    except Exception:
        att_rows = {}
    chars = []
    for c in links:
        try:
            sm, _ts = bnet.character(c["realm"], c["name"], 0, locale=locale)
        except Exception:
            sm = {}
        a = att_rows.get((c["name"] or "").lower()) or {}
        chars.append({
            "id": c["id"], "realm": c["realm"], "name": c["name"], "display": c["display"],
            "is_main": bool(c["is_main"]),
            "level": sm.get("level"), "class": sm.get("class"), "class_key": sm.get("class_key"),
            "spec": sm.get("spec"), "ilvl_equipped": sm.get("ilvl_equipped"), "ilvl_avg": sm.get("ilvl_avg"),
            "achievements": sm.get("achievement_points"), "last_login": sm.get("last_login"),
            "att_pct": a.get("pct"), "att_nights": a.get("nights"),
            "recipes": craft.get((c["name"] or "").lower(), 0),
        })
    keys_out = [{"char": k.get("char") or "", "dungeon": k.get("dungeon") or "", "level": k.get("level") or 2}
                for k in (my_keys if isinstance(my_keys, list) else [])][:12]
    return {"chars": chars, "keys": keys_out}


def _game_sync_report(db_ts: float = 0.0) -> dict:
    """État de synchro exposé à l'UI (le ts est repris de la base après redémarrage)."""
    gs = dict(_game_sync_state)
    if not gs.get("ts"):
        gs["ts"] = db_ts
    return gs


def _game_sync(profs=None) -> None:
    """Synchronise les recettes des paliers « 2 dernières extensions » des métiers utiles."""
    st = _game_sync_state
    if st["state"] == "running":
        return
    st.update({"state": "running", "prof": "", "done": 0, "total": 0, "error": ""})
    wanted = {str(p).casefold() for p in (profs or [])}
    cibles = [p for p in GAME_PREP_PROFS if not wanted or p[1].casefold() in wanted]
    total_written = 0
    try:
        for pid, nom in cibles:
            prof = bnet.game_profession(pid, locale="fr_FR")
            try:
                prof_en = bnet.game_profession(pid, locale="en_US")
            except bnet.BnetError:
                prof_en = {}
            tiers = (prof.get("skill_tiers") or [])[-2:]  # les 2 paliers les plus récents
            tiers_en = {t.get("id"): (t.get("name") or "") for t in (prof_en.get("skill_tiers") or [])}
            st["prof"] = nom
            rows_all = []
            for rank, tier in enumerate(reversed(tiers)):  # rang 0 = la plus récente extension
                recs = bnet.game_tier_recipes(pid, tier["id"], locale="fr_FR")
                st["total"] = (st["total"] or 0) + len(recs)
                for r in recs:
                    try:
                        d = bnet.game_recipe(r["id"], locale="fr_FR")
                    except bnet.BnetError:
                        continue  # recette non exposée — ignorée
                    try:
                        d_en = bnet.game_recipe(r["id"], locale="en_US")
                    except bnet.BnetError:
                        d_en = {}
                    ci = d.get("crafted_item") or {}
                    ci_en = d_en.get("crafted_item") or {}
                    mats, mats_en = [], []
                    for m in d.get("reagents") or []:
                        rr = m.get("reagent") or {}
                        mats.append({"id": rr.get("id") or 0, "name": rr.get("name") or "",
                                     "qty": float(m.get("quantity") or 0)})
                    for m in d_en.get("reagents") or []:
                        rr = m.get("reagent") or {}
                        mats_en.append({"id": rr.get("id") or 0, "name": rr.get("name") or "",
                                        "qty": float(m.get("quantity") or 0)})
                    try:
                        rrank = int(d.get("rank") or 1)
                    except (TypeError, ValueError):
                        rrank = 1
                    rows_all.append((int(d.get("id") or r["id"]), nom, tier.get("name") or "", rank,
                                     ci.get("name") or d.get("name") or "", ci.get("id") or 0, rrank,
                                     json.dumps(mats, ensure_ascii=False), time.time(),
                                     ci_en.get("name") or d_en.get("name") or "",
                                     tiers_en.get(tier["id"]) or "",
                                     prof_en.get("name") or "",
                                     json.dumps(mats_en, ensure_ascii=False)))
                    st["done"] += 1
                    if st["done"] % 4 == 0:
                        time.sleep(0.02)  # politesse (limite Blizzard : 100 req/s)
            with _db_lock, _db() as conn:
                conn.execute("DELETE FROM game_recipes WHERE prof=?", (nom,))
                conn.executemany(
                    "INSERT OR REPLACE INTO game_recipes (id, prof, tier, exp_rank, item, item_id, rank_no, mats, updated, "
                    "item_en, tier_en, prof_en, mats_en) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows_all)
            total_written += len(rows_all)
        st.update({"state": "done", "ts": time.time()})
        print(f"[game-recipes] synchro OK : {total_written} recettes", flush=True)
    except Exception as exc:  # noqa: BLE001 — tâche de fond : on trace sans casser
        st.update({"state": "error", "error": str(exc)[:200]})
        print(f"[game-recipes] erreur : {exc}", flush=True)


def _game_recipes_loop() -> None:
    """Au démarrage puis toutes les 6 h : synchro si vide ou trop ancienne."""
    time.sleep(50)
    while True:
        try:
            with _db_lock, _db() as conn:
                row = conn.execute("SELECT COUNT(*) AS n, MAX(updated) AS ts FROM game_recipes").fetchone()
            n, ts = int(row["n"] or 0), float(row["ts"] or 0)
            if _game_sync_state["state"] != "running" and (n == 0 or time.time() - ts > GAME_SYNC_TTL):
                _game_sync()
        except Exception as exc:  # noqa: BLE001
            print(f"[game-recipes] boucle : {exc}", flush=True)
        time.sleep(6 * 3600)


LOOT_SYNC_TTL = 7 * 86400.0
_loot_sync_state = {"state": "idle", "ts": 0.0, "error": ""}


def _loot_sync() -> None:
    """Synchronise le butin des raids et donjons (journal de jeu) dans item_loot."""
    st = _loot_sync_state
    if st["state"] == "running":
        return
    st.update({"state": "running", "error": ""})
    try:
        rows = bnet.journal_loot()
        now = time.time()
        with _db_lock, _db() as conn:
            conn.execute("DELETE FROM item_loot")
            conn.executemany(
                "INSERT INTO item_loot (item_id, kind, inst_fr, inst_en, boss_fr, boss_en, updated) "
                "VALUES (?,?,?,?,?,?,?)",
                [(r["item_id"], r["kind"], r["inst_fr"], r["inst_en"], r["boss_fr"], r["boss_en"], now)
                 for r in rows])
        st.update({"state": "idle", "ts": now})
        print(f"[loot] butin synchronisé : {len(rows)} ligne(s)", flush=True)
    except Exception as exc:  # noqa: BLE001
        st.update({"state": "idle", "error": str(exc)})
        print(f"[loot] synchro : {exc}", flush=True)


def _loot_loop() -> None:
    """Au démarrage puis toutes les 6 h : synchro du butin si vide ou trop ancien."""
    time.sleep(70)
    while True:
        try:
            with _db_lock, _db() as conn:
                row = conn.execute("SELECT COUNT(*) AS n, MAX(updated) AS ts FROM item_loot").fetchone()
            n, ts = int(row["n"] or 0), float(row["ts"] or 0)
            if _loot_sync_state["state"] != "running" and (n == 0 or time.time() - ts > LOOT_SYNC_TTL):
                _loot_sync()
        except Exception as exc:  # noqa: BLE001
            print(f"[loot] boucle : {exc}", flush=True)
        time.sleep(6 * 3600)


class PrepSyncGameRequest(BaseModel):
    profs: list[str] = []


class PrepRecipesImportRequest(BaseModel):
    payload: str = Field(..., max_length=2_000_000)


def _prep_parse_recipes(text: str) -> dict:
    """Extrait un export de recettes d'artisan : JSON brut ou fichier SavedVariables (clé "recipes")."""
    t = (text or "").strip()
    if not t:
        raise HTTPException(400, "Contenu vide.")
    raw = None
    st = re.search(r'\["recipes"\]\s*=\s*"((?:[^"\\]|\\.)*)"', t, re.S)
    if st:
        raw = _lua_unescape(st.group(1))
    elif t.startswith("{") and '"professions"' in t:
        raw = t
    else:
        j = t.find('{"v":')
        k = t.rfind("}")
        if j >= 0 and k > j and '"professions"' in t[j:k + 1]:
            raw = t[j:k + 1]
    if raw is None:
        snippet = " ".join(t[:90].split())
        raise HTTPException(400, "Format non reconnu (reçu : %d caractères — « %s… »). En jeu : /cohors recettes, "
                                 "puis /reload, puis choisis le fichier WTF/Account/<compte>/SavedVariables/Cohors.lua."
                                 % (len(t), snippet))
    raw = re.sub(r"(\s*:\s*)0x([0-9A-Fa-f]+)", r'\1"0x\2"', raw)
    try:
        data = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Données illisibles (JSON invalide).")
    if not isinstance(data, dict) or not isinstance(data.get("professions"), list):
        raise HTTPException(400, "Données inattendues (aucun métier dans cet export).")
    return data


@app.post("/api/prep/import-recipes")
def api_prep_import_recipes(body: PrepRecipesImportRequest, request: Request):
    """Import d'un export d'addon (/cohors recettes) : officiers, ou chacun pour ses propres personnages."""
    user = _require_user(request)
    data = _prep_parse_recipes(body.payload)
    crafter = str(data.get("player") or "").strip()[:60] or "?"
    if _user_role(user) not in ("officer", "admin") and not _owns_char(user, crafter):
        raise HTTPException(403, "Tu ne peux importer que les recettes de tes propres personnages "
                                 "(lie-les sur la page Personnages, ou demande à un officier).")
    realm = str(data.get("realm") or "").strip()[:60]
    rows = []
    for prof in (data.get("professions") or [])[:10]:
        pname = str((prof or {}).get("name") or "").strip()[:60]
        for rec in ((prof or {}).get("recipes") or [])[:1500]:
            item = str((rec or {}).get("n") or "").strip()[:120]
            if not item:
                continue
            mats = []
            for m in ((rec or {}).get("m") or [])[:30]:
                if isinstance(m, list) and len(m) >= 3:
                    try:
                        q = float(m[2] or 0)
                    except (TypeError, ValueError):
                        q = 0.0
                    mats.append({"id": _int_any(m[0]), "name": str(m[1] or "")[:120], "qty": q})
            exp = str((rec or {}).get("e") or "").strip()[:60]
            try:
                trank = max(0, min(99, int((rec or {}).get("t") or 0)))
            except (TypeError, ValueError):
                trank = 0
            rows.append((crafter, realm, pname, item, _int_any((rec or {}).get("i")), exp, trank,
                         json.dumps(mats, ensure_ascii=False)))
    if not rows:
        raise HTTPException(400, "Aucune recette exploitable dans cet export.")
    now = time.time()
    with _db_lock, _db() as conn:
        conn.execute("DELETE FROM craft_recipes WHERE crafter=?", (crafter,))
        conn.executemany(
            "INSERT OR REPLACE INTO craft_recipes (crafter, realm, profession, item, item_id, expansion, exp_rank, mats, updated) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [(c, r, p, i, iid, e, tk, mm, now) for (c, r, p, i, iid, e, tk, mm) in rows],
        )
    return {"ok": True, "crafter": crafter, "recipes": len(rows),
            "professions": len(data.get("professions") or [])}


class MyRecipesSave(BaseModel):
    realm: str = Field(..., min_length=2, max_length=60)
    name: str = Field(..., min_length=2, max_length=60)
    prof: str = Field("", max_length=60)
    items: list[int] = []


@app.get("/api/my/recipes")
def api_my_recipes(request: Request, realm: str = "", name: str = "", prof: str = ""):
    """Recettes connues d'un de MES personnages + catalogue du jeu du métier choisi (self-service)."""
    user = _require_user(request)
    realm_l = realm.strip().lower()
    name_s = name.strip()[:60]
    _valid_char(realm_l, name_s)
    if _user_role(user) not in ("officer", "admin") and not _owns_char(user, name_s):
        raise HTTPException(403, "Ce personnage n'est pas lié à ton compte.")
    want_en = _user_locale(request).startswith("en")
    with _db_lock, _db() as conn:
        prows = conn.execute(
            "SELECT data FROM char_professions WHERE realm=? AND name=?",
            (realm_l, name_s.lower())).fetchone()
        known_rows = conn.execute(
            "SELECT item FROM craft_recipes WHERE lower(crafter)=lower(?)", (name_s,)).fetchall()
        game_profs = [dict(r) for r in conn.execute(
            "SELECT DISTINCT prof, prof_en FROM game_recipes ORDER BY prof").fetchall()]
        catalog = []
        if prof.strip():
            catalog = [dict(r) for r in conn.execute(
                "SELECT id, item, item_en, item_id, exp_rank, rank_no, mats, mats_en, tier, tier_en, prof, prof_en "
                "FROM game_recipes WHERE prof=? ORDER BY item COLLATE NOCASE, rank_no",
                (prof.strip()[:60],)).fetchall()]
    profs = []
    if prows:
        try:
            pd = json.loads(prows["data"]) or {}
        except (ValueError, TypeError):
            pd = {}
        for p in (pd.get("profs") or []):
            key = p.get("name_fr") or p.get("name") or ""
            if key:
                profs.append({"key": key, "label": ((p.get("name_en") or key) if want_en else key),
                              "points": p.get("points"), "max": p.get("max")})
    known = {str(r["item"]).casefold() for r in known_rows}
    cat = []
    seen_items: set = set()
    for c in catalog:
        item_key = str(c.get("item")).casefold()
        if item_key in seen_items:   # un seul exemplaire par objet (rang mini conservé)
            continue
        seen_items.add(item_key)
        try:
            mats = json.loads(((c.get("mats_en") or c.get("mats")) if want_en else c.get("mats")) or "[]")
        except ValueError:
            mats = []
        cat.append({
            "id": c["id"],            # id de recette (clé de sélection, unique)
            "item_id": c["item_id"],
            "name": ((c.get("item_en") or c.get("item")) if want_en else c.get("item")) or "",
            "exp": ((c.get("tier_en") or c.get("tier")) if want_en else c.get("tier")) or "",
            "exp_rank": c.get("exp_rank") or 0,
            "mats": mats,
            "known": item_key in known,
        })
    return {"char": {"realm": realm_l, "name": name_s}, "professions": profs,
            "game_profs": [{"key": r["prof"],
                            "label": ((r.get("prof_en") or r["prof"]) if want_en else r["prof"])}
                           for r in game_profs],
            "prof": prof.strip(), "catalog": cat}


@app.post("/api/my/recipes")
def api_my_recipes_save(body: MyRecipesSave, request: Request):
    """Enregistre (remplace) les recettes connues d'un de MES personnages pour un métier."""
    user = _require_user(request)
    realm_l = body.realm.strip().lower()
    name_s = body.name.strip()[:60]
    _valid_char(realm_l, name_s)
    if _user_role(user) not in ("officer", "admin") and not _owns_char(user, name_s):
        raise HTTPException(403, "Ce personnage n'est pas lié à ton compte.")
    prof = body.prof.strip()[:60]
    if not prof:
        raise HTTPException(400, "Choisis d'abord un métier.")
    ids: list[int] = []
    for i in (body.items or [])[:2000]:
        try:
            iid = int(i)
        except (TypeError, ValueError):
            continue
        if iid not in ids:
            ids.append(iid)
    with _db_lock, _db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, item, item_id, tier, exp_rank, mats FROM game_recipes WHERE prof=?", (prof,)).fetchall()]
    by_key = {int(r["id"]): r for r in rows}
    picked = [by_key[i] for i in ids if i in by_key]
    if ids and not picked:
        raise HTTPException(400, "Ces recettes ne correspondent pas au métier choisi (ou n'existent pas).")
    now = time.time()
    prof_alt = PROF_EN.get(prof, prof)
    with _db_lock, _db() as conn:
        conn.execute("DELETE FROM craft_recipes WHERE lower(crafter)=lower(?) AND profession IN (?,?)",
                     (name_s, prof, prof_alt))
        if picked:
            conn.executemany(
                "INSERT OR REPLACE INTO craft_recipes (crafter, realm, profession, item, item_id, "
                "expansion, exp_rank, mats, updated) VALUES (?,?,?,?,?,?,?,?,?)",
                [(name_s, realm_l, prof, r["item"], r["item_id"], r.get("tier") or "",
                  r.get("exp_rank") or 0, r.get("mats") or "[]", now) for r in picked])
    return {"ok": True, "saved": len(picked), "prof": prof}


@app.post("/api/prep/sync-game")
def api_prep_sync_game(body: PrepSyncGameRequest, request: Request):
    """(Officiers) Lance la synchro des recettes du jeu en tâche de fond."""
    _require_officer(request)
    if _game_sync_state["state"] == "running":
        return {"ok": True, "running": True}
    threading.Thread(target=_game_sync, args=(body.profs or [],), daemon=True,
                     name="game-recipes-manual").start()
    return {"ok": True, "started": True}


@app.post("/api/gcal/import")
def api_gcal_import(payload: GcalImportRequest, request: Request):
    """Importe un export du calendrier in-game (JSON collé ou fichier SavedVariables Cohors.lua)."""
    _require_officer(request)
    data = _gcal_parse(payload.payload)
    events: list[dict] = []
    responses = 0
    for e in (data.get("events") or []):
        if not isinstance(e, dict):
            continue
        inv = []
        for i in (e.get("inv") or []):
            if not isinstance(i, dict) or not i.get("n"):
                continue
            try:
                st = int(i.get("s")) if i.get("s") is not None else -1
            except (TypeError, ValueError):
                st = -1
            inv.append({"n": str(i["n"])[:60], "s": st,
                        "t": (i.get("t") if isinstance(i.get("t"), (int, float)) else None)})
        responses += len(inv)
        try:
            ts = float(e.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        events.append({
            "id": _int_any(e.get("id")),
            "title": str(e.get("title") or "?")[:200],
            "date": str(e.get("date") or "")[:20],
            "ts": ts,
            "type": _int_any(e.get("type")),
            "inv": inv,
        })
    blob = json.dumps({"events": events}, ensure_ascii=False)
    with _db_lock, _db() as conn:
        conn.execute(
            "INSERT INTO gcal_import (id, ts, player, data) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET ts=excluded.ts, player=excluded.player, data=excluded.data",
            (time.time(), str(data.get("player") or "")[:60], blob),
        )
    return {"ok": True, "events": len(events), "responses": responses}


class UnavailRequest(BaseModel):
    day_from: str = Field(..., min_length=10, max_length=10)
    day_to: str = Field("", max_length=10)
    note: str = Field("", max_length=120)


@app.get("/api/me/unavail")
def api_my_unavail(request: Request):
    """Mes périodes d'indisponibilité (surtout pour les raids)."""
    user = _require_user(request)
    with _db_lock, _db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, day_from, day_to, note FROM unavails WHERE email=? ORDER BY day_from",
            (user["email"],)).fetchall()]
    return {"items": rows}


@app.post("/api/me/unavail")
def api_my_unavail_add(body: UnavailRequest, request: Request):
    user = _require_user(request)
    try:
        d1 = datetime.strptime(body.day_from.strip(), "%Y-%m-%d").date()
        d2 = datetime.strptime((body.day_to.strip() or body.day_from.strip()), "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Dates invalides (format attendu : AAAA-MM-JJ).")
    if d2 < d1:
        d1, d2 = d2, d1
    if (d2 - d1).days > 180:
        raise HTTPException(400, "Période trop longue (180 jours maximum).")
    note = body.note.strip()[:120]
    with _db_lock, _db() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM unavails WHERE email=?",
                         (user["email"],)).fetchone()["n"]
        if n >= 20:
            raise HTTPException(400, "Trop de périodes (20 maximum).")
        dup = conn.execute(
            "SELECT 1 AS x FROM unavails WHERE email=? AND day_from=? AND day_to=? AND note=?",
            (user["email"], d1.isoformat(), d2.isoformat(), note)).fetchone()
        if dup is None:
            conn.execute("INSERT INTO unavails (email, day_from, day_to, note, created) VALUES (?,?,?,?,?)",
                         (user["email"], d1.isoformat(), d2.isoformat(), note, time.time()))
    return {"ok": True}


@app.delete("/api/me/unavail/{uid}")
def api_my_unavail_del(uid: int, request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        conn.execute("DELETE FROM unavails WHERE id=? AND email=?", (uid, user["email"]))
    return {"ok": True}


@app.post("/api/gcal/relance/{event_id}")
def api_gcal_relance(event_id: int, request: Request):
    """Poste sur Discord une relance pour les membres sans réponse (événement in-game)."""
    _require_officer(request)
    cfg = _bot_config()
    token, channel = (cfg["token"] or "").strip(), (cfg["channel_id"] or "").strip()
    if not (cfg["enabled"] and token and channel):
        raise HTTPException(400, "Bot Discord non configuré ou inactif (Admin → Bot Discord).")
    with _db_lock, _db() as conn:
        row = conn.execute("SELECT data FROM gcal_import WHERE id=1").fetchone()
        mrow = conn.execute("SELECT data FROM gcal_meta WHERE event_key=?",
                            ("id:" + str(event_id),)).fetchone()
    if row is None:
        raise HTTPException(404, "Aucun import du calendrier in-game.")
    try:
        events = (json.loads(row["data"]) or {}).get("events") or []
    except ValueError:
        events = []
    ev = next((e for e in events if _int_any(e.get("id")) == event_id), None)
    if ev is None:
        raise HTTPException(404, "Événement introuvable dans le dernier import.")
    forced = {}
    if mrow is not None:
        try:
            forced = (json.loads(mrow["data"]) or {}).get("ovr") or {}
        except ValueError:
            forced = {}
    waiting = [i.get("n") for i in (ev.get("inv") or [])
               if int(i.get("s", -1)) not in (1, 2, 3, 8) and i.get("n") not in forced]
    if not waiting:
        raise HTTPException(400, "Tout le monde a répondu 👍")
    link = f"{PUBLIC_BASE_URL}/calendar" if PUBLIC_BASE_URL else ""
    emb = {
        "title": "⏰ Il manque des réponses",
        "description": (f"**{ev.get('title') or 'Raid'}** — {ev.get('date') or ''}\n"
                        f"En attente de réponse : **{', '.join(waiting[:40])}**")
                       + (f"\n\n👉 [Répondre sur le site]({link})" if link else ""),
        "color": discord_bot.COLOR_CRIMSON,
        "footer": {"text": f"{_brand_identity()['guild_name']} · calendrier"},
    }
    try:
        discord_bot.send(token, channel, embeds=[emb])
    except discord_bot.DiscordError as exc:
        raise HTTPException(400, f"Discord — {exc}")
    return {"ok": True, "count": len(waiting)}


@app.get("/api/admin/mail")
def admin_mail_get(request: Request):
    _require_admin(request)
    rows = _mail_rows()
    cfg = mailer._config()
    env_ok = bool(os.environ.get("SMTP_HOST", "").strip() and os.environ.get("SMTP_USER", "").strip())
    src = "admin" if (rows.get("host") and rows.get("user")) else ("env" if env_ok else "")
    pw = rows.get("password") or ""
    return {
        "config": {k: rows.get(k, "") for k in MAIL_KEYS},
        "password_hint": ("•" * 6 + pw[-4:]) if len(pw) >= 4 else ("•" * len(pw) if pw else ""),
        "configured": cfg is not None,
        "source": src,
        "env_available": env_ok,
        "effective": {"host": (cfg or {}).get("host", ""), "port": (cfg or {}).get("port", ""),
                      "mode": (cfg or {}).get("mode", ""), "sender": (cfg or {}).get("sender", "")},
    }


class MailConfigRequest(BaseModel):
    values: dict[str, str] = {}
    clear: bool = False


@app.post("/api/admin/mail")
def admin_mail_save(payload: MailConfigRequest, request: Request):
    _require_admin(request)
    if payload.clear:
        with _db_lock, _db() as conn:
            conn.execute("DELETE FROM mail_config")
        _apply_mail_config()
        return {"ok": True, "cleared": True}
    values = {k: str(v).strip() for k, v in (payload.values or {}).items() if k in MAIL_KEYS}
    if not values:
        raise HTTPException(400, "Aucune valeur à enregistrer.")
    rows = dict(_mail_rows())
    if values.get("password", None) == "":
        values.pop("password")  # mot de passe vide = inchangé
    rows.update(values)
    if not (rows.get("host") and rows.get("user")):
        raise HTTPException(400, "Serveur et identifiant sont obligatoires.")
    try:
        port = int(rows.get("port") or 587)
    except (TypeError, ValueError):
        raise HTTPException(400, "Port invalide.")
    if not (1 <= port <= 65535):
        raise HTTPException(400, "Port entre 1 et 65535.")
    mode = (rows.get("mode") or "starttls").lower()
    if mode not in ("starttls", "ssl", "none"):
        raise HTTPException(400, "Mode de sécurité inconnu.")
    res = mailer.check(rows.get("host", ""), port, mode, rows.get("user", ""),
                       rows.get("password", ""), rows.get("sender", ""), rows.get("helo", ""))
    if not res["ok"]:
        raise HTTPException(400, f"SMTP — {res['detail']}")
    rows["port"] = str(port)
    rows["mode"] = mode
    with _db_lock, _db() as conn:
        for k, v in rows.items():
            conn.execute("INSERT OR REPLACE INTO mail_config (key, value, updated) VALUES (?,?,?)",
                         (k, v, time.time()))
    _apply_mail_config()
    return {"ok": True, "test": res["detail"]}


class MailTestRequest(BaseModel):
    to: str = Field(..., max_length=200)


@app.post("/api/admin/mail/test")
def admin_mail_test(payload: MailTestRequest, request: Request):
    _require_admin(request)
    to = payload.to.strip()
    if "@" not in to or " " in to or len(to) < 6:
        raise HTTPException(400, "Adresse e-mail invalide.")
    ident = _brand_identity()
    title = (ident["short_name"] or ident["guild_name"]).strip()
    try:
        mailer.send_mail(to, f"Test — {title}",
                         f"Ceci est un e-mail de test envoyé depuis {title} "
                         f"({ident['base_url'] or 'le site'}).\n\n"
                         "Si tu reçois ce message, la configuration SMTP fonctionne.",
                         f"<p>Ceci est un e-mail de test envoyé depuis <b>{title}</b>.</p>"
                         "<p>Si tu reçois ce message, la configuration SMTP fonctionne.</p>")
    except mailer.MailError as exc:
        return {"ok": False, "detail": str(exc)}
    return {"ok": True, "detail": f"E-mail de test envoyé à {to}."}


# ---------------------------------------------------------------------------
# Guilde (royaume, région, Warcraft Logs) — réglages de l'administration
# ---------------------------------------------------------------------------
GUILD_KEYS = ("region", "realm", "slug", "locale", "wcl_region", "wcl_name")
GUILD_BNET_REGIONS = ("eu", "us", "kr", "tw")
GUILD_WCL_REGIONS = ("EU", "US", "KR", "TW", "CN")
GUILD_LOCALES = ("en_US", "es_MX", "pt_BR", "en_GB", "es_ES", "fr_FR", "ru_RU",
                 "de_DE", "it_IT", "ko_KR", "zh_TW", "zh_CN")
_GUILD_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")


def _guild_rows() -> dict:
    with _db_lock, _db() as conn:
        return {r["key"]: r["value"] for r in conn.execute("SELECT * FROM guild_config").fetchall()}


def _guild_effective() -> dict:
    """Valeurs en vigueur : administration appliquée, sinon fichier serveur."""
    return {"region": bnet.REGION, "realm": bnet.GUILD_REALM, "slug": bnet.GUILD_SLUG,
            "locale": bnet.LOCALE, "wcl_region": wcl.REGION, "wcl_name": wcl.GUILD_NAME}


def _apply_guild_config() -> None:
    """Recopie l'identité de guilde vers les clients (prioritaire sur l'environnement)."""
    rows = _guild_rows()
    bnet.set_guild_info(region=rows.get("region"), realm=rows.get("realm"),
                        slug=rows.get("slug"), locale=rows.get("locale"))
    wcl.set_guild_info(region=rows.get("wcl_region"), name=rows.get("wcl_name"),
                       realm=rows.get("realm"))


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


@app.get("/api/admin/api-keys")
def admin_api_keys_get(request: Request):
    _require_admin(request)
    rows = _api_keys_rows()
    out = []
    for prov, meta in API_PROVIDERS.items():
        cid, secret, source = _api_effective(prov)
        row = rows.get(prov)
        out.append({
            "provider": prov,
            "label": meta["label"],
            "help_url": meta["help_url"],
            "help_fr": meta["help_fr"],
            "configured": bool(cid and secret),
            "source": source,  # admin | env | ""
            "id_hint": (cid[:8] + "…" + cid[-4:]) if len(cid) > 14 else cid,
            "secret_hint": _mask(secret),
            "updated": (row["updated"] if row else 0),
            "env_available": bool((os.environ.get(f"{'BNET' if prov == 'bnet' else 'WCL'}_CLIENT_ID", "").strip()
                                   and os.environ.get(f"{'BNET' if prov == 'bnet' else 'WCL'}_CLIENT_SECRET", "").strip())),
        })
    return {"providers": out}


@app.post("/api/admin/api-keys")
def admin_api_keys_save(payload: ApiKeyRequest, request: Request):
    _require_admin(request)
    prov = payload.provider.strip().lower()
    if prov not in API_PROVIDERS:
        raise HTTPException(400, "Fournisseur inconnu.")
    mod = bnet if prov == "bnet" else wcl
    if payload.clear:
        with _db_lock, _db() as conn:
            conn.execute("DELETE FROM api_keys WHERE provider=?", (prov,))
        _apply_api_keys()
        return {"ok": True, "cleared": True}
    cid, secret, _src = _api_effective(prov)
    new_id = (payload.client_id or "").strip() or cid
    new_secret = payload.client_secret.strip() or secret
    if not new_id or not new_secret:
        raise HTTPException(400, "Client ID et secret sont requis (le secret existant est conservé si le champ est vide).")
    res = mod.check(new_id, new_secret)
    if not res.get("ok"):
        raise HTTPException(400, f'{API_PROVIDERS[prov]["label"]} — {res.get("detail")}')
    if not (payload.client_id or "").strip() and not payload.client_secret.strip():
        raise HTTPException(400, "Rien à enregistrer (champs vides).")
    with _db_lock, _db() as conn:
        conn.execute("INSERT OR REPLACE INTO api_keys (provider, client_id, client_secret, updated) VALUES (?,?,?,?)",
                     (prov, new_id, new_secret, time.time()))
    _apply_api_keys()
    return {"ok": True}


@app.post("/api/admin/api-keys/test")
def admin_api_keys_test(payload: ApiKeyTestRequest, request: Request):
    _require_admin(request)
    prov = payload.provider.strip().lower()
    if prov not in API_PROVIDERS:
        raise HTTPException(400, "Fournisseur inconnu.")
    cid, secret, source = _api_effective(prov)
    if not cid or not secret:
        return {"ok": False, "detail": "Aucune clé configurée.", "source": source}
    mod = bnet if prov == "bnet" else wcl
    res = mod.check(cid, secret)
    res["source"] = source
    return res


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
        "notify_chars": bool(cfg["notify_chars"]),
        "notify_weekly": bool(cfg["notify_weekly"]),
        "last_recap": cfg["last_recap"],
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
    if payload.notify_chars is not None:
        updates["notify_chars"] = 1 if payload.notify_chars else 0
    if payload.notify_weekly is not None:
        updates["notify_weekly"] = 1 if payload.notify_weekly else 0
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
            "title": "✅ Cohors — test",
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

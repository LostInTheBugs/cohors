"""Cohors — sessions, current user and role guards."""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time

from fastapi import HTTPException, Request, Response

from app.core.config import COOKIE_DOMAIN, COOKIE_SECURE, SESSION_COOKIE, SESSION_DAYS
from app.core.db import _db, _db_lock
from app.security import real_client_ip


def _session_key(token: str) -> str:
    """Return SHA-256 hex digest of *token*."""
    return hashlib.sha256(token.encode()).hexdigest()


def _new_session(conn: sqlite3.Connection, user_id: int) -> str:
    now = time.time()
    token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))
    conn.execute(
        "INSERT INTO sessions (token, user_id, created, last_seen, expires, hashed) VALUES (?,?,?,?,?,1)",
        (_session_key(token), user_id, now, now, now + SESSION_DAYS * 86400),
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
            (_session_key(token), time.time()),
        ).fetchone()
        if row is not None:
            conn.execute("UPDATE sessions SET last_seen=? WHERE token=?", (time.time(), _session_key(token)))
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


def _client_ip(request: Request) -> str:
    # Voir app/security.py:real_client_ip — on lit l'IP à TRUSTED_PROXY_HOPS positions de la fin
    # d'X-Forwarded-For (défaut 1 : notre proxy) ; le reste est fourni par le client et forgeable.
    try:
        hops = max(1, int(os.environ.get("TRUSTED_PROXY_HOPS", "1")))
    except ValueError:
        hops = 1
    return real_client_ip(request.headers.get("x-forwarded-for"),
                          request.client.host if request.client else None, hops=hops)

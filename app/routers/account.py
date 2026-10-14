"""Cohors — account settings: language, display name, voice nickname, password, e-mail, newcomer checklist."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.core.auth import _get_session_user, _require_user, _session_key, _user_lang
from app.core.config import SESSION_COOKIE, STATIC_DIR
from app.core.db import _db, _db_lock
from app.security import hash_password as _hash_password, verify_password as _verify_password
from app.services.accounts import change_email, onboarding, onboarding_set

router = APIRouter()


@router.api_route("/settings", methods=["GET", "HEAD"])
def settings_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "settings.html")


class SettingsRequest(BaseModel):
    lang: str | None = Field(None, max_length=5)
    name: str | None = Field(None, max_length=60)
    voice_nick: str | None = Field(None, max_length=60)


@router.post("/api/me/settings")
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


@router.post("/api/me/password")
def change_my_password(payload: PasswordChangeRequest, request: Request):
    user = _require_user(request)
    if not _verify_password(payload.current, user["pwd"]):
        raise HTTPException(400, "Mot de passe actuel incorrect.")
    token = request.cookies.get(SESSION_COOKIE) or ""
    with _db_lock, _db() as conn:
        conn.execute("UPDATE users SET pwd=? WHERE id=?", (_hash_password(payload.new), user["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id=? AND token != ?", (user["id"], _session_key(token)))
    return {"ok": True}


class EmailChangeRequest(BaseModel):
    email: str = Field(..., max_length=200)
    password: str = Field(..., max_length=200)


@router.post("/api/me/email")
def change_my_email(payload: EmailChangeRequest, request: Request):
    """Change sa propre adresse e-mail (mot de passe demandé) ; reportée partout où elle sert de clé."""
    user = _require_user(request)
    if not _verify_password(payload.password, user["pwd"]):
        raise HTTPException(400, "Mot de passe incorrect.")
    try:
        with _db_lock, _db() as conn:
            _old, new = change_email(conn, user["id"], payload.email)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "email": new}


@router.get("/api/me/onboarding")
def my_onboarding(request: Request):
    """Étapes d'accueil (tableau de bord) : personnages liés + main, wishlist, addon."""
    user = _require_user(request)
    with _db_lock, _db() as conn:
        return onboarding(conn, user)


class OnboardingSet(BaseModel):
    addon: bool | None = None
    hide: bool | None = None


@router.post("/api/me/onboarding")
def set_my_onboarding(payload: OnboardingSet, request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        onboarding_set(conn, user, payload.addon, payload.hide)
        row = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        return onboarding(conn, row)

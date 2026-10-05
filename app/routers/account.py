"""Cohors — account settings: language, display name, voice nickname, password."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.core.auth import _get_session_user, _require_user, _session_key, _user_lang
from app.core.config import SESSION_COOKIE, STATIC_DIR
from app.core.db import _db, _db_lock
from app.security import hash_password as _hash_password, verify_password as _verify_password

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

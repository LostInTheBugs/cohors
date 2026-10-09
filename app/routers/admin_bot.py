"""Cohors — admin: Discord bot settings, server/channel discovery, test message and weekly recap on demand."""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import discord_bot, secretbox
from app.core.auth import _require_admin
from app.core.db import _db, _db_lock
from app.services.bot import _bot_config, _bot_save
from app.services.bot_loop import _weekly_recap_embed

router = APIRouter()


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


@router.get("/api/admin/bot")
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
        "undecryptable": False,
    }
    # undecryptable: lire la valeur brute en base
    with _db_lock, _db() as conn:
        raw_token = conn.execute("SELECT token FROM bot_config WHERE id=1").fetchone()
        if raw_token:
            out["undecryptable"] = secretbox.undecryptable(raw_token["token"])
    if token:
        try:
            who = discord_bot.me(token)
            out["status"] = "ok"
            out["bot_user"] = str(who.get("username") or "?")
        except discord_bot.DiscordError as exc:
            out["status"] = "error"
            out["status_error"] = str(exc)
    return out


@router.post("/api/admin/bot")
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


@router.get("/api/admin/bot/guilds")
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


@router.get("/api/admin/bot/guilds/{guild_id}/channels")
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


@router.post("/api/admin/bot/test")
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


@router.post("/api/admin/bot/recap")
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

"""Cohors — player readiness: item level, Mythic+ rating, missing enchants and empty sockets."""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import discord_bot
from app.core.auth import _require_officer, _require_user, _user_locale, _user_role
from app.core.brand import _brand_identity
from app.core.config import PUBLIC_BASE_URL
from app.core.db import _db, _db_lock
from app.core.util import CLASS_KEY_FR
from app.services.bot import _bot_config
from app.services.readiness import (ENCHANT_SLOTS, READY_MAX_AGE, READY_MIN_LEVEL, READY_TRACK_KEY, _above,
                                    _collect, _readiness, _track_ilvl, char_readiness, readiness_embed)

router = APIRouter()


class ReadyThreshold(BaseModel):
    min_ilvl: int = Field(..., ge=1, le=2000)


class ReadyPost(BaseModel):
    mains_only: bool = False


@router.get("/api/readiness")
def api_readiness(request: Request):
    """Préparation des personnages actifs de niveau max : iLvl, cote M+, enchantements et châsses."""
    user = _require_user(request)
    rows, track_ilvl = _collect(_user_locale(request).startswith("en"))
    todo = sum(1 for x in rows if x["known"] and (x["missing_enchants"] or x["empty_sockets"]))
    return {"rows": rows, "counts": {"total": len(rows), "todo": todo,
                                     "unknown": sum(1 for x in rows if not x["known"])},
            "enchant_slots": list(ENCHANT_SLOTS), "track_ilvl": track_ilvl,
            "can_edit": _user_role(user) in ("officer", "admin")}


@router.get("/api/readiness/char/{realm}/{name}")
def api_readiness_char(realm: str, name: str, request: Request):
    """Préparation d'un personnage, pour sa fiche (enchantements, châsses, set, progression)."""
    _require_user(request)
    r = char_readiness(realm[:60], name[:60], _user_locale(request).startswith("en"))
    if r is None:
        raise HTTPException(404, "Aucun relevé pour ce personnage.")
    return r


@router.get("/api/readiness/mine")
def api_readiness_mine(request: Request):
    """Mes personnages liés (niveau max, relevés de moins de 7 jours) qui ont des enchantements ou des gemmes
    à poser sur leurs pièces Héros / Mythe — pour la carte du tableau de bord."""
    user = _require_user(request)
    want_en = _user_locale(request).startswith("en")
    cutoff = time.time() - READY_MAX_AGE
    with _db_lock, _db() as conn:
        links = conn.execute("SELECT realm, name, display, is_main FROM char_links WHERE user_email=?",
                             (user["email"],)).fetchall()
        track_ilvl = _track_ilvl(conn)
        snaps = {}
        for ln in links:
            r = conn.execute("SELECT ts, data FROM char_snapshots WHERE realm=? AND name=? ORDER BY id DESC LIMIT 1",
                             ((ln["realm"] or "").lower(), (ln["name"] or "").lower())).fetchone()
            if r is not None and r["ts"] >= cutoff:
                snaps[(ln["realm"], ln["name"])] = r
    out = []
    for ln in links:
        sr = snaps.get((ln["realm"], ln["name"]))
        if sr is None:
            continue
        try:
            d = json.loads(sr["data"]) or {}
        except (ValueError, TypeError):
            continue
        if (d.get("level") or 0) < READY_MIN_LEVEL:
            continue
        r = _readiness(d, want_en)
        if not r["known"]:
            continue
        ench = [it["slot"] for it in _above(r["ench_items"], track_ilvl)]
        sock = sum(it["n"] for it in _above(r["sock_items"], track_ilvl))
        if ench or sock:
            out.append({"name": ln["display"] or (ln["name"] or "").title(), "realm": (ln["realm"] or "").lower(),
                        "key": (ln["name"] or "").lower(), "main": bool(ln["is_main"]), "ilvl": d.get("ilvl"),
                        "class_key": CLASS_KEY_FR.get(d.get("class") or ""), "missing_enchants": ench,
                        "empty_sockets": sock})
    out.sort(key=lambda x: (not x["main"], -(len(x["missing_enchants"]) + x["empty_sockets"]), x["name"].lower()))
    return {"chars": out, "track_ilvl": track_ilvl, "linked": len(links)}


@router.post("/api/readiness/post")
def api_readiness_post(body: ReadyPost, request: Request):
    """Officiers : publie sur Discord la liste des personnages à préparer (pièces Héros / Mythe)."""
    _require_officer(request)
    cfg = _bot_config() or {}
    token, channel = (cfg.get("token") or "").strip(), (cfg.get("channel_id") or "").strip()
    if not (cfg.get("enabled") and token and channel):
        raise HTTPException(400, "Bot Discord non configuré ou inactif (Admin → Bot Discord).")
    rows, track_ilvl = _collect(False)
    link = f"{PUBLIC_BASE_URL}/readiness" if PUBLIC_BASE_URL else ""
    emb = readiness_embed(rows, track_ilvl, _brand_identity()["guild_name"], link, body.mains_only,
                          discord_bot.COLOR_GOLD)
    try:
        discord_bot.send(token, channel, embeds=[emb])
    except discord_bot.DiscordError as exc:
        raise HTTPException(400, f"Discord — {exc}")
    return {"ok": True}


@router.post("/api/readiness/threshold")
def api_readiness_threshold(body: ReadyThreshold, request: Request):
    """Officiers : iLvl minimum des pièces prises en compte par le filtre « Héros / Mythe »."""
    _require_officer(request)
    if body.min_ilvl < 100:
        raise HTTPException(400, "iLvl trop bas.")
    with _db_lock, _db() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (READY_TRACK_KEY, str(body.min_ilvl)))
    return {"ok": True, "track_ilvl": body.min_ilvl}

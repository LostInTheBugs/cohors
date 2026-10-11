"""Cohors — player readiness: item level, Mythic+ rating, missing enchants and empty sockets."""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import bnet, discord_bot
from app.core.auth import _require_officer, _require_user, _user_locale, _user_role
from app.core.brand import _brand_identity
from app.core.config import PUBLIC_BASE_URL
from app.core.db import _db, _db_lock
from app.core.util import CLASS_KEY_FR, _pick
from app.services.bot import _bot_config

router = APIRouter()

READY_MAX_AGE = 7 * 86400.0   # relevés de moins de 7 jours (joueurs actifs)
READY_MIN_LEVEL = 90          # niveau maximum (Midnight)
# Emplacements enchantables en Midnight (plus de cape ni de brassards) ; la main gauche seulement si
# c'est une arme (bouclier et objet tenu en main gauche ne s'enchantent pas).
ENCHANT_SLOTS = ("HEAD", "SHOULDER", "CHEST", "LEGS", "FEET", "FINGER_1", "FINGER_2", "MAIN_HAND")
# Filtre « pièces Héros / Mythe » : iLvl minimum d'une pièce pour qu'un enchantement ou une gemme manquant
# compte. L'API Blizzard ne donne pas la piste d'amélioration (« Héros 4/6 ») : seulement l'iLvl.
# Par défaut Héros 1/6 de Midnight saison 2 (305) ; les officiers l'ajustent à chaque saison.
READY_TRACK_ILVL_DEFAULT = 305
READY_TRACK_KEY = "readiness_min_ilvl"


class ReadyThreshold(BaseModel):
    min_ilvl: int = Field(..., ge=1, le=2000)


class ReadyPost(BaseModel):
    mains_only: bool = False


def _track_ilvl(conn) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (READY_TRACK_KEY,)).fetchone()
    try:
        return int(row["value"]) if row else READY_TRACK_ILVL_DEFAULT
    except (TypeError, ValueError):
        return READY_TRACK_ILVL_DEFAULT


def _readiness(d: dict, want_en: bool) -> dict:
    """Enchantements manquants et châsses vides d'un relevé ; known=False si le relevé est trop ancien
    pour contenir ces informations."""
    items = d.get("items") or []
    known = any(it.get("ench") is not None for it in items)
    missing, empty = [], 0
    ench_items, sock_items = [], []      # détail par pièce, avec son iLvl (filtre Héros / Mythe côté page)
    if known:
        for it in items:
            st = it.get("st") or ""
            label = ((it.get("slot_en") or it.get("slot")) if want_en else it.get("slot")) or st
            il = it.get("ilvl")
            if (st in ENCHANT_SLOTS or (st == "OFF_HAND" and it.get("wpn"))) and it.get("ench") is False:
                missing.append(label)
                ench_items.append({"slot": label, "ilvl": il})
            try:
                n = max(0, int(it.get("sock") or 0) - int(it.get("gems") or 0))
            except (TypeError, ValueError):
                n = 0
            if n:
                empty += n
                sock_items.append({"slot": label, "ilvl": il, "n": n})
    return {"known": known, "missing_enchants": missing, "empty_sockets": empty,
            "ench_items": ench_items, "sock_items": sock_items}


def _collect(want_en: bool) -> tuple[list[dict], int]:
    """Personnages actifs de niveau max (relevés < 7 jours) avec leur préparation, + seuil Héros / Mythe."""
    cutoff = time.time() - READY_MAX_AGE
    with _db_lock, _db() as conn:
        snaps = conn.execute(
            "SELECT realm, name, ts, data FROM char_snapshots "
            "WHERE id IN (SELECT MAX(id) FROM char_snapshots GROUP BY realm, name) AND ts >= ?",
            (cutoff,)).fetchall()
        mains = {(r["name"] or "").strip().lower() for r in conn.execute(
            "SELECT name FROM char_links WHERE is_main=1").fetchall()}
        track_ilvl = _track_ilvl(conn)
    disp: dict[str, str] = {}
    try:
        roster, _t = bnet.roster()
        disp = {(m.get("name") or "").lower(): m.get("name") or "" for m in (roster.get("members") or [])}
    except bnet.BnetError:
        pass
    rows = []
    for sr in snaps:
        try:
            d = json.loads(sr["data"]) or {}
        except (ValueError, TypeError):
            continue
        if (d.get("level") or 0) < READY_MIN_LEVEL or d.get("ilvl") is None:
            continue
        r = _readiness(d, want_en)
        rows.append({
            "name": disp.get(sr["name"]) or sr["name"].title(), "key": sr["name"], "realm": sr["realm"],
            "class_key": CLASS_KEY_FR.get(d.get("class") or ""), "spec": _pick(d, "spec", want_en) or "",
            "ilvl": d.get("ilvl"), "mplus": d.get("mplus"), "main": sr["name"] in mains, "ts": sr["ts"], **r,
        })
    rows.sort(key=lambda x: (-(len(x["missing_enchants"]) + x["empty_sockets"]), not x["known"],
                             -(x["ilvl"] or 0), x["name"].lower()))
    return rows, track_ilvl


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


def _above(items: list[dict], min_ilvl: int) -> list[dict]:
    """Pièces retenues par le filtre Héros / Mythe (iLvl inconnu : gardée)."""
    return [it for it in items if it.get("ilvl") is None or it["ilvl"] >= min_ilvl]


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


READY_POST_MAX = 25   # personnages listés dans le message Discord (les plus en retard d'abord)


def readiness_embed(rows: list[dict], track_ilvl: int, guild: str = "la guilde", link: str = "",
                    mains_only: bool = False, color: int = 0xDFA55A) -> dict:
    """Embed Discord : personnages à préparer sur leurs pièces Héros / Mythe (enchantements, gemmes)."""
    todo = []
    for r in rows:
        if not r.get("known") or (mains_only and not r.get("main")):
            continue
        ench = [it["slot"] for it in _above(r.get("ench_items") or [], track_ilvl)]
        sock = sum(it["n"] for it in _above(r.get("sock_items") or [], track_ilvl))
        if ench or sock:
            todo.append((r, ench, sock))
    todo.sort(key=lambda x: (-(len(x[1]) + x[2]), -(x[0].get("ilvl") or 0), x[0]["name"].lower()))
    lines = []
    for r, ench, sock in todo[:READY_POST_MAX]:
        parts = []
        if ench:
            parts.append("🪄 " + ", ".join(ench))
        if sock:
            parts.append(f"💎 {sock} châsse{'s' if sock > 1 else ''} vide{'s' if sock > 1 else ''}")
        lines.append(f"**{r['name']}**{' ⭐' if r.get('main') else ''} ({r.get('ilvl') or '?'}) — " + " · ".join(parts))
    if len(todo) > READY_POST_MAX:
        lines.append(f"_… et {len(todo) - READY_POST_MAX} autre(s) : voir le bilan._")
    fields, cur = [], ""
    for ln in lines:                       # champs Discord : 1 024 caractères au plus
        if cur and len(cur) + 1 + len(ln) > 1024:
            fields.append(cur)
            cur = ""
        cur = (cur + "\n" + ln) if cur else ln[:1024]
    if cur:
        fields.append(cur)
    scope = "mains" if mains_only else "personnages"
    desc = (f"{len(todo)} {scope} à préparer — enchantements et gemmes manquants sur les pièces Héros / Mythe "
            f"(iLvl ≥ {track_ilvl})." if todo else f"Tous les {scope} sont prêts sur leurs pièces Héros / Mythe ✅")
    if link:
        desc += f"\n👉 [Bilan des joueurs]({link})"
    return {"title": "🩺 Préparation du raid", "description": desc, "color": color,
            "fields": [{"name": "À corriger" if i == 0 else "\u200b", "value": v} for i, v in enumerate(fields[:5])],
            "footer": {"text": f"{guild} · bilan des joueurs"}}


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

"""Cohors — player readiness: item level, Mythic+ rating, missing enchants and empty sockets."""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import bnet
from app.core.auth import _require_officer, _require_user, _user_locale, _user_role
from app.core.db import _db, _db_lock
from app.core.util import CLASS_KEY_FR, _pick

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


@router.get("/api/readiness")
def api_readiness(request: Request):
    """Préparation des personnages actifs de niveau max : iLvl, cote M+, enchantements et châsses."""
    user = _require_user(request)
    want_en = _user_locale(request).startswith("en")
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


@router.post("/api/readiness/threshold")
def api_readiness_threshold(body: ReadyThreshold, request: Request):
    """Officiers : iLvl minimum des pièces prises en compte par le filtre « Héros / Mythe »."""
    _require_officer(request)
    if body.min_ilvl < 100:
        raise HTTPException(400, "iLvl trop bas.")
    with _db_lock, _db() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (READY_TRACK_KEY, str(body.min_ilvl)))
    return {"ok": True, "track_ilvl": body.min_ilvl}

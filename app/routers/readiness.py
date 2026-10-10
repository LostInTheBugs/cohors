"""Cohors — player readiness: item level, Mythic+ rating, missing enchants and empty sockets."""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Request

from app import bnet
from app.core.auth import _require_user, _user_locale
from app.core.db import _db, _db_lock
from app.core.util import CLASS_KEY_FR, _pick

router = APIRouter()

READY_MAX_AGE = 7 * 86400.0   # relevés de moins de 7 jours (joueurs actifs)
READY_MIN_LEVEL = 90          # niveau maximum (Midnight)
# Emplacements enchantables en Midnight (plus de cape ni de brassards) ; la main gauche seulement si
# c'est une arme (bouclier et objet tenu en main gauche ne s'enchantent pas).
ENCHANT_SLOTS = ("HEAD", "SHOULDER", "CHEST", "LEGS", "FEET", "FINGER_1", "FINGER_2", "MAIN_HAND")


def _readiness(d: dict, want_en: bool) -> dict:
    """Enchantements manquants et châsses vides d'un relevé ; known=False si le relevé est trop ancien
    pour contenir ces informations."""
    items = d.get("items") or []
    known = any(it.get("ench") is not None for it in items)
    missing, empty = [], 0
    if known:
        for it in items:
            st = it.get("st") or ""
            if (st in ENCHANT_SLOTS or (st == "OFF_HAND" and it.get("wpn"))) and it.get("ench") is False:
                missing.append(((it.get("slot_en") or it.get("slot")) if want_en else it.get("slot")) or st)
            try:
                empty += max(0, int(it.get("sock") or 0) - int(it.get("gems") or 0))
            except (TypeError, ValueError):
                pass
    return {"known": known, "missing_enchants": missing, "empty_sockets": empty}


@router.get("/api/readiness")
def api_readiness(request: Request):
    """Préparation des personnages actifs de niveau max : iLvl, cote M+, enchantements et châsses."""
    _require_user(request)
    want_en = _user_locale(request).startswith("en")
    cutoff = time.time() - READY_MAX_AGE
    with _db_lock, _db() as conn:
        snaps = conn.execute(
            "SELECT realm, name, ts, data FROM char_snapshots "
            "WHERE id IN (SELECT MAX(id) FROM char_snapshots GROUP BY realm, name) AND ts >= ?",
            (cutoff,)).fetchall()
        mains = {(r["name"] or "").strip().lower() for r in conn.execute(
            "SELECT name FROM char_links WHERE is_main=1").fetchall()}
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
            "enchant_slots": list(ENCHANT_SLOTS)}

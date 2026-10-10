"""Cohors — item card: where an item drops, who wants it, who already has it, who can craft it."""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException, Request

from app import bnet
from app.core.auth import _require_user, _user_locale
from app.core.db import _db, _db_lock
from app.core.util import CLASS_KEY_FR
from app.services.crafting import _craft_results
from app.services.wishlist import _bis_by_user

router = APIRouter()

HOLDERS_MAX_AGE = 30 * 86400.0  # relevés plus anciens ignorés (personnages partis de la guilde)


@router.get("/api/item/{iid}")
def api_item(iid: int, request: Request):
    """Fiche objet : source (butin), wishlists, personnages qui l'ont équipé, artisans."""
    user = _require_user(request)
    if iid <= 0 or iid > 10_000_000:
        raise HTTPException(400, "Identifiant d'objet invalide.")
    loc = _user_locale(request)
    want_en = loc.startswith("en")
    cutoff = time.time() - HOLDERS_MAX_AGE
    with _db_lock, _db() as conn:
        loot = [dict(r) for r in conn.execute(
            "SELECT kind, inst_fr, inst_en, boss_fr, boss_en, name_fr, name_en FROM item_loot WHERE item_id=?",
            (iid,)).fetchall()]
        wishes = [dict(r) for r in conn.execute(
            "SELECT user_email, name, kind, prio FROM wishlist WHERE item_id=?", (iid,)).fetchall()]
        bis_all = _bis_by_user(conn) if wishes else {}
        mains = {r["user_email"]: dict(r) for r in conn.execute(
            "SELECT user_email, realm, name, display FROM char_links WHERE is_main=1").fetchall()}
        unames = {r["email"]: (r["name"] or "") for r in conn.execute("SELECT email, name FROM users").fetchall()}
        owner_of = {(r["name"] or "").strip().lower(): r["user_email"] for r in conn.execute(
            "SELECT name, user_email FROM char_links").fetchall()}
        snaps = conn.execute(
            "SELECT realm, name, ts, data FROM char_snapshots "
            "WHERE id IN (SELECT MAX(id) FROM char_snapshots GROUP BY realm, name) AND ts >= ?",
            (cutoff,)).fetchall()
        cls_of: dict[str, str] = {}
        holders = []
        local_names: list[tuple[str, str]] = [(r["name_fr"], r["name_en"]) for r in loot]
        for sr in snaps:
            try:
                d = json.loads(sr["data"]) or {}
            except (ValueError, TypeError):
                continue
            cls_of[sr["name"]] = d.get("class") or ""
            for it in d.get("items") or []:
                try:
                    if int(it.get("id") or 0) != iid:
                        continue
                except (TypeError, ValueError):
                    continue
                local_names.append((it.get("name") or "", it.get("name_en") or ""))
                holders.append({
                    "name": sr["name"], "realm": sr["realm"],
                    "class_key": CLASS_KEY_FR.get(d.get("class") or ""),
                    "ilvl": it.get("ilvl"), "char_ilvl": d.get("ilvl"),
                    "slot": ((it.get("slot_en") or it.get("slot")) if want_en else it.get("slot")) or "",
                })
                break
    # nom affiché des personnages : casse du roster quand on la connaît
    disp: dict[str, str] = {}
    try:
        roster, _t = bnet.roster()
        disp = {(m.get("name") or "").lower(): m.get("name") or "" for m in (roster.get("members") or [])}
    except bnet.BnetError:
        pass
    for h in holders:
        h["key"] = h["name"]
        h["name"] = disp.get(h["name"]) or h["name"].title()
    holders.sort(key=lambda h: (-(h.get("ilvl") or 0), h["name"].lower()))
    # wishlists : affichées sous le nom du main (comme le calendrier)
    wishers = []
    for w in wishes:
        email = w["user_email"]
        mn = mains.get(email)
        bis = (bis_all.get(email) or {}).get(iid)
        k = (mn["name"] if mn else "").strip().lower()
        wishers.append({
            "name": (mn["display"] or mn["name"]) if mn else (unames.get(email) or email.split("@")[0]),
            "realm": (mn["realm"] if mn else ""), "key": k,
            "class_key": CLASS_KEY_FR.get(cls_of.get(k) or "") if k else None,
            "kind": w["kind"] or "item",
            "prio": bool(w["prio"]) or bool(bis),
            "bis": ({"char": bis.get("char"), "gain": bis.get("gain")} if bis else None),
        })
        local_names.append((w["name"] or "", ""))
    wishers.sort(key=lambda w: (not w["prio"], w["name"].lower()))
    sources = []
    seen = set()
    for r in loot:
        inst = (r["inst_en"] or r["inst_fr"]) if want_en else r["inst_fr"]
        boss = (r["boss_en"] or r["boss_fr"]) if want_en else r["boss_fr"]
        if (inst, boss) in seen:
            continue
        seen.add((inst, boss))
        sources.append({"kind": r["kind"] or "", "instance": inst or "", "boss": boss or ""})
    crafters = _craft_results(lambda fr, en, i: i == iid, want_en)
    # identité de l'objet : API Blizzard (localisée, icône), sinon noms connus en base
    meta = {"id": iid, "name": "", "quality": "", "icon": None, "type": ""}
    try:
        b = bnet.item(iid, locale=loc)
        meta.update({"name": b.get("name") or "", "quality": b.get("quality") or "", "icon": b.get("icon"),
                     "type": " · ".join(x for x in (b.get("inv_type_fr") or "", b.get("subclass") or "") if x)})
    except bnet.BnetError:
        pass
    if not meta["name"] or meta["name"] == f"Objet {iid}":
        for fr, en in local_names:
            nm = (en or fr) if want_en else (fr or en)
            if nm:
                meta["name"] = nm
                break
        if not meta["name"] and crafters:
            meta["name"] = crafters[0]["item"]
    if not meta["name"]:
        meta["name"] = f"Objet {iid}"
    craft_list = crafters[0]["crafters"] if crafters else []
    for c in craft_list:   # commande possible : l'artisan a un compte Cohors, et ce n'est pas le mien
        owner = owner_of.get(c["key"])
        c["orderable"] = bool(owner) and owner != user["email"]
    return {"item": meta, "sources": sources, "wishers": wishers, "holders": holders,
            "crafters": craft_list,
            "craft": ({"profession": crafters[0]["profession"], "expansion": crafters[0]["expansion"]}
                      if crafters else None)}

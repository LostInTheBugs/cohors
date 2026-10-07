"""Cohors — wishlist: items and recipes to get, priorities, where they drop / who crafts them, add-on export."""
from __future__ import annotations

import json
import threading
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app import bnet
from app.core.auth import _require_user, _user_locale
from app.core.db import _db, _db_lock
from app.core.util import ITEM_REF_RE
from app.services.loot import _loot_sync, _loot_sync_state
from app.services.wishlist import _bis_by_user, _recipe_wish_key

router = APIRouter()


class WishlistAdd(BaseModel):
    item: str = Field(..., min_length=4, max_length=300)
    prio: bool = False


class PrioSet(BaseModel):
    on: bool = False


class RecipeWish(BaseModel):
    recipe_id: int
    on: bool = True


@router.post("/api/wishlist/recipe")
def api_wishlist_recipe(payload: RecipeWish, request: Request):
    """Ajoute (ou retire) une recette de métier à la wishlist — c'est ce qui alimente l'alerte
    « en instance » de l'addon, avec les pièces d'équipement."""
    user = _require_user(request)
    loc = _user_locale(request)
    en = loc.startswith("en")
    with _db_lock, _db() as conn:
        gr = conn.execute(
            "SELECT id, item, item_en, item_id, prof, prof_en FROM game_recipes WHERE id=?",
            (payload.recipe_id,)).fetchone()
        if gr is None:
            raise HTTPException(404, "Recette inconnue")
        name = ((gr["item_en"] or gr["item"]) if en else gr["item"]) or ""
        key = _recipe_wish_key(gr["item_id"], gr["item"] or name)
        if not payload.on:
            conn.execute("DELETE FROM wishlist WHERE user_email=? AND item_id=?",
                         (user["email"], key))
            return {"ok": True, "on": False, "key": key}
        exists = conn.execute("SELECT 1 AS x FROM wishlist WHERE user_email=? AND item_id=?",
                              (user["email"], key)).fetchone()
        if exists:
            return {"ok": True, "on": True, "already": True, "key": key, "name": name}
        conn.execute(
            "INSERT INTO wishlist (user_email, item_id, name, slot, inv_type, quality, icon, added, prio, kind) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (user["email"], key, name, "Recette", 0, 1, None, time.time(), 0, "recipe"))
    return {"ok": True, "on": True, "key": key, "name": name}


@router.get("/api/wishlist/export")
def api_wishlist_export(request: Request):
    """Export texte de la wishlist pour l'addon : une ligne par objet « clé|type|nom ».
    Les pièces sont reconnues en jeu par leur identifiant, les recettes par leur nom."""
    user = _require_user(request)
    with _db_lock, _db() as conn:
        rows = conn.execute(
            "SELECT item_id, name, kind FROM wishlist WHERE user_email=? "
            "ORDER BY kind, name COLLATE NOCASE", (user["email"],)).fetchall()

    def clean(s) -> str:
        return " ".join(str(s or "").replace("|", " ").split())[:120]

    lines = ["CohorsWL1"]
    for r in rows:
        lines.append("%d|%s|%s" % (int(r["item_id"] or 0), r["kind"] or "item", clean(r["name"])))
    body = "\n".join(lines) + "\n"
    return Response(body, media_type="text/plain; charset=utf-8",
                    headers={"Cache-Control": "no-store",
                             "Content-Disposition": 'attachment; filename="cohors-wishlist.txt"'})


@router.get("/api/wishlist")
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
            "SELECT item_id, name, slot, quality, icon, added, prio, kind FROM wishlist "
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
            is_recipe = (r["kind"] or "item") == "recipe"
            items.append({
                "item_id": r["item_id"], "name": nm,
                "slot": r["slot"], "slot_fr": bnet.slot_label(r["slot"], loc),
                "quality": r["quality"], "icon": r["icon"], "added": r["added"],
                "source": src, "craft": None if is_recipe else craft,
                "me": None if is_recipe else me,
                "prio": bool(r["prio"]), "bis": bis, "kind": r["kind"] or "item",
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


@router.post("/api/wishlist")
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


@router.delete("/api/wishlist/{item_id}")
def api_wishlist_del(item_id: int, request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        conn.execute("DELETE FROM wishlist WHERE user_email=? AND item_id=?", (user["email"], item_id))
    return {"ok": True}


@router.post("/api/wishlist/{item_id}/prio")
def api_wishlist_prio(item_id: int, payload: PrioSet, request: Request):
    """Marque (ou non) une pièce de la wishlist comme prioritaire (⭐)."""
    user = _require_user(request)
    with _db_lock, _db() as conn:
        conn.execute("UPDATE wishlist SET prio=? WHERE user_email=? AND item_id=?",
                     (1 if payload.on else 0, user["email"], item_id))
    return {"ok": True, "on": bool(payload.on)}

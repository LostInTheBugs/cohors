"""Cohors — "Moi" space: overview, Mythic+ key alerts, notifications, my recipes, my unavailability periods."""
from __future__ import annotations

import json
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import bnet
from app.core.auth import _owns_char, _require_user, _user_locale, _user_role
from app.core.db import _db, _db_lock
from app.core.util import _valid_char
from app.routers.attendance import api_attendance
from app.services.crafting import PROF_EN, _prof_store
from app.services.mplus import _dungeon_key
from app.services.wishlist import _recipe_wish_key

router = APIRouter()


class AlertRequest(BaseModel):
    dungeon: str = Field("", max_length=80)
    min_level: int = 2


@router.get("/api/me/alerts")
def api_my_alerts(request: Request):
    """Mes alertes MM+ (clés recherchées)."""
    user = _require_user(request)
    with _db_lock, _db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, dungeon, min_level, created FROM mplus_alerts WHERE email=? ORDER BY created DESC",
            (user["email"],)).fetchall()]
    return {"alerts": rows}


@router.post("/api/me/alerts")
def api_my_alerts_add(body: AlertRequest, request: Request):
    user = _require_user(request)
    dun = _dungeon_key(body.dungeon)
    lv = max(2, min(40, int(body.min_level or 2)))
    with _db_lock, _db() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM mplus_alerts WHERE email=?",
                         (user["email"],)).fetchone()["n"]
        if n >= 20:
            raise HTTPException(400, "Trop d'alertes (20 maximum).")
        if conn.execute("SELECT 1 AS x FROM mplus_alerts WHERE email=? AND dungeon=? AND min_level=?",
                        (user["email"], dun, lv)).fetchone() is None:
            conn.execute("INSERT INTO mplus_alerts (email, dungeon, min_level, created) VALUES (?,?,?,?)",
                         (user["email"], dun, lv, time.time()))
    return {"ok": True}


@router.delete("/api/me/alerts/{aid}")
def api_my_alerts_del(aid: int, request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        conn.execute("DELETE FROM mplus_alerts WHERE id=? AND email=?", (aid, user["email"]))
    return {"ok": True}


@router.get("/api/me/notifs")
def api_my_notifs(request: Request):
    """Mes notifications (20 dernières) + compteur non lues."""
    user = _require_user(request)
    with _db_lock, _db() as conn:
        unread = conn.execute("SELECT COUNT(*) AS n FROM notifs WHERE email=? AND seen=0",
                              (user["email"],)).fetchone()["n"]
        items = [dict(r) for r in conn.execute(
            "SELECT id, kind, data, seen, created FROM notifs WHERE email=? ORDER BY created DESC LIMIT 20",
            (user["email"],)).fetchall()]
    for it in items:
        try:
            it["data"] = json.loads(it["data"] or "{}")
        except ValueError:
            it["data"] = {}
    return {"unread": unread, "items": items}


@router.post("/api/me/notifs/read")
def api_my_notifs_read(request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        conn.execute("UPDATE notifs SET seen=1 WHERE email=? AND seen=0", (user["email"],))
    return {"ok": True}


class MyRecipesSave(BaseModel):
    realm: str = Field(..., min_length=2, max_length=60)
    name: str = Field(..., min_length=2, max_length=60)
    prof: str = Field("", max_length=60)
    items: list[int] = []


@router.get("/api/my/recipes")
def api_my_recipes(request: Request, realm: str = "", name: str = "", prof: str = ""):
    """Recettes connues d'un de MES personnages + catalogue du jeu du métier choisi (self-service)."""
    user = _require_user(request)
    realm_l = realm.strip().lower()
    name_s = name.strip()[:60]
    _valid_char(realm_l, name_s)
    if _user_role(user) not in ("officer", "admin") and not _owns_char(user, name_s):
        raise HTTPException(403, "Ce personnage n'est pas lié à ton compte.")
    want_en = _user_locale(request).startswith("en")
    with _db_lock, _db() as conn:
        prows = conn.execute(
            "SELECT data FROM char_professions WHERE realm=? AND name=?",
            (realm_l, name_s.lower())).fetchone()
        known_rows = conn.execute(
            "SELECT item FROM craft_recipes WHERE lower(crafter)=lower(?)", (name_s,)).fetchall()
        game_profs = [dict(r) for r in conn.execute(
            "SELECT DISTINCT prof, prof_en FROM game_recipes ORDER BY prof").fetchall()]
        catalog = []
        if prof.strip():
            catalog = [dict(r) for r in conn.execute(
                "SELECT id, item, item_en, item_id, exp_rank, rank_no, mats, mats_en, tier, tier_en, prof, prof_en "
                "FROM game_recipes WHERE prof=? ORDER BY item COLLATE NOCASE, rank_no",
                (prof.strip()[:60],)).fetchall()]
            wl_keys = {int(r["item_id"]) for r in conn.execute(
                "SELECT item_id FROM wishlist WHERE user_email=? AND kind='recipe'",
                (user["email"],)).fetchall()}
    profs = []
    if prows:
        try:
            pd = json.loads(prows["data"]) or {}
        except (ValueError, TypeError):
            pd = {}
        for p in (pd.get("profs") or []):
            key = p.get("name_fr") or p.get("name") or ""
            if key:
                profs.append({"key": key, "label": ((p.get("name_en") or key) if want_en else key),
                              "points": p.get("points"), "max": p.get("max")})
    known = {str(r["item"]).casefold() for r in known_rows}
    api_known: set[int] = set()
    if prows:
        for p in ((json.loads(prows["data"] or "{}") or {}).get("profs") or []):
            api_known |= {int(x) for x in (p.get("known") or [])}
    cat = []
    seen_items: set = set()
    for c in catalog:
        item_key = str(c.get("item")).casefold()
        if item_key in seen_items:   # un seul exemplaire par objet (rang mini conservé)
            continue
        seen_items.add(item_key)
        try:
            mats = json.loads(((c.get("mats_en") or c.get("mats")) if want_en else c.get("mats")) or "[]")
        except ValueError:
            mats = []
        wkey = _recipe_wish_key(c["item_id"], c["item"])
        cat.append({
            "id": c["id"],            # id de recette (clé de sélection, unique)
            "item_id": c["item_id"],
            "wkey": wkey,             # clé wishlist (☆/⭐)
            "wished": wkey in wl_keys,
            "name": ((c.get("item_en") or c.get("item")) if want_en else c.get("item")) or "",
            "exp": ((c.get("tier_en") or c.get("tier")) if want_en else c.get("tier")) or "",
            "exp_rank": c.get("exp_rank") or 0,
            "mats": mats,
            "known": item_key in known or int(c["id"]) in api_known,
            "known_src": "addon" if item_key in known else ("blizzard" if int(c["id"]) in api_known else ""),
        })
    return {"char": {"realm": realm_l, "name": name_s}, "professions": profs,
            "game_profs": [{"key": r["prof"],
                            "label": ((r.get("prof_en") or r["prof"]) if want_en else r["prof"])}
                           for r in game_profs],
            "prof": prof.strip(), "catalog": cat,
            "profs_ts": _prof_ts(realm_l, name_s)}


def _prof_ts(realm: str, name: str) -> float:
    """Date du dernier relevé des métiers d'un personnage (0 = jamais)."""
    with _db_lock, _db() as conn:
        row = conn.execute("SELECT ts FROM char_professions WHERE realm=? AND name=?",
                           (realm.strip().lower(), name.strip().lower())).fetchone()
    return float(row["ts"] or 0) if row else 0.0


class MyRecipesRefresh(BaseModel):
    realm: str = Field(..., min_length=1, max_length=60)
    name: str = Field(..., min_length=1, max_length=60)


@router.post("/api/my/recipes/refresh")
def api_my_recipes_refresh(body: MyRecipesRefresh, request: Request):
    """Relit tout de suite chez Blizzard les métiers (et recettes connues) d'un de MES personnages."""
    user = _require_user(request)
    realm_l, name_s = body.realm.strip().lower(), body.name.strip()[:60]
    _valid_char(realm_l, name_s)
    if _user_role(user) not in ("officer", "admin") and not _owns_char(user, name_s):
        raise HTTPException(403, "Ce personnage n'est pas lié à ton compte.")
    try:
        _prof_store(realm_l, name_s, force=True)
    except bnet.BnetError as exc:
        raise HTTPException(502, f"Blizzard : {exc}") from exc
    return {"ok": True, "ts": _prof_ts(realm_l, name_s)}


@router.post("/api/my/recipes")
def api_my_recipes_save(body: MyRecipesSave, request: Request):
    """Enregistre (remplace) les recettes connues d'un de MES personnages pour un métier."""
    user = _require_user(request)
    realm_l = body.realm.strip().lower()
    name_s = body.name.strip()[:60]
    _valid_char(realm_l, name_s)
    if _user_role(user) not in ("officer", "admin") and not _owns_char(user, name_s):
        raise HTTPException(403, "Ce personnage n'est pas lié à ton compte.")
    prof = body.prof.strip()[:60]
    if not prof:
        raise HTTPException(400, "Choisis d'abord un métier.")
    ids: list[int] = []
    for i in (body.items or [])[:2000]:
        try:
            iid = int(i)
        except (TypeError, ValueError):
            continue
        if iid not in ids:
            ids.append(iid)
    with _db_lock, _db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, item, item_id, tier, exp_rank, mats FROM game_recipes WHERE prof=?", (prof,)).fetchall()]
    by_key = {int(r["id"]): r for r in rows}
    picked = [by_key[i] for i in ids if i in by_key]
    if ids and not picked:
        raise HTTPException(400, "Ces recettes ne correspondent pas au métier choisi (ou n'existent pas).")
    now = time.time()
    prof_alt = PROF_EN.get(prof, prof)
    with _db_lock, _db() as conn:
        conn.execute("DELETE FROM craft_recipes WHERE lower(crafter)=lower(?) AND profession IN (?,?)",
                     (name_s, prof, prof_alt))
        if picked:
            conn.executemany(
                "INSERT OR REPLACE INTO craft_recipes (crafter, realm, profession, item, item_id, "
                "expansion, exp_rank, mats, updated) VALUES (?,?,?,?,?,?,?,?,?)",
                [(name_s, realm_l, prof, r["item"], r["item_id"], r.get("tier") or "",
                  r.get("exp_rank") or 0, r.get("mats") or "[]", now) for r in picked])
    return {"ok": True, "saved": len(picked), "prof": prof}


class UnavailRequest(BaseModel):
    day_from: str = Field(..., min_length=10, max_length=10)
    day_to: str = Field("", max_length=10)
    note: str = Field("", max_length=120)


@router.get("/api/me/unavail")
def api_my_unavail(request: Request):
    """Mes périodes d'indisponibilité (surtout pour les raids)."""
    user = _require_user(request)
    with _db_lock, _db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, day_from, day_to, note FROM unavails WHERE email=? ORDER BY day_from",
            (user["email"],)).fetchall()]
    return {"items": rows}


@router.post("/api/me/unavail")
def api_my_unavail_add(body: UnavailRequest, request: Request):
    user = _require_user(request)
    try:
        d1 = datetime.strptime(body.day_from.strip(), "%Y-%m-%d").date()
        d2 = datetime.strptime((body.day_to.strip() or body.day_from.strip()), "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Dates invalides (format attendu : AAAA-MM-JJ).")
    if d2 < d1:
        d1, d2 = d2, d1
    if (d2 - d1).days > 180:
        raise HTTPException(400, "Période trop longue (180 jours maximum).")
    note = body.note.strip()[:120]
    with _db_lock, _db() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM unavails WHERE email=?",
                         (user["email"],)).fetchone()["n"]
        if n >= 20:
            raise HTTPException(400, "Trop de périodes (20 maximum).")
        dup = conn.execute(
            "SELECT 1 AS x FROM unavails WHERE email=? AND day_from=? AND day_to=? AND note=?",
            (user["email"], d1.isoformat(), d2.isoformat(), note)).fetchone()
        if dup is None:
            conn.execute("INSERT INTO unavails (email, day_from, day_to, note, created) VALUES (?,?,?,?,?)",
                         (user["email"], d1.isoformat(), d2.isoformat(), note, time.time()))
    return {"ok": True}


@router.delete("/api/me/unavail/{uid}")
def api_my_unavail_del(uid: int, request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        conn.execute("DELETE FROM unavails WHERE id=? AND email=?", (uid, user["email"]))
    return {"ok": True}


@router.get("/api/me/overview")
def api_me_overview(request: Request):
    """Page 🙋 Moi : mes personnages + stats rapides (ilvl, présence, recettes) et mes clés."""
    user = _require_user(request)
    locale = _user_locale(request)
    with _db_lock, _db() as conn:
        links = [dict(r) for r in conn.execute(
            "SELECT id, realm, name, display, is_main FROM char_links WHERE user_email=? "
            "ORDER BY is_main DESC, display COLLATE NOCASE", (user["email"],)).fetchall()]
        craft = {(r["c"] or ""): r["n"] for r in conn.execute(
            "SELECT lower(crafter) AS c, COUNT(*) AS n FROM craft_recipes GROUP BY lower(crafter)").fetchall()}
        post = conn.execute("SELECT keys FROM mplus_posts WHERE user=?", (user["email"],)).fetchone()
    try:
        my_keys = json.loads(post["keys"]) if post else []
    except (ValueError, TypeError):
        my_keys = []
    try:
        att = api_attendance(request, days=30, refresh=0)
        att_rows = {(r.get("name") or "").lower(): r for r in (att.get("rows") or [])}
    except Exception:
        att_rows = {}
    chars = []
    for c in links:
        try:
            sm, _ts = bnet.character(c["realm"], c["name"], 0, locale=locale)
        except Exception:
            sm = {}
        a = att_rows.get((c["name"] or "").lower()) or {}
        chars.append({
            "id": c["id"], "realm": c["realm"], "name": c["name"], "display": c["display"],
            "is_main": bool(c["is_main"]),
            "level": sm.get("level"), "class": sm.get("class"), "class_key": sm.get("class_key"),
            "spec": sm.get("spec"), "ilvl_equipped": sm.get("ilvl_equipped"), "ilvl_avg": sm.get("ilvl_avg"),
            "achievements": sm.get("achievement_points"), "last_login": sm.get("last_login"),
            "att_pct": a.get("pct"), "att_nights": a.get("nights"),
            "recipes": craft.get((c["name"] or "").lower(), 0),
        })
    keys_out = [{"char": k.get("char") or "", "dungeon": k.get("dungeon") or "", "level": k.get("level") or 2}
                for k in (my_keys if isinstance(my_keys, list) else [])][:12]
    return {"chars": chars, "keys": keys_out}

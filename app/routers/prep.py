"""Cohors — raid prep workshop: plan, recipes and materials, member contributions, guild bank,
add-on recipe import and on-demand game-recipe sync."""
from __future__ import annotations

import json
import re
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import bnet
from app.core.auth import _owns_char, _require_officer, _require_user, _user_locale, _user_role
from app.core.db import _db, _db_lock
from app.core.util import _int_any, _lua_unescape, _snap_day
from app.services.crafting import _known_craft_rows
from app.services.game_recipes import _game_sync, _game_sync_report, _game_sync_state

router = APIRouter()


class PrepPlanRequest(BaseModel):
    title: str = Field("", max_length=120)
    event_ts: float = 0
    items: list[dict] = []
    raids: list[str] = []
    bosses: list[str] = []


class PrepRecipeRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    mats: list[dict] = []


class PrepClaimRequest(BaseModel):
    mat: str = Field(..., min_length=1, max_length=120)
    qty: float = 0


def _prep_needs(plan_items: list, recipes: list) -> tuple[list[dict], list[str]]:
    """Agrège les compos nécessaires (objets du plan × quantités × recettes)."""
    rec = {}
    for r in recipes:
        rec[str(r.get("name") or "").casefold()] = r.get("mats") or []
    needs: dict = {}
    unknown: list = []
    for it in plan_items:
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        try:
            qty = max(0.0, min(9999.0, float(it.get("qty") or 0)))
        except (TypeError, ValueError):
            qty = 0.0
        mats = rec.get(name.casefold())
        if not mats:   # recette absente OU compos inconnues (données Blizzard incomplètes)
            if name not in unknown:
                unknown.append(name)
            continue
        for m in mats:
            mn = str((m or {}).get("name") or "").strip()
            if not mn:
                continue
            try:
                mq = max(0.0, min(999999.0, float((m or {}).get("qty") or 0)))
            except (TypeError, ValueError):
                mq = 0.0
            needs[mn] = needs.get(mn, 0.0) + mq * qty
    out = [{"mat": k, "need": round(v, 2)} for k, v in needs.items()]
    out.sort(key=lambda n: n["mat"].casefold())
    return out, unknown


@router.get("/api/prep")
def api_prep_get(request: Request):
    """Plan de préparation + recettes + besoins agrégés + apports des membres."""
    user = _require_user(request)
    with _db_lock, _db() as conn:
        plan = conn.execute(
            "SELECT title, event_ts, items, raids, bosses, updated, updated_by FROM prep_plan WHERE id=1").fetchone()
        recipes = [dict(r) for r in conn.execute(
            "SELECT id, name, mats, updated FROM prep_recipes ORDER BY name COLLATE NOCASE").fetchall()]
        claims = [dict(r) for r in conn.execute(
            "SELECT mat, qty, user, name FROM prep_claims").fetchall()]
        bank_rows = [dict(r) for r in conn.execute("SELECT mat, qty FROM prep_bank").fetchall()]
        # artisans : recettes connues d'après Blizzard, puis exports de l'add-on (prioritaires)
        crafts = _known_craft_rows(conn) + [dict(r) for r in conn.execute(
            "SELECT crafter, profession, item, item_id, expansion, exp_rank, mats FROM craft_recipes").fetchall()]
        game = [dict(r) for r in conn.execute(
            "SELECT prof, tier, exp_rank, item, item_id, rank_no, mats, "
            "item_en, tier_en, prof_en, mats_en FROM game_recipes").fetchall()]
        gts_row = conn.execute("SELECT MAX(updated) AS ts FROM game_recipes").fetchone()
    for r in recipes:
        try:
            r["mats"] = json.loads(r["mats"] or "[]")
        except ValueError:
            r["mats"] = []
    p = dict(plan) if plan else {"title": "", "event_ts": 0, "items": "[]", "raids": "[]",
                                 "bosses": "[]", "updated": 0, "updated_by": ""}
    for _k in ("items", "raids", "bosses"):
        try:
            p[_k] = json.loads(p.get(_k) or "[]")
        except (ValueError, TypeError):
            p[_k] = []
    # Résolution des compos par objet : recettes maison > jeu > artisans, en ignorant les
    # entrées vides (l'API du jeu n'a pas les compos de nombreuses recettes récentes).
    cand: dict = {}
    for c in crafts:
        try:
            _m = json.loads(c["mats"] or "[]")
        except ValueError:
            _m = []
        nm = str(c.get("item") or "").strip()
        if nm:
            e = cand.setdefault(nm.casefold(), {"name": nm, "maison": [], "jeu": [], "artisans": []})
            e["artisans"] = _m or e["artisans"]   # une ligne sans compos n'écrase pas une ligne qui en a
    game_best: dict = {}
    for c in game:
        try:
            g_rank = int(c.get("rank_no") or 1)
        except (TypeError, ValueError):
            g_rank = 1
        for it_nm, mats_raw in ((c.get("item"), c.get("mats")), (c.get("item_en"), c.get("mats_en"))):
            nm = str(it_nm or "").strip()
            if not nm:
                continue
            cur = game_best.get(nm.casefold())
            if cur is None or g_rank < cur[0]:
                try:
                    _gm = json.loads(mats_raw or "[]")
                except (TypeError, ValueError):
                    _gm = []
                game_best[nm.casefold()] = (g_rank, nm, _gm)
    for _k, (_rk, _nm, _gm) in game_best.items():
        e = cand.setdefault(_k, {"name": _nm, "maison": [], "jeu": [], "artisans": []})
        e["jeu"] = _gm
        e["name"] = e.get("name") or _nm
    for r in recipes:
        nm = str(r.get("name") or "").strip()
        if nm:
            e = cand.setdefault(nm.casefold(), {"name": nm, "maison": [], "jeu": [], "artisans": []})
            e["maison"] = r.get("mats") or []
    res_list = [{"name": v["name"], "mats": (v["maison"] or v["jeu"] or v["artisans"] or [])}
                for v in cand.values()]
    needs, unknown = _prep_needs(p["items"], res_list)
    by_mat: dict = {}
    for c in claims:
        by_mat.setdefault(str(c["mat"]), []).append(
            {"qty": c["qty"], "name": c["name"] or c["user"], "mine": c["user"] == user["email"]})
    known = {n["mat"] for n in needs}
    for n in needs:
        cs = by_mat.get(n["mat"], [])
        cs.sort(key=lambda x: x["name"].casefold())
        n["claims"] = cs
        n["claimed"] = round(sum(x["qty"] for x in cs), 2)
    for mat, cs in by_mat.items():
        if mat not in known:
            needs.append({"mat": mat, "need": 0, "claims": cs,
                          "claimed": round(sum(x["qty"] for x in cs), 2)})
    bank_map = {str(r["mat"]): round(float(r["qty"] or 0), 2) for r in bank_rows}
    for n in needs:
        n["bank"] = bank_map.get(str(n["mat"]), 0.0)
        n["rest"] = round(max(0.0, float(n.get("need") or 0) - float(n.get("claimed") or 0) - n["bank"]), 2)
    can = _user_role(user) in ("officer", "admin")
    catalog: dict = {}
    for c in crafts:
        key = str(c["item"]).casefold()
        ent = catalog.get(key)
        if ent is None:
            try:
                cmats = json.loads(c["mats"] or "[]")
            except ValueError:
                cmats = []
            ent = {"item": c["item"], "item_id": c["item_id"] or 0, "prof": c["profession"],
                   "exp": c["expansion"] or "", "exp_rank": c["exp_rank"] or 0,
                   "mats": cmats, "crafters": []}
            catalog[key] = ent
        else:
            # même objet dans plusieurs paliers : garder le plus récent (rang mini)
            if (c["exp_rank"] or 0) < (ent["exp_rank"] or 0):
                try:
                    ent["mats"] = json.loads(c["mats"] or "[]")
                except ValueError:
                    pass
                ent["exp"] = c["expansion"] or ""
                ent["exp_rank"] = c["exp_rank"] or 0
        if c["crafter"].casefold() not in {x.casefold() for x in ent["crafters"]}:
            ent["crafters"].append(c["crafter"])
    cat_list = sorted(catalog.values(), key=lambda e: str(e["item"]).casefold())
    exps: dict = {}
    for c in crafts:
        nm = str(c["expansion"] or "").strip()
        rk = c["exp_rank"] or 0
        if nm and (nm not in exps or rk < exps[nm]):
            exps[nm] = rk
    exps_list = [{"name": n, "rank": r} for n, r in sorted(exps.items(), key=lambda kv: kv[1])]
    # Recettes du jeu : une entrée par objet et par métier (on garde le rang le plus bas),
    # enrichies de « qui peut la fabriquer » depuis les exports des artisans.
    known_by_item: dict = {}
    for ent_g in catalog.values():
        known_by_item.setdefault(str(ent_g["item"]).casefold(), []).extend(ent_g["crafters"])
    want_en = _user_locale(request).startswith("en")
    game_cat: dict = {}
    for c in game:
        item_nm = (c.get("item_en") or c.get("item")) if want_en else c.get("item")
        prof_nm = (c.get("prof_en") or c.get("prof")) if want_en else c.get("prof")
        exp_nm = (c.get("tier_en") or c.get("tier")) if want_en else c.get("tier")
        key = (str(item_nm).casefold(), prof_nm)
        try:
            gmats = json.loads(((c.get("mats_en") or c.get("mats")) if want_en else c.get("mats")) or "[]")
        except ValueError:
            gmats = []
        ent = game_cat.get(key)
        if ent is None or int(c["rank_no"] or 1) < int(ent["rank"] or 1):
            game_cat[key] = {"item": item_nm, "item_id": c["item_id"] or 0, "prof": prof_nm,
                             "exp": exp_nm or "", "exp_rank": c["exp_rank"] or 0,
                             "rank": c["rank_no"] or 1, "mats": gmats, "item_fr": c["item"]}
    for ent in game_cat.values():
        # les artisans sont connus par le nom FR (exports addon) — l'appariement reste FR
        ent["crafters"] = known_by_item.get(str(ent.get("item_fr") or ent["item"]).casefold(), [])
    game_list = sorted(game_cat.values(), key=lambda e: (str(e["prof"]), str(e["item"]).casefold()))
    # 🚫 Indisponibilités : croisées avec l'objectif (événement du calendrier in-game).
    unavail_block: dict = {"event": None, "mode": "upcoming", "rows": [],
                           "counts": {"members": 0, "conflict": 0}}
    with _db_lock, _db() as conn:
        unavails = [dict(r) for r in conn.execute(
            "SELECT email, day_from, day_to, note FROM unavails ORDER BY day_from").fetchall()]
        links = {r["name"]: r["user_email"] for r in conn.execute(
            "SELECT user_email, name FROM char_links").fetchall()}
        unames = {(r["name"] or "").lower(): r["email"] for r in conn.execute(
            "SELECT email, name FROM users WHERE active=1").fetchall()}
        uname_by_email = {r["email"]: (r["name"] or r["email"]) for r in conn.execute(
            "SELECT email, name FROM users").fetchall()}
        grow = conn.execute("SELECT data FROM gcal_import WHERE id=1").fetchone()
    by_email: dict = {}
    for urow in unavails:
        by_email.setdefault(urow["email"], []).append(urow)
    if p.get("event_ts"):
        try:
            events = (json.loads(grow["data"]) or {}).get("events") if grow else []
        except (ValueError, TypeError):
            events = []
        ev = None
        for e in (events or []):
            try:
                if abs(float(e.get("ts") or 0) - float(p["event_ts"])) < 60:
                    ev = e
                    break
            except (TypeError, ValueError):
                continue
        if ev is None and events:
            # objectif saisi à la main : on rapproche par jour (Paris)
            want = _snap_day(float(p["event_ts"]))
            for e in events:
                try:
                    if _snap_day(float(e.get("ts") or 0)) == want:
                        ev = e
                        break
                except (TypeError, ValueError):
                    continue
        if ev:
            day = _snap_day(float(ev.get("ts") or p["event_ts"]))
            status_map = {1: "ok", 3: "ok", 2: "no", 8: "maybe"}
            rows = []
            for m in (ev.get("inv") or []):
                nm = str(m.get("n") or "").strip()
                if not nm:
                    continue
                owner = links.get(nm.lower()) or unames.get(nm.lower())
                periods = [x for x in by_email.get(owner, [])
                           if x["day_from"] <= day <= x["day_to"]] if owner else []
                if not periods:
                    continue
                st = status_map.get(m.get("s"), "wait")
                rows.append({"name": nm, "user": uname_by_email.get(owner, "") if owner else "",
                             "status": st,
                             "periods": [{"from": x["day_from"], "to": x["day_to"], "note": x["note"] or ""}
                                         for x in periods],
                             "conflict": st == "ok"})
            rows.sort(key=lambda r: (not r["conflict"], r["name"].casefold()))
            unavail_block = {"event": {"title": ev.get("title") or "", "date": ev.get("date") or "",
                                       "ts": float(ev.get("ts") or 0)},
                             "mode": "event", "rows": rows,
                             "counts": {"members": len(rows),
                                        "conflict": sum(1 for r in rows if r["conflict"])}}
    if unavail_block["mode"] == "upcoming" and unavails:
        today = _snap_day()
        limit = _snap_day(time.time() + 14 * 86400)
        rows = []
        for email, periods in by_email.items():
            ps = [x for x in periods if x["day_to"] >= today and x["day_from"] <= limit]
            if ps:
                rows.append({"name": uname_by_email.get(email, email), "user": "", "status": "",
                             "periods": [{"from": x["day_from"], "to": x["day_to"], "note": x["note"] or ""}
                                         for x in ps],
                             "conflict": False})
        rows.sort(key=lambda r: r["periods"][0]["from"])
        unavail_block["rows"] = rows
        unavail_block["counts"] = {"members": len(rows), "conflict": 0}
    try:
        _jr, _jts = bnet.journal_raids(_user_locale(request))
        raid_catalog = _jr
    except bnet.BnetError:
        raid_catalog = {"expansion": "", "raids": []}
    return {"plan": p, "recipes": recipes, "needs": needs, "unknown": unknown,
            "unavail": unavail_block, "raid_catalog": raid_catalog, "bank": bank_map,
            "catalog": cat_list, "exps": exps_list, "crafters": sorted({c["crafter"] for c in crafts}),
            "game": game_list, "game_sync": _game_sync_report(float(gts_row["ts"] or 0)),
            "me": {"name": user["name"] if "name" in user.keys() else user["email"]},
            "can_edit": can}


@router.post("/api/prep/plan")
def api_prep_plan(body: PrepPlanRequest, request: Request):
    user = _require_officer(request)
    items = []
    for it in (body.items or [])[:60]:
        name = str((it or {}).get("name") or "").strip()[:120]
        if not name:
            continue
        try:
            qty = max(0.0, min(9999.0, float((it or {}).get("qty") or 0)))
        except (TypeError, ValueError):
            qty = 0.0
        items.append({"name": name, "qty": qty})
    raids = []
    for r in (body.raids or [])[:8]:
        nm = str(r or "").strip()[:80]
        if nm and nm not in raids:
            raids.append(nm)
    bosses = []
    for x in (body.bosses or [])[:40]:
        nm = str(x or "").strip()[:80]
        if nm and nm not in bosses:
            bosses.append(nm)
    with _db_lock, _db() as conn:
        conn.execute(
            "INSERT INTO prep_plan (id, title, event_ts, items, raids, bosses, updated, updated_by) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET title=excluded.title, event_ts=excluded.event_ts, items=excluded.items, "
            "raids=excluded.raids, bosses=excluded.bosses, updated=excluded.updated, updated_by=excluded.updated_by",
            (body.title.strip()[:120], float(body.event_ts or 0), json.dumps(items, ensure_ascii=False),
             json.dumps(raids, ensure_ascii=False), json.dumps(bosses, ensure_ascii=False),
             time.time(), user["name"] if "name" in user.keys() else user["email"]),
        )
    return {"ok": True, "items": len(items), "raids": len(raids), "bosses": len(bosses)}


@router.post("/api/prep/recipes")
def api_prep_recipe_save(body: PrepRecipeRequest, request: Request):
    _require_officer(request)
    mats = []
    for m in (body.mats or [])[:40]:
        mn = str((m or {}).get("name") or "").strip()[:120]
        if not mn:
            continue
        try:
            mq = max(0.0, min(999999.0, float((m or {}).get("qty") or 0)))
        except (TypeError, ValueError):
            mq = 0.0
        mats.append({"name": mn, "qty": mq})
    name = body.name.strip()[:120]
    now = time.time()
    with _db_lock, _db() as conn:
        row = conn.execute("SELECT id FROM prep_recipes WHERE name=? COLLATE NOCASE", (name,)).fetchone()
        if row:
            conn.execute("UPDATE prep_recipes SET mats=?, updated=? WHERE id=?",
                         (json.dumps(mats, ensure_ascii=False), now, row["id"]))
            rid = row["id"]
        else:
            cur = conn.execute("INSERT INTO prep_recipes (name, mats, created, updated) VALUES (?,?,?,?)",
                               (name, json.dumps(mats, ensure_ascii=False), now, now))
            rid = cur.lastrowid
    return {"ok": True, "id": rid, "mats": len(mats)}


@router.delete("/api/prep/recipes/{rid}")
def api_prep_recipe_del(rid: int, request: Request):
    _require_officer(request)
    with _db_lock, _db() as conn:
        conn.execute("DELETE FROM prep_recipes WHERE id=?", (rid,))
    return {"ok": True}


@router.post("/api/prep/claim")
def api_prep_claim(body: PrepClaimRequest, request: Request):
    user = _require_user(request)
    mat = body.mat.strip()[:120]
    try:
        qty = max(0.0, min(999999.0, float(body.qty or 0)))
    except (TypeError, ValueError):
        qty = 0.0
    with _db_lock, _db() as conn:
        if qty <= 0:
            conn.execute("DELETE FROM prep_claims WHERE mat=? AND user=?", (mat, user["email"]))
        else:
            conn.execute(
                "INSERT INTO prep_claims (mat, qty, user, name, updated) VALUES (?,?,?,?,?) "
                "ON CONFLICT(mat, user) DO UPDATE SET qty=excluded.qty, name=excluded.name, updated=excluded.updated",
                (mat, qty, user["email"], user["name"] if "name" in user.keys() else "", time.time()),
            )
    return {"ok": True}


class PrepBankRequest(BaseModel):
    mat: str = Field(..., min_length=1, max_length=120)
    qty: float = 0


@router.post("/api/prep/bank")
def api_prep_bank(body: PrepBankRequest, request: Request):
    """Officiers : quantité d'une compos déjà en banque de guilde (0 = retirer la ligne)."""
    user = _require_officer(request)
    mat = body.mat.strip()[:120]
    if not mat:
        raise HTTPException(400, "Compos manquante.")
    qty = max(0.0, min(999999.0, float(body.qty or 0)))
    with _db_lock, _db() as conn:
        if qty <= 0:
            conn.execute("DELETE FROM prep_bank WHERE mat=?", (mat,))
        else:
            conn.execute(
                "INSERT INTO prep_bank (mat, qty, updated, updated_by) VALUES (?,?,?,?) "
                "ON CONFLICT(mat) DO UPDATE SET qty=excluded.qty, updated=excluded.updated, "
                "updated_by=excluded.updated_by",
                (mat, qty, time.time(), user["name"] if "name" in user.keys() else user["email"]),
            )
    return {"ok": True, "mat": mat, "qty": qty}


@router.post("/api/prep/reset")
def api_prep_reset(request: Request):
    user = _require_officer(request)
    with _db_lock, _db() as conn:
        conn.execute("DELETE FROM prep_claims")
        conn.execute("UPDATE prep_plan SET items='[]', raids='[]', bosses='[]', updated=?, updated_by=? WHERE id=1",
                     (time.time(), user["name"] if "name" in user.keys() else user["email"]))
    return {"ok": True}


class PrepSyncGameRequest(BaseModel):
    profs: list[str] = []


class PrepRecipesImportRequest(BaseModel):
    payload: str = Field(..., max_length=2_000_000)


def _prep_parse_recipes(text: str) -> dict:
    """Extrait un export de recettes d'artisan : JSON brut ou fichier SavedVariables (clé "recipes")."""
    t = (text or "").strip()
    if not t:
        raise HTTPException(400, "Contenu vide.")
    raw = None
    st = re.search(r'\["recipes"\]\s*=\s*"((?:[^"\\]|\\.)*)"', t, re.S)
    if st:
        raw = _lua_unescape(st.group(1))
    elif t.startswith("{") and '"professions"' in t:
        raw = t
    else:
        j = t.find('{"v":')
        k = t.rfind("}")
        if j >= 0 and k > j and '"professions"' in t[j:k + 1]:
            raw = t[j:k + 1]
    if raw is None:
        snippet = " ".join(t[:90].split())
        raise HTTPException(400, "Format non reconnu (reçu : %d caractères — « %s… »). En jeu : /cohors recettes, "
                                 "puis /reload, puis choisis le fichier WTF/Account/<compte>/SavedVariables/Cohors.lua."
                                 % (len(t), snippet))
    raw = re.sub(r"(\s*:\s*)0x([0-9A-Fa-f]+)", r'\1"0x\2"', raw)
    try:
        data = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Données illisibles (JSON invalide).")
    if not isinstance(data, dict) or not isinstance(data.get("professions"), list):
        raise HTTPException(400, "Données inattendues (aucun métier dans cet export).")
    return data


@router.post("/api/prep/import-recipes")
def api_prep_import_recipes(body: PrepRecipesImportRequest, request: Request):
    """Import d'un export d'addon (/cohors recettes) : officiers, ou chacun pour ses propres personnages."""
    user = _require_user(request)
    data = _prep_parse_recipes(body.payload)
    crafter = str(data.get("player") or "").strip()[:60] or "?"
    if _user_role(user) not in ("officer", "admin") and not _owns_char(user, crafter):
        raise HTTPException(403, "Tu ne peux importer que les recettes de tes propres personnages "
                                 "(lie-les sur la page Personnages, ou demande à un officier).")
    realm = str(data.get("realm") or "").strip()[:60]
    # Un export d'addon plus ancien contient une entrée « profession » PAR PALIER (jusqu'à une
    # vingtaine d'entrées, recettes dupliquées d'un palier à l'autre) : on les fusionne en gardant
    # la DERNIÈRE occurrence de chaque recette (ses matériaux sont les plus complets) — l'ancienne
    # coupe à 10 entrées perdait des métiers entiers (Cuisine, le 20/09).
    def _row(rec, pname):
        item = str((rec or {}).get("n") or "").strip()[:120]
        if not item:
            return None
        mats = []
        for m in ((rec or {}).get("m") or [])[:30]:
            if isinstance(m, list) and len(m) >= 3:
                try:
                    q = float(m[2] or 0)
                except (TypeError, ValueError):
                    q = 0.0
                mats.append({"id": _int_any(m[0]), "name": str(m[1] or "")[:120], "qty": q})
        exp = str((rec or {}).get("e") or "").strip()[:60]
        try:
            trank = max(0, min(99, int((rec or {}).get("t") or 0)))
        except (TypeError, ValueError):
            trank = 0
        return (crafter, realm, pname, item, _int_any((rec or {}).get("i")), exp, trank,
                json.dumps(mats, ensure_ascii=False))
    merged: dict[tuple, tuple] = {}
    for prof in (data.get("professions") or [])[:60]:
        pname = str((prof or {}).get("name") or "").strip()[:60]
        for rec in ((prof or {}).get("recipes") or [])[:1500]:
            row = _row(rec, pname)
            if row:
                merged[(pname, row[3])] = row
    rows = list(merged.values())
    if not rows:
        raise HTTPException(400, "Aucune recette exploitable dans cet export.")
    now = time.time()
    with _db_lock, _db() as conn:
        conn.execute("DELETE FROM craft_recipes WHERE crafter=?", (crafter,))
        conn.executemany(
            "INSERT OR REPLACE INTO craft_recipes (crafter, realm, profession, item, item_id, expansion, exp_rank, mats, updated) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [(c, r, p, i, iid, e, tk, mm, now) for (c, r, p, i, iid, e, tk, mm) in rows],
        )
    return {"ok": True, "crafter": crafter, "recipes": len(rows),
            "professions": len(data.get("professions") or [])}


@router.post("/api/prep/sync-game")
def api_prep_sync_game(body: PrepSyncGameRequest, request: Request):
    """(Officiers) Lance la synchro des recettes du jeu en tâche de fond."""
    _require_officer(request)
    if _game_sync_state["state"] == "running":
        return {"ok": True, "running": True}
    threading.Thread(target=_game_sync, args=(body.profs or [],), daemon=True,
                     name="game-recipes-manual").start()
    return {"ok": True, "started": True}

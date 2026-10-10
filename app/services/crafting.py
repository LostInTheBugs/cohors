"""Cohors — crafting knowledge: recipes known by characters according to the Blizzard API."""
from __future__ import annotations

import json

from app import bnet
from app.core.db import _db, _db_lock
from app.core.util import CLASS_KEY_FR, _snap_day


def _known_craft_rows(conn, crafter: str | None = None) -> list[dict]:
    """Recettes connues d'après l'API Blizzard (métiers des personnages), au format de craft_recipes.

    Complète les exports de l'add-on : mêmes champs (crafter, profession, item, item_id, expansion,
    exp_rank, mats), un artisan = le nom du personnage (casse d'affichage).
    """
    sql, args = "SELECT name, data FROM char_professions", ()
    if crafter:
        sql, args = sql + " WHERE name=?", ((crafter or "").strip().lower(),)
    want: dict[int, list[str]] = {}
    for pr in conn.execute(sql, args).fetchall():
        try:
            profs = (json.loads(pr["data"]) or {}).get("profs") or []
        except (ValueError, TypeError):
            continue
        who = (pr["name"] or "").strip().title()
        for p in profs:
            for rid in p.get("known") or []:
                want.setdefault(int(rid), []).append(who)
    if not want:
        return []
    rows = []
    ids = list(want)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for g in conn.execute(
                f"SELECT id, prof, tier, exp_rank, item, item_id, mats FROM game_recipes "
                f"WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall():
            for who in sorted(set(want[int(g["id"])])):
                rows.append({"crafter": who, "profession": g["prof"], "item": g["item"],
                             "item_id": g["item_id"] or 0, "expansion": g["tier"] or "",
                             "exp_rank": g["exp_rank"] or 0, "mats": g["mats"] or "[]"})
    return rows


PROF_EN = {v: k for k, v in bnet.PROF_FR.items()}  # libellé FR → nom anglais (API)


def _prof_store(realm: str, name: str, force: bool = False) -> None:
    """Enregistre (ou remplace) les métiers d'un personnage."""
    data, ts = bnet.professions(realm, name, force=force, locale="fr_FR")
    with _db_lock, _db() as conn:
        conn.execute(
            "INSERT INTO char_professions (realm, name, ts, data) VALUES (?,?,?,?) "
            "ON CONFLICT(realm, name) DO UPDATE SET ts=excluded.ts, data=excluded.data",
            (realm.lower(), name.lower(), ts, json.dumps(data, ensure_ascii=False)),
        )


def _craft_results(match, want_en: bool) -> list[dict]:
    """Objets fabricables retenus par `match(nom_fr, nom_en, item_id)` et qui sait les fabriquer.

    Sources : recettes du jeu connues d'après l'API Blizzard (char_professions × game_recipes) et
    recettes déclarées (craft_recipes : add-on, Mes recettes). Une entrée par objet ; les objets que
    personne ne sait fabriquer viennent en dernier.
    """
    with _db_lock, _db() as conn:
        game = [dict(r) for r in conn.execute(
            "SELECT id, prof, prof_en, tier, tier_en, exp_rank, item, item_en, item_id, rank_no "
            "FROM game_recipes").fetchall()]
        addon = [dict(r) for r in conn.execute(
            "SELECT crafter, realm, profession, item, item_id, expansion, exp_rank FROM craft_recipes").fetchall()]
        profs = [dict(r) for r in conn.execute("SELECT realm, name, data FROM char_professions").fetchall()]
        cls_by_name: dict[str, str] = {}
        for row in conn.execute("SELECT name, data FROM char_snapshots WHERE day = ?", (_snap_day(),)).fetchall():
            try:
                c = (json.loads(row["data"]) or {}).get("class")
            except (ValueError, TypeError):
                c = None
            if c:
                cls_by_name[row["name"]] = c
    # 1) recettes du jeu dont l'objet correspond (nom FR ou EN), regroupées par objet
    groups: dict = {}
    by_item_id: dict[int, tuple] = {}
    by_name: dict[str, tuple] = {}
    gkey_of_recipe: dict[int, tuple] = {}
    for g in game:
        fr, en = str(g.get("item") or "").strip(), str(g.get("item_en") or "").strip()
        iid = int(g.get("item_id") or 0)
        if not fr or not match(fr, en, iid):
            continue
        key = ("id", iid) if iid else ("n", fr.casefold())
        e = groups.get(key)
        if e is None:
            e = groups[key] = {"item": fr, "item_en": en, "item_id": iid,
                               "prof": g.get("prof") or "", "prof_en": g.get("prof_en") or "",
                               "tier": g.get("tier") or "", "tier_en": g.get("tier_en") or "",
                               "exp_rank": int(g.get("exp_rank") or 0), "who": {}}
        gkey_of_recipe[int(g["id"])] = key
        if iid:
            by_item_id[iid] = key
        for nm in (fr, en):
            if nm:
                by_name[nm.casefold()] = key
    # 2) personnages qui connaissent ces recettes d'après l'API Blizzard
    realm_of: dict[str, str] = {}
    if gkey_of_recipe:
        for pr in profs:
            try:
                plist = (json.loads(pr["data"]) or {}).get("profs") or []
            except (ValueError, TypeError):
                continue
            k = (pr["name"] or "").strip().lower()
            for p in plist:
                for rid in p.get("known") or []:
                    try:
                        key = gkey_of_recipe.get(int(rid))
                    except (TypeError, ValueError):
                        key = None
                    if key:
                        groups[key]["who"].setdefault(k, set()).add("blizzard")
                        realm_of.setdefault(k, pr["realm"] or "")
    # 3) recettes déclarées (add-on / Mes recettes) : rattachées à l'objet du jeu, sinon objet à part
    for a in addon:
        nm = str(a.get("item") or "").strip()
        iid = int(a.get("item_id") or 0)
        if not nm or not match(nm, "", iid):
            continue
        key = by_item_id.get(iid) if iid else None
        key = key or by_name.get(nm.casefold())
        if key is None:
            key = ("id", iid) if iid else ("n", nm.casefold())
            if key not in groups:
                groups[key] = {"item": nm, "item_en": "", "item_id": iid,
                               "prof": a.get("profession") or "", "prof_en": "",
                               "tier": a.get("expansion") or "", "tier_en": "",
                               "exp_rank": int(a.get("exp_rank") or 0), "who": {}}
        k = str(a.get("crafter") or "").strip().lower()
        if k:
            groups[key]["who"].setdefault(k, set()).add("addon")
            realm_of.setdefault(k, a.get("realm") or "")
    disp: dict[str, str] = {}
    realms: dict[str, str] = {}
    if any(e["who"] for e in groups.values()):
        try:
            roster, _t = bnet.roster()
            for m in (roster.get("members") or []):
                k = (m.get("name") or "").lower()
                if k:
                    disp[k] = m.get("name") or k
                    realms[k] = m.get("realm") or bnet.GUILD_REALM
        except bnet.BnetError:
            pass
    results = []
    for e in groups.values():
        crafters = [{"name": disp.get(k) or k.title(), "key": k,
                     "realm": realms.get(k) or realm_of.get(k) or bnet.GUILD_REALM,
                     "class_key": CLASS_KEY_FR.get(cls_by_name.get(k) or ""),
                     "src": sorted(src)} for k, src in e["who"].items()]
        crafters.sort(key=lambda c: c["name"].lower())
        prof = e["prof"]
        if want_en:
            prof = e["prof_en"] or PROF_EN.get(prof, prof)
        results.append({
            "item": ((e["item_en"] or e["item"]) if want_en else e["item"]),
            "item_id": e["item_id"], "profession": prof,
            "expansion": ((e["tier_en"] or e["tier"]) if want_en else e["tier"]),
            "exp_rank": e["exp_rank"], "crafters": crafters,
        })
    results.sort(key=lambda r: (not r["crafters"], r["exp_rank"], r["item"].casefold()))
    return results

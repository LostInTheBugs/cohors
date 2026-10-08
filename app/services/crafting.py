"""Cohors — crafting knowledge: recipes known by characters according to the Blizzard API."""
from __future__ import annotations

import json

from app import bnet
from app.core.db import _db, _db_lock


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

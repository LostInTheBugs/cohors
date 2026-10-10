"""Cohors — game recipes (Blizzard Game Data API): sync of every profession that has recipes, every 6 h or on demand."""
from __future__ import annotations

import json
import time

from app import bnet
from app.core.db import _db, _db_lock
from app.services.wishlist import _recipe_wish_key

# ---- Recettes du jeu (API Game Data Blizzard) --------------------------------
GAME_PREP_PROFS = ((185, "Cuisine"), (171, "Alchimie"), (773, "Calligraphie"),
                   (164, "Forge"), (165, "Travail du cuir"), (202, "Ingénierie"),
                   (197, "Couture"), (755, "Joaillerie"),
                   # v2026.10.021 : tous les métiers qui ont des recettes (recherche « Qui sait fabriquer… »)
                   (333, "Enchantement"), (186, "Minéralogie"), (182, "Herboristerie"), (393, "Dépeçage"))
GAME_SYNC_TTL = 86400.0  # resynchro auto au plus une fois par jour (boucle 6 h + au démarrage)
_game_sync_state = {"state": "idle", "prof": "", "done": 0, "total": 0, "error": "", "ts": 0.0}


def _game_sync_report(db_ts: float = 0.0) -> dict:
    """État de synchro exposé à l'UI (le ts est repris de la base après redémarrage)."""
    gs = dict(_game_sync_state)
    if not gs.get("ts"):
        gs["ts"] = db_ts
    return gs


def _game_sync(profs=None) -> None:
    """Synchronise les recettes des paliers « 2 dernières extensions » des métiers utiles."""
    st = _game_sync_state
    if st["state"] == "running":
        return
    st.update({"state": "running", "prof": "", "done": 0, "total": 0, "error": ""})
    wanted = {str(p).casefold() for p in (profs or [])}
    cibles = [p for p in GAME_PREP_PROFS if not wanted or p[1].casefold() in wanted]
    total_written = 0
    failed: list[str] = []
    try:
        for pid, nom in cibles:
            try:
                prof = bnet.game_profession(pid, locale="fr_FR")
                try:
                    prof_en = bnet.game_profession(pid, locale="en_US")
                except bnet.BnetError:
                    prof_en = {}
                tiers = (prof.get("skill_tiers") or [])[-2:]  # les 2 paliers les plus récents
                tiers_en = {t.get("id"): (t.get("name") or "") for t in (prof_en.get("skill_tiers") or [])}
                st["prof"] = nom
                rows_all = []
                for rank, tier in enumerate(reversed(tiers)):  # rang 0 = la plus récente extension
                    recs = bnet.game_tier_recipes(pid, tier["id"], locale="fr_FR")
                    st["total"] = (st["total"] or 0) + len(recs)
                    for r in recs:
                        try:
                            d = bnet.game_recipe(r["id"], locale="fr_FR")
                        except bnet.BnetError:
                            continue  # recette non exposée — ignorée
                        try:
                            d_en = bnet.game_recipe(r["id"], locale="en_US")
                        except bnet.BnetError:
                            d_en = {}
                        ci = d.get("crafted_item") or {}
                        ci_en = d_en.get("crafted_item") or {}
                        mats, mats_en = [], []
                        for m in d.get("reagents") or []:
                            rr = m.get("reagent") or {}
                            mats.append({"id": rr.get("id") or 0, "name": rr.get("name") or "",
                                         "qty": float(m.get("quantity") or 0)})
                        for m in d_en.get("reagents") or []:
                            rr = m.get("reagent") or {}
                            mats_en.append({"id": rr.get("id") or 0, "name": rr.get("name") or "",
                                            "qty": float(m.get("quantity") or 0)})
                        try:
                            rrank = int(d.get("rank") or 1)
                        except (TypeError, ValueError):
                            rrank = 1
                        item_id, inv_type, subclass_en, ilvl = int(ci.get("id") or 0), "", "", 0
                        if not item_id:
                            # l'API ne donne plus l'objet fabriqué : on le retrouve par son nom anglais exact
                            try:
                                found = bnet.search_item_exact(ci_en.get("name") or d_en.get("name") or "")
                            except bnet.BnetError:
                                found = None
                            if found:
                                item_id, inv_type = found["id"], found["inv_type"]
                                subclass_en, ilvl = found["subclass_en"], found["ilvl"]
                        rows_all.append((int(d.get("id") or r["id"]), nom, tier.get("name") or "", rank,
                                         ci.get("name") or d.get("name") or "", item_id, rrank,
                                         json.dumps(mats, ensure_ascii=False), time.time(),
                                         ci_en.get("name") or d_en.get("name") or "",
                                         tiers_en.get(tier["id"]) or "",
                                         prof_en.get("name") or "",
                                         json.dumps(mats_en, ensure_ascii=False),
                                         inv_type, subclass_en, ilvl))
                        st["done"] += 1
                        if st["done"] % 4 == 0:
                            time.sleep(0.02)  # politesse (limite Blizzard : 100 req/s)
                with _db_lock, _db() as conn:
                    conn.execute("DELETE FROM game_recipes WHERE prof=?", (nom,))
                    conn.executemany(
                        "INSERT OR REPLACE INTO game_recipes (id, prof, tier, exp_rank, item, item_id, rank_no, mats, updated, "
                        "item_en, tier_en, prof_en, mats_en, inv_type, subclass_en, ilvl) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows_all)
                    # Recettes de la wishlist enregistrées avant qu'on connaisse l'objet (clé négative dérivée
                    # du nom) : on les rebascule sur l'id de l'objet, sinon l'étoile disparaîtrait.
                    for row in rows_all:
                        name_fr, item_id = row[4], int(row[5] or 0)
                        if item_id > 0:
                            old_key = _recipe_wish_key(0, name_fr)
                            conn.execute("UPDATE OR IGNORE wishlist SET item_id=? WHERE kind='recipe' AND item_id=?",
                                         (item_id, old_key))
                            conn.execute("DELETE FROM wishlist WHERE kind='recipe' AND item_id=?", (old_key,))
                total_written += len(rows_all)
            except Exception as exc:  # noqa: BLE001 — un métier en échec n'empêche pas les suivants
                failed.append(f"{nom} : {exc}")
                print(f"[game-recipes] {nom} : {exc}", flush=True)
        if failed:
            raise RuntimeError(" ; ".join(failed))
        st.update({"state": "done", "ts": time.time()})
        print(f"[game-recipes] synchro OK : {total_written} recettes", flush=True)
    except Exception as exc:  # noqa: BLE001 — tâche de fond : on trace sans casser
        st.update({"state": "error", "error": str(exc)[:200]})
        print(f"[game-recipes] erreur : {exc}", flush=True)


def _missing_profs() -> list[str]:
    """Métiers de GAME_PREP_PROFS sans aucune recette en base (nouveaux dans la liste, ou synchro ratée)."""
    with _db_lock, _db() as conn:
        have = {r["prof"] for r in conn.execute("SELECT DISTINCT prof FROM game_recipes").fetchall()}
    return [nom for _pid, nom in GAME_PREP_PROFS if nom not in have]


def _game_recipes_loop() -> None:
    """Au démarrage puis toutes les 6 h : synchro si vide ou trop ancienne, ou si un métier n'a encore aucune recette."""
    time.sleep(50)
    while True:
        try:
            with _db_lock, _db() as conn:
                row = conn.execute("SELECT COUNT(*) AS n, MAX(updated) AS ts FROM game_recipes").fetchone()
            n, ts = int(row["n"] or 0), float(row["ts"] or 0)
            if _game_sync_state["state"] != "running" and (n == 0 or time.time() - ts > GAME_SYNC_TTL):
                _game_sync()
            elif _game_sync_state["state"] != "running" and _missing_profs():
                _game_sync(_missing_profs())  # métier ajouté à la liste : synchro sans attendre la prochaine
        except Exception as exc:  # noqa: BLE001
            print(f"[game-recipes] boucle : {exc}", flush=True)
        time.sleep(6 * 3600)

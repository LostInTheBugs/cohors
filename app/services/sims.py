"""Cohors — simulation engine: queue, single worker, SimC crash fallback, "Stuff conseillé" runs."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from app import bnet
from app.core.config import SIM_TIMEOUT
from app.core.db import _db, _db_lock
from app.services.stuff import (
    HEAL_SPECS, STUFF_CONTENTS, STUFF_HEAL_PRIO, STUFF_ITERATIONS,
    _stuff_best_crafted, _stuff_bis_filter, _stuff_bis_list, _stuff_fetch_stats, _stuff_max_levels,
    _stuff_parse_export, _stuff_plausible, _stuff_rank_heal, _stuff_sim_input,
)
from app.simclient import run_sim  # soumet au worker de simulation (socket Unix, sans docker.sock ici)


def _next_queued() -> str | None:
    with _db_lock, _db() as conn:
        running = conn.execute("SELECT COUNT(*) AS c FROM sims WHERE status='running'").fetchone()["c"]
        if running:
            return None
        row = conn.execute("SELECT id FROM sims WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
        if row is None:
            return None
        conn.execute("UPDATE sims SET status='running', started=? WHERE id=?", (time.time(), row["id"]))
        return row["id"]


# Objets qui font planter l'image officielle de SimulationCraft (segfault en multi-cœur,
# voir _sim_with_crash_fallback) — relancés sur un seul cœur, retirés en dernier recours.
CRASH_ITEM_IDS = {"270162": "Réceptacle rituel de l'Entortillâme"}


def _strip_crash_items(text: str) -> tuple[str, list[str]]:
    kept, removed = [], []
    for line in text.splitlines():
        m = re.match(r"^\s*(head|neck|shoulder|back|chest|shirt|tabard|wrist|hands|waist|legs|feet|finger1|finger2|trinket1|trinket2|main_hand|off_hand)\s*=", line)
        if m:
            ids = re.findall(r"\bid=(\d+)", line)
            hit = next((i for i in ids if i in CRASH_ITEM_IDS), None)
            if hit:
                removed.append(CRASH_ITEM_IDS[hit] + f" (id {hit})")
                continue
        kept.append(line)
    return "\n".join(kept), removed


def _sim_with_crash_fallback(profile: Path, iterations: int, extra: list[str] | None,
                             timeout: int, outdir: Path | None = None) -> tuple[dict, str | None]:
    """Lance la sim ; si le moteur plante (139) sur un objet connu, relance sur un seul cœur.

    Cause réelle (vérifiée en 2026-09 sur l'image officielle simulationcraftorg/simc) :
    l'image est basée sur Alpine/musl, dont les threads secondaires n'ont que 128 Ko de
    pile ; l'effet du Réceptacle rituel en demande plus. Le même objet passe sans souci
    avec threads=1 (fil principal) ou sur un SimC compilé pour glibc. On relance donc
    d'abord avec threads=1 (résultat complet, juste plus lent) et on ne retire l'objet
    qu'en dernier recours. Renvoie (résultat, note à afficher ou None).
    """
    res = run_sim(profile_path=profile, iterations=iterations, outdir=outdir, timeout=timeout, extra=extra)
    if res.get("ok") or res.get("rc") != 139:
        return res, None
    try:
        text = profile.read_text()
        stripped, removed = _strip_crash_items(text)
        if not removed:
            return res, None
        single = list(extra or []) + ["threads=1"]
        res = run_sim(profile_path=profile, iterations=iterations, outdir=outdir, timeout=timeout, extra=single)
        if res.get("ok"):
            return res, ("Calcul lancé sur un seul cœur : " + ", ".join(removed) + " fait planter le moteur "
                         "SimulationCraft en multi-cœur (bug de l'image du moteur, pas de l'export) — "
                         "résultat complet, juste plus lent.")
        profile.write_text(stripped)
        res = run_sim(profile_path=profile, iterations=iterations, outdir=outdir, timeout=timeout, extra=extra)
        if res.get("ok"):
            return res, ("Sim lancée SANS " + ", ".join(removed) + " — cet objet fait planter le moteur "
                         "SimulationCraft (bug du moteur, pas de l'export).")
    except Exception:  # noqa: BLE001
        pass
    return res, None


def _run_one(sim_id: str) -> None:
    with _db_lock, _db() as conn:
        row = conn.execute("SELECT * FROM sims WHERE id=?", (sim_id,)).fetchone()
        if row is None or row["status"] != "running":
            return
        input_file = Path(row["input_file"])
        iterations = int(row["iterations"])

    try:
        kind = row["kind"] or "dps"
        if kind == "stuff":
            _run_stuff(row)
            return
        extra = ["calculate_scale_factors=1"] if kind == "weights" else None
        if kind == "group":
            extra = ["calculate_scale_factors=0", "fight_style=Patchwerk", "max_time=300"]
        res, crash_note = _sim_with_crash_fallback(input_file, iterations, extra, SIM_TIMEOUT, input_file.parent)
        if crash_note:
            res["note"] = crash_note
        if not res.get("ok") and res.get("rc") == 139 and not res.get("note"):
            res["log_tail"] = ("Le moteur a planté (segfault) sur cet export — c'est un bug du moteur SimC (souvent un objet précis, connu : Réceptacle rituel de l'Entortillâme). " + (res.get("log_tail") or ""))[:2000]
        ok = bool(res.get("ok"))
        weights = json.dumps(res.get("scale_factors")) if res.get("scale_factors") else None
        gear = json.dumps(res.get("gear")) if res.get("gear") else None
        if kind == "group" and res.get("group"):
            gear = json.dumps(res.get("group"))
        if kind == "group":
            res["dps"] = None  # DPS multi-acteurs : pas de valeur globale
        with _db_lock, _db() as conn:
            conn.execute(
                """UPDATE sims SET status=?, dps=?, dps_error_pct=?, wall_s=?, report_html=?, report_json=?,
                                    error=?, finished=?, weights=?, gear=? WHERE id=?""",
                (
                    "done" if ok else "failed",
                    res.get("dps"), res.get("dps_error_pct"), res.get("wall_s"),
                    res.get("html"), res.get("json"),
                    (res.get("note") or None) if ok else (res.get("log_tail") or "échec de la simulation")[-2000:],
                    time.time(), weights, gear, sim_id,
                ),
            )
    except Exception as exc:  # noqa: BLE001
        with _db_lock, _db() as conn:
            conn.execute(
                "UPDATE sims SET status='failed', error=?, finished=? WHERE id=?",
                (str(exc)[-2000:], time.time(), sim_id),
            )


def _run_stuff_bis(sim_id: str, parsed: dict, items: list[dict], plan: dict, t0: float) -> None:
    """Mode BIS : liste des meilleures pièces du guide filtrée par contenu, possession et comparaison."""
    blk = _stuff_bis_list(parsed["cls"], parsed["spec"]) or {}
    # Filtrer le guide BIS selon les contenus cochés
    contents = plan.get("content") or ["raid"]
    if isinstance(contents, str):
        contents = [contents]
    blk = _stuff_bis_filter(blk, contents)
    slot_fr = {}
    with _db_lock, _db() as conn:
        loc = "fr_FR"
        loc_en = "en_US"
        results = {"mode": "bis", "source": blk.get("source_url", ""),
                   "source_fr": blk.get("source_label_fr", ""),
                   "updated_fr": blk.get("updated_fr", ""), "slots": [],
                   "content": contents}
        owned_by_id, equipped_by_id = {}, set()
        for it in items:
            m = re.search(r"id=(\d+)", it.get("opts") or "")
            if not m:
                continue
            owned_by_id[int(m.group(1))] = it
            if it.get("equipped"):
                equipped_by_id.add(int(m.group(1)))
        cur_by_slot = {it["slot"]: it for it in items if it.get("equipped")}
        need_stats = []
        for e in blk.get("slots") or []:
            row = {"slot": e.get("slot"), "id": e.get("id"),
                   "src_fr": e.get("src_fr", ""), "src_en": e.get("src_en", "")}
            cur = cur_by_slot.get(e.get("slot"))
            row["current"] = ({"name": cur.get("name"), "ilvl": cur.get("ilvl")} if cur else None)
            row["equipped"] = int(e.get("id") or 0) in equipped_by_id
            row["owned"] = int(e.get("id") or 0) in owned_by_id
            if row["owned"] and not row["equipped"]:
                row["bag_ilvl"] = (owned_by_id[int(e.get("id"))] or {}).get("ilvl")
            try:
                row["name_fr"] = bnet.item(int(e.get("id")), locale=loc).get("name") or ""
                row["name_en"] = bnet.item(int(e.get("id")), locale=loc_en).get("name") or ""
            except Exception:  # noqa: BLE001
                row["name_fr"], row["name_en"] = "", ""
            need_stats.append({"opts": f',id={int(e.get("id"))}', "ilvl": 0})
            results["slots"].append(row)
        _stuff_max_levels(need_stats, conn)
        for row, st in zip(results["slots"], need_stats):
            row["max_ilvl"] = st.get("max_ilvl")
        if "craft" in contents:
            results["crafted"] = _stuff_best_crafted(conn, parsed["cls"], parsed["spec"], contents,
                                                     set(owned_by_id), equipped_by_id)
    results["missing"] = sum(1 for r in results["slots"] if not r["owned"] and not r["equipped"])
    results["have"] = len(results["slots"]) - results["missing"]
    wall = time.time() - t0
    with _db_lock, _db() as conn:
        conn.execute("UPDATE sims SET status='done', wall_s=?, gear=?, finished=? WHERE id=?",
                     (round(wall, 3), json.dumps(results), time.time(), sim_id))


def _run_stuff(row: sqlite3.Row) -> None:
    """Exécute un « Stuff conseillé » (kind='stuff') : stats des pièces puis classement."""
    sim_id = row["id"]
    workdir = Path(row["input_file"]).parent
    t0 = time.time()
    res = None
    try:
        plan = json.loads(row["plan"] or "{}")
        with _db_lock, _db() as conn:
            prof = conn.execute("SELECT id, name, input FROM profiles WHERE id=?",
                                (plan.get("profile_id"),)).fetchone()
        if prof is None:
            raise RuntimeError("Profil introuvable (il a peut-être été supprimé).")
        parsed = _stuff_parse_export(prof["input"])
        parsed["_raw"] = prof["input"]
        if not parsed["bags"]:
            raise RuntimeError("Aucune pièce dans les sacs de cet export — réexporte avec l'addon.")
        items, seen = [], set()
        for it in parsed["equipped"]:
            key = (it["slot"], it["opts"])
            if key in seen:
                continue
            seen.add(key)
            d = dict(it)
            d["equipped"] = True
            items.append(d)
        for it in parsed["bags"]:
            key = (it["slot"], it["opts"])
            if key in seen:
                continue
            seen.add(key)
            d = dict(it)
            d["equipped"] = False
            items.append(d)
        boosted, examples = 0, []
        if plan.get("max_rank"):
            with _db_lock, _db() as conn:
                boosted, examples = _stuff_max_levels(items, conn)
        if (plan.get("mode") or "cur") == "bis":
            _run_stuff_bis(sim_id, parsed, items, plan, t0)
            return
        stats, dropped, crashed = _stuff_fetch_stats(items, parsed["cls"], parsed["spec"], workdir)
        for it in items:
            it["stats"] = stats.get(it.get("actor"))
        content = plan.get("content") or "raid"
        is_heal = parsed["spec"] in HEAL_SPECS
        if is_heal:
            prio = dict(STUFF_HEAL_PRIO.get(f'{parsed["cls"]}/{parsed["spec"]}')
                        or {"orders": {}, "source": "", "source_fr": ""})
            prio["known"] = bool(prio.get("orders"))
            results = _stuff_rank_heal(parsed, items, prio, content)
            results["prio_known"] = prio["known"]
            results["note_fr"] = prio.get("note_fr") or ""
            note = None if prio["known"] else "Classé au niveau d'objet : priorités de stats pas encore répertoriées pour cette spécialisation."
        else:
            equipped_by_slot = {it["slot"]: it for it in items if it.get("equipped")}
            cands = _stuff_plausible(items, equipped_by_slot)
            if not cands:
                raise RuntimeError("Rien dans les sacs ne peut battre l'équipement actuel — bon signe !")
            text, n_cands = _stuff_sim_input(parsed, items, plan.get("loadout") or {})
            sim_file = workdir / "stuff.simc"
            sim_file.write_text(text)
            extra = list(STUFF_CONTENTS[content]["opts"])
            res, note = _sim_with_crash_fallback(sim_file, STUFF_ITERATIONS, extra, SIM_TIMEOUT, workdir)
            if not res.get("ok"):
                raise RuntimeError("La simulation a échoué : " + ((res.get("log_tail") or "raison inconnue")[-400:]))
            base = None
            try:
                data = json.loads(Path(res["json"]).read_text())
                p0 = (data.get("sim", {}).get("players") or [{}])[0]
                base = ((p0.get("collected_data") or {}).get("dps") or {}).get("mean")
            except Exception:  # noqa: BLE001
                base = None
            items_res = []
            for g in (res.get("gear") or []):
                m = re.search(r"\[(\w+)\]$", g.get("name") or "")
                dps = float(g.get("dps") or 0.0)
                items_res.append({"name": re.sub(r"\s*\[[^\]]*\]\s*$", "", g.get("name") or ""),
                                  "slot": m.group(1) if m else "",
                                  "dps": dps,
                                  "gain": (dps - base) if base else None,
                                  "gain_pct": round(100 * (dps - base) / base, 2) if base else None})
            items_res.sort(key=lambda x: -(x["dps"] or 0))
            results = {"mode": "sim", "content": content, "baseline": base,
                       "iterations": STUFF_ITERATIONS, "items": items_res,
                       "candidates": n_cands, "report": bool(res.get("html"))}
        results["sim_settings"] = STUFF_CONTENTS.get(content, {}).get("sim_fr", "")
        results["max_rank"] = bool(plan.get("max_rank"))
        results["boosted"] = boosted
        results["boost_examples"] = examples
        results["equipped_count"] = len(parsed["equipped"])
        results["bag_count"] = len(parsed["bags"])
        results["valid_count"] = sum(1 for it in items if it.get("stats"))
        results["invalid_count"] = dropped
        if crashed:
            results["crashed"] = [c.get("name") or "?" for c in crashed]
        results["loadout"] = (plan.get("loadout") or {}).get("name") or ""
        wall = time.time() - t0
        with _db_lock, _db() as conn:
            conn.execute(
                """UPDATE sims SET status='done', dps=?, wall_s=?, gear=?, error=?, report_html=?, report_json=?, finished=? WHERE id=?""",
                (results.get("baseline"), round(wall, 3), json.dumps(results), note,
                 (res or {}).get("html"), (res or {}).get("json"), time.time(), sim_id))
    except Exception as exc:  # noqa: BLE001
        with _db_lock, _db() as conn:
            conn.execute("UPDATE sims SET status='failed', error=?, finished=? WHERE id=?",
                         (str(exc)[-2000:], time.time(), sim_id))


def _recover_stale_sims() -> None:
    """Simulations restées « running » après un redémarrage de l'APP : le worker ne connaît pas
    nos ids et personne ne suit plus ces jobs — sans nettoyage, _next_queued voit toujours une
    simulation « en cours » et la file reste bloquée pour toujours (trouvé le 20/09, revue)."""
    try:
        with _db_lock, _db() as conn:
            n = conn.execute(
                "UPDATE sims SET status='failed', error=?, finished=? WHERE status='running'",
                ("interrompu par un redémarrage du service — relance la simulation.", time.time()),
            ).rowcount
        if n:
            print(f"[sims] {n} simulation(s) restée(s) « running » → marquée(s) interrompue(s)", flush=True)
    except sqlite3.Error as exc:
        print(f"[sims] récupération des sims « running » : {exc}")


def _worker_loop() -> None:
    while True:
        sim_id = _next_queued()
        if sim_id is None:
            time.sleep(1.0)
            continue
        _run_one(sim_id)

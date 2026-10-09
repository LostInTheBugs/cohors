"""Cohors — daily character snapshots: capture, Discord progress alerts, WCL backfill, sync loop."""
from __future__ import annotations

import json
import time

from app import bnet, discord_bot, wcl
from app.core.brand import _brand_identity
from app.core.db import _db, _db_lock
from app.core.util import _pick, _snap_day
from app.services.bot import _bot_config
from app.services.crafting import _prof_store
from app.services.jobs import _job_conf, _job_int, _job_status_set


def _char_changes(prev: dict, new: dict) -> dict:
    """Différences annonçables entre deux relevés : palier d'iLvl, montures, mascottes."""
    ch: dict = {}
    p_il, n_il = prev.get("ilvl"), new.get("ilvl")
    if p_il and n_il and n_il > p_il and (n_il // 5) > (p_il // 5):
        ch["ilvl_from"], ch["ilvl_to"] = p_il, n_il
    for key in ("mounts", "pets"):
        p, n = prev.get(key), new.get(key)
        if p is not None and n is not None and n > p:
            ch[key] = n - p
    return ch


def _char_alert(name: str, prev: dict, new: dict) -> None:
    """Annonce Discord (persos liés) : palier d'iLvl, nouvelles montures / mascottes."""
    ch = _char_changes(prev, new)
    if not ch:
        return
    try:
        cfg = _bot_config()
    except Exception:  # noqa: BLE001
        return
    if (cfg is None or not cfg["enabled"] or not cfg["notify_chars"]
            or not (cfg["token"] or "").strip() or not (cfg["channel_id"] or "").strip()):
        return
    try:
        discord_bot.send(cfg["token"], cfg["channel_id"], embeds=[discord_bot.char_embed(name, ch, _brand_identity()["guild_name"])])
    except Exception as exc:  # noqa: BLE001
        print(f"[snap] alerte {name}: {exc}")


def _char_snapshot(realm: str, name: str) -> dict:
    """État d'un personnage (résumé + équipement + collections) pour un relevé quotidien.

    Les noms (classe, spé, objets, emplacements) sont relevés en FR **et** en EN :
    l'historique reste lisible dans la langue du compte au moment de la consultation.
    """
    s, _ = bnet.character(realm, name, locale="fr_FR")
    g, _ = bnet.equipment(realm, name, locale="fr_FR")
    x, _ = bnet.extras(realm, name)
    s_en: dict = {}
    g_en: dict = {}
    try:
        s_en, _ = bnet.character(realm, name, locale="en_US")
        g_en, _ = bnet.equipment(realm, name, locale="en_US")
    except bnet.BnetError as exc:
        print(f"[snap] versions EN indisponibles pour {name}: {exc}")
    en_items = {it.get("item_id"): it for it in (g_en.get("items") or [])}
    items = []
    for it in (g.get("items") or []):
        e = en_items.get(it.get("item_id")) or {}
        items.append({
            "slot": it.get("slot"), "slot_en": e.get("slot"),
            "name": it.get("name"), "name_en": e.get("name"),
            "ilvl": it.get("ilvl"), "q": it.get("quality"), "id": it.get("item_id"),
        })
    return {
        "level": s.get("level"), "spec": s.get("spec"), "spec_en": s_en.get("spec"),
        "class": s.get("class"), "class_en": s_en.get("class"),
        "ilvl": s.get("ilvl_equipped"), "ilvl_avg": s.get("ilvl_avg"),
        "last_login": s.get("last_login"),
        "achv": s.get("achievement_points"),
        "mounts": x.get("mounts"), "pets": x.get("pets"), "mplus": x.get("mplus_rating"),
        "items": items,
    }


def _snap_store(realm: str, name: str, data: dict, day: str | None = None, ts: float | None = None) -> None:
    """Enregistre (ou remplace) le relevé d'un jour (défaut : aujourd'hui)."""
    with _db_lock, _db() as conn:
        conn.execute(
            "INSERT INTO char_snapshots (realm, name, day, ts, data) VALUES (?,?,?,?,?) "
            "ON CONFLICT(realm, name, day) DO UPDATE SET ts=excluded.ts, data=excluded.data",
            (realm.lower(), name.lower(), day or _snap_day(), time.time() if ts is None else ts,
             json.dumps(data, ensure_ascii=False)),
        )


def _snap_capture(realm: str, name: str) -> None:
    """Capture silencieuse (thread à la demande) — les erreurs sont seulement journalisées."""
    try:
        _snap_store(realm, name, _char_snapshot(realm, name))
    except Exception as exc:  # noqa: BLE001
        print(f"[snap] {name}: {exc}")


# Emplacements Blizzard (ordre des tableaux CombatantInfo WCL, index 0-17).
WCL_SLOTS = ["Tête", "Cou", "Épaules", "Chemise", "Torse", "Taille", "Jambes", "Pieds",
             "Poignets", "Mains", "1er anneau", "2e anneau", "1er bijou", "2e bijou",
             "Dos", "Main droite", "Main gauche", "Tabard"]
WCL_SLOTS_EN = ["Head", "Neck", "Shoulders", "Shirt", "Chest", "Waist", "Legs", "Feet",
                "Wrists", "Hands", "Ring 1", "Ring 2", "Trinket 1", "Trinket 2",
                "Back", "Main Hand", "Off Hand", "Tabard"]


def _snap_backfill(days: int = 30, force: bool = False) -> dict:
    """Rétro-remplit les relevés depuis les logs de raid (WCL) — uniquement les jours manquants.

    Blizzard ne fournit AUCUN historique : l'équipement passé ne peut venir que des
    rapports de combat (CombatantInfo), qui donnent l'état exact au moment du raid.
    """
    try:
        rep_list, _ts = wcl.reports(limit=50, force=force)
    except wcl.WclError as exc:
        return {"ok": False, "error": str(exc)}
    cutoff_ms = (time.time() - days * 86400) * 1000
    reports = sorted(
        [r for r in (rep_list.get("data") or []) if (r.get("startTime") or 0) >= cutoff_ms],
        key=lambda r: r.get("startTime") or 0,
    )
    with _db_lock, _db() as conn:
        have = {(r["realm"], r["name"], r["day"]) for r in conn.execute(
            "SELECT realm, name, day FROM char_snapshots").fetchall()}
    # cible : tout le roster de la guilde (+ les persos liés par sécurité)
    candidates: dict[str, tuple] = {}
    try:
        roster, _ts = bnet.roster()
        for m in roster.get("members") or []:
            nm = (m.get("name") or "").lower()
            if nm:
                candidates[nm] = ((m.get("realm") or bnet.GUILD_REALM), nm)
    except bnet.BnetError as exc:
        print(f"[snap] backfill : roster indisponible ({exc})")
    with _db_lock, _db() as conn:
        for r in conn.execute("SELECT DISTINCT realm, name FROM char_links").fetchall():
            candidates.setdefault(r["name"].lower(), (r["realm"], r["name"]))
    linked_by_name = candidates
    per: dict[tuple, dict] = {}
    for rep in reports:
        code = rep.get("code")
        day = _snap_day((rep.get("startTime") or 0) / 1000)
        try:
            comb, _ts2 = wcl.report_combatants(code)
        except wcl.WclError as exc:
            print(f"[snap] WCL {code}: {exc}")
            continue
        for pname, gear in (comb.get("players") or {}).items():
            link = linked_by_name.get(pname.lower())
            if link is None:
                continue
            k = (link[0], link[1], day)
            if k in have:
                continue
            items = [
                {"slot": WCL_SLOTS[i], "slot_en": WCL_SLOTS_EN[i],
                 "id": g.get("id"), "ilvl": g.get("itemLevel")}
                for i, g in enumerate(gear)
                if g and g.get("id") and i < len(WCL_SLOTS)
            ]
            if items:
                per[k] = {"ts": (rep.get("startTime") or 0) / 1000, "items": items}
    added = 0
    for (realm, name, day), entry in per.items():
        items = []
        for it in entry["items"]:
            try:
                meta = bnet.item(it["id"], locale="fr_FR")
                nm, q = meta.get("name"), meta.get("quality")
            except bnet.BnetError:
                nm, q = f"Objet {it['id']}", None
            try:
                nm_en = bnet.item(it["id"], locale="en_US").get("name")
            except bnet.BnetError:
                nm_en = None
            items.append({"slot": it["slot"], "slot_en": it.get("slot_en"), "name": nm, "name_en": nm_en,
                          "ilvl": it["ilvl"], "q": q, "id": it["id"]})
        ilvls = [it["ilvl"] for it in items if it.get("ilvl") and it["ilvl"] > 1]
        data = {
            "level": None, "spec": None, "class": None,
            "ilvl": round(sum(ilvls) / len(ilvls)) if ilvls else None, "ilvl_avg": None,
            "achv": None, "mounts": None, "pets": None, "mplus": None,
            "items": items, "src": "wcl",
        }
        _snap_store(realm, name, data, day=day, ts=entry["ts"])
        added += 1
    return {"ok": True, "added": added, "reports": len(reports)}


def _snap_tick() -> None:
    """Un passage : relevés de TOUS les personnages du roster (liés rafraîchis plus souvent) + purge."""
    with _db_lock, _db() as conn:
        linked = {r["name"] for r in conn.execute("SELECT DISTINCT name FROM char_links").fetchall()}
        latest = {
            r["k"]: r["ts"]
            for r in conn.execute(
                "SELECT realm || '|' || name AS k, MAX(ts) AS ts FROM char_snapshots GROUP BY realm, name"
            ).fetchall()
        }
        prof_latest = {
            r["k"]: r["ts"]
            for r in conn.execute(
                "SELECT realm || '|' || name AS k, MAX(ts) AS ts FROM char_professions GROUP BY realm, name"
            ).fetchall()
        }
    try:
        roster, _ts = bnet.roster()
        members = [(m.get("realm") or bnet.GUILD_REALM, m.get("name") or "")
                   for m in (roster.get("members") or [])]
        roster_ok = True
    except bnet.BnetError as exc:
        print(f"[snap] roster indisponible ({exc}) — repli sur les personnages liés")
        with _db_lock, _db() as conn:
            members = [(r["realm"], r["name"]) for r in conn.execute(
                "SELECT DISTINCT realm, name FROM char_links").fetchall()]
        roster_ok = False
    targets = sorted(((realm, name, name.lower() in linked) for realm, name in members if name),
                     key=lambda t: not t[2])  # persos liés d'abord
    now = time.time()
    done_this_tick = 0
    snapped = 0
    profs = 0
    limit_linked = _job_int("snap_linked_h") * 60
    limit_roster = _job_int("snap_roster_h") * 60
    max_tick = _job_int("snap_max_tick")
    prof_days = _job_int("prof_days")
    for realm, name, is_linked in targets:
        k = f"{realm}|{name.lower()}"
        last = latest.get(k)
        limit_min = limit_linked if is_linked else limit_roster
        need_snap = not (last and now - last < limit_min * 60)
        plast = prof_latest.get(k)
        need_prof = not (plast and now - plast < prof_days * 86400)
        if not need_snap and not need_prof:
            continue
        if done_this_tick >= max_tick:
            break  # borne le temps du passage ; le reste au tick suivant
        done_this_tick += 1
        if need_snap:
            try:
                prev = None
                if name.lower() in linked:
                    with _db_lock, _db() as conn:
                        prow = conn.execute(
                            "SELECT data FROM char_snapshots WHERE realm=? AND name=? AND day=?",
                            (realm.lower(), name.lower(), _snap_day()),
                        ).fetchone()
                        if prow is None:
                            prow = conn.execute(
                                "SELECT data FROM char_snapshots WHERE realm=? AND name=? "
                                "ORDER BY day DESC LIMIT 1",
                                (realm.lower(), name.lower()),
                            ).fetchone()
                    if prow is not None:
                        try:
                            prev = json.loads(prow["data"] or "{}")
                        except (ValueError, TypeError):
                            prev = None
                data = _char_snapshot(realm, name)
                _snap_store(realm, name, data)
                if prev:
                    _char_alert(name, prev, data)
            except bnet.BnetError as exc:
                if getattr(exc, "status", None) == 404 and roster_ok:
                    # personnage inexistant côté API : purge des relevés s'il a quitté le roster (ToU §18)
                    if name.lower() not in {n.lower() for _r, n in members}:
                        with _db_lock, _db() as conn:
                            conn.execute("DELETE FROM char_snapshots WHERE realm=? AND name=?", (realm, name.lower()))
                        print(f"[snap] {name} absent du roster → relevés supprimés")
                else:
                    print(f"[snap] {name}: {exc}")
        if need_prof:
            try:
                _prof_store(realm, name)
                profs += 1
            except bnet.BnetError as exc:
                print(f"[snap] prof {name}: {exc}")
        if need_snap:
            snapped += 1
    cutoff = _snap_day(now - (_job_int("snap_keep_days") - 1) * 86400)
    with _db_lock, _db() as conn:
        conn.execute("DELETE FROM char_snapshots WHERE day < ?", (cutoff,))
    return {"snapped": snapped, "profs": profs, "checked": done_this_tick, "cutoff": cutoff}


def _snap_loop() -> None:
    time.sleep(20)
    first = True
    while True:
        if _job_conf("snap_enabled") in ("0", "false", "no", ""):
            _job_status_set("snapshots", detail="en pause (administration)")
            time.sleep(max(30, _job_int("snap_interval_min") * 60))
            continue
        try:
            res = _snap_tick()
            _job_status_set("snapshots", detail=f'{res.get("snapped", 0)} relevé(s), '
                                                f'{res.get("profs", 0)} métier(s)')
            if first:
                first = False
                with _db_lock, _db() as conn:
                    done_row = conn.execute("SELECT value FROM meta WHERE key='snap_backfill_v1'").fetchone()
                if done_row is None:
                    res = _snap_backfill()
                    print(f"[snap] backfill WCL : {res}")
                    if res.get("ok"):
                        with _db_lock, _db() as conn:
                            conn.execute(
                                "INSERT OR REPLACE INTO meta (key, value) VALUES ('snap_backfill_v1', ?)",
                                (str(int(time.time())),),
                            )
        except Exception as exc:  # noqa: BLE001
            print(f"[snap] tick: {exc}")
            _job_status_set("snapshots", error=str(exc))
        time.sleep(max(30, _job_int("snap_interval_min") * 60))


def _is_tracked_char(realm: str, name: str) -> bool:
    """Le personnage est-il suivi (lié au compte ou membre du roster de guilde) ?"""
    with _db_lock, _db() as conn:
        row = conn.execute("SELECT 1 FROM char_links WHERE realm=? AND name=? LIMIT 1", (realm, name)).fetchone()
    if row is not None:
        return True
    try:
        roster, _ts = bnet.roster()
    except bnet.BnetError:
        return False
    return any((m.get("name") or "").lower() == name for m in (roster.get("members") or []))


def _snap_summary(day: str, ts: float, d: dict, want_en: bool = False) -> dict:
    return {"day": day, "ts": ts, "level": d.get("level"),
            "spec": _pick(d, "spec", want_en), "class": _pick(d, "class", want_en),
            "ilvl": d.get("ilvl"), "ilvl_avg": d.get("ilvl_avg"),
            "achv": d.get("achv"), "mounts": d.get("mounts"), "pets": d.get("pets"),
            "mplus": d.get("mplus"), "src": d.get("src")}

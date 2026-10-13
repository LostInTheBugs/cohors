"""Cohors — « inscrits absents » : joueurs qui ont accepté un raid du calendrier in-game mais
n'apparaissent pas dans le log Warcraft Logs de ce soir-là.

L'addon n'exporte que les événements à venir (veille comprise) : à chaque import, la dernière version
de chaque événement est gardée dans gcal_history. Un événement passé est croisé avec la soirée loggée
du même jour (Europe/Paris) ; un joueur venu sur un autre personnage de son compte (char_links) compte
comme présent. Les réponses forcées par un officier (gcal_meta.ovr) priment sur celles du jeu.
"""
from __future__ import annotations

import json
import time

from app.core.util import _snap_day

STATUS = {1: "ok", 3: "ok", 2: "no", 8: "maybe"}


def _key(name) -> str:
    return str(name or "").split("-", 1)[0].strip().lower()


def event_key(e: dict) -> str:
    """Clé stable d'un événement du calendrier in-game (id Blizzard, sinon horodatage) — gcal_meta."""
    try:
        if e.get("id"):
            return "id:" + str(int(e["id"]))
    except (TypeError, ValueError):
        pass
    try:
        return "ts:" + str(int(float(e.get("ts") or 0)))
    except (TypeError, ValueError):
        return "ts:0"


def remember_events(conn, events: list) -> None:
    """Garde la dernière version connue de chaque événement importé (appelé à chaque import)."""
    now = time.time()
    for e in events:
        try:
            ts = float(e.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts <= 0:
            continue
        conn.execute(
            "INSERT INTO gcal_history (event_key, ts, title, inv, updated) VALUES (?,?,?,?,?) "
            "ON CONFLICT(event_key) DO UPDATE SET ts=excluded.ts, title=excluded.title, inv=excluded.inv, "
            "updated=excluded.updated",
            (event_key(e), ts, str(e.get("title") or "")[:200], json.dumps(e.get("inv") or [], ensure_ascii=False), now))


def calendar_noshows(conn, evenings: list, since: float) -> dict:
    """Croise les événements passés du calendrier (depuis `since`) avec les soirées loggées.

    evenings : [{"day": "YYYY-MM-DD", "players": [noms]}] (attendance). Renvoie
    {"matched": n, "unlogged": n, "rows": [{name, key, accepted, came, noshow, days}]} — une ligne par
    joueur ayant accepté au moins un raid croisé, les plus souvent absents d'abord.
    """
    now = time.time()
    # le dernier import (fait avant la création de gcal_history) est aussi pris en compte
    imp = conn.execute("SELECT data FROM gcal_import WHERE id=1").fetchone()
    if imp is not None:
        try:
            cur = (json.loads(imp["data"]) or {}).get("events") or []
        except (ValueError, TypeError):
            cur = []
        known = {r["event_key"] for r in conn.execute("SELECT event_key FROM gcal_history").fetchall()}
        remember_events(conn, [e for e in cur if isinstance(e, dict) and event_key(e) not in known])
    by_day: dict[str, set] = {}
    for ev in evenings:
        by_day.setdefault(ev["day"], set()).update(_key(p) for p in ev.get("players") or [])
    owner = {_key(r["name"]): r["user_email"] for r in conn.execute(
        "SELECT name, user_email FROM char_links").fetchall()}
    metas = {}
    for r in conn.execute("SELECT event_key, data FROM gcal_meta").fetchall():
        try:
            metas[r["event_key"]] = json.loads(r["data"]) or {}
        except (ValueError, TypeError):
            pass
    rows = conn.execute(
        "SELECT event_key, ts, title, inv FROM gcal_history WHERE ts >= ? AND ts < ? ORDER BY ts",
        (since, now - 3 * 3600)).fetchall()
    stats: dict[str, dict] = {}
    matched = unlogged = 0
    for r in rows:
        try:
            inv = json.loads(r["inv"]) or []
        except (ValueError, TypeError):
            continue
        ovr = (metas.get(r["event_key"]) or {}).get("ovr") or {}
        ok = [i["n"] for i in inv if isinstance(i, dict) and i.get("n")
              and (ovr.get(i["n"]) or STATUS.get(i.get("s"), "wait")) == "ok"]
        if not ok:
            continue
        day = _snap_day(r["ts"])
        present = by_day.get(day)
        if present is None:
            unlogged += 1
            continue
        matched += 1
        accounts = {owner[p] for p in present if p in owner}
        for n in ok:
            k = _key(n)
            came = k in present or (owner.get(k) is not None and owner[k] in accounts)
            st = stats.setdefault(k, {"name": n.split("-", 1)[0], "key": k, "accepted": 0, "came": 0,
                                      "noshow": 0, "days": []})
            st["accepted"] += 1
            if came:
                st["came"] += 1
            else:
                st["noshow"] += 1
                st["days"].append(day)
    out = sorted(stats.values(), key=lambda s: (-s["noshow"], -s["accepted"], s["key"]))
    return {"matched": matched, "unlogged": unlogged, "rows": out}

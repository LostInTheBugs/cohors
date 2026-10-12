"""Cohors — raid loot history, noted in game by the add-on and imported by an officer.

The add-on cannot reach the site: at the end of the evening the officer clicks « Soirée » (Loot tab)
and pastes the text it writes (« CohorsLOOT1 ») on the Calendar page:

    CohorsLOOT1
    L|<epoch>|<item_id>|<personnage (Nom ou Nom-Royaume)>|<encounterID>|<nom de l'objet>

The add-on keeps its last 300 entries, so importing twice is harmless: a line already known (same
instant, same item, same character) is skipped. Characters are keyed like everywhere else on the
site: name without realm, lower case.
"""
from __future__ import annotations

import time

from app.core.db import _db, _db_lock

LH_FORMAT = "CohorsLOOT1"
LH_DAYS = 30                 # « a eu N objets ces 30 derniers jours »
LH_MAX_LINES = 2000
LH_MAX_AGE = 365 * 86400     # une ligne plus vieille d'un an est refusée (horloge ou texte faux)


def char_key(name) -> str:
    return str(name or "").split("-", 1)[0].strip().lower()


def _clean(s, n=80) -> str:
    return " ".join(str(s or "").replace("|", " ").split())[:n]


def parse(text: str):
    """→ (lignes valides, nombre de lignes illisibles) ; ValueError si ce n'est pas un export."""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    if not lines or lines[0] != LH_FORMAT:
        raise ValueError(f"en-tête « {LH_FORMAT} » introuvable — colle tout le texte de l'addon (onglet Loot → « Soirée »)")
    now = time.time()
    rows, bad = [], 0
    for ln in lines[1:LH_MAX_LINES + 1]:
        parts = ln.split("|")
        try:
            if parts[0] != "L" or len(parts) < 5:
                raise ValueError
            at, iid, who = float(parts[1]), int(parts[2]), parts[3]
            enc = int(parts[4] or 0)
        except (ValueError, IndexError):
            bad += 1
            continue
        key = char_key(who)
        if iid <= 0 or not key or at > now + 3600 or at < now - LH_MAX_AGE:
            bad += 1
            continue
        rows.append({"at": at, "item_id": iid, "char": key, "name": _clean(who.split("-", 1)[0], 40),
                     "item_name": _clean(parts[5] if len(parts) > 5 else ""), "encounter": enc})
    return rows, bad


def import_text(text: str, by: str) -> dict:
    rows, bad = parse(text)
    added = 0
    with _db_lock, _db() as conn:
        for r in rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO loot_history (at, item_id, char, name, item_name, encounter, added_by, added) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (r["at"], r["item_id"], r["char"], r["name"], r["item_name"], r["encounter"], by, time.time()))
            added += cur.rowcount or 0
    return {"ok": True, "added": added, "known": len(rows) - added, "bad": bad}


def counts(conn, days: int = LH_DAYS) -> dict:
    """{perso (clé) : objets reçus sur la période}."""
    since = time.time() - days * 86400
    return {r["char"]: int(r["n"]) for r in conn.execute(
        "SELECT char, COUNT(*) AS n FROM loot_history WHERE at >= ? GROUP BY char", (since,)).fetchall()}


def for_char(name: str, days: int = 90, limit: int = 50) -> list:
    since = time.time() - days * 86400
    with _db_lock, _db() as conn:
        rows = conn.execute(
            "SELECT at, item_id, item_name FROM loot_history WHERE char=? AND at >= ? ORDER BY at DESC LIMIT ?",
            (char_key(name), since, limit)).fetchall()
    return [dict(r) for r in rows]


def received(conn, chars, item_ids, days: int = 90) -> dict:
    """{item_id : [{name, at}]} — objets reçus par ces persos (wishlist : « 🎁 reçu le … »)."""
    keys = sorted({char_key(c) for c in chars if c})
    ids = sorted({int(i) for i in item_ids if i and int(i) > 0})
    if not keys or not ids:
        return {}
    since = time.time() - days * 86400
    out: dict = {}
    q = ("SELECT at, item_id, name FROM loot_history WHERE at >= ? AND char IN (%s) AND item_id IN (%s) "
         "ORDER BY at DESC" % (",".join("?" * len(keys)), ",".join("?" * len(ids))))
    for r in conn.execute(q, (since, *keys, *ids)).fetchall():
        out.setdefault(int(r["item_id"]), []).append({"name": r["name"], "at": r["at"]})
    return out

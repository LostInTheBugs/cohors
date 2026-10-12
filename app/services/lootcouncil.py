"""Cohors — loot council export: who wants which item, what it would bring them, for the in-game add-on.

The add-on cannot reach the site, so the officer pastes a text export before the raid (« CohorsLC1 »):

    CohorsLC1
    H|<généré (epoch)>|<seuil Héros/Mythe>|<guilde>
    U|<u1>|<nom affiché du compte (main)>
    P|<personnage>|<classe>|<u1 ou vide>|<pièces de tier>|<total>|<HEAD=315;NECK=320;…>
    W|<item_id>|<u1>|<prio 0/1>|<gain BIS en % ou 0>|<iLvl minimal visé (piste Mythe/Héros/Champion)>

Lines are « | »-separated; names never contain « | » (stripped). One P line per active max-level
character (snapshot < 7 days), with the item level worn per slot so the add-on can compute the gain.
"""
from __future__ import annotations

import json
import time

from app.core.db import _db, _db_lock
from app.core.util import CLASS_KEY_FR
from app.services.readiness import READY_MAX_AGE, READY_MIN_LEVEL, _tier, _track_ilvl, track_mins
from app.services.wishlist import _bis_by_user

LC_FORMAT = "CohorsLC1"


def _clean(s) -> str:
    return " ".join(str(s or "").replace("|", " ").replace(";", " ").replace("=", " ").split())[:60]


def council_export(guild: str = "") -> str:
    """Texte « CohorsLC1 » (voir le docstring du module)."""
    cutoff = time.time() - READY_MAX_AGE
    with _db_lock, _db() as conn:
        snaps = conn.execute(
            "SELECT realm, name, data FROM char_snapshots "
            "WHERE id IN (SELECT MAX(id) FROM char_snapshots GROUP BY realm, name) AND ts >= ?",
            (cutoff,)).fetchall()
        links = conn.execute("SELECT user_email, name, display, is_main FROM char_links").fetchall()
        users = {r["email"]: (r["name"] or r["email"].split("@")[0]) for r in conn.execute(
            "SELECT email, name FROM users").fetchall()}
        wishes = conn.execute(
            "SELECT user_email, item_id, prio, track FROM wishlist WHERE item_id > 0 AND (kind IS NULL OR kind='item') "
            "ORDER BY item_id").fetchall()
        bis = _bis_by_user(conn)
        track = _track_ilvl(conn)
        tmins = track_mins(conn)
    # comptes : clé courte u1, u2… ; nom affiché = main déclaré, sinon nom du compte
    ukey: dict[str, str] = {}
    udisp: dict[str, str] = {}
    owner_of: dict[str, str] = {}
    for ln in sorted(links, key=lambda r: (not r["is_main"], r["name"] or "")):
        email = ln["user_email"]
        if email not in ukey:
            ukey[email] = f"u{len(ukey) + 1}"
        if ln["is_main"] or email not in udisp:
            udisp[email] = ln["display"] or (ln["name"] or "").title()
        owner_of[(ln["name"] or "").strip().lower()] = email
    for w in wishes:                                   # comptes sans personnage lié mais avec une wishlist
        if w["user_email"] not in ukey:
            ukey[w["user_email"]] = f"u{len(ukey) + 1}"
            udisp[w["user_email"]] = users.get(w["user_email"], "")
    lines = [LC_FORMAT, f"H|{int(time.time())}|{track}|{_clean(guild)}"]
    for email, k in sorted(ukey.items(), key=lambda kv: int(kv[1][1:])):
        lines.append(f"U|{k}|{_clean(udisp.get(email))}")
    for sr in sorted(snaps, key=lambda r: r["name"]):
        try:
            d = json.loads(sr["data"]) or {}
        except (ValueError, TypeError):
            continue
        if (d.get("level") or 0) < READY_MIN_LEVEL:
            continue
        slots = []
        for it in d.get("items") or []:
            st, il = it.get("st"), it.get("ilvl")
            if st and il:
                slots.append(f"{st}={int(il)}")
        t = _tier(d, False) or {}
        email = owner_of.get(sr["name"])
        lines.append("P|%s|%s|%s|%d|%d|%s" % (
            _clean(sr["name"]), CLASS_KEY_FR.get(d.get("class") or "", ""), ukey.get(email, "") if email else "",
            int(t.get("n") or 0), int(t.get("of") or 0), ";".join(slots)))
    for w in wishes:
        b = (bis.get(w["user_email"]) or {}).get(int(w["item_id"]))
        lines.append("W|%d|%s|%d|%s|%d" % (int(w["item_id"]), ukey[w["user_email"]], 1 if w["prio"] else 0,
                                           ("%.1f" % float(b.get("gain") or 0)) if b else "0",
                                           tmins.get(w["track"] or "myth", tmins["myth"])))
    return "\n".join(lines) + "\n"

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

import json
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


LH_POST_MAX = 30          # lignes au plus dans le message Discord


def import_text(text: str, by: str) -> dict:
    """Importe le texte de l'addon. Les objets NOUVEAUX qui étaient dans la wishlist du compte qui les
    a reçus valent une notification au joueur (« retire-le de ta wishlist ? »). Renvoie aussi les
    nouvelles lignes (`new`, avec `wish` / `prio`) pour le message Discord."""
    rows, bad = parse(text)
    new = []
    now = time.time()
    with _db_lock, _db() as conn:
        for r in rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO loot_history (at, item_id, char, name, item_name, encounter, added_by, added) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (r["at"], r["item_id"], r["char"], r["name"], r["item_name"], r["encounter"], by, now))
            if cur.rowcount:
                new.append(dict(r))
        owner = {str(x["name"] or "").strip().lower(): x["user_email"] for x in conn.execute(
            "SELECT name, user_email FROM char_links").fetchall()}
        wish = {(x["user_email"], int(x["item_id"])): bool(x["prio"]) for x in conn.execute(
            "SELECT user_email, item_id, prio FROM wishlist WHERE item_id > 0 AND (kind IS NULL OR kind='item')").fetchall()}
        for r in new:
            email = owner.get(r["char"])
            r["wish"] = email is not None and (email, r["item_id"]) in wish
            r["prio"] = bool(r["wish"] and wish[(email, r["item_id"])])
            if r["wish"]:
                conn.execute("INSERT INTO notifs (email, kind, data, created) VALUES (?,?,?,?)",
                             (email, "loot", json.dumps({"item": r["item_name"], "item_id": r["item_id"],
                                                         "who": r["name"], "at": r["at"]}, ensure_ascii=False), now))
    return {"ok": True, "added": len(new), "known": len(rows) - len(new), "bad": bad,
            "wished": sum(1 for r in new if r["wish"]), "new": new}


def loot_embed(new: list, guild: str = "la guilde", link: str = "", color: int = 0xDFA55A) -> dict:
    """Embed Discord du butin importé : une ligne par objet, ⭐ / 🎯 quand il était en wishlist."""
    rows = sorted(new, key=lambda r: (r["at"], r["name"].lower()))
    days = sorted({time.strftime("%d/%m", time.localtime(r["at"])) for r in rows})
    lines = []
    for r in rows[:LH_POST_MAX]:
        tag = " — ⭐ prio wishlist" if r.get("prio") else (" — 🎯 wishlist" if r.get("wish") else "")
        lines.append(f"**{r['name']}** · {r['item_name'] or ('objet ' + str(r['item_id']))}{tag}")
    if len(rows) > LH_POST_MAX:
        lines.append(f"_… et {len(rows) - LH_POST_MAX} autre(s)._")
    fields, cur = [], ""
    for ln in lines:                       # champs Discord : 1 024 caractères au plus
        if cur and len(cur) + 1 + len(ln) > 1024:
            fields.append(cur)
            cur = ""
        cur = (cur + "\n" + ln) if cur else ln[:1024]
    if cur:
        fields.append(cur)
    nwish = sum(1 for r in rows if r.get("wish"))
    desc = f"{len(rows)} objet(s) reçu(s)" + (f", dont {nwish} en wishlist 🎯" if nwish else "") + "."
    if link:
        desc += f"\n👉 [Calendrier et historique]({link})"
    return {"title": "🎁 Butin du raid" + (f" — {', '.join(days)}" if days else ""), "description": desc,
            "color": color,
            "fields": [{"name": "Qui a reçu quoi" if i == 0 else "\u200b", "value": v} for i, v in enumerate(fields[:5])],
            "footer": {"text": f"{guild} · noté par l'addon Cohors"}}


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

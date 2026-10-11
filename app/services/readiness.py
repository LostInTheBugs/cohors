"""Cohors — player readiness: missing enchants and empty sockets per character, Hero / Myth item-level
threshold, Discord embed. Shared by the readiness routes and the bot's weekly recap."""
from __future__ import annotations

import json
import time

from app import bnet
from app.core.db import _db, _db_lock
from app.core.util import CLASS_KEY_FR, _pick, _snap_day

READY_MAX_AGE = 7 * 86400.0   # relevés de moins de 7 jours (joueurs actifs)
READY_MIN_LEVEL = 90          # niveau maximum (Midnight)
# Emplacements enchantables en Midnight (plus de cape ni de brassards) ; la main gauche seulement si
# c'est une arme (bouclier et objet tenu en main gauche ne s'enchantent pas).
ENCHANT_SLOTS = ("HEAD", "SHOULDER", "CHEST", "LEGS", "FEET", "FINGER_1", "FINGER_2", "MAIN_HAND")
# Filtre « pièces Héros / Mythe » : iLvl minimum d'une pièce pour qu'un enchantement ou une gemme manquant
# compte. L'API Blizzard ne donne pas la piste d'amélioration (« Héros 4/6 ») : seulement l'iLvl.
# Par défaut Héros 1/6 de Midnight saison 2 (305) ; les officiers l'ajustent à chaque saison.
READY_TRACK_ILVL_DEFAULT = 305
READY_TRACK_KEY = "readiness_min_ilvl"
READY_PROGRESS_DAYS = 7       # progression : comparaison avec le relevé d'il y a 7 jours (ou le plus ancien connu)


def _track_ilvl(conn) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (READY_TRACK_KEY,)).fetchone()
    try:
        return int(row["value"]) if row else READY_TRACK_ILVL_DEFAULT
    except (TypeError, ValueError):
        return READY_TRACK_ILVL_DEFAULT


def _readiness(d: dict, want_en: bool) -> dict:
    """Enchantements manquants et châsses vides d'un relevé ; known=False si le relevé est trop ancien
    pour contenir ces informations."""
    items = d.get("items") or []
    known = any(it.get("ench") is not None for it in items)
    missing, empty = [], 0
    ench_items, sock_items = [], []      # détail par pièce, avec son iLvl (filtre Héros / Mythe côté page)
    if known:
        for it in items:
            st = it.get("st") or ""
            label = ((it.get("slot_en") or it.get("slot")) if want_en else it.get("slot")) or st
            il = it.get("ilvl")
            if (st in ENCHANT_SLOTS or (st == "OFF_HAND" and it.get("wpn"))) and it.get("ench") is False:
                missing.append(label)
                ench_items.append({"slot": label, "ilvl": il})
            try:
                n = max(0, int(it.get("sock") or 0) - int(it.get("gems") or 0))
            except (TypeError, ValueError):
                n = 0
            if n:
                empty += n
                sock_items.append({"slot": label, "ilvl": il, "n": n})
    return {"known": known, "missing_enchants": missing, "empty_sockets": empty,
            "ench_items": ench_items, "sock_items": sock_items}


def _baselines(conn, want_en: bool, today: str) -> dict:
    """Relevé de référence par personnage pour la progression : le dernier d'il y a au moins 7 jours ; s'il ne
    contient pas encore les enchantements (collectés depuis 2026.10.025), le plus ancien suivant qui les a."""
    ref_day = _snap_day(time.time() - READY_PROGRESS_DAYS * 86400)
    floor_day = _snap_day(time.time() - 3 * READY_PROGRESS_DAYS * 86400)
    days: dict = {}
    for sr in conn.execute("SELECT id, realm, name, day FROM char_snapshots WHERE day >= ? AND day < ? ORDER BY day",
                           (floor_day, today)).fetchall():
        days.setdefault((sr["realm"], sr["name"]), []).append((sr["day"], sr["id"]))
    best: dict = {}
    for k, lst in days.items():
        older = [x for x in lst if x[0] <= ref_day]
        cands = ([older[-1]] if older else []) + [x for x in lst if x[0] > ref_day]
        for day, sid in cands:                     # en pratique un ou deux relevés lus par personnage
            row = conn.execute("SELECT data FROM char_snapshots WHERE id=?", (sid,)).fetchone()
            try:
                r = _readiness(json.loads(row["data"]) or {}, want_en)
            except (ValueError, TypeError):
                continue
            if r["known"]:
                best[k] = (day, r)
                break
    return best


def _collect(want_en: bool) -> tuple[list[dict], int]:
    """Personnages actifs de niveau max (relevés < 7 jours) avec leur préparation, + seuil Héros / Mythe."""
    cutoff = time.time() - READY_MAX_AGE
    with _db_lock, _db() as conn:
        snaps = conn.execute(
            "SELECT realm, name, ts, data FROM char_snapshots "
            "WHERE id IN (SELECT MAX(id) FROM char_snapshots GROUP BY realm, name) AND ts >= ?",
            (cutoff,)).fetchall()
        mains = {(r["name"] or "").strip().lower() for r in conn.execute(
            "SELECT name FROM char_links WHERE is_main=1").fetchall()}
        track_ilvl = _track_ilvl(conn)
        base = _baselines(conn, want_en, _snap_day())
    disp: dict[str, str] = {}
    try:
        roster, _t = bnet.roster()
        disp = {(m.get("name") or "").lower(): m.get("name") or "" for m in (roster.get("members") or [])}
    except bnet.BnetError:
        pass
    rows = []
    for sr in snaps:
        try:
            d = json.loads(sr["data"]) or {}
        except (ValueError, TypeError):
            continue
        if (d.get("level") or 0) < READY_MIN_LEVEL or d.get("ilvl") is None:
            continue
        r = _readiness(d, want_en)
        rows.append({
            "name": disp.get(sr["name"]) or sr["name"].title(), "key": sr["name"], "realm": sr["realm"],
            "class_key": CLASS_KEY_FR.get(d.get("class") or ""), "spec": _pick(d, "spec", want_en) or "",
            "ilvl": d.get("ilvl"), "mplus": d.get("mplus"), "main": sr["name"] in mains, "ts": sr["ts"], **r,
            "before": ({"day": b[0], "ench_items": b[1]["ench_items"], "sock_items": b[1]["sock_items"]}
                       if (b := base.get((sr["realm"], sr["name"]))) and r["known"]
                       and b[0] < _snap_day(sr["ts"]) else None),
        })
    rows.sort(key=lambda x: (-(len(x["missing_enchants"]) + x["empty_sockets"]), not x["known"],
                             -(x["ilvl"] or 0), x["name"].lower()))
    return rows, track_ilvl


def _above(items: list[dict], min_ilvl: int) -> list[dict]:
    """Pièces retenues par le filtre Héros / Mythe (iLvl inconnu : gardée)."""
    return [it for it in items if it.get("ilvl") is None or it["ilvl"] >= min_ilvl]


READY_POST_MAX = 25   # personnages listés dans le message Discord (les plus en retard d'abord)


def readiness_embed(rows: list[dict], track_ilvl: int, guild: str = "la guilde", link: str = "",
                    mains_only: bool = False, color: int = 0xDFA55A) -> dict:
    """Embed Discord : personnages à préparer sur leurs pièces Héros / Mythe (enchantements, gemmes)."""
    todo = todo_rows(rows, track_ilvl, mains_only)
    lines = []
    for r, ench, sock in todo[:READY_POST_MAX]:
        parts = []
        if ench:
            parts.append("🪄 " + ", ".join(ench))
        if sock:
            parts.append(f"💎 {sock} châsse{'s' if sock > 1 else ''} vide{'s' if sock > 1 else ''}")
        lines.append(f"**{r['name']}**{' ⭐' if r.get('main') else ''} ({r.get('ilvl') or '?'}) — " + " · ".join(parts))
    if len(todo) > READY_POST_MAX:
        lines.append(f"_… et {len(todo) - READY_POST_MAX} autre(s) : voir le bilan._")
    fields, cur = [], ""
    for ln in lines:                       # champs Discord : 1 024 caractères au plus
        if cur and len(cur) + 1 + len(ln) > 1024:
            fields.append(cur)
            cur = ""
        cur = (cur + "\n" + ln) if cur else ln[:1024]
    if cur:
        fields.append(cur)
    scope = "mains" if mains_only else "personnages"
    desc = (f"{len(todo)} {scope} à préparer — enchantements et gemmes manquants sur les pièces Héros / Mythe "
            f"(iLvl ≥ {track_ilvl})." if todo else f"Tous les {scope} sont prêts sur leurs pièces Héros / Mythe ✅")
    if link:
        desc += f"\n👉 [Bilan des joueurs]({link})"
    return {"title": "🩺 Préparation du raid", "description": desc, "color": color,
            "fields": [{"name": "À corriger" if i == 0 else "\u200b", "value": v} for i, v in enumerate(fields[:5])],
            "footer": {"text": f"{guild} · bilan des joueurs"}}


def todo_rows(rows: list[dict], track_ilvl: int, mains_only: bool = False) -> list[tuple[dict, list[str], int]]:
    """(personnage, emplacements sans enchantement, châsses vides) sur les pièces ≥ seuil, les plus en retard
    d'abord ; seulement les personnages qui ont quelque chose à corriger."""
    todo = []
    for r in rows:
        if not r.get("known") or (mains_only and not r.get("main")):
            continue
        ench = [it["slot"] for it in _above(r.get("ench_items") or [], track_ilvl)]
        sock = sum(it["n"] for it in _above(r.get("sock_items") or [], track_ilvl))
        if ench or sock:
            todo.append((r, ench, sock))
    todo.sort(key=lambda x: (-(len(x[1]) + x[2]), -(x[0].get("ilvl") or 0), x[0]["name"].lower()))
    return todo


def progress(rows: list[dict], track_ilvl: int) -> dict:
    """Progression depuis le relevé de référence, sur les pièces ≥ seuil : personnages qui ont posé des
    enchantements ou des gemmes, ceux qui n'ont plus rien à corriger, et le total posé."""
    improved = fixed = ench = gems = 0
    since = None
    for r in rows:
        b = r.get("before")
        if not b or not r.get("known"):
            continue
        e0 = len(_above(b["ench_items"], track_ilvl))
        g0 = sum(it["n"] for it in _above(b["sock_items"], track_ilvl))
        e1 = len(_above(r.get("ench_items") or [], track_ilvl))
        g1 = sum(it["n"] for it in _above(r.get("sock_items") or [], track_ilvl))
        de, dg = max(0, e0 - e1), max(0, g0 - g1)
        if de or dg:
            improved += 1
            ench += de
            gems += dg
            if not e1 and not g1:
                fixed += 1
        since = b["day"] if since is None else min(since, b["day"])
    return {"since": since, "improved": improved, "fixed": fixed, "ench": ench, "gems": gems}

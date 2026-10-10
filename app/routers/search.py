"""Cohors — global search (navigation bar, Ctrl+K): characters, items and recipes already in the database."""
from __future__ import annotations

import difflib
import itertools
import json
import re
import threading
import time
import unicodedata

from fastapi import APIRouter, Request

from app import bnet
from app.core.auth import _require_user, _user_locale
from app.core.db import _db, _db_lock
from app.core.util import CLASS_KEY_FR, _pick

router = APIRouter()

SEARCH_INDEX_TTL = 300.0           # index reconstruit au plus toutes les 5 min
SNAP_MAX_AGE = 30 * 86400.0        # personnages : relevés de moins de 30 jours (membres actuels)
MAX_CHARS, MAX_ITEMS, MAX_RECIPES = 8, 12, 5
_IDX: dict = {"ts": 0.0, "data": None}
_IDX_LOCK = threading.Lock()


def _norm(s) -> str:
    """Forme de comparaison : sans accents ni casse, espaces réduits (« Hÿpérion » → « hyperion »)."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.casefold().replace("’", "'").split())


def _build_index() -> dict:
    """Personnages (derniers relevés), objets (butin, équipement porté, recettes, wishlists) et
    recettes sans objet connu — avec leurs formes normalisées pour la recherche."""
    cutoff = time.time() - SNAP_MAX_AGE
    with _db_lock, _db() as conn:
        snaps = conn.execute(
            "SELECT realm, name, data FROM char_snapshots "
            "WHERE id IN (SELECT MAX(id) FROM char_snapshots GROUP BY realm, name) AND ts >= ?",
            (cutoff,)).fetchall()
        loot = conn.execute(
            "SELECT item_id, kind, inst_fr, inst_en, boss_fr, boss_en, name_fr, name_en FROM item_loot").fetchall()
        game = conn.execute("SELECT item, item_en, item_id, prof, prof_en FROM game_recipes").fetchall()
        declared = conn.execute("SELECT item, item_id, profession FROM craft_recipes").fetchall()
        wished = conn.execute("SELECT item_id, name FROM wishlist WHERE item_id > 0").fetchall()
    chars, items, recipes = [], {}, {}

    def item(iid: int) -> dict:
        return items.setdefault(iid, {"id": iid, "fr": "", "en": "", "loot": None, "prof": None})

    for sr in snaps:
        try:
            d = json.loads(sr["data"]) or {}
        except (ValueError, TypeError):
            continue
        chars.append({"key": sr["name"], "realm": sr["realm"], "n": _norm(sr["name"]),
                      "class_key": CLASS_KEY_FR.get(d.get("class") or ""), "ilvl": d.get("ilvl"),
                      "spec": d.get("spec") or "", "spec_en": d.get("spec_en") or ""})
        for it in d.get("items") or []:
            try:
                iid = int(it.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if iid > 0:
                e = item(iid)
                e["fr"] = e["fr"] or (it.get("name") or "")
                e["en"] = e["en"] or (it.get("name_en") or "")
    for r in loot:
        e = item(int(r["item_id"]))
        e["fr"] = r["name_fr"] or e["fr"]
        e["en"] = r["name_en"] or e["en"]
        if e["loot"] is None:
            e["loot"] = {"kind": r["kind"] or "", "inst_fr": r["inst_fr"] or "", "inst_en": r["inst_en"] or "",
                         "boss_fr": r["boss_fr"] or "", "boss_en": r["boss_en"] or ""}
    for r in game:
        iid = int(r["item_id"] or 0)
        if iid > 0:
            e = item(iid)
            e["fr"] = e["fr"] or (r["item"] or "")
            e["en"] = e["en"] or (r["item_en"] or "")
            e["prof"] = e["prof"] or {"fr": r["prof"] or "", "en": r["prof_en"] or ""}
        elif r["item"]:
            recipes.setdefault(_norm(r["item"]), {"fr": r["item"], "en": r["item_en"] or "",
                                                  "prof": {"fr": r["prof"] or "", "en": r["prof_en"] or ""}})
    for r in declared:
        iid = int(r["item_id"] or 0)
        if iid > 0:
            e = item(iid)
            e["fr"] = e["fr"] or (r["item"] or "")
            e["prof"] = e["prof"] or {"fr": r["profession"] or "", "en": ""}
        elif r["item"]:
            recipes.setdefault(_norm(r["item"]), {"fr": r["item"], "en": "",
                                                  "prof": {"fr": r["profession"] or "", "en": ""}})
    for r in wished:
        e = item(int(r["item_id"]))
        e["fr"] = e["fr"] or (r["name"] or "")
    for e in items.values():
        e["n"] = (_norm(e["fr"]), _norm(e["en"]))
    for k, rc in recipes.items():
        rc["n"] = (k, _norm(rc["en"]))
    # vocabulaire pour « Vouliez-vous dire… » : mots normalisés → forme affichée (accents conservés)
    words: dict[str, str] = {}
    names = [c["key"] for c in chars]
    names += [x for e in items.values() for x in (e["fr"], e["en"]) if x]
    names += [x for rc in recipes.values() for x in (rc["fr"], rc["en"]) if x]
    for nm in names:
        for w in _WORD_RE.findall(str(nm).lower()):
            nw = _norm(w)
            if len(nw) >= 3:
                words.setdefault(nw, w)
    return {"chars": chars, "items": [e for e in items.values() if e["fr"] or e["en"]],
            "recipes": list(recipes.values()), "words": words}


_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")


def _has_hit(idx: dict, needle: str) -> bool:
    """La recherche `needle` (déjà normalisée) trouve-t-elle au moins un personnage, objet ou recette ?"""
    return (any(needle in c["n"] for c in idx["chars"])
            or any(needle in n for e in idx["items"] for n in e["n"] if n)
            or any(needle in n for x in idx["recipes"] for n in x["n"] if n))


def _suggest(idx: dict, needle: str) -> list[str]:
    """Jusqu'à 3 corrections proches de la saisie (fautes de frappe), mot par mot, en ne gardant
    que celles qui donnent au moins un résultat."""
    words = idx.get("words") or {}
    keys = list(words)
    toks = needle.split()
    if not toks or len(toks) > 4:
        return []
    per_tok = []
    for t in toks:
        if t in words or len(t) < 3:
            per_tok.append([t])
            continue
        close = difflib.get_close_matches(t, keys, n=4, cutoff=0.75)
        if not close:
            return []
        per_tok.append(close)
    if all(len(c) == 1 and c[0] == t for c, t in zip(per_tok, toks)):
        return []
    # combinaisons, les plus proches d'abord (somme des rangs), 16 essais au plus
    combos = sorted(itertools.product(*[range(len(c)) for c in per_tok]), key=lambda ix: (sum(ix), ix))[:16]
    out = []
    for ix in combos:
        keys_ix = [per_tok[k][i] for k, i in enumerate(ix)]
        if not _has_hit(idx, " ".join(keys_ix)):
            continue
        phrase = " ".join(words.get(w, w) for w in keys_ix)
        if phrase not in out:
            out.append(phrase)
        if len(out) == 3:
            break
    return out


def _index() -> dict:
    with _IDX_LOCK:
        if _IDX["data"] is None or time.time() - _IDX["ts"] > SEARCH_INDEX_TTL:
            _IDX["data"] = _build_index()
            _IDX["ts"] = time.time()
        return _IDX["data"]


def _rank(names: tuple, needle: str):
    """None si aucun nom ne contient la recherche ; sinon 0 (commence par) ou 1 (contient)."""
    best = None
    for n in names:
        if n and needle in n:
            r = 0 if n.startswith(needle) else 1
            best = r if best is None else min(best, r)
    return best


@router.get("/api/search")
def api_search(request: Request, q: str = ""):
    """Recherche globale : personnages, objets (→ fiche objet) et recettes sans objet (→ artisanat)."""
    _require_user(request)
    want_en = _user_locale(request).startswith("en")
    needle = _norm(q)[:60]
    if len(needle) < 2:
        return {"q": q, "chars": [], "items": [], "recipes": [], "suggest": []}
    idx = _index()
    cands = [(r, c) for c in idx["chars"] if (r := _rank((c["n"],), needle)) is not None]
    cands.sort(key=lambda x: (x[0], -(x[1]["ilvl"] or 0), x[1]["n"]))
    disp: dict[str, str] = {}
    if cands:
        try:
            roster, _t = bnet.roster()
            disp = {(m.get("name") or "").lower(): m.get("name") or "" for m in (roster.get("members") or [])}
        except bnet.BnetError:
            pass
    chars = [{"name": disp.get(c["key"]) or c["key"].title(), "key": c["key"], "realm": c["realm"],
              "class_key": c["class_key"], "ilvl": c["ilvl"], "spec": _pick(c, "spec", want_en) or ""}
             for _r, c in cands[:MAX_CHARS]]
    found = [(r, e) for e in idx["items"] if (r := _rank(e["n"], needle)) is not None]
    found.sort(key=lambda x: (x[0], x[1]["loot"] is None and x[1]["prof"] is None, _norm(x[1]["fr"] or x[1]["en"])))
    items = []
    for _r, e in found[:MAX_ITEMS]:
        lt, pf = e["loot"], e["prof"]
        items.append({
            "id": e["id"],
            "name": ((e["en"] or e["fr"]) if want_en else (e["fr"] or e["en"])),
            "source": ({"kind": lt["kind"],
                        "boss": (lt["boss_en"] or lt["boss_fr"]) if want_en else lt["boss_fr"],
                        "instance": (lt["inst_en"] or lt["inst_fr"]) if want_en else lt["inst_fr"]} if lt else None),
            "profession": (((pf["en"] or pf["fr"]) if want_en else pf["fr"]) if pf else ""),
        })
    rec = [(r, x) for x in idx["recipes"] if (r := _rank(x["n"], needle)) is not None]
    rec.sort(key=lambda x: (x[0], x[1]["n"][0]))
    recipes = [{"name": ((x["en"] or x["fr"]) if want_en else x["fr"]),
                "profession": ((x["prof"]["en"] or x["prof"]["fr"]) if want_en else x["prof"]["fr"])}
               for _r, x in rec[:MAX_RECIPES]]
    suggest = _suggest(idx, needle) if not (chars or items or recipes) else []
    return {"q": q, "chars": chars, "items": items, "recipes": recipes, "suggest": suggest,
            "more": {"chars": max(0, len(cands) - MAX_CHARS), "items": max(0, len(found) - MAX_ITEMS)}}

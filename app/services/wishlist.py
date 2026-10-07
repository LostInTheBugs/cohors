"""Cohors — wishlist helpers shared with other features: BIS per user (from saved sims) and recipe keys."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

_SIM_ITEM_RE = re.compile(r"\[(\w+):(\d+)\]\s*$")


def _sim_char_name(path, label: str = "") -> str:
    """Nom du personnage depuis l'en-tête du profil SimC (1re ligne « classe="Nom" »)."""
    try:
        for line in Path(path).read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m2 = re.match(r'^\w+="?([^"=\n]+)"?\s*$', line)
            return (m2.group(1).strip() if m2 else "") or label or ""
    except OSError:
        pass
    return label or ""


def _bis_by_user(conn) -> dict:
    """Meilleures améliorations par emplacement déduites des sims « Top Stuff » de chaque compte.

    {email: {item_id: {char, gain}}} — gain = % de DPS vs le stuff actuel du profil simulé.
    """
    out: dict = {}
    try:
        rows = conn.execute(
            "SELECT label, user_email, dps, gear, input_file FROM sims "
            "WHERE kind='gear' AND status='done' AND gear IS NOT NULL AND dps IS NOT NULL "
            "ORDER BY created DESC LIMIT 200").fetchall()
    except sqlite3.OperationalError:
        return out
    for sr in rows:
        try:
            gear = json.loads(sr["gear"]) or []
        except (ValueError, TypeError):
            continue
        base = float(sr["dps"] or 0)
        if base <= 0 or not sr["user_email"]:
            continue
        best: dict = {}
        for g in gear:
            m2 = _SIM_ITEM_RE.search(str(g.get("name") or ""))
            if not m2:
                continue
            iid = int(m2.group(2))
            try:
                dl = 100.0 * (float(g.get("dps") or 0) - base) / base
            except (TypeError, ValueError):
                continue
            if dl > 0.05 and (m2.group(1) not in best or dl > best[m2.group(1)]["gain"]):
                best[m2.group(1)] = {"item_id": iid, "gain": round(dl, 2)}
        if not best:
            continue
        chart = _sim_char_name(sr["input_file"], sr["label"] or "")
        dset = out.setdefault(sr["user_email"], {})
        for _slot, b in best.items():
            dset.setdefault(b["item_id"], {"char": chart, "gain": b["gain"]})
    return out


def _recipe_wish_key(item_id, name) -> int:
    """Clé stable d'une recette dans la wishlist : l'objet fabriqué s'il est connu, sinon un
    identifiant négatif dérivé du nom (les recettes n'ont pas toujours d'item_id en base)."""
    try:
        iid = int(item_id or 0)
    except (TypeError, ValueError):
        iid = 0
    if iid > 0:
        return iid
    import zlib
    return -(int(zlib.crc32(str(name or "").strip().casefold().encode("utf-8"))) & 0x7FFFFFFF or 1)

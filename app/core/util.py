"""Cohors — small pure helpers shared by several routers."""
from __future__ import annotations

import re
import time
from datetime import datetime

from fastapi import HTTPException


def _int_any(v) -> int:
    """Entier depuis un nombre ou une chaîne (y compris hexadécimal « 0x… » écrit par le client WoW)."""
    try:
        if isinstance(v, str):
            s = v.strip()
            if s.lower().startswith("0x"):
                return int(s, 16)
            return int(float(s))
        return int(v if v is not None else 0)
    except (TypeError, ValueError):
        return 0


ITEM_REF_RE = re.compile(r"(?<!\d)(\d{4,7})(?!\d)")


def _snap_day(ts: float | None = None) -> str:
    """Jour courant (Europe/Paris, DST-safe) au format YYYY-MM-DD."""
    moment = time.time() if ts is None else ts
    try:
        from zoneinfo import ZoneInfo

        return datetime.fromtimestamp(moment, ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return time.strftime("%Y-%m-%d", time.gmtime(moment))


def _lua_unescape(t: str) -> str:
    """Dé-échappe une chaîne Lua écrite par le jeu dans un fichier SavedVariables."""
    out: list[str] = []
    i, n = 0, len(t)
    while i < n:
        c = t[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        nxt = t[i + 1]
        if nxt in "\\\"'":
            out.append(nxt)
            i += 2
        elif nxt == "n":
            out.append("\n")
            i += 2
        elif nxt == "r":
            out.append("\r")
            i += 2
        elif nxt == "t":
            out.append("\t")
            i += 2
        elif nxt.isdigit():
            j = i + 1
            while j < n and j < i + 4 and t[j].isdigit():
                j += 1
            out.append(chr(int(t[i + 1:j])))
            i = j
        else:
            out.append(nxt)
            i += 2
    return "".join(out)


_REALM_RE = re.compile(r"^[a-z0-9-]{2,40}$")
_CHARNAME_RE = re.compile(r"^[A-Za-z\u00c0-\u00ff][A-Za-z\u00c0-\u00ff'\-]{1,23}$")


def _valid_char(realm: str, name: str) -> None:
    if not _REALM_RE.match(realm.lower()) or not _CHARNAME_RE.match(name):
        raise HTTPException(400, "Nom de personnage ou royaume invalide.")


# Clés de classe anglaises (couleurs côté front) ↔ libellés Blizzard localisés (données stockées).
CLASS_KEY_FR = {
    "Chevalier de la mort": "DeathKnight", "Chasseur de démons": "DemonHunter", "Druide": "Druid",
    "Évocateur": "Evoker", "Chasseur": "Hunter", "Mage": "Mage", "Moine": "Monk", "Paladin": "Paladin",
    "Prêtre": "Priest", "Voleur": "Rogue", "Chaman": "Shaman", "Démoniste": "Warlock", "Guerrier": "Warrior",
}


def _pick(d: dict, key: str, want_en: bool):
    """Valeur d'un relevé dans la langue demandée (version EN si dispo, sinon FR)."""
    v = d.get(key)
    return (d.get(key + "_en") or v) if want_en else v

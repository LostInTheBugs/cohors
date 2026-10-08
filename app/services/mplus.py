"""Cohors — Mythic+ helpers shared by keys, alerts and notifications."""
from __future__ import annotations

from app import bnet


def _dungeon_key(name: str) -> str:
    """Clé canonique d'un donjon MM+ (nom anglais) pour comparer FR/EN."""
    nm = (name or "").strip()
    if not nm:
        return ""
    try:
        dun, _ts = bnet.mplus_dungeons()
        for x in (dun.get("dungeons") or []):
            if nm.lower() in ((x.get("name") or "").lower(), (x.get("en") or "").lower()):
                return (x.get("en") or x.get("name") or nm).lower()
    except bnet.BnetError:
        pass
    return nm.lower()

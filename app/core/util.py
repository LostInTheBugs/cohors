"""Cohors — small pure helpers shared by several routers."""
from __future__ import annotations


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

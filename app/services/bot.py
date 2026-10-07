"""Cohors — Discord bot settings: read (token decrypted) and partial update (token encrypted)."""
from __future__ import annotations

import time

from app import secretbox
from app.core.db import _db, _db_lock


def _bot_config() -> dict | None:
    with _db_lock, _db() as conn:
        row = conn.execute("SELECT * FROM bot_config WHERE id=1").fetchone()
    if row is None:
        return None
    out = dict(row)
    out["token"] = secretbox.decrypt(out.get("token") or "")
    return out


def _bot_save(updates: dict) -> None:
    if not updates:
        return
    if "token" in updates:
        updates["token"] = secretbox.encrypt(updates["token"])
    sets = ", ".join(f"{k}=?" for k in updates)
    with _db_lock, _db() as conn:
        conn.execute(f"UPDATE bot_config SET {sets}, updated=? WHERE id=1", (*updates.values(), time.time()))

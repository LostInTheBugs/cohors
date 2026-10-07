"""Cohors — guild dashboard: recent guild events, member count and next in-game raid."""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Request

from app import bnet
from app.core.auth import _require_user
from app.core.db import _db, _db_lock
from app.core.util import _int_any

router = APIRouter()


@router.get("/api/dashboard")
def api_dashboard(request: Request):
    _require_user(request)
    with _db_lock, _db() as conn:
        rows = conn.execute(
            "SELECT kind, member, created FROM guild_events ORDER BY created DESC, id DESC LIMIT 15"
        ).fetchall()
        gcal = conn.execute("SELECT ts, player, data FROM gcal_import WHERE id=1").fetchone()
    out: dict = {"events": [dict(r) for r in rows], "members": None, "next_raid": None}
    try:
        data, ts = bnet.roster()
        out["members"] = {"count": len(data.get("members") or []), "fetched": ts}
    except bnet.BnetError:
        pass
    # prochain raid d'apres le calendrier in-game importe (addon Cohors)
    try:
        if gcal is not None:
            evs = (json.loads(gcal["data"]) or {}).get("events") or []
            now = time.time()
            upcoming = [e for e in evs if float(e.get("ts") or 0) > now - 3600]
            upcoming.sort(key=lambda e: float(e.get("ts") or 0))
            pick = next((e for e in upcoming if _int_any(e.get("type")) == 0), None)
            if pick is None and upcoming:
                pick = upcoming[0]
            if pick is not None:
                inv = pick.get("inv") or []
                ok = sum(1 for i in inv if _int_any(i.get("s")) in (1, 3))
                maybe = sum(1 for i in inv if _int_any(i.get("s")) == 8)
                no = sum(1 for i in inv if _int_any(i.get("s")) == 2)
                out["next_raid"] = {
                    "title": str(pick.get("title") or "Raid"),
                    "date": str(pick.get("date") or ""),
                    "ts": float(pick.get("ts") or 0),
                    "ok": ok, "maybe": maybe, "no": no, "wait": len(inv) - ok - maybe - no,
                    "imported_at": gcal["ts"], "player": gcal["player"] or "",
                }
    except (ValueError, TypeError):
        pass
    return out

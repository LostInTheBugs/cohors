"""Cohors — Mythic+ board: availability and keys announced by each member, alerts to matching members."""
from __future__ import annotations

import json
import re
import time

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app import bnet
from app.core.auth import _require_user, _user_locale
from app.core.db import _db, _db_lock
from app.services.mplus import _dungeon_key

router = APIRouter()


class MplusPostRequest(BaseModel):
    roles: list[str] = []
    slots: list[dict] = []
    keys: list[dict] = []


def _mplus_clean_slots(slots) -> list[dict]:
    out = []
    for s in (slots or [])[:6]:
        if not isinstance(s, dict):
            continue
        days = sorted({str(d)[:10] for d in (s.get("days") or [])
                       if re.match(r"^\d{4}-\d{2}-\d{2}$", str(d))})[:20]
        fr = str(s.get("from") or "")[:5]
        to = str(s.get("to") or "")[:5]
        fr = fr if re.match(r"^\d{2}:\d{2}$", fr) else ""
        to = to if re.match(r"^\d{2}:\d{2}$", to) else ""
        if days and (fr or to):
            out.append({"days": days, "from": fr, "to": to})
    return out


def _notify_mplus_keys(owner_email: str, owner_name: str, keys: list[dict]) -> None:
    """Prévient les membres dont une alerte MM+ correspond aux clés annoncées."""
    now = time.time()
    with _db_lock, _db() as conn:
        alerts = [dict(r) for r in conn.execute(
            "SELECT email, dungeon, min_level FROM mplus_alerts WHERE email != ?",
            (owner_email,)).fetchall()]
        for k in keys:
            dk = _dungeon_key(k.get("dungeon"))
            for a in alerts:
                if a["dungeon"] and a["dungeon"] != dk:
                    continue
                if int(k.get("level") or 2) < int(a["min_level"] or 2):
                    continue
                payload = json.dumps({"who": owner_name, "dungeon": k.get("dungeon") or "",
                                      "level": k.get("level") or 2}, ensure_ascii=False)
                dup = conn.execute(
                    "SELECT 1 AS x FROM notifs WHERE email=? AND kind='mplus' AND data=? AND created > ?",
                    (a["email"], payload, now - 7 * 86400)).fetchone()
                if dup:
                    continue
                conn.execute("INSERT INTO notifs (email, kind, data, created) VALUES (?,?,?,?)",
                             (a["email"], "mplus", payload, now))


def _mplus_clean_keys(keys) -> list[dict]:
    out = []
    for k in (keys or [])[:12]:
        if not isinstance(k, dict):
            continue
        ch = str(k.get("char") or "").strip()[:40]
        du = str(k.get("dungeon") or "").strip()[:80]
        try:
            lv = max(2, min(40, int(k.get("level") or 2)))
        except (TypeError, ValueError):
            lv = 2
        if ch or du:
            out.append({"char": ch, "dungeon": du, "level": lv})
    return out


@router.get("/api/mplus")
def api_mplus(request: Request):
    """Tableau d'organisation MM+ : dispos de chacun + clés annoncées."""
    user = _require_user(request)
    with _db_lock, _db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT user, name, roles, slots, keys, updated FROM mplus_posts ORDER BY updated DESC").fetchall()]
        mine = [dict(r) for r in conn.execute(
            "SELECT name, display, is_main FROM char_links WHERE user_email=?", (user["email"],)).fetchall()]
    posts = []
    for r in rows:
        for f in ("roles", "slots", "keys"):
            try:
                r[f] = json.loads(r[f] or "[]")
            except ValueError:
                r[f] = []
        r["mine"] = r["user"] == user["email"]
        posts.append(r)
    try:
        dun, _ts = bnet.mplus_dungeons()
        dungeons = dun.get("dungeons") or []
    except bnet.BnetError:
        dungeons = []
    if _user_locale(request).startswith("en"):
        dungeons = [dict(x, name=(x.get("en") or x.get("name"))) for x in dungeons]
    return {"posts": posts, "dungeons": dungeons,
            "my_chars": [{"name": c["name"], "display": c["display"], "is_main": bool(c["is_main"])}
                         for c in mine],
            "me": {"name": user["name"] if "name" in user.keys() else user["email"]}}


@router.post("/api/mplus")
def api_mplus_save(body: MplusPostRequest, request: Request):
    user = _require_user(request)
    roles = [r for r in (body.roles or []) if r in ("tank", "heal", "dps")][:3]
    slots = _mplus_clean_slots(body.slots)
    keys = _mplus_clean_keys(body.keys)
    email = user["email"]
    name = (user["name"] if "name" in user.keys() else "") or email
    with _db_lock, _db() as conn:
        _old = conn.execute("SELECT keys FROM mplus_posts WHERE user=?", (email,)).fetchone()
    try:
        _old_keys = {(_dungeon_key(k.get("dungeon")), int(k.get("level") or 2))
                     for k in (json.loads(_old["keys"]) if _old else [])}
    except (ValueError, TypeError, AttributeError):
        _old_keys = set()
    with _db_lock, _db() as conn:
        if not roles and not slots and not keys:
            conn.execute("DELETE FROM mplus_posts WHERE user=?", (email,))
            return {"ok": True, "deleted": True}
        conn.execute(
            "INSERT INTO mplus_posts (user, name, roles, slots, keys, updated) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(user) DO UPDATE SET name=excluded.name, roles=excluded.roles, "
            "slots=excluded.slots, keys=excluded.keys, updated=excluded.updated",
            (email, name, json.dumps(roles),
             json.dumps(slots, ensure_ascii=False), json.dumps(keys, ensure_ascii=False), time.time()))
    _new_keys = [k for k in keys if (_dungeon_key(k.get("dungeon")), int(k.get("level") or 2)) not in _old_keys]
    if _new_keys:
        _notify_mplus_keys(email, name, _new_keys)
    return {"ok": True, "roles": roles, "slots": len(slots), "keys": len(keys)}

"""Cohors — my characters: linking guild characters to accounts, main character, mains list."""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import bnet
from app.core.auth import _require_user
from app.core.db import _db, _db_lock

router = APIRouter()


class CharLinkRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=40)
    main: bool = False


@router.get("/api/me/chars")
def my_chars(request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        rows = conn.execute(
            "SELECT id, realm, name, display, is_main, created FROM char_links WHERE user_email=? ORDER BY is_main DESC, display",
            (user["email"],),
        ).fetchall()
    return {"chars": [dict(r) for r in rows]}


@router.get("/api/mains")
def api_mains(request: Request):
    """Tous les personnages liés de la guilde, regroupés par compte/main (sans e-mails)."""
    _require_user(request)
    with _db_lock, _db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT c.user_email, c.realm, c.name, c.display, c.is_main, COALESCE(u.name, '') AS user_name "
            "FROM char_links c LEFT JOIN users u ON u.email = c.user_email "
            "ORDER BY c.is_main DESC, c.name COLLATE NOCASE"
        ).fetchall()]
    groups: dict = {}
    for r in rows:
        g = groups.setdefault(r["user_email"], {"user": r["user_name"] or "(compte)", "chars": []})
        g["chars"].append({"realm": r["realm"], "name": r["name"],
                           "display": r["display"] or r["name"], "main": bool(r["is_main"])})
    out = list(groups.values())
    for g in out:
        g["chars"].sort(key=lambda c: (not c["main"], (c["display"] or "").lower()))
    out.sort(key=lambda g: ((not any(c["main"] for c in g["chars"])), g["user"].lower()))
    return {"ok": True, "accounts": out, "total_accounts": len(out), "total_chars": len(rows)}


@router.post("/api/me/chars")
def link_char(payload: CharLinkRequest, request: Request):
    user = _require_user(request)
    name = payload.name.strip()
    try:
        data, _ts = bnet.roster()
    except bnet.BnetError as exc:
        raise HTTPException(502, f"Roster indisponible : {exc}")
    hit = next((m for m in (data.get("members") or []) if (m.get("name") or "").lower() == name.lower()), None)
    if hit is None:
        raise HTTPException(404, "Personnage introuvable dans le roster de la guilde — vérifie l'orthographe.")
    realm = (hit.get("realm") or bnet.GUILD_REALM).lower()
    display = hit.get("name") or name
    lname = display.lower()
    with _db_lock, _db() as conn:
        dup = conn.execute(
            "SELECT id FROM char_links WHERE user_email=? AND realm=? AND name=?",
            (user["email"], realm, lname),
        ).fetchone()
        if dup is not None:
            raise HTTPException(400, "Ce personnage est déjà lié à ton compte.")
        taken = conn.execute(
            "SELECT id FROM char_links WHERE realm=? AND name=? AND user_email != ? LIMIT 1",
            (realm, lname, user["email"]),
        ).fetchone() is not None
        first_char = conn.execute(
            "SELECT COUNT(*) AS c FROM char_links WHERE user_email=?", (user["email"],)
        ).fetchone()["c"] == 0
        set_main = bool(payload.main) or first_char
        if set_main:
            conn.execute("UPDATE char_links SET is_main=0 WHERE user_email=?", (user["email"],))
        cur = conn.execute(
            "INSERT INTO char_links (user_email, realm, name, display, is_main, created) VALUES (?,?,?,?,?,?)",
            (user["email"], realm, lname, display, 1 if set_main else 0, time.time()),
        )
        cid = int(cur.lastrowid or 0)
    return {"id": cid, "ok": True, "taken": taken, "display": display}


@router.delete("/api/me/chars/{cid}")
def unlink_char(cid: int, request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        r = conn.execute("SELECT * FROM char_links WHERE id=?", (cid,)).fetchone()
        if r is None or (r["user_email"] != user["email"] and not user["is_admin"]):
            raise HTTPException(404, "Personnage non lié à ton compte.")
        conn.execute("DELETE FROM char_links WHERE id=?", (cid,))
    return {"ok": True}


@router.post("/api/me/chars/{cid}/main")
def set_main_char(cid: int, request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        r = conn.execute("SELECT * FROM char_links WHERE id=?", (cid,)).fetchone()
        if r is None or r["user_email"] != user["email"]:
            raise HTTPException(404, "Personnage non lié à ton compte.")
        conn.execute("UPDATE char_links SET is_main=0 WHERE user_email=?", (user["email"],))
        conn.execute("UPDATE char_links SET is_main=1 WHERE id=?", (cid,))
    return {"ok": True}

"""Cohors — raid calendar: planning raids and member sign-ups."""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.auth import _require_officer, _require_user, _user_role
from app.core.db import _db, _db_lock

router = APIRouter()


class RaidRequest(BaseModel):
    title: str = Field("", max_length=120)
    starts: float
    duration_min: int = Field(180, ge=15, le=720)
    note: str = Field("", max_length=300)


class SignupRequest(BaseModel):
    status: str = Field(..., max_length=10)


@router.get("/api/raids")
def api_raids(request: Request):
    user = _require_user(request)
    now = time.time()
    with _db_lock, _db() as conn:
        rows = conn.execute(
            "SELECT * FROM raids WHERE starts >= ? ORDER BY starts LIMIT 20", (now - 7200,)
        ).fetchall()
        past = conn.execute(
            "SELECT id, title, starts FROM raids WHERE starts < ? ORDER BY starts DESC LIMIT 5",
            (now - 7200,),
        ).fetchall()
        su_rows = conn.execute(
            "SELECT s.*, u.name AS user_name FROM raid_signups s LEFT JOIN users u ON u.email = s.user_email"
        ).fetchall()
    by_raid: dict = {}
    for r in su_rows:
        by_raid.setdefault(r["raid_id"], []).append(r)

    def pack(row) -> dict:
        counts = {"yes": 0, "maybe": 0, "no": 0}
        names = {"yes": [], "maybe": [], "no": []}
        mine = ""
        for s in by_raid.get(row["id"], []):
            st = s["status"]
            if st in counts:
                counts[st] += 1
                names[st].append(s["user_name"] or (s["user_email"] or "").split("@")[0])
            if s["user_email"] == user["email"]:
                mine = st
        return {
            "id": row["id"], "title": row["title"], "starts": row["starts"],
            "duration_min": row["duration_min"], "note": row["note"],
            "created_by": row["created_by"], "counts": counts, "names": names, "mine": mine,
        }

    return {
        "raids": [pack(r) for r in rows],
        "past": [dict(r) for r in past],
        "can_plan": _user_role(user) in ("officer", "admin"),
    }


@router.post("/api/raids")
def create_raid(payload: RaidRequest, request: Request):
    user = _require_officer(request)
    starts = float(payload.starts)
    if starts < time.time() - 3600:
        raise HTTPException(400, "La date du raid est déjà passée.")
    title = payload.title.strip()[:120] or "Raid de guilde"
    with _db_lock, _db() as conn:
        cur = conn.execute(
            "INSERT INTO raids (title, starts, duration_min, note, created_by, created) VALUES (?,?,?,?,?,?)",
            (title, starts, int(payload.duration_min), payload.note.strip()[:300],
             user["name"] or user["email"], time.time()),
        )
        rid = int(cur.lastrowid or 0)
    return {"ok": True, "id": rid}


@router.delete("/api/raids/{rid}")
def delete_raid(rid: int, request: Request):
    _require_officer(request)
    with _db_lock, _db() as conn:
        conn.execute("DELETE FROM raids WHERE id=?", (rid,))
        conn.execute("DELETE FROM raid_signups WHERE raid_id=?", (rid,))
    return {"ok": True}


@router.post("/api/raids/{rid}/signup")
def raid_signup(rid: int, payload: SignupRequest, request: Request):
    user = _require_user(request)
    st = payload.status.strip().lower()
    if st not in ("yes", "no", "maybe", ""):
        raise HTTPException(400, "Réponse inconnue.")
    with _db_lock, _db() as conn:
        if conn.execute("SELECT id FROM raids WHERE id=?", (rid,)).fetchone() is None:
            raise HTTPException(404, "Raid inconnu.")
        if st == "":
            conn.execute("DELETE FROM raid_signups WHERE raid_id=? AND user_email=?", (rid, user["email"]))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO raid_signups (raid_id, user_email, status, updated) VALUES (?,?,?,?)",
                (rid, user["email"], st, time.time()),
            )
    return {"ok": True}

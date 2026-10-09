"""Cohors — saved /simc profiles (private or shared with the guild) and group simulations."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.auth import _client_ip, _require_user
from app.core.config import (
    ITER_CHOICES, MAX_INPUT_CHARS, PER_IP_ACTIVE, PER_IP_COOLDOWN_S, PER_USER_ACTIVE, QUEUE_MAX, REPORTS_DIR,
)
from app.core.db import _db, _db_lock
from app.core.util import _reject_blocked_profile

router = APIRouter()


MAX_PROFILES = 20


class ProfileRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    input: str = Field(..., min_length=30, max_length=MAX_INPUT_CHARS)
    shared: bool = False


class ProfilePatch(BaseModel):
    name: str | None = Field(None, max_length=60)
    input: str | None = Field(None, min_length=30, max_length=MAX_INPUT_CHARS)
    shared: bool | None = None


def _profile_public(r: sqlite3.Row, mine: bool) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "user_name": r["user_name"],
        "shared": bool(r["shared"]),
        "updated": r["updated"],
        "size": len(r["input"] or ""),
        "mine": mine,
    }


@router.get("/api/profiles")
def list_profiles(request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        mine = conn.execute(
            "SELECT * FROM profiles WHERE user_email=? ORDER BY updated DESC LIMIT 100", (user["email"],)
        ).fetchall()
        shared = conn.execute(
            "SELECT * FROM profiles WHERE shared=1 AND user_email<>? ORDER BY updated DESC LIMIT 100", (user["email"],)
        ).fetchall()
    return {"mine": [_profile_public(r, True) for r in mine], "shared": [_profile_public(r, False) for r in shared]}


@router.get("/api/profiles/{pid}")
def get_profile(pid: int, request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        r = conn.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
    if r is None:
        raise HTTPException(404, "Profil inconnu.")
    mine = r["user_email"] == user["email"]
    if not mine and not r["shared"]:
        raise HTTPException(403, "Ce profil n'est pas partagé.")
    d = _profile_public(r, mine)
    d["input"] = r["input"]
    return d


@router.post("/api/profiles")
def create_profile(payload: ProfileRequest, request: Request):
    user = _require_user(request)
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Nom de profil requis.")
    text = payload.input.replace("\r\n", "\n").strip()
    _reject_blocked_profile(text)
    now = time.time()
    with _db_lock, _db() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM profiles WHERE user_email=?", (user["email"],)).fetchone()["c"]
        if count >= MAX_PROFILES:
            raise HTTPException(400, f"Limite de {MAX_PROFILES} profils atteinte — supprime-en un d'abord.")
        cur = conn.execute(
            "INSERT INTO profiles (user_email, user_name, name, input, shared, created, updated) VALUES (?,?,?,?,?,?,?)",
            (user["email"], user["name"], name, text, 1 if payload.shared else 0, now, now),
        )
        pid = int(cur.lastrowid or 0)
    return {"id": pid, "ok": True}


@router.patch("/api/profiles/{pid}")
def update_profile(pid: int, payload: ProfilePatch, request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        r = conn.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
        if r is None:
            raise HTTPException(404, "Profil inconnu.")
        if r["user_email"] != user["email"]:
            raise HTTPException(403, "Ce profil n'est pas à toi.")
        name = payload.name.strip() if payload.name is not None else r["name"]
        if not name:
            raise HTTPException(400, "Nom de profil requis.")
        text = payload.input.replace("\r\n", "\n").strip() if payload.input is not None else r["input"]
        if payload.input is not None:
            _reject_blocked_profile(text)
        shared = (1 if payload.shared else 0) if payload.shared is not None else r["shared"]
        conn.execute(
            "UPDATE profiles SET name=?, input=?, shared=?, updated=? WHERE id=?",
            (name, text, shared, time.time(), pid),
        )
    return {"ok": True}


@router.delete("/api/profiles/{pid}")
def delete_profile(pid: int, request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        r = conn.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
        if r is None:
            raise HTTPException(404, "Profil inconnu.")
        if r["user_email"] != user["email"] and not user["is_admin"]:
            raise HTTPException(403, "Ce profil n'est pas à toi.")
        conn.execute("DELETE FROM profiles WHERE id=?", (pid,))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Sim de groupe (profils /simc combinés en une seule simulation multi-acteurs)
# ---------------------------------------------------------------------------
_ACTOR_RE = re.compile(r'^[a-z_]+="[^"]+"\s*$')


def _profile_actor(text: str) -> str | None:
    """Nom du personnage (1re ligne acteur hors commentaires) d'un export /simc, ou None."""
    for ln in (text or "").replace("\r\n", "\n").splitlines():
        st = ln.strip()
        if not st or st.startswith("#"):
            continue
        if _ACTOR_RE.match(st):
            return st.split('"')[1]
        return None
    return None


def _build_group_input(rows: list) -> tuple[str, list[str]]:
    """Combine N exports /simc en un seul fichier multi-acteurs (sim de groupe)."""
    warnings: list[str] = []
    blocks: list[str] = []
    seen: set[str] = set()
    for r in rows:
        text = (r["input"] or "").replace("\r\n", "\n")
        actor = _profile_actor(text)
        if not actor:
            warnings.append(f"{r['name']} : format /simc non reconnu — ignoré.")
            continue
        if actor.lower() in seen:
            warnings.append(f"{r['name']} : {actor} est déjà inclus — doublon ignoré.")
            continue
        seen.add(actor.lower())
        lines: list[str] = []
        started = False
        for ln in text.splitlines():
            st = ln.strip()
            if not started:
                if _ACTOR_RE.match(st):
                    started = True
                else:
                    continue
            lines.append(ln.rstrip())
        blocks.append("\n".join(lines))
    if not blocks:
        raise HTTPException(400, "Aucun profil exploitable dans la sélection.")
    header = (
        "# Sim de groupe — exports /simc combinés\n"
        "fight_style=Patchwerk\n"
        "max_time=300\n"
        "calculate_scale_factors=0\n"
    )
    return header + "\n".join(blocks) + "\n", warnings


class GroupSimRequest(BaseModel):
    ids: list[int]
    iterations: int = 5000
    label: str = ""


@router.post("/api/group/sim")
def submit_group_sim(payload: GroupSimRequest, request: Request):
    user = _require_user(request)
    ids = [int(i) for i in payload.ids][:40]
    if len(ids) < 2:
        raise HTTPException(400, "Sélectionne au moins 2 profils.")
    if payload.iterations not in ITER_CHOICES:
        raise HTTPException(400, f"Valeurs d'itérations acceptées : {', '.join(map(str, ITER_CHOICES))}")
    ip = _client_ip(request)
    now = time.time()
    with _db_lock, _db() as conn:
        ph = ",".join("?" * len(ids))
        rows = conn.execute(f"SELECT * FROM profiles WHERE id IN ({ph})", ids).fetchall()
        allowed = [r for r in rows if r["user_email"] == user["email"] or r["shared"]]
        if len(allowed) != len(rows):
            raise HTTPException(403, "Un des profils n'est pas accessible.")
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM sims WHERE ip=? AND status IN ('queued','running')", (ip,)
        ).fetchone()["c"]
        if active >= PER_IP_ACTIVE:
            raise HTTPException(429, f"Tu as déjà {active} simulation(s) en attente — patiente un peu.")
        user_active = conn.execute(
            "SELECT COUNT(*) AS c FROM sims WHERE user_email=? AND status IN ('queued','running')",
            (user["email"],),
        ).fetchone()["c"]
        if user_active >= PER_USER_ACTIVE:
            raise HTTPException(429, f"Tu as déjà {user_active} simulation(s) en attente — patiente un peu.")
        last_ts = conn.execute("SELECT MAX(created) AS m FROM sims WHERE ip=?", (ip,)).fetchone()["m"]
        if last_ts and now - last_ts < PER_IP_COOLDOWN_S:
            wait = int(PER_IP_COOLDOWN_S - (now - last_ts)) + 1
            raise HTTPException(429, f"Doucement ! Réessaie dans {wait} s.")
        queue_len = conn.execute("SELECT COUNT(*) AS c FROM sims WHERE status IN ('queued','running')").fetchone()["c"]
        if queue_len >= QUEUE_MAX:
            raise HTTPException(503, "La file est pleine, réessaie dans quelques minutes.")
        text, warnings = _build_group_input(allowed)
        if len(text) > MAX_INPUT_CHARS:
            raise HTTPException(400, "Profils trop volumineux pour une sim combinée — retire quelques profils.")
        _reject_blocked_profile(text)
        input_hash = hashlib.sha256(f"group\n{payload.iterations}\n{text}".encode()).hexdigest()
        cached = conn.execute(
            "SELECT * FROM sims WHERE input_hash=? AND status='done' ORDER BY finished DESC LIMIT 1", (input_hash,)
        ).fetchone()
        sim_id = uuid.uuid4().hex[:20]
        sim_dir = REPORTS_DIR / sim_id
        sim_dir.mkdir(parents=True, exist_ok=True)
        input_file = sim_dir / "input.simc"
        input_file.write_text(text)
        label = payload.label.strip()[:60] or f"Sim de groupe ({len(allowed)} profils)"
        if cached:
            conn.execute(
                """INSERT INTO sims (id, created, ip, label, iterations, status, input_hash, input_file, cached_from,
                                     dps, dps_error_pct, wall_s, report_html, report_json, started, finished,
                                     user_email, user_name, kind, gear)
                   VALUES (?,?,?,?,?, 'done', ?,?,?,?,?,?,?,?,?,?,?,?,'group',?)""",
                (sim_id, now, ip, label, payload.iterations, input_hash, str(input_file), cached["id"],
                 None, None, cached["wall_s"], cached["report_html"], cached["report_json"],
                 now, now, user["email"], user["name"], cached["gear"]),
            )
            return {"id": sim_id, "status": "done", "cached": True, "warnings": warnings, "count": len(allowed)}
        conn.execute(
            "INSERT INTO sims (id, created, ip, label, iterations, status, input_hash, input_file, user_email, user_name, kind) "
            "VALUES (?,?,?,?,?, 'queued', ?,?,?,?, 'group')",
            (sim_id, now, ip, label, payload.iterations, input_hash, str(input_file), user["email"], user["name"]),
        )
    return {"id": sim_id, "status": "queued", "position": queue_len + 1, "warnings": warnings, "count": len(allowed)}

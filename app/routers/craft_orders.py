"""Cohors — craft orders: a member asks a guild crafter for an item; both follow the order and get notified."""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import bnet
from app.core.auth import _require_user
from app.core.db import _db, _db_lock

router = APIRouter()

ORDERS_OPEN_MAX = 20                      # demandes en cours par joueur
ORDER_STATUSES = ("open", "accepted", "done", "declined", "cancelled")
# Transitions permises : l'artisan fait avancer la commande, le demandeur peut l'annuler.
CRAFTER_MOVES = {"open": ("accepted", "declined", "done"), "accepted": ("done", "declined")}
REQUESTER_MOVES = {"open": ("cancelled",), "accepted": ("cancelled",)}


class CraftOrderNew(BaseModel):
    crafter: str = Field(..., min_length=2, max_length=60)
    realm: str = Field("", max_length=60)
    item_id: int = Field(0, ge=0, le=10_000_000)
    item: str = Field(..., min_length=1, max_length=120)
    profession: str = Field("", max_length=60)
    note: str = Field("", max_length=200)


class CraftOrderMove(BaseModel):
    status: str = Field(..., max_length=20)


def _display_names(conn) -> dict[str, str]:
    """E-mail → nom affiché : le main déclaré, sinon le nom du compte."""
    out = {r["email"]: (r["name"] or r["email"].split("@")[0]) for r in conn.execute(
        "SELECT email, name FROM users").fetchall()}
    for r in conn.execute("SELECT user_email, display, name FROM char_links WHERE is_main=1").fetchall():
        out[r["user_email"]] = r["display"] or r["name"] or out.get(r["user_email"], "")
    return out


def _notify(conn, email: str, event: str, order: dict, who: str) -> None:
    conn.execute("INSERT INTO notifs (email, kind, data, created) VALUES (?,?,?,?)",
                 (email, "craft", json.dumps({"event": event, "item": order["item"], "item_id": order["item_id"],
                                              "who": who, "id": order["id"]}, ensure_ascii=False), time.time()))


def _crafter_disp(key: str) -> str:
    try:
        roster, _t = bnet.roster()
        for m in roster.get("members") or []:
            if (m.get("name") or "").lower() == key:
                return m.get("name") or key.title()
    except bnet.BnetError:
        pass
    return key.title()


@router.post("/api/craft/orders")
def api_craft_order_new(body: CraftOrderNew, request: Request):
    """Demande un objet à un artisan ; son compte reçoit une notification."""
    user = _require_user(request)
    key = body.crafter.strip().lower()
    item = " ".join(body.item.split())
    now = time.time()
    with _db_lock, _db() as conn:
        owner = conn.execute("SELECT user_email FROM char_links WHERE name=? ORDER BY is_main DESC LIMIT 1",
                             (key,)).fetchone()
        if owner is None:
            raise HTTPException(400, "Cet artisan n'a pas de compte Cohors lié — contacte-le en jeu.")
        if owner["user_email"] == user["email"]:
            raise HTTPException(400, "C'est l'un de tes propres personnages.")
        n_open = conn.execute("SELECT COUNT(*) AS n FROM craft_orders WHERE requester=? AND status IN ('open','accepted')",
                              (user["email"],)).fetchone()["n"]
        if n_open >= ORDERS_OPEN_MAX:
            raise HTTPException(400, f"Tu as déjà {ORDERS_OPEN_MAX} demandes en cours.")
        dup = conn.execute("SELECT 1 AS x FROM craft_orders WHERE requester=? AND crafter=? AND item=? "
                           "AND status IN ('open','accepted')", (user["email"], key, item)).fetchone()
        if dup:
            raise HTTPException(400, "Tu as déjà demandé cet objet à cet artisan.")
        cur = conn.execute(
            "INSERT INTO craft_orders (requester, crafter, crafter_realm, crafter_email, item_id, item, profession, note, "
            "status, created, updated) VALUES (?,?,?,?,?,?,?,?,'open',?,?)",
            (user["email"], key, body.realm.strip().lower(), owner["user_email"], body.item_id, item,
             body.profession.strip(), body.note.strip(), now, now))
        order = {"id": cur.lastrowid, "item": item, "item_id": body.item_id}
        _notify(conn, owner["user_email"], "new", order, _display_names(conn).get(user["email"], ""))
    return {"ok": True, "id": order["id"]}


@router.get("/api/craft/orders")
def api_craft_orders(request: Request):
    """Mes demandes et les commandes à fabriquer pour mes personnages (les plus récentes d'abord)."""
    user = _require_user(request)
    with _db_lock, _db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM craft_orders WHERE requester=? OR crafter_email=? ORDER BY updated DESC LIMIT 200",
            (user["email"], user["email"])).fetchall()]
        names = _display_names(conn)
    disp: dict[str, str] = {}
    try:
        roster, _t = bnet.roster()
        disp = {(m.get("name") or "").lower(): m.get("name") or "" for m in (roster.get("members") or [])}
    except bnet.BnetError:
        pass
    mine, todo = [], []
    for r in rows:
        o = {"id": r["id"], "item": r["item"], "item_id": r["item_id"], "profession": r["profession"],
             "note": r["note"], "status": r["status"], "created": r["created"], "updated": r["updated"],
             "crafter": disp.get(r["crafter"]) or r["crafter"].title(), "crafter_key": r["crafter"],
             "crafter_realm": r["crafter_realm"], "requester": names.get(r["requester"], "")}
        if r["requester"] == user["email"]:
            mine.append(o)
        if r["crafter_email"] == user["email"]:
            todo.append(o)
    return {"mine": mine, "todo": todo,
            "open_todo": sum(1 for o in todo if o["status"] in ("open", "accepted"))}


@router.post("/api/craft/orders/{oid}")
def api_craft_order_move(oid: int, body: CraftOrderMove, request: Request):
    """Fait avancer une commande : l'artisan accepte / refuse / marque « fait », le demandeur annule."""
    user = _require_user(request)
    new = body.status.strip()
    if new not in ORDER_STATUSES:
        raise HTTPException(400, "Statut inconnu.")
    with _db_lock, _db() as conn:
        r = conn.execute("SELECT * FROM craft_orders WHERE id=?", (oid,)).fetchone()
    if r is None or user["email"] not in (r["requester"], r["crafter_email"]):
        raise HTTPException(404, "Commande introuvable.")
    allowed = set()
    if user["email"] == r["crafter_email"]:
        allowed |= set(CRAFTER_MOVES.get(r["status"], ()))
    if user["email"] == r["requester"]:
        allowed |= set(REQUESTER_MOVES.get(r["status"], ()))
    if new not in allowed:
        raise HTTPException(400, "Changement impossible pour cette commande.")
    by_crafter = user["email"] == r["crafter_email"]
    crafter_name = _crafter_disp(r["crafter"]) if by_crafter else ""
    with _db_lock, _db() as conn:
        cur = conn.execute("UPDATE craft_orders SET status=?, updated=? WHERE id=? AND status=?",
                           (new, time.time(), oid, r["status"]))
        if cur.rowcount != 1:
            raise HTTPException(409, "La commande vient de changer — recharge la page.")
        other = r["requester"] if by_crafter else r["crafter_email"]
        who = crafter_name if by_crafter else _display_names(conn).get(user["email"], "")
        _notify(conn, other, new, {"id": oid, "item": r["item"], "item_id": r["item_id"]}, who)
    return {"ok": True, "status": new}

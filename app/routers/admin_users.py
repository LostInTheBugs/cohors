"""Cohors — admin: user accounts (list, activate, role, delete, reset link) and invitations."""
from __future__ import annotations

import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import mailer
from app.core.auth import _require_admin, _require_officer
from app.core.brand import _brand_identity
from app.core.config import INVITE_TTL_DAYS, PUBLIC_BASE_URL
from app.core.db import _db, _db_lock

router = APIRouter()


class InviteRequest(BaseModel):
    email: str = Field("", max_length=200)
    note: str = Field("", max_length=120)
    send_email: bool = False


class ActiveRequest(BaseModel):
    active: bool


def _invite_link(token: str) -> str:
    base = PUBLIC_BASE_URL or ""
    return f"{base}/invite/{token}"


@router.get("/api/admin/users")
def admin_users(request: Request):
    _require_admin(request)
    with _db_lock, _db() as conn:
        rows = conn.execute(
            """SELECT u.id, u.email, u.name, u.is_admin, u.role, u.active, u.created, u.last_login,
                      (SELECT COUNT(*) FROM sims s WHERE s.user_email = u.email) AS sims_count,
                      (SELECT c.display FROM char_links c WHERE c.user_email = u.email AND c.is_main = 1 LIMIT 1) AS main_char,
                      (SELECT COUNT(*) FROM char_links c WHERE c.user_email = u.email) AS chars_count
               FROM users u ORDER BY u.created""",
        ).fetchall()
    return {"users": [dict(r) for r in rows]}


@router.post("/api/admin/users/{uid}/active")
def admin_set_active(uid: int, payload: ActiveRequest, request: Request):
    me_row = _require_admin(request)
    if uid == me_row["id"]:
        raise HTTPException(400, "Impossible de modifier ton propre compte.")
    with _db_lock, _db() as conn:
        if conn.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone() is None:
            raise HTTPException(404, "Compte inconnu")
        conn.execute("UPDATE users SET active=? WHERE id=?", (1 if payload.active else 0, uid))
        if not payload.active:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    return {"ok": True}


@router.delete("/api/admin/users/{uid}")
def admin_delete_user(uid: int, request: Request):
    me_row = _require_admin(request)
    if uid == me_row["id"]:
        raise HTTPException(400, "Impossible de supprimer ton propre compte.")
    with _db_lock, _db() as conn:
        u = conn.execute("SELECT email FROM users WHERE id=?", (uid,)).fetchone()
        if u is None:
            raise HTTPException(404, "Compte inconnu")
        conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM char_links WHERE user_email=?", (u["email"],))
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
    return {"ok": True}


@router.post("/api/admin/users/{uid}/reset-link")
def admin_reset_link(uid: int, request: Request):
    _require_admin(request)
    now = time.time()
    with _db_lock, _db() as conn:
        u = conn.execute("SELECT email, name FROM users WHERE id=?", (uid,)).fetchone()
        if u is None:
            raise HTTPException(404, "Compte inconnu")
        token = secrets.token_urlsafe(24)
        conn.execute(
            "INSERT INTO invites (token, email, note, created, expires) VALUES (?,?,?,?,?)",
            (token, u["email"], f"réinitialisation — {u['name']}", now, now + INVITE_TTL_DAYS * 86400),
        )
    return {"link": _invite_link(token), "expires_in_days": INVITE_TTL_DAYS}


class RoleRequest(BaseModel):
    role: str = Field(..., max_length=20)


@router.post("/api/admin/users/{uid}/role")
def admin_set_role(uid: int, payload: RoleRequest, request: Request):
    """Change le rôle d'un compte (réservé aux administrateurs)."""
    me_row = _require_admin(request)
    role = payload.role.strip().lower()
    if role not in ("member", "officer", "admin"):
        raise HTTPException(400, "Rôle inconnu (membre, officier ou administrateur).")
    if uid == me_row["id"]:
        raise HTTPException(400, "Impossible de modifier ton propre rôle.")
    with _db_lock, _db() as conn:
        if conn.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone() is None:
            raise HTTPException(404, "Compte inconnu")
        conn.execute(
            "UPDATE users SET role=?, is_admin=? WHERE id=?",
            (role, 1 if role == "admin" else 0, uid),
        )
    return {"ok": True, "role": role}


@router.get("/api/admin/invites")
def admin_invites(request: Request):
    _require_officer(request)
    now = time.time()
    with _db_lock, _db() as conn:
        rows = conn.execute("SELECT * FROM invites ORDER BY created DESC LIMIT 50").fetchall()
    out = []
    for r in rows:
        if r["used"] is not None:
            status = "used"
        elif r["expires"] < now:
            status = "expired"
        else:
            status = "pending"
        out.append({
            "token": r["token"], "email": r["email"], "note": r["note"], "created": r["created"],
            "expires": r["expires"], "status": status, "used_by": r["used_by"],
            "link": _invite_link(r["token"]),
        })
    return {"invites": out, "smtp_configured": mailer.smtp_configured()}


@router.post("/api/admin/invites")
def admin_create_invite(payload: InviteRequest, request: Request):
    _require_officer(request)
    now = time.time()
    token = secrets.token_urlsafe(24)
    email = payload.email.strip().lower() or None
    with _db_lock, _db() as conn:
        conn.execute(
            "INSERT INTO invites (token, email, note, created, expires) VALUES (?,?,?,?,?)",
            (token, email, payload.note.strip()[:120], now, now + INVITE_TTL_DAYS * 86400),
        )
    mail_result = None
    if payload.send_email and email:
        try:
            ident = _brand_identity()
            text, html = mailer.invite_mail(_invite_link(token), INVITE_TTL_DAYS, **ident)
            mailer.send_mail(email, f"Invitation — {ident['short_name'] or ident['guild_name']}", text, html)
            mail_result = {"sent": True, "to": email}
        except mailer.MailError as exc:
            mail_result = {"sent": False, "error": str(exc)}
    return {"token": token, "link": _invite_link(token), "expires_in_days": INVITE_TTL_DAYS, "mail": mail_result}


@router.post("/api/admin/invites/{token}/send")
def admin_send_invite(token: str, request: Request):
    _require_officer(request)
    with _db_lock, _db() as conn:
        r = conn.execute("SELECT * FROM invites WHERE token=?", (token,)).fetchone()
    if r is None:
        raise HTTPException(404, "Invitation inconnue.")
    if r["used"] is not None or r["expires"] < time.time():
        raise HTTPException(400, "Invitation déjà utilisée ou expirée.")
    if not r["email"]:
        raise HTTPException(400, "Cette invitation est un lien libre (sans e-mail).")
    try:
        ident = _brand_identity()
        text, html = mailer.invite_mail(_invite_link(token), INVITE_TTL_DAYS, **ident)
        mailer.send_mail(r["email"], f"Invitation — {ident['short_name'] or ident['guild_name']}", text, html)
    except mailer.MailError as exc:
        raise HTTPException(502, str(exc))
    return {"ok": True, "sent_to": r["email"]}


@router.delete("/api/admin/invites/{token}")
def admin_revoke_invite(token: str, request: Request):
    _require_officer(request)
    with _db_lock, _db() as conn:
        conn.execute("DELETE FROM invites WHERE token=? AND used IS NULL", (token,))
    return {"ok": True}

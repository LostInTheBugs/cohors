"""Cohors — admin: SMTP settings (read without password, save after a live check, test e-mail)."""
from __future__ import annotations

import os
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import mailer, secretbox
from app.core.auth import _require_admin
from app.core.brand import _brand_identity
from app.core.db import _db, _db_lock
from app.services.mail_settings import MAIL_KEYS, _apply_mail_config, _mail_rows

router = APIRouter()


@router.get("/api/admin/mail")
def admin_mail_get(request: Request):
    _require_admin(request)
    rows = _mail_rows()
    cfg = mailer._config()
    env_ok = bool(os.environ.get("SMTP_HOST", "").strip() and os.environ.get("SMTP_USER", "").strip())
    src = "admin" if (rows.get("host") and rows.get("user")) else ("env" if env_ok else "")
    pw = rows.get("password") or ""
    # undecryptable: lire la valeur brute en base
    with _db_lock, _db() as conn:
        raw_pw_row = conn.execute("SELECT value FROM mail_config WHERE key='password'").fetchone()
        undecryptable_pw = bool(raw_pw_row) and secretbox.undecryptable(raw_pw_row["value"])
    # password est exclu de config pour ne pas l'exposer en clair
    public_cfg = {k: rows.get(k, "") for k in MAIL_KEYS if k != "password"}
    return {
        "config": public_cfg,
        "password_hint": ("•" * 6 + pw[-4:]) if len(pw) >= 4 else ("•" * len(pw) if pw else ""),
        "configured": cfg is not None,
        "source": src,
        "env_available": env_ok,
        "effective": {"host": (cfg or {}).get("host", ""), "port": (cfg or {}).get("port", ""),
                      "mode": (cfg or {}).get("mode", ""), "sender": (cfg or {}).get("sender", "")},
        "undecryptable": undecryptable_pw,
    }


class MailConfigRequest(BaseModel):
    values: dict[str, str] = {}
    clear: bool = False


@router.post("/api/admin/mail")
def admin_mail_save(payload: MailConfigRequest, request: Request):
    _require_admin(request)
    if payload.clear:
        with _db_lock, _db() as conn:
            conn.execute("DELETE FROM mail_config")
        _apply_mail_config()
        return {"ok": True, "cleared": True}
    values = {k: str(v).strip() for k, v in (payload.values or {}).items() if k in MAIL_KEYS}
    if not values:
        raise HTTPException(400, "Aucune valeur à enregistrer.")
    rows = dict(_mail_rows())
    if values.get("password", None) == "":
        values.pop("password")  # mot de passe vide = inchangé
    rows.update(values)
    if not (rows.get("host") and rows.get("user")):
        raise HTTPException(400, "Serveur et identifiant sont obligatoires.")
    try:
        port = int(rows.get("port") or 587)
    except (TypeError, ValueError):
        raise HTTPException(400, "Port invalide.")
    if not (1 <= port <= 65535):
        raise HTTPException(400, "Port entre 1 et 65535.")
    mode = (rows.get("mode") or "starttls").lower()
    if mode not in ("starttls", "ssl", "none"):
        raise HTTPException(400, "Mode de sécurité inconnu.")
    res = mailer.check(rows.get("host", ""), port, mode, rows.get("user", ""),
                       rows.get("password", ""), rows.get("sender", ""), rows.get("helo", ""))
    if not res["ok"]:
        raise HTTPException(400, f"SMTP — {res['detail']}")
    rows["port"] = str(port)
    rows["mode"] = mode
    with _db_lock, _db() as conn:
        for k, v in rows.items():
            val = secretbox.encrypt(v) if k == "password" else v
            conn.execute("INSERT OR REPLACE INTO mail_config (key, value, updated) VALUES (?,?,?)",
                         (k, val, time.time()))
    _apply_mail_config()
    return {"ok": True, "test": res["detail"]}


class MailTestRequest(BaseModel):
    to: str = Field(..., max_length=200)


@router.post("/api/admin/mail/test")
def admin_mail_test(payload: MailTestRequest, request: Request):
    _require_admin(request)
    to = payload.to.strip()
    if "@" not in to or " " in to or len(to) < 6:
        raise HTTPException(400, "Adresse e-mail invalide.")
    ident = _brand_identity()
    title = (ident["short_name"] or ident["guild_name"]).strip()
    try:
        mailer.send_mail(to, f"Test — {title}",
                         f"Ceci est un e-mail de test envoyé depuis {title} "
                         f"({ident['base_url'] or 'le site'}).\n\n"
                         "Si tu reçois ce message, la configuration SMTP fonctionne.",
                         f"<p>Ceci est un e-mail de test envoyé depuis <b>{title}</b>.</p>"
                         "<p>Si tu reçois ce message, la configuration SMTP fonctionne.</p>")
    except mailer.MailError as exc:
        return {"ok": False, "detail": str(exc)}
    return {"ok": True, "detail": f"E-mail de test envoyé à {to}."}

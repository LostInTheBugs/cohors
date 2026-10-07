"""Cohors — SMTP settings stored by the admin (password decrypted on read), applied to the mailer."""
from __future__ import annotations

from app import mailer, secretbox
from app.core.db import _db, _db_lock

MAIL_KEYS = ("host", "port", "mode", "user", "password", "sender", "helo")


def _mail_rows() -> dict:
    with _db_lock, _db() as conn:
        rows = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM mail_config").fetchall()}
    pw = rows.get("password")
    if pw:
        rows["password"] = secretbox.decrypt(pw)
    return rows


def _apply_mail_config() -> None:
    """Applique les réglages SMTP enregistrés (sinon retour aux variables d'environnement)."""
    rows = _mail_rows()
    mailer.set_config(rows if rows.get("host") and rows.get("user") else None)

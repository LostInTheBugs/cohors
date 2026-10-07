"""Cohors — guild identity row (name, short name) used in e-mails, metadata and announcements."""
from __future__ import annotations

import sqlite3
import time

from app.core.config import PUBLIC_BASE_URL
from app.core.db import _db, _db_lock


def _brand_row(conn) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM branding WHERE id=1").fetchone()
    if row is None:
        conn.execute("INSERT INTO branding (id, updated) VALUES (1, ?)", (time.time(),))
        row = conn.execute("SELECT * FROM branding WHERE id=1").fetchone()
    return row


def _brand_identity() -> dict:
    """Identité de guilde pour les e-mails et métadonnées (nom, nom court, adresse du site)."""
    name, short = "la guilde", ""
    try:
        with _db_lock, _db() as conn:
            row = _brand_row(conn)
        name = (row["guild_name"] or "").strip() or "la guilde"
        short = (row["guild_short"] or "").strip()
    except sqlite3.Error:
        pass
    return {"guild_name": name, "short_name": short, "base_url": PUBLIC_BASE_URL}


_IMG_SIGS = ((b"\x89PNG\r\n\x1a\n", "png", "image/png"),
             (b"\xff\xd8\xff", "jpg", "image/jpeg"),
             (b"GIF87a", "gif", "image/gif"),
             (b"GIF89a", "gif", "image/gif"),
             (b"RIFF", "webp", "image/webp"))


def _img_type(raw: bytes):
    """(extension, type MIME) d'après la signature du fichier, sinon ('', '')."""
    for sig, ext, mime in _IMG_SIGS:
        if raw.startswith(sig):
            if sig == b"RIFF" and raw[8:12] != b"WEBP":
                continue
            return ext, mime
    return "", ""

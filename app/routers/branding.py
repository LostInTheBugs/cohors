"""Cohors — guild identity: public branding (names, logo, background) and admin upload/reset."""
from __future__ import annotations

import re
import time

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app.core.auth import _require_admin
from app.core.brand import _brand_files, _brand_row, _img_type
from app.core.config import BRAND_DIR, STATIC_DIR
from app.core.db import _db, _db_lock

router = APIRouter()


_IMG_MIMES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


@router.get("/api/branding")
def api_branding(request: Request):
    """Identité publique (page de connexion incluse) : noms, logo et fond effectifs."""
    with _db_lock, _db() as conn:
        row = _brand_row(conn)
    d = dict(row)
    files = _brand_files()
    short = (d.get("guild_short") or "Cohors").strip()[:24] or "Cohors"
    name = (d.get("guild_name") or "").strip()[:60]
    v = int(d.get("updated") or 0)
    return {
        "name": name, "short": short,
        "logo": "/branding/logo?v=" + str(v),
        "bg": ("/branding/bg?v=" + str(v)) if "bg" in files else "",
        "bg_color": (d.get("bg_color") or "").strip(),
        "custom_logo": "logo" in files, "custom_bg": "bg" in files,
        "raw": {"name": d.get("guild_name") or "", "short": d.get("guild_short") or "",
                "color": d.get("bg_color") or ""},
    }


@router.get("/branding/logo")
def branding_logo():
    p = _brand_files().get("logo")
    if p is not None:
        return FileResponse(p, media_type=_IMG_MIMES.get(p.suffix.lower().lstrip("."), "image/png"),
                            headers={"Cache-Control": "no-cache"})
    return FileResponse(STATIC_DIR / "logo.png", media_type="image/png",
                        headers={"Cache-Control": "no-cache"})


@router.get("/branding/bg")
def branding_bg():
    p = _brand_files().get("bg")
    if p is None:
        raise HTTPException(404, "Pas de fond personnalisé")
    return FileResponse(p, media_type=_IMG_MIMES.get(p.suffix.lower().lstrip("."), "image/png"),
                        headers={"Cache-Control": "no-cache"})


@router.post("/api/admin/branding")
async def api_admin_branding(
        request: Request,
        guild_name: str = Form(""), guild_short: str = Form(""), bg_color: str = Form(""),
        reset_logo: int = Form(0), reset_bg: int = Form(0), reset_color: int = Form(0),
        reset_names: int = Form(0), reset_all: int = Form(0),
        logo: UploadFile = File(None), bg: UploadFile = File(None)):
    """Met à jour l'identité de la guilde (administrateur)."""
    user = _require_admin(request)
    if reset_all:
        reset_logo = reset_bg = reset_color = reset_names = 1
    gn = guild_name.strip()[:60]
    gs = guild_short.strip()[:24]
    col = bg_color.strip().lower()
    if col and not re.match(r"^#([0-9a-f]{3}|[0-9a-f]{6})$", col):
        raise HTTPException(400, "Couleur de fond invalide (ex. #0b0f17).")
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    for uf, key in ((logo, "logo"), (bg, "bg")):
        if uf is None:
            continue
        raw = await uf.read()
        if not raw:
            continue
        if len(raw) > 2 * 1024 * 1024:
            raise HTTPException(413, "Image trop lourde (2 Mo max).")
        ext, _mime = _img_type(raw)
        if not ext:
            raise HTTPException(400, "Format d'image non reconnu (PNG, JPEG, GIF ou WebP).")
        for old in BRAND_DIR.glob(key + ".*"):
            old.unlink(missing_ok=True)
        (BRAND_DIR / f"{key}.{ext}").write_bytes(raw)
    if reset_logo:
        for old in BRAND_DIR.glob("logo.*"):
            old.unlink(missing_ok=True)
    if reset_bg:
        for old in BRAND_DIR.glob("bg.*"):
            old.unlink(missing_ok=True)
    with _db_lock, _db() as conn:
        _brand_row(conn)
        conn.execute(
            "UPDATE branding SET guild_name=?, guild_short=?, bg_color=?, updated=?, updated_by=? WHERE id=1",
            ("" if reset_names else gn, "" if reset_names else gs,
             "" if reset_color else col, time.time(), user["email"]))
    return {"ok": True}

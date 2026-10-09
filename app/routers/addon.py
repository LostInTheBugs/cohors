"""Cohors — download of the in-game WoW addon (zip) that exports the guild calendar."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response

from app.core.auth import _require_user

router = APIRouter()


@router.get("/api/addon")
def api_addon(request: Request):
    """Addon WoW « Cohors » (zip) — collecte le calendrier de guilde en jeu."""
    _require_user(request)
    import io as _io
    import zipfile as _zip
    src = Path(__file__).resolve().parents[2] / "addon" / "Cohors"
    if not src.is_dir():
        raise HTTPException(404, "Addon introuvable sur le serveur.")
    buf = _io.BytesIO()
    with _zip.ZipFile(buf, "w", _zip.ZIP_DEFLATED) as z:
        for fp in sorted(src.glob("*")):
            if fp.is_file():
                z.write(fp, f"Cohors/{fp.name}")
    buf.seek(0)
    return Response(buf.read(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="Cohors-addon.zip"'})

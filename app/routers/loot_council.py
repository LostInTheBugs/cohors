"""Cohors — loot council: export for the in-game add-on (who wants an item, what it brings them)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.core.auth import _require_officer
from app.core.brand import _brand_identity
from app.services.lootcouncil import council_export

router = APIRouter()


@router.get("/api/loot/export")
def api_loot_export(request: Request):
    """Officiers : texte « CohorsLC1 » à coller dans l'add-on avant le raid (onglet Loot)."""
    _require_officer(request)
    body = council_export(_brand_identity()["guild_name"])
    return Response(body, media_type="text/plain; charset=utf-8",
                    headers={"Cache-Control": "no-store",
                             "Content-Disposition": 'attachment; filename="cohors-loot.txt"'})

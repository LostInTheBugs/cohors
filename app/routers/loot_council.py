"""Cohors — loot council: export for the in-game add-on (who wants an item, what it brings them)
and the raid loot history the add-on notes (imported by an officer)."""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from app.core.auth import _require_officer, _require_user
from app.core.brand import _brand_identity
from app.services import loothistory
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


class LootHistoryImport(BaseModel):
    text: str


@router.post("/api/loot/history")
def api_loot_history_import(payload: LootHistoryImport, request: Request):
    """Officiers : butin noté par l'addon (onglet Loot → « Soirée »), texte « CohorsLOOT1 »."""
    user = _require_officer(request)
    if len(payload.text or "") > 400_000:
        raise HTTPException(413, "Texte trop long")
    try:
        return loothistory.import_text(payload.text, user["email"] or "")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/api/loot/history/char/{name}")
def api_loot_history_char(name: str, request: Request):
    """Butin de raid reçu par un personnage (90 derniers jours) — fiche personnage."""
    _require_user(request)
    rows = loothistory.for_char(name)
    since = time.time() - loothistory.LH_DAYS * 86400
    return {"items": rows, "n30": sum(1 for r in rows if r["at"] >= since), "days": loothistory.LH_DAYS}

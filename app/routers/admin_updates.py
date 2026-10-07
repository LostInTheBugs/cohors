"""Cohors — admin: update panel (state, auto-check settings, check now, request / cancel an update)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.auth import _require_admin
from app.core.config import VERSION
from app.services.updates import (
    UPD_CHECK_VALUES, _UPD_REQUEST, _upd_request_write, _upd_run_check, _upd_set, _upd_state,
)

router = APIRouter()


class UpdateSettings(BaseModel):
    values: dict[str, str] = {}


@router.get("/api/admin/updates")
def api_admin_updates(request: Request):
    """État des mises à jour : version installée, dernière release connue, demande, applicateur."""
    _require_admin(request)
    return {"ok": True, "state": _upd_state()}


@router.post("/api/admin/updates")
def api_admin_updates_save(payload: UpdateSettings, request: Request):
    """Réglages : cadence de vérification et application automatique."""
    _require_admin(request)
    clean = {}
    for key, value in (payload.values or {}).items():
        if key == "upd_check_h":
            try:
                hours = int(float(str(value)))
            except (TypeError, ValueError):
                raise HTTPException(400, "Cadence de vérification invalide")
            if hours not in UPD_CHECK_VALUES:
                raise HTTPException(400, "Cadence de vérification hors bornes")
            clean[key] = str(hours)
        elif key == "upd_apply_auto":
            if str(value) not in ("0", "1"):
                raise HTTPException(400, "Application automatique : 0 ou 1 attendu")
            clean[key] = str(value)
    if clean:
        _upd_set(clean)
    return {"ok": True, "state": _upd_state()}


@router.post("/api/admin/updates/check")
def api_admin_updates_check(request: Request):
    """Vérifie tout de suite la dernière release (un appel réseau)."""
    _require_admin(request)
    return {"ok": True, "state": _upd_run_check()}


@router.post("/api/admin/updates/apply")
def api_admin_updates_apply(request: Request):
    """Dépose une demande de mise à jour ; l'applicateur de l'hôte l'applique (jamais le conteneur)."""
    user = _require_admin(request)
    st = _upd_state()
    if not st["latest"]:
        raise HTTPException(400, "Vérifie d'abord les mises à jour disponibles")
    if not st["available"]:
        raise HTTPException(400, f"Déjà à jour ({VERSION})")
    _upd_request_write(st["latest"]["version"], str(user["email"]))
    return {"ok": True, "state": _upd_state()}


@router.delete("/api/admin/updates/request")
def api_admin_updates_cancel(request: Request):
    """Annule une demande de mise à jour en attente."""
    _require_admin(request)
    try:
        _UPD_REQUEST.unlink()
    except FileNotFoundError:
        pass
    return {"ok": True, "state": _upd_state()}

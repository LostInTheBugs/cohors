"""Cohors — admin: Battle.net / Warcraft Logs API keys (read masked, save, test)."""
from __future__ import annotations

import os
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import bnet, secretbox, wcl
from app.core.auth import _require_admin
from app.core.db import _db, _db_lock
from app.services.api_keys import API_PROVIDERS, _api_effective, _api_keys_rows, _apply_api_keys, _mask

router = APIRouter()


class ApiKeyRequest(BaseModel):
    """Clés d'un fournisseur : secret vide = inchangé ; clear=True = retour à l'environnement."""

    provider: str = Field(..., max_length=20)
    client_id: str | None = Field(None, max_length=200)
    client_secret: str = Field("", max_length=400)
    clear: bool = False


class ApiKeyTestRequest(BaseModel):
    provider: str = Field(..., max_length=20)


@router.get("/api/admin/api-keys")
def admin_api_keys_get(request: Request):
    _require_admin(request)
    rows = _api_keys_rows()
    out = []
    # raw values for undecryptable check
    raw_map: dict[str, str] = {}
    with _db_lock, _db() as conn:
        for r in conn.execute("SELECT provider, client_secret FROM api_keys").fetchall():
            raw_map[r["provider"]] = r["client_secret"]
    for prov, meta in API_PROVIDERS.items():
        cid, secret, source = _api_effective(prov)
        row = rows.get(prov)
        raw_cs = raw_map.get(prov)
        out.append({
            "provider": prov,
            "label": meta["label"],
            "help_url": meta["help_url"],
            "help_fr": meta["help_fr"],
            "configured": bool(cid and secret),
            "source": source,  # admin | env | ""
            "id_hint": (cid[:8] + "…" + cid[-4:]) if len(cid) > 14 else cid,
            "secret_hint": _mask(secret),
            "updated": (row["updated"] if row else 0),
            "env_available": bool((os.environ.get(f"{'BNET' if prov == 'bnet' else 'WCL'}_CLIENT_ID", "").strip()
                                   and os.environ.get(f"{'BNET' if prov == 'bnet' else 'WCL'}_CLIENT_SECRET", "").strip())),
            "undecryptable": bool(raw_cs) and secretbox.undecryptable(raw_cs),
        })
    return {"providers": out}


@router.post("/api/admin/api-keys")
def admin_api_keys_save(payload: ApiKeyRequest, request: Request):
    _require_admin(request)
    prov = payload.provider.strip().lower()
    if prov not in API_PROVIDERS:
        raise HTTPException(400, "Fournisseur inconnu.")
    mod = bnet if prov == "bnet" else wcl
    if payload.clear:
        with _db_lock, _db() as conn:
            conn.execute("DELETE FROM api_keys WHERE provider=?", (prov,))
        _apply_api_keys()
        return {"ok": True, "cleared": True}
    cid, secret, _src = _api_effective(prov)
    new_id = (payload.client_id or "").strip() or cid
    new_secret = payload.client_secret.strip() or secret
    if not new_id or not new_secret:
        raise HTTPException(400, "Client ID et secret sont requis (le secret existant est conservé si le champ est vide).")
    res = mod.check(new_id, new_secret)
    if not res.get("ok"):
        raise HTTPException(400, f'{API_PROVIDERS[prov]["label"]} — {res.get("detail")}')
    if not (payload.client_id or "").strip() and not payload.client_secret.strip():
        raise HTTPException(400, "Rien à enregistrer (champs vides).")
    with _db_lock, _db() as conn:
        conn.execute("INSERT OR REPLACE INTO api_keys (provider, client_id, client_secret, updated) VALUES (?,?,?,?)",
                     (prov, new_id, secretbox.encrypt(new_secret), time.time()))
    _apply_api_keys()
    return {"ok": True}


@router.post("/api/admin/api-keys/test")
def admin_api_keys_test(payload: ApiKeyTestRequest, request: Request):
    _require_admin(request)
    prov = payload.provider.strip().lower()
    if prov not in API_PROVIDERS:
        raise HTTPException(400, "Fournisseur inconnu.")
    cid, secret, source = _api_effective(prov)
    if not cid or not secret:
        return {"ok": False, "detail": "Aucune clé configurée.", "source": source}
    mod = bnet if prov == "bnet" else wcl
    res = mod.check(cid, secret)
    res["source"] = source
    return res

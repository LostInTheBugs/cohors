"""Cohors — admin: guild identity (Battle.net region, realm, slug, data locale, Warcraft Logs) and live check."""
from __future__ import annotations

import re
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import bnet, wcl
from app.core.auth import _require_admin
from app.core.db import _db, _db_lock
from app.services.guild_settings import _apply_guild_config, _guild_effective, _guild_rows

router = APIRouter()


GUILD_KEYS = ("region", "realm", "slug", "locale", "wcl_region", "wcl_name")
GUILD_BNET_REGIONS = ("eu", "us", "kr", "tw")
GUILD_WCL_REGIONS = ("EU", "US", "KR", "TW", "CN")
GUILD_LOCALES = ("en_US", "es_MX", "pt_BR", "en_GB", "es_ES", "fr_FR", "ru_RU",
                 "de_DE", "it_IT", "ko_KR", "zh_TW", "zh_CN")
_GUILD_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")


def _guild_normalize(values: dict) -> dict:
    """Normalise puis valide les valeurs fournies (400 avec motif si invalide)."""
    clean: dict = {}
    if values.get("region"):
        region = str(values["region"]).strip().lower()
        if region not in GUILD_BNET_REGIONS:
            raise HTTPException(400, "Région Battle.net inconnue (au choix : eu, us, kr, tw).")
        clean["region"] = region
    if values.get("wcl_region"):
        wcl_region = str(values["wcl_region"]).strip().upper()
        if wcl_region not in GUILD_WCL_REGIONS:
            raise HTTPException(400, "Région Warcraft Logs inconnue (au choix : EU, US, KR, TW, CN).")
        clean["wcl_region"] = wcl_region
    if values.get("locale"):
        locale = str(values["locale"]).strip()
        if locale not in GUILD_LOCALES:
            raise HTTPException(400, "Langue de données inconnue (ex. fr_FR, en_US, de_DE).")
        clean["locale"] = locale
    for key, label in (("realm", "royaume"), ("slug", "slug de guilde")):
        if values.get(key):
            val = str(values[key]).strip().lower()
            if not _GUILD_SLUG_RE.match(val):
                raise HTTPException(400, f"Le {label} doit être un slug en minuscules (ex. hyjal, ma-guilde).")
            clean[key] = val
    if values.get("wcl_name"):
        name = str(values["wcl_name"]).strip()
        if len(name) > 60:
            raise HTTPException(400, "Nom Warcraft Logs trop long (60 caractères maximum).")
        clean["wcl_name"] = name
    return clean


class GuildConfigRequest(BaseModel):
    values: dict[str, str] = {}
    clear: bool = False


class GuildTestRequest(BaseModel):
    values: dict[str, str] = {}


@router.get("/api/admin/guild")
def admin_guild_get(request: Request):
    _require_admin(request)
    rows = _guild_rows()
    return {"config": _guild_effective(),
            "source": {k: ("admin" if rows.get(k) else "env") for k in GUILD_KEYS}}


@router.post("/api/admin/guild")
def admin_guild_save(payload: GuildConfigRequest, request: Request):
    _require_admin(request)
    if payload.clear:
        with _db_lock, _db() as conn:
            conn.execute("DELETE FROM guild_config")
        _apply_guild_config()
        return {"ok": True, "cleared": True}
    values = {k: str(v).strip() for k, v in (payload.values or {}).items() if k in GUILD_KEYS}
    if not values:
        raise HTTPException(400, "Aucune valeur à enregistrer.")
    clean = _guild_normalize(values)
    with _db_lock, _db() as conn:
        for key in values:
            value = clean.get(key, "")
            if value:
                conn.execute("INSERT OR REPLACE INTO guild_config (key, value, updated) VALUES (?,?,?)",
                             (key, value, time.time()))
            else:
                conn.execute("DELETE FROM guild_config WHERE key=?", (key,))
    _apply_guild_config()
    return {"ok": True, "config": _guild_effective()}


@router.post("/api/admin/guild/test")
def admin_guild_test(payload: GuildTestRequest, request: Request):
    """Contrôle (des valeurs saisies, sinon de celles en vigueur) sur les deux services."""
    _require_admin(request)
    eff = _guild_effective()
    overrides = {k: str(v).strip() for k, v in (payload.values or {}).items()
                 if k in GUILD_KEYS and str(v).strip()}
    values = {k: overrides.get(k) or eff.get(k, "") for k in GUILD_KEYS}
    try:
        clean = _guild_normalize(values)
    except HTTPException as exc:
        detail = str(exc.detail)
        return {"bnet": {"ok": False, "detail": detail}, "wcl": {"ok": False, "detail": detail}}
    final = {**values, **clean}
    return {"bnet": bnet.guild_lookup(final["realm"], final["slug"], final["region"]),
            "wcl": wcl.guild_lookup(final["wcl_name"], final["realm"], final["wcl_region"])}

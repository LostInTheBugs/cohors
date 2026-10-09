"""Cohors — progressive web app: dynamic manifest (follows the guild identity), service worker, offline page."""
from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse

from app.core.brand import _brand_files, _brand_row
from app.core.config import STATIC_DIR
from app.core.db import _db, _db_lock

router = APIRouter()


@router.api_route("/manifest.webmanifest", methods=["GET", "HEAD"])
def pwa_manifest(request: Request):
    """Manifeste PWA dynamique : nom, nom court et icône suivent l'identité de la guilde."""
    short, name = "Cohors", ""
    try:
        with _db_lock, _db() as conn:
            row = _brand_row(conn)
        short = (row["guild_short"] or "Cohors").strip()[:24] or "Cohors"
        name = (row["guild_name"] or "").strip()[:60]
    except sqlite3.Error:
        pass
    icons = []
    if _brand_files().get("logo"):
        icons.append({"src": "/branding/logo", "sizes": "any", "type": "image/png", "purpose": "any"})
    icons += [
        {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ]
    man = {
        "name": f"{short} — {name}" if name else "Cohors — Compagnon de guilde",
        "short_name": short,
        "description": "Compagnon de guilde World of Warcraft — simulations, roster, raids, artisanat et suivi.",
        "lang": "fr",
        "start_url": "/dashboard",
        "scope": "/",
        "display": "standalone",
        "background_color": "#0b0f17",
        "theme_color": "#b1002e",
        "icons": icons,
    }
    return Response(json.dumps(man, ensure_ascii=False), media_type="application/manifest+json")


@router.api_route("/sw.js", methods=["GET", "HEAD"])
def pwa_sw(request: Request):
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@router.api_route("/offline.html", methods=["GET", "HEAD"])
def pwa_offline(request: Request):
    return FileResponse(STATIC_DIR / "offline.html")

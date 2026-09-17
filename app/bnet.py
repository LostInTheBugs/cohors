"""Client Battle.net (OAuth client credentials) — roster de guilde + profils perso.

Documentation: https://develop.battle.net/documentation/guides/using-oauth/client-credentials-flow

Données de jeu fournies par Blizzard Entertainment. Cache mémoire 30 min ;
rafraîchissement forcé possible (« Actualiser ») mais limité à 1×/min.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

REGION = os.environ.get("BNET_REGION", "eu")
GUILD_REALM = os.environ.get("BNET_GUILD_REALM", "hyjal")
GUILD_SLUG = os.environ.get("BNET_GUILD_SLUG", "lords-of-the-pit")
LOCALE = os.environ.get("BNET_LOCALE", "fr_FR")

TOKEN_URL = "https://oauth.battle.net/token"
TTL = 1800.0          # cache des données : 30 min
MIN_FORCE_S = 60.0    # délai minimum entre deux rafraîchissements forcés

_lock = threading.Lock()
_token: dict = {"value": None, "expires": 0.0}
_cache: dict[str, dict] = {}  # clé -> {"ts": float, "data": ...}


class BnetError(Exception):
    """Erreur API Battle.net (statut HTTP + message court, en français)."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)


# ---------------------------------------------------------------------------
# Bas niveau
# ---------------------------------------------------------------------------
def _access_token() -> str:
    with _lock:
        if _token["value"] and time.time() < _token["expires"] - 120:
            return _token["value"]
        cid = os.environ.get("BNET_CLIENT_ID", "").strip()
        secret = os.environ.get("BNET_CLIENT_SECRET", "").strip()
        if not cid or not secret:
            raise BnetError(500, "Clés API Battle.net non configurées sur le serveur.")
        auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        req = urllib.request.Request(
            TOKEN_URL,
            method="POST",
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=b"grant_type=client_credentials",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as exc:
            raise BnetError(exc.code, "Authentification Battle.net refusée (clés invalides ?).") from exc
        _token["value"] = payload["access_token"]
        _token["expires"] = time.time() + int(payload.get("expires_in", 3600))
        return _token["value"]


def _get(path: str, params: dict | None = None) -> dict:
    url = f"https://{REGION}.api.blizzard.com{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_access_token()}"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise BnetError(404, "Personnage introuvable sur ce royaume.") from exc
        raise BnetError(exc.code, "Erreur de l'API Battle.net.") from exc


def _cached(key: str, force: bool) -> dict | None:
    """Entrée de cache utilisable ? (TTL 30 min ; forcé = 1×/min max)."""
    with _lock:
        hit = _cache.get(key)
    if hit is None:
        return None
    age = time.time() - hit["ts"]
    if not force and age < TTL:
        return hit
    if force and age < MIN_FORCE_S:
        return hit
    return None


def _store(key: str, data: dict) -> float:
    with _lock:
        _cache[key] = {"ts": time.time(), "data": data}
        return _cache[key]["ts"]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def roster(force: bool = False) -> tuple[dict, float]:
    """Roster de la guilde (membres, rangs, niveaux)."""
    hit = _cached("roster", force)
    if hit:
        return hit["data"], hit["ts"]
    raw = _get(
        f"/data/wow/guild/{GUILD_REALM}/{GUILD_SLUG}/roster",
        {"namespace": f"profile-{REGION}", "locale": LOCALE},
    )
    members = []
    for m in raw.get("members", []):
        ch = m.get("character") or {}
        members.append(
            {
                "name": ch.get("name"),
                "level": ch.get("level"),
                "rank": int(m.get("rank") or 0),
                "realm": ((ch.get("realm") or {}).get("slug")) or GUILD_REALM,
            }
        )
    members.sort(key=lambda mm: (mm["rank"], -(mm["level"] or 0), (mm["name"] or "").lower()))
    data = {
        "guild": (raw.get("guild") or {}).get("name") or "Guilde",
        "realm": GUILD_REALM,
        "region": REGION,
        "members": members,
    }
    return data, _store("roster", data)


def character(realm: str, name: str, force: bool = False) -> tuple[dict, float]:
    """Résumé de personnage (niveau, spé, classe, ilvl, dernier jeu)."""
    realm, name = realm.lower(), name.lower()
    key = f"char/{realm}/{name}"
    hit = _cached(key, force)
    if hit:
        return hit["data"], hit["ts"]
    raw = _get(
        f"/profile/wow/character/{urllib.parse.quote(realm)}/{urllib.parse.quote(name)}",
        {"namespace": f"profile-{REGION}", "locale": LOCALE},
    )
    data = {
        "name": raw.get("name"),
        "realm": realm,
        "level": raw.get("level"),
        "class": (raw.get("character_class") or {}).get("name"),
        "spec": (raw.get("active_spec") or {}).get("name"),
        "race": (raw.get("race") or {}).get("name"),
        "faction": (raw.get("faction") or {}).get("name"),
        "ilvl_equipped": raw.get("equipped_item_level"),
        "ilvl_avg": raw.get("average_item_level"),
        "achievement_points": raw.get("achievement_points"),
        "last_login": raw.get("last_login_timestamp"),
        "armory": f"https://worldofwarcraft.blizzard.com/fr-fr/character/{REGION}/{realm}/{name}",
    }
    return data, _store(key, data)


def equipment(realm: str, name: str, force: bool = False) -> tuple[dict, float]:
    """Équipement porté (pièces + item level + identifiants Wowhead)."""
    realm, name = realm.lower(), name.lower()
    key = f"gear/{realm}/{name}"
    hit = _cached(key, force)
    if hit:
        return hit["data"], hit["ts"]
    raw = _get(
        f"/profile/wow/character/{urllib.parse.quote(realm)}/{urllib.parse.quote(name)}/equipment",
        {"namespace": f"profile-{REGION}", "locale": LOCALE},
    )
    items = []
    for it in raw.get("equipped_items", []):
        slot = it.get("slot") or {}
        items.append(
            {
                "slot": slot.get("name") or slot.get("type") or "?",
                "name": it.get("name"),
                "ilvl": (it.get("level") or {}).get("value"),
                "quality": ((it.get("quality") or {}).get("type") or "COMMON"),
                "item_id": (it.get("item") or {}).get("id"),
            }
        )
    ilvls = [it["ilvl"] for it in items if it.get("ilvl")]
    data = {"ilvl": round(sum(ilvls) / len(ilvls)) if ilvls else None, "items": items}
    return data, _store(key, data)

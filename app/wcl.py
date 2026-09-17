"""Client Warcraft Logs v2 (GraphQL, client credentials) — rapports de raid & parses.

Docs : https://www.warcraftlogs.com/api/docs · quota 3600 points/h.
Cache mémoire : liste 15 min, rapport + parses 30 min ; rafraîchissement forcé ≤ 1×/min.
Données fournies par Warcraft Logs (usage non commercial — cf. leurs conditions).
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request

TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
API_URL = "https://www.warcraftlogs.com/api/v2/client"

REGION = os.environ.get("WCL_GUILD_REGION", "EU")
GUILD_NAME = os.environ.get("WCL_GUILD_NAME", "Lords Of The Pit")
GUILD_REALM = os.environ.get("WCL_GUILD_REALM", "hyjal")

TTL_LIST = 900.0     # liste des rapports : 15 min
TTL_REPORT = 1800.0  # rapport + parses : 30 min
MIN_FORCE_S = 60.0   # rafraîchissement forcé : 1×/min max
RAID_ZONE_ID = int(os.environ.get("WCL_RAID_ZONE_ID", "53"))  # raid courant (parses perso)

_lock = threading.Lock()
_token: dict = {"value": None, "expires": 0.0}
_cache: dict[str, dict] = {}


class WclError(Exception):
    """Erreur API Warcraft Logs (statut HTTP + message court)."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)


def _access_token() -> str:
    with _lock:
        if _token["value"] and time.time() < _token["expires"] - 3600:
            return _token["value"]
        cid = os.environ.get("WCL_CLIENT_ID", "").strip()
        secret = os.environ.get("WCL_CLIENT_SECRET", "").strip()
        if not cid or not secret:
            raise WclError(500, "Clés API Warcraft Logs non configurées sur le serveur.")
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
            raise WclError(exc.code, "Authentification Warcraft Logs refusée (clés invalides ?).") from exc
        _token["value"] = payload["access_token"]
        _token["expires"] = time.time() + int(payload.get("expires_in", 3600))
        return _token["value"]


def _gql(query: str, variables: dict | None = None) -> dict:
    body: dict = {"query": query}
    if variables:
        body["variables"] = variables
    req = urllib.request.Request(
        API_URL,
        method="POST",
        headers={"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json"},
        data=json.dumps(body).encode(),
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise WclError(exc.code if exc.code in (400, 404) else 502, "Erreur de l'API Warcraft Logs.") from exc
    if payload.get("errors"):
        msg = str(((payload["errors"] or [{}])[0] or {}).get("message", "erreur GraphQL"))[:200]
        raise WclError(502, f"Warcraft Logs : {msg}")
    return payload.get("data") or {}


def _cached(key: str, ttl: float, force: bool) -> dict | None:
    with _lock:
        hit = _cache.get(key)
    if hit is None:
        return None
    age = time.time() - hit["ts"]
    if not force and age < ttl:
        return hit
    if force and age < MIN_FORCE_S:
        return hit
    return None


def _store(key: str, data: dict) -> float:
    with _lock:
        _cache[key] = {"ts": time.time(), "data": data}
        return _cache[key]["ts"]


def reports(limit: int = 30, force: bool = False) -> tuple[dict, float]:
    """Derniers rapports de la guilde (toutes zones)."""
    key = f"list/{limit}"
    hit = _cached(key, TTL_LIST, force)
    if hit:
        return hit["data"], hit["ts"]
    query = (
        "query($n: String!, $s: String!, $r: String!, $lim: Int!) { reportData { reports("
        "guildName: $n, guildServerSlug: $s, guildServerRegion: $r, limit: $lim) { "
        "total per_page current_page data { code title startTime endTime zone { id name } } } } }"
    )
    data = _gql(query, {"n": GUILD_NAME, "s": GUILD_REALM, "r": REGION, "lim": limit})
    node = (data.get("reportData") or {}).get("reports") or {}
    out = {"total": node.get("total"), "data": node.get("data") or []}
    return out, _store(key, out)


def report_full(code: str, force: bool = False) -> tuple[dict, float]:
    """Rapport complet : fights (boss uniquement) + parses DPS des kills."""
    key = f"report/{code}"
    hit = _cached(key, TTL_REPORT, force)
    if hit:
        return hit["data"], hit["ts"]
    query = (
        "query($c: String!) { reportData { report(code: $c) { code title startTime endTime "
        "zone { id name } fights { id name kill difficulty encounterID fightPercentage "
        "bossPercentage averageItemLevel size startTime endTime } } } }"
    )
    data = _gql(query, {"c": code})
    rep = (data.get("reportData") or {}).get("report")
    if not rep:
        raise WclError(404, "Rapport introuvable (ou non public).")
    fights = [f for f in (rep.get("fights") or []) if f.get("encounterID")]
    rep["fights"] = fights

    rankings: dict[str, dict] = {}
    kill_ids = [f["id"] for f in fights if f.get("kill")]
    if kill_ids:
        try:
            rq = (
                "query($c: String!, $ids: [Int!]!) { reportData { report(code: $c) { "
                "rankings(fightIDs: $ids, playerMetric: dps) } } }"
            )
            rdata = _gql(rq, {"c": code, "ids": kill_ids})
            rk = ((rdata.get("reportData") or {}).get("report") or {}).get("rankings") or {}
            for entry in rk.get("data", []):
                rankings[str(entry.get("fightID"))] = entry
        except WclError:
            rankings = {}  # le rapport reste consultable sans parses

    out = {"report": rep, "rankings": rankings}
    return out, _store(key, out)


def deaths(code: str, force: bool = False) -> tuple[list[dict], float]:
    """Morts d'un rapport (un événement par mort : joueur, fight, tueur) — cache 30 min."""
    key = f"deaths/{code}"
    hit = _cached(key, TTL_REPORT, force)
    if hit:
        return hit["data"], hit["ts"]
    full, _ts = report_full(code)
    fids = [f["id"] for f in (full["report"].get("fights") or [])]
    rows: list[dict] = []
    if fids:
        query = (
            "query($c: String!, $fids: [Int!]!) { reportData { report(code: $c) { "
            "deaths: table(dataType: Deaths, hostilityType: Friendlies, fightIDs: $fids) } } }"
        )
        try:
            data = _gql(query, {"c": code, "fids": fids})
            table = ((data.get("reportData") or {}).get("report") or {}).get("deaths") or {}
            entries = ((table.get("data") or {}).get("entries")) or []
        except WclError:
            entries = []
        for e in entries:
            kb = e.get("killingBlow") or {}
            icon = e.get("icon") or ""
            rows.append({
                "name": e.get("name"),
                "class": e.get("type"),
                "spec": icon.split("-", 1)[1] if "-" in icon else "",
                "fight": e.get("fight"),
                "timestamp": e.get("timestamp"),
                "killer": (kb.get("name") if isinstance(kb, dict) else None),
            })
    return rows, _store(key, rows)


def character_rankings(realm: str, name: str, zone_id: int | None = None, force: bool = False) -> tuple[dict, float]:
    """Meilleurs parses d'un personnage sur la zone de raid courante."""
    zone = int(zone_id or RAID_ZONE_ID)
    key = f"zr/{realm.lower()}/{name.lower()}/{zone}"
    hit = _cached(key, TTL_REPORT, force)
    if hit:
        return hit["data"], hit["ts"]
    query = (
        "query($n: String!, $s: String!, $r: String!, $z: Int!) { characterData { character("
        "name: $n, serverSlug: $s, serverRegion: $r) { id name zoneRankings(zoneID: $z) } } }"
    )
    data = _gql(query, {"n": name, "s": realm, "r": REGION, "z": zone})
    ch = (data.get("characterData") or {}).get("character")
    if not ch:
        raise WclError(404, "Personnage introuvable sur Warcraft Logs.")
    zr = ch.get("zoneRankings") or {}
    out = {
        "name": ch.get("name") or name,
        "zone": zr.get("zone") or zone,
        "difficulty": zr.get("difficulty"),
        "best_average": zr.get("bestPerformanceAverage"),
        "median_average": zr.get("medianPerformanceAverage"),
        "rankings": [
            {
                "boss": (r.get("encounter") or {}).get("name"),
                "rank_percent": r.get("rankPercent"),
                "median_percent": r.get("medianPercent"),
                "kills": r.get("totalKills"),
                "best_amount": r.get("bestAmount"),
            }
            for r in (zr.get("rankings") or [])
        ],
    }
    return out, _store(key, out)


def zone_label(zone_id: int | None = None) -> str:
    """Nom lisible de la zone (via les rapports récents, sinon « Zone N »)."""
    zone = int(zone_id or RAID_ZONE_ID)
    try:
        data, _ts = reports(limit=50)
    except WclError:
        return f"Zone {zone}"
    for r in data.get("data", []):
        z = r.get("zone") or {}
        if z.get("id") == zone and z.get("name"):
            return z["name"]
    return f"Zone {zone}"

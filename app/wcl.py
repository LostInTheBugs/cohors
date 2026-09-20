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

# Défauts du fichier serveur — surchargés depuis l'administration (set_guild_info).
_ENV = {
    "region": os.environ.get("WCL_GUILD_REGION", "EU"),
    "name": os.environ.get("WCL_GUILD_NAME", "Lords Of The Pit"),
    "realm": os.environ.get("WCL_GUILD_REALM", "hyjal"),
}
REGION = _ENV["region"]
GUILD_NAME = _ENV["name"]
GUILD_REALM = _ENV["realm"]

TTL_LIST = 900.0     # liste des rapports : 15 min
TTL_REPORT = 1800.0  # rapport + parses : 30 min
MIN_FORCE_S = 60.0   # rafraîchissement forcé : 1×/min max
RAID_ZONE_ID = int(os.environ.get("WCL_RAID_ZONE_ID", "53"))  # raid courant (parses perso)

_lock = threading.Lock()
_token: dict = {"value": None, "expires": 0.0}
_cache: dict[str, dict] = {}
_cfg: dict = {"client_id": None, "client_secret": None}  # clés posées depuis l'administration


def set_credentials(client_id: str | None, client_secret: str | None) -> None:
    """Clés renseignées dans l'administration — prioritaires sur l'environnement."""
    _cfg["client_id"] = (client_id or "").strip() or None
    _cfg["client_secret"] = (client_secret or "").strip() or None
    _token["value"], _token["expires"] = None, 0.0


def credentials() -> tuple[str, str]:
    """Clés effectives : administration d'abord, sinon environnement."""
    return (_cfg["client_id"] or os.environ.get("WCL_CLIENT_ID", "").strip(),
            _cfg["client_secret"] or os.environ.get("WCL_CLIENT_SECRET", "").strip())


def set_guild_info(region: str | None = None, name: str | None = None, realm: str | None = None) -> None:
    """Identité de guilde posée depuis l'administration — prioritaire sur l'environnement.

    Champ vide = retour à la valeur du fichier serveur ; tout changement invalide le cache.
    """
    global REGION, GUILD_NAME, GUILD_REALM
    REGION = (region or "").strip().upper() or _ENV["region"]
    GUILD_NAME = (name or "").strip() or _ENV["name"]
    GUILD_REALM = (realm or "").strip().lower() or _ENV["realm"]
    with _lock:
        _cache.clear()


def guild_lookup(name: str, realm: str, region: str) -> dict:
    """Vérifie qu'une guilde existe sur Warcraft Logs (« 🔎 Vérifier » de l'administration)."""
    query = ("query($n: String!, $s: String!, $r: String!) { guildData { "
             "guild(name: $n, serverSlug: $s, serverRegion: $r) { id name } } }")
    try:
        data = _gql(query, {"n": name.strip(), "s": realm.strip().lower(),
                            "r": region.strip().upper()})
    except WclError as exc:
        return {"ok": False, "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"Connexion impossible ({type(exc).__name__})."}
    guild = ((data.get("guildData") or {}).get("guild"))
    if not guild:
        return {"ok": False, "detail": "Guilde introuvable — vérifie le nom exact et la région."}
    return {"ok": True, "name": guild.get("name") or name}


def check(client_id: str, client_secret: str) -> dict:
    """Teste un couple de clés (jeton + requête de quota) sans toucher au cache."""
    cid, secret = (client_id or "").strip(), (client_secret or "").strip()
    if not cid or not secret:
        return {"ok": False, "detail": "Client ID et secret requis."}
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    req = urllib.request.Request(
        TOKEN_URL, method="POST",
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data=b"grant_type=client_credentials")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        msg = "Clés refusées par Warcraft Logs." if exc.code in (400, 401, 403) else f"Erreur HTTP {exc.code}."
        return {"ok": False, "detail": msg}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"Connexion impossible ({type(exc).__name__})."}
    token = payload.get("access_token")
    q = urllib.request.Request(
        API_URL, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=json.dumps({"query": "query { rateLimitData { limitPerHour pointsSpentThisHour } }"}).encode())
    try:
        with urllib.request.urlopen(q, timeout=20) as resp:
            data = json.load(resp)
        rl = ((data.get("data") or {}).get("rateLimitData") or {})
        detail = (f"Jeton + API OK — quota {rl.get('limitPerHour', '?')} pts/h, "
                  f"{rl.get('pointsSpentThisHour', '?')} utilisés cette heure.")
        return {"ok": True, "detail": detail}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"Jeton obtenu mais API injoignable ({type(exc).__name__})."}


class WclError(Exception):
    """Erreur API Warcraft Logs (statut HTTP + message court)."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)


def _access_token() -> str:
    with _lock:
        if _token["value"] and time.time() < _token["expires"] - 3600:
            return _token["value"]
        cid, secret = credentials()
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


def report_combatants(code: str, force: bool = False) -> tuple[dict, float]:
    """Équipement des joueurs d'un rapport (CombatantInfo) — dernier état de chaque joueur.

    Renvoie {"players": {nom: [gear par emplacement (index = ordre Blizzard 0-17)]}}.
    """
    key = f"combat/{code}"
    hit = _cached(key, TTL_REPORT, force)
    if hit:
        return hit["data"], hit["ts"]
    full, _ts = report_full(code)
    fights = full["report"].get("fights") or []
    players: dict[str, list] = {}
    if fights:
        aq = ("query($c: String!) { reportData { report(code: $c) { "
              "masterData { actors { id name type } } } } }")
        actors = (((_gql(aq, {"c": code}).get("reportData") or {}).get("report") or {})
                  .get("masterData") or {}).get("actors") or []
        names = {a.get("id"): a.get("name") for a in actors if a.get("type") == "Player"}
        fids = [f["id"] for f in fights]
        q = ("query($c: String!, $fids: [Int!]!) { reportData { report(code: $c) { "
             "events(fightIDs: $fids, dataType: CombatantInfo, limit: 500) { data } } } }")
        try:
            evs = ((((_gql(q, {"c": code, "fids": fids}).get("reportData") or {}).get("report") or {})
                    .get("events") or {}).get("data")) or []
        except WclError:
            evs = []
        seen: dict[str, float] = {}
        for e in evs:
            if e.get("type") != "combatantinfo":
                continue
            nm = names.get(e.get("sourceID"))
            ts = e.get("timestamp") or 0
            if nm and ts >= seen.get(nm, -1):
                seen[nm] = ts
                players[nm] = e.get("gear") or []
    out = {"players": players}
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

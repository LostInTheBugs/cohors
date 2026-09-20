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

# Défauts du fichier serveur — surchargés depuis l'administration (set_guild_info).
_ENV = {
    "region": os.environ.get("BNET_REGION", "eu"),
    "realm": os.environ.get("BNET_GUILD_REALM", "hyjal"),
    "slug": os.environ.get("BNET_GUILD_SLUG", "lords-of-the-pit"),
    "locale": os.environ.get("BNET_LOCALE", "fr_FR"),
}
REGION = _ENV["region"]
GUILD_REALM = _ENV["realm"]
GUILD_SLUG = _ENV["slug"]
LOCALE = _ENV["locale"]


def _loc(locale: str | None = None) -> str:
    """Locale API : celle demandée, sinon la locale par défaut du serveur."""
    return locale or LOCALE

TOKEN_URL = "https://oauth.battle.net/token"
TTL = 1800.0          # cache des données : 30 min
MIN_FORCE_S = 60.0    # délai minimum entre deux rafraîchissements forcés

_lock = threading.Lock()
_token: dict = {"value": None, "expires": 0.0}
_cache: dict[str, dict] = {}  # clé -> {"ts": float, "data": ...}
_cfg: dict = {"client_id": None, "client_secret": None}  # clés posées depuis l'administration


def set_credentials(client_id: str | None, client_secret: str | None) -> None:
    """Clés renseignées dans l'administration — prioritaires sur l'environnement.

    Un changement invalide le jeton en cache pour que le prochain appel reprenne les nouvelles clés.
    """
    _cfg["client_id"] = (client_id or "").strip() or None
    _cfg["client_secret"] = (client_secret or "").strip() or None
    _token["value"], _token["expires"] = None, 0.0


def credentials() -> tuple[str, str]:
    """Clés effectives : administration d'abord, sinon environnement."""
    return (_cfg["client_id"] or os.environ.get("BNET_CLIENT_ID", "").strip(),
            _cfg["client_secret"] or os.environ.get("BNET_CLIENT_SECRET", "").strip())


def set_guild_info(region: str | None = None, realm: str | None = None,
                   slug: str | None = None, locale: str | None = None) -> None:
    """Identité de guilde posée depuis l'administration — prioritaire sur l'environnement.

    Champ vide = retour à la valeur du fichier serveur ; tout changement invalide le cache.
    """
    global REGION, GUILD_REALM, GUILD_SLUG, LOCALE
    REGION = (region or "").strip().lower() or _ENV["region"]
    GUILD_REALM = (realm or "").strip().lower() or _ENV["realm"]
    GUILD_SLUG = (slug or "").strip().lower() or _ENV["slug"]
    LOCALE = (locale or "").strip() or _ENV["locale"]
    with _lock:
        _cache.clear()


def guild_lookup(realm: str, slug: str, region: str) -> dict:
    """Vérifie qu'une guilde existe à ce royaume/slug (« 🔎 Vérifier » de l'administration)."""
    realm, slug, region = realm.strip().lower(), slug.strip().lower(), region.strip().lower()
    url = (f"https://{region}.api.blizzard.com/data/wow/guild/"
           f"{urllib.parse.quote(realm)}/{urllib.parse.quote(slug)}")
    url += "?" + urllib.parse.urlencode({"namespace": f"profile-{region}", "locale": _loc()})
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_access_token()}"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = json.load(resp)
    except BnetError as exc:
        return {"ok": False, "detail": str(exc)}
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"ok": False, "detail": "Guilde introuvable — vérifie la région, le royaume et le slug."}
        return {"ok": False, "detail": f"Erreur de l'API Battle.net (HTTP {exc.code})."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"Connexion impossible ({type(exc).__name__})."}
    return {"ok": True,
            "name": raw.get("name") or "?",
            "realm": ((raw.get("realm") or {}).get("name")) or realm,
            "members": raw.get("member_count"),
            "faction": ((raw.get("faction") or {}).get("name")) or ""}


def check(client_id: str, client_secret: str) -> dict:
    """Teste un couple de clés sans toucher au cache (« Jeton obtenu » ou message d'erreur)."""
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
            json.load(resp)
    except urllib.error.HTTPError as exc:
        msg = "Clés refusées par Battle.net." if exc.code in (400, 401, 403) else f"Erreur HTTP {exc.code}."
        return {"ok": False, "detail": msg}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"Connexion impossible ({type(exc).__name__})."}
    return {"ok": True, "detail": "Jeton obtenu — les clés fonctionnent."}


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
        cid, secret = credentials()
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


def _get(path: str, params: dict | None = None, not_found: str = "Personnage introuvable sur ce royaume.") -> dict:
    url = f"https://{REGION}.api.blizzard.com{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_access_token()}"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise BnetError(404, not_found) from exc
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
def journal_raids(locale: str | None = None) -> tuple[dict, float]:
    """Raids de la saison en cours (journal de jeu) et leurs boss, pour l'objectif de prépa.

    Catalogue optionnel : en cas d'erreur API, renvoie un catalogue vide (le plan reste)
    et l'échec n'est pas propagé."""
    loc = _loc(locale)
    key = f"journal_raids_{loc}"
    hit = _cached(key, False)
    if hit:
        return hit["data"], hit["ts"]
    ns = {"namespace": f"static-{REGION}", "locale": loc}
    data: dict = {"expansion": "", "raids": []}
    try:
        idx = _get("/data/wow/journal-expansion/index", dict(ns))
        tiers = [t for t in (idx.get("tiers") or []) if t.get("id")]
        if tiers:
            tiers.sort(key=lambda t: int(t["id"]), reverse=True)   # la plus récente d'abord
            for tier in tiers[:3]:
                det = _get(f"/data/wow/journal-expansion/{tier['id']}", dict(ns))
                raids = []
                for r in (det.get("raids") or [])[:10]:
                    try:
                        ins = _get(f"/data/wow/journal-instance/{r.get('id')}", dict(ns))
                    except BnetError:
                        continue
                    raids.append({"name": ins.get("name") or r.get("name") or "?",
                                  "bosses": [e.get("name") for e in (ins.get("encounters") or [])
                                             if e.get("name")]})
                if raids:
                    data = {"expansion": tier.get("name") or "", "raids": raids}
                    break
    except BnetError:
        pass
    ts = _store(key, data)
    return data, ts


def journal_loot() -> list[dict]:
    """Butin des raids et donjons du dernier palier (journal de jeu) — noms FR et EN.

    Une entrée par couple (objet, rencontre) : {item_id, kind, inst_fr, inst_en, boss_fr, boss_en}.
    """
    ns_fr = {"namespace": f"static-{REGION}", "locale": "fr_FR"}
    ns_en = {"namespace": f"static-{REGION}", "locale": "en_US"}
    idx = _get("/data/wow/journal-expansion/index", dict(ns_fr))
    tiers = sorted([t for t in (idx.get("tiers") or []) if t.get("id")],
                   key=lambda t: int(t["id"]), reverse=True)
    if not tiers:
        return []
    det = _get(f"/data/wow/journal-expansion/{tiers[0]['id']}", dict(ns_fr))
    rows: list[dict] = []
    for kind, grp in (("raid", "raids"), ("dungeon", "dungeons")):
        for ref in (det.get(grp) or [])[:20]:
            if not ref.get("id"):
                continue
            try:
                ins_fr = _get(f"/data/wow/journal-instance/{ref['id']}", dict(ns_fr))
                ins_en = _get(f"/data/wow/journal-instance/{ref['id']}", dict(ns_en))
            except BnetError:
                continue
            in_fr = ins_fr.get("name") or ref.get("name") or "?"
            in_en = ins_en.get("name") or in_fr
            encs_en = {e.get("id"): (e.get("name") or "") for e in (ins_en.get("encounters") or [])}
            for enc in (ins_fr.get("encounters") or []):
                if not enc.get("id"):
                    continue
                b_fr = enc.get("name") or "?"
                b_en = encs_en.get(enc.get("id")) or b_fr
                try:
                    e_fr = _get(f"/data/wow/journal-encounter/{enc['id']}", dict(ns_fr))
                except BnetError:
                    continue
                for it in (e_fr.get("items") or []):
                    iid = ((it.get("item") or {}).get("id")) or it.get("id")
                    if not iid:
                        continue
                    rows.append({"item_id": int(iid), "kind": kind,
                                 "inst_fr": in_fr, "inst_en": in_en,
                                 "boss_fr": b_fr, "boss_en": b_en})
                time.sleep(0.03)
    return rows


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


def character(realm: str, name: str, force: bool = False, locale: str | None = None) -> tuple[dict, float]:
    """Résumé de personnage (niveau, spé, classe, ilvl, dernier jeu) — localisé."""
    loc = _loc(locale)
    realm, name = realm.lower(), name.lower()
    key = f"char/{loc}/{realm}/{name}"
    hit = _cached(key, force)
    if hit:
        return hit["data"], hit["ts"]
    raw = _get(
        f"/profile/wow/character/{urllib.parse.quote(realm)}/{urllib.parse.quote(name)}",
        {"namespace": f"profile-{REGION}", "locale": loc},
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
        "armory": "https://worldofwarcraft.blizzard.com/"
        + ("en-gb" if loc.startswith("en") else "fr-fr")
        + f"/character/{REGION}/{realm}/{name}",
    }
    return data, _store(key, data)


def equipment(realm: str, name: str, force: bool = False, locale: str | None = None) -> tuple[dict, float]:
    """Équipement porté (pièces + item level + identifiants Wowhead) — localisé."""
    loc = _loc(locale)
    realm, name = realm.lower(), name.lower()
    key = f"gear/{loc}/{realm}/{name}"
    hit = _cached(key, force)
    if hit:
        return hit["data"], hit["ts"]
    raw = _get(
        f"/profile/wow/character/{urllib.parse.quote(realm)}/{urllib.parse.quote(name)}/equipment",
        {"namespace": f"profile-{REGION}", "locale": loc},
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


# Libellés FR des métiers (l'API professions renvoie les noms en anglais).
# Repli sûr : si un nom inconnu arrive, il est conservé tel quel.
PROF_FR = {
    "Alchemy": "Alchimie", "Blacksmithing": "Forge", "Enchanting": "Enchantement",
    "Engineering": "Ingénierie", "Herbalism": "Herboristerie", "Inscription": "Calligraphie",
    "Jewelcrafting": "Joaillerie", "Leatherworking": "Travail du cuir", "Mining": "Minéralogie",
    "Skinning": "Dépeçage", "Tailoring": "Couture", "Archaeology": "Archéologie",
    "Cooking": "Cuisine", "Fishing": "Pêche", "First Aid": "Secourisme",
}


def professions(realm: str, name: str, force: bool = False, locale: str | None = None) -> tuple[dict, float]:
    """Métiers du personnage (primaires + secondaires) — palier le plus récent.

    L'API renvoie les noms en anglais quelle que soit la locale : on expose `name_en`
    (brut) + `name_fr`/`name` (FR via PROF_FR) et on sert selon la langue demandée.
    """
    loc = _loc(locale)
    realm, name = realm.lower(), name.lower()
    key = f"prof/{loc}/{realm}/{name}"
    hit = _cached(key, force)
    if hit:
        return hit["data"], hit["ts"]
    raw = _get(
        f"/profile/wow/character/{urllib.parse.quote(realm)}/{urllib.parse.quote(name)}/professions",
        {"namespace": f"profile-{REGION}", "locale": loc},
    )
    profs = []
    for p in (raw.get("primaries") or []) + (raw.get("secondaries") or []):
        prof = p.get("profession") or {}
        tiers = p.get("tiers") or []
        tier = tiers[-1] if tiers else {}
        points = tier.get("skill_points")
        maxp = tier.get("max_skill_points")
        if points is None and maxp is None:
            # métiers sans paliers (ex. Archéologie) : points au niveau racine
            points, maxp = p.get("skill_points"), p.get("max_skill_points")
        raw_name = prof.get("name") or "?"
        profs.append({
            "name": (raw_name if loc.startswith("en") else PROF_FR.get(raw_name, raw_name)),
            "name_fr": PROF_FR.get(raw_name, raw_name),
            "name_en": raw_name,
            "id": prof.get("id"),
            "tier": (tier.get("tier") or {}).get("name"),
            "points": points,
            "max": maxp,
        })
    data = {"profs": profs}
    return data, _store(key, data)


def extras(realm: str, name: str, force: bool = False, locale: str | None = None) -> tuple[dict, float]:
    """Fiche enrichie : hauts faits, collections (montures / mascottes) et rating M+."""
    realm, name = realm.lower(), name.lower()
    key = f"extras/{realm}/{name}"
    hit = _cached(key, force)
    if hit:
        return hit["data"], hit["ts"]
    base = f"/profile/wow/character/{urllib.parse.quote(realm)}/{urllib.parse.quote(name)}"
    ns = f"profile-{REGION}"
    data: dict = {"achv_points": None, "mounts": None, "pets": None, "mplus_rating": None}
    try:
        ach = _get(f"{base}/achievements", {"namespace": ns, "locale": LOCALE})
        data["achv_points"] = ach.get("total_points")
    except BnetError:
        pass
    try:
        mo = _get(f"{base}/collections/mounts", {"namespace": ns})
        data["mounts"] = len(mo.get("mounts") or [])
    except BnetError:
        pass
    try:
        pe = _get(f"{base}/collections/pets", {"namespace": ns})
        data["pets"] = len(pe.get("pets") or [])
    except BnetError:
        pass
    try:
        mk = _get(f"{base}/mythic-keystone-profile", {"namespace": ns})
        cur = mk.get("current_mythic_rating") or {}
        data["mplus_rating"] = cur.get("rating") if isinstance(cur, dict) else None
    except BnetError:
        pass
    return data, _store(key, data)


def mystic_rating(realm: str, name: str, force: bool = False) -> tuple[dict, float]:
    """Rating Mythique+ courant d'un personnage (léger : 1 seul appel API)."""
    realm, name = realm.lower(), name.lower()
    key = f"mk/{realm}/{name}"
    hit = _cached(key, force)
    if hit:
        return hit["data"], hit["ts"]
    base = f"/profile/wow/character/{urllib.parse.quote(realm)}/{urllib.parse.quote(name)}"
    rating = None
    try:
        mk = _get(f"{base}/mythic-keystone-profile", {"namespace": f"profile-{REGION}"})
        cur = mk.get("current_mythic_rating") or {}
        rating = cur.get("rating") if isinstance(cur, dict) else None
    except BnetError:
        pass
    data = {"name": name, "rating": rating}
    return data, _store(key, data)


# ---------------------------------------------------------------------------
# Objets (comparateur de pièces — Top Stuff)
# ---------------------------------------------------------------------------
SLOT_FR = {
    "head": "Tête", "neck": "Cou", "shoulder": "Épaules", "chest": "Torse",
    "waist": "Taille", "legs": "Jambes", "feet": "Pieds", "wrist": "Poignets",
    "hands": "Mains", "back": "Dos", "finger1": "Anneau 1", "finger2": "Anneau 2",
    "trinket1": "Bijou 1", "trinket2": "Bijou 2",
}
# inventory_type Blizzard -> emplacement(s) SimC (les doubles = deux profilesets)
SLOT_EN = {
    "head": "Head", "neck": "Neck", "shoulder": "Shoulders", "chest": "Chest",
    "waist": "Waist", "legs": "Legs", "feet": "Feet", "wrist": "Wrist",
    "hands": "Hands", "back": "Back", "finger1": "Ring 1", "finger2": "Ring 2",
    "trinket1": "Trinket 1", "trinket2": "Trinket 2",
}


def slot_label(slot: str, locale: str | None = None) -> str:
    """Libellé d'emplacement SimC dans la langue demandée (FR par défaut)."""
    m = SLOT_EN if _loc(locale).startswith("en") else SLOT_FR
    return m.get(slot or "", slot or "")


INV_TO_SLOTS = {
    "HEAD": ["head"], "NECK": ["neck"], "SHOULDER": ["shoulder"],
    "CHEST": ["chest"], "ROBE": ["chest"], "BODY": ["chest"],
    "WAIST": ["waist"], "LEGS": ["legs"], "FEET": ["feet"], "WRIST": ["wrist"],
    "HANDS": ["hands"], "BACK": ["back"], "CLOAK": ["back"],
    "FINGER": ["finger1", "finger2"], "TRINKET": ["trinket1", "trinket2"],
}


# ---------------------------------------------------------------------------
# Recettes du jeu (Game Data — base « préparation de raid »)
# ---------------------------------------------------------------------------
def game_profession(prof_id: int, locale: str | None = None) -> dict:
    """Métier du jeu + ses paliers d'extension (noms localisés)."""
    return _get(f"/data/wow/profession/{int(prof_id)}",
                {"namespace": f"static-{REGION}", "locale": _loc(locale)},
                not_found="Métier introuvable.")


def game_tier_recipes(prof_id: int, tier_id: int, locale: str | None = None) -> list[dict]:
    """Recettes d'un palier d'extension (id + nom)."""
    raw = _get(f"/data/wow/profession/{int(prof_id)}/skill-tier/{int(tier_id)}",
               {"namespace": f"static-{REGION}", "locale": _loc(locale)},
               not_found="Palier introuvable.")
    out = []
    for cat in raw.get("categories") or []:
        for r in cat.get("recipes") or []:
            if r.get("id"):
                out.append({"id": r["id"], "name": r.get("name") or ""})
    return out


def game_recipe(recipe_id: int, locale: str | None = None) -> dict:
    """Détail d'une recette : objet fabriqué + compos (quantités)."""
    return _get(f"/data/wow/recipe/{int(recipe_id)}",
                {"namespace": f"static-{REGION}", "locale": _loc(locale)},
                not_found="Recette introuvable.")


def mplus_dungeons() -> tuple[dict, float]:
    """Donjons M+ de la saison courante (Raider.IO) avec les noms FR (API journal Blizzard)."""
    key = "mplus_dungeons"
    hit = _cached(key, False)
    if hit:
        return hit["data"], hit["ts"]
    dungeons_en: list[str] = []
    for exp_id in (11, 10, 9):  # Midnight, puis replis
        try:
            req = urllib.request.Request(
                f"https://raider.io/api/v1/mythic-plus/static-data?expansion_id={exp_id}",
                headers={"User-Agent": "lotp-guild-app/1.0"},
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.load(resp)
        except Exception:  # noqa: BLE001 — source externe : on tente l'extension suivante
            continue
        seasons = [s for s in (data.get("seasons") or []) if s.get("is_main_season")]
        if not seasons:
            seasons = data.get("seasons") or []
        if not seasons:
            continue
        seasons.sort(key=lambda s: str((s.get("starts") or {}).get("eu") or ""))  # la plus récente
        dungeons_en = [str(x.get("name")) for x in (seasons[-1].get("dungeons") or []) if x.get("name")]
        if dungeons_en:
            break
    out: list[dict] = []
    if dungeons_en:
        en_map: dict = {}
        try:
            idx = _get("/data/wow/journal-instance/index",
                       {"namespace": f"static-{REGION}", "locale": "en_US"})
            en_map = {x.get("name"): x.get("id") for x in (idx.get("instances") or [])}
        except BnetError:
            pass
        for nom in dungeons_en:
            fr = nom
            iid = en_map.get(nom)
            if iid:
                try:
                    det = _get(f"/data/wow/journal-instance/{iid}",
                               {"namespace": f"static-{REGION}", "locale": LOCALE})
                    fr = det.get("name") or nom
                except BnetError:
                    pass
            out.append({"en": nom, "name": fr})
    data = {"dungeons": out}
    return data, _store(key, data)


def item(item_id: int, locale: str | None = None) -> dict:
    """Objet (nom, qualité, emplacement, icône) depuis l'API Blizzard — localisé, cache 30 min."""
    loc = _loc(locale)
    key = f"item/{loc}/{int(item_id)}"
    hit = _cached(key, False)
    if hit:
        return hit["data"]
    raw = _get(
        f"/data/wow/item/{int(item_id)}",
        {"namespace": f"static-{REGION}", "locale": loc},
        not_found="Pièce introuvable (identifiant invalide ?).",
    )
    inv = raw.get("inventory_type") or {}
    icon = None
    try:
        media = _get(f"/data/wow/media/item/{int(item_id)}", {"namespace": f"static-{REGION}"})
        icon = next((a.get("value") for a in media.get("assets", []) if a.get("key") == "icon"), None)
    except BnetError:
        pass
    data = {
        "id": int(item_id),
        "name": raw.get("name") or f"Objet {item_id}",
        "quality": (raw.get("quality") or {}).get("type") or "COMMON",
        "inv_type": inv.get("type") or "",
        "inv_type_fr": inv.get("name") or "",
        "subclass": (raw.get("item_subclass") or {}).get("name") or "",
        "icon": icon,
    }
    _store(key, data)
    return data

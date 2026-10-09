"""Cohors — guild pages: guild info, leaderboards, progression, recently seen, crafting directory, fun awards."""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app import bnet, wcl
from app.core.auth import _require_admin, _require_user, _user_locale, _user_role
from app.core.db import _db, _db_lock
from app.core.util import CLASS_KEY_FR, _pick, _snap_day
from app.services.crafting import PROF_EN

router = APIRouter()


GUILD_INFO_KEYS = ("intro", "discord_url", "discord_note", "ts_host", "ts_password", "ts_note", "web_url", "web_note")


@router.get("/api/guild/info")
def api_guild_info(request: Request):
    user = _require_user(request)
    with _db_lock, _db() as conn:
        rows = conn.execute("SELECT key, value, updated FROM guild_info").fetchall()
    items = {r["key"]: r["value"] for r in rows}
    updated = max(((r["updated"] or 0.0) for r in rows), default=0.0)
    return {"items": items, "updated": updated, "can_edit": _user_role(user) == "admin"}


class GuildInfoRequest(BaseModel):
    items: dict[str, str]


@router.post("/api/guild/info")
def update_guild_info(payload: GuildInfoRequest, request: Request):
    user = _require_admin(request)
    now = time.time()
    saved = 0
    with _db_lock, _db() as conn:
        for k, v in (payload.items or {}).items():
            if k not in GUILD_INFO_KEYS or not isinstance(v, str):
                continue
            conn.execute(
                "UPDATE guild_info SET value=?, updated=?, updated_by=? WHERE key=?",
                (v.strip()[:2000], now, user["name"] or user["email"], k),
            )
            saved += 1
    return {"ok": True, "saved": saved}


# ---------------------------------------------------------------------------
# Classements de guilde (parses récents + clés M+ des mains liés)
# ---------------------------------------------------------------------------
_LB_CACHE: dict = {"ts": 0.0, "data": None}
LB_TTL = 1800.0


def _build_leaderboard() -> dict:
    """Agrège les parses des derniers rapports WCL + le rating M+ des mains liés."""
    data: dict = {"parses": [], "mplus": [], "reports": 0, "built": time.time()}
    try:
        rl, _ts = wcl.reports(limit=8)
        rows: list[dict] = []
        count = 0
        for rep in (rl.get("data") or []):
            code = rep.get("code")
            try:
                full, _t = wcl.report_full(code)
            except wcl.WclError:
                continue
            ranks = full.get("rankings") or {}
            if not ranks:
                continue
            count += 1
            fights = {str(f.get("id")): f for f in (full["report"].get("fights") or [])}
            for fid, entry in ranks.items():
                if not entry.get("kill"):
                    continue
                f = fights.get(str(fid)) or {}
                boss = (entry.get("encounter") or {}).get("name") or f.get("name") or "?"
                diff = entry.get("difficulty") or f.get("difficulty") or 0
                for role in ("dps", "tanks", "healers"):
                    for c in (((entry.get("roles") or {}).get(role) or {}).get("characters") or []):
                        if c.get("rankPercent") is None or not c.get("name"):
                            continue
                        rows.append({
                            "name": c.get("name"), "class": c.get("class"), "spec": c.get("spec"),
                            "amount": c.get("amount"), "percent": c.get("rankPercent"),
                            "boss": boss, "role": role, "difficulty": diff,
                            "report": code, "date": full["report"].get("startTime"),
                        })
        rows.sort(key=lambda r: (r.get("percent") or 0), reverse=True)
        data["parses"] = rows[:500]
        data["reports"] = count
    except wcl.WclError as exc:
        data["error"] = str(exc)
    try:
        with _db_lock, _db() as conn:
            mains = conn.execute(
                "SELECT realm, name, display FROM char_links WHERE is_main=1"
            ).fetchall()
        mplus = []
        seen_names: set = set()
        for m in mains[:40]:
            key = (m["name"] or "").lower()
            if not key or key in seen_names:
                continue
            seen_names.add(key)
            try:
                mk, _t = bnet.mystic_rating(m["realm"], m["name"])
            except bnet.BnetError:
                continue
            if mk.get("rating"):
                mplus.append({"name": m["display"] or m["name"], "rating": mk["rating"]})
        mplus.sort(key=lambda r: r["rating"], reverse=True)
        data["mplus"] = mplus
    except Exception:  # noqa: BLE001
        pass
    return data


def _progression_data(days: int = 30, locale: str = "fr_FR") -> dict:
    """Classement des progressions (relevés quotidiens) + courbe iLvl moyen.

    Fenêtre glissante de « days » jours (7 ou 30). Les gains comparent le premier et
    le dernier relevé de chaque personnage dans la fenêtre.
    """
    days = 7 if int(days) == 7 else 30
    want_en = locale.startswith("en")
    cutoff = _snap_day(time.time() - (days - 1) * 86400)
    with _db_lock, _db() as conn:
        rows = conn.execute(
            "SELECT realm, name, day, data FROM char_snapshots WHERE day >= ? ORDER BY name, day",
            (cutoff,),
        ).fetchall()
    per: dict[tuple, list] = {}
    for r in rows:
        try:
            d = json.loads(r["data"])
        except (ValueError, TypeError):
            continue
        per.setdefault((r["realm"], r["name"]), []).append((r["day"], d))

    def gain(f: dict, l: dict, key: str):
        a, b = f.get(key), l.get(key)
        return (b - a) if (a is not None and b is not None) else None

    out_rows, measured = [], 0
    day_ilvl: dict[str, dict] = {}      # jour -> {(royaume, nom): iLvl} (persos niveau 90)
    for (realm, name), snaps in per.items():
        snaps.sort(key=lambda t: t[0])
        if len(snaps) >= 2:
            measured += 1
        for day, d in snaps:
            lvl = d.get("level")
            if d.get("ilvl") is not None and (lvl is None or lvl >= 90):
                day_ilvl.setdefault(day, {})[(realm, name)] = d["ilvl"]
        if len(snaps) < 2:
            continue
        fd, f = snaps[0]
        ld, l = snaps[-1]
        gains = {k: gain(f, l, k) for k in ("ilvl", "achv", "mounts", "pets", "mplus")}
        if not any(v is not None and v != 0 for v in gains.values()):
            continue
        out_rows.append({
            "realm": realm, "name": name,
            "class": _pick(l, "class", want_en) or _pick(f, "class", want_en),
            "spec": _pick(l, "spec", want_en) or _pick(f, "spec", want_en),
            "class_key": CLASS_KEY_FR.get((l.get("class") or f.get("class")) or ""),
            "first_day": fd, "last_day": ld,
            "ilvl0": f.get("ilvl"), "ilvl1": l.get("ilvl"),
            "d_ilvl": gains["ilvl"], "d_achv": gains["achv"], "d_mounts": gains["mounts"],
            "d_pets": gains["pets"], "d_mplus": gains["mplus"],
        })
    out_rows.sort(key=lambda r: (r["d_ilvl"] if r["d_ilvl"] is not None else -10**6,
                                 r["d_achv"] if r["d_achv"] is not None else -10**6), reverse=True)

    # courbe : population constante (présente au 1er ET au dernier jour éligibles) — moyenne comparable
    days_sorted = sorted(day_ilvl)
    curve: list = []
    pop: set = set()
    base = next((d for d in days_sorted if len(day_ilvl[d]) >= 5), None)
    if base:
        end = days_sorted[-1]
        pop = set(day_ilvl[base]) & set(day_ilvl[end])
        if len(pop) >= 3:
            for day in days_sorted:
                vals = [v for k, v in day_ilvl[day].items() if k in pop]
                if len(vals) >= 3:
                    curve.append({"day": day, "avg": round(sum(vals) / len(vals), 1), "n": len(vals)})
        else:
            pop = set()
    return {"days": days, "built": time.time(), "rows": out_rows, "curve": curve,
            "measured": measured, "progressed": len(out_rows), "curve_pop": len(pop)}


@router.get("/api/progression")
def api_progression(request: Request, days: int = 30):
    """Classement des progressions (relevés quotidiens) + courbe iLvl moyen (7 ou 30 j)."""
    _require_user(request)
    return _progression_data(days, _user_locale(request))


# Spécialisations (noms FR renvoyés par l'API) → rôle : tank / heal / dps.
SPEC_ROLE = {
    "Sang": "tank", "Vengeance": "tank", "Gardien": "tank", "Maître brasseur": "tank", "Protection": "tank",
    "Restauration": "heal", "Sacré": "heal", "Discipline": "heal", "Tisse-brume": "heal", "Préservation": "heal",
    "Givre": "dps", "Impie": "dps", "Dévastation": "dps", "Équilibre": "dps", "Farouche": "dps",
    "Augmentation": "dps", "Maîtrise des bêtes": "dps", "Précision": "dps", "Survie": "dps",
    "Arcanes": "dps", "Feu": "dps", "Marche-vent": "dps", "Vindicte": "dps", "Ombre": "dps",
    "Assassinat": "dps", "Hors-la-loi": "dps", "Finesse": "dps", "Élémentaire": "dps",
    "Amélioration": "dps", "Affliction": "dps", "Démonologie": "dps", "Destruction": "dps",
    "Armes": "dps", "Fureur": "dps", "Dévoration": "dps",
}


@router.get("/api/avail")
def api_avail(request: Request, hours: int = 24):
    """Vu dernièrement : persos niveau max vus récemment (relevé du jour), groupés par rôle."""
    _require_user(request)
    want_en = _user_locale(request).startswith("en")
    hours = hours if hours in (24, 48, 168) else 24
    cutoff = time.time() - hours * 3600
    with _db_lock, _db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT realm, name, data FROM char_snapshots WHERE day = ?", (_snap_day(),)).fetchall()]
    disp: dict[str, str] = {}
    try:
        roster, _t = bnet.roster()
        for m in (roster.get("members") or []):
            k = (m.get("name") or "").lower()
            if k:
                disp[k] = m.get("name") or k
    except bnet.BnetError:
        pass
    out = []
    for r in rows:
        try:
            d = json.loads(r["data"]) or {}
        except (ValueError, TypeError):
            continue
        lvl = d.get("level")
        if lvl is not None and lvl < 90:
            continue
        if d.get("ilvl") is None:
            continue
        seen = d.get("last_login")
        seen_s = (seen / 1000) if seen else None
        if seen_s is not None and seen_s < cutoff:
            continue
        k = r["name"]
        out.append({
            "name": disp.get(k) or k, "key": k, "realm": r["realm"],
            "class_key": CLASS_KEY_FR.get(d.get("class") or ""),
            "spec": _pick(d, "spec", want_en), "role": SPEC_ROLE.get(d.get("spec") or ""),
            "ilvl": d.get("ilvl"), "level": lvl, "seen": seen_s,
        })
    out.sort(key=lambda x: -(x.get("ilvl") or 0))
    return {"hours": hours, "built": time.time(), "rows": out}


PROF_ORDER = ["Alchimie", "Calligraphie", "Couture", "Dépeçage", "Enchantement", "Forge",
              "Herboristerie", "Ingénierie", "Joaillerie", "Minéralogie", "Travail du cuir",
              "Archéologie", "Cuisine", "Pêche"]


@router.get("/api/craft")
def api_craft(request: Request):
    """Annuaire d'artisanat : qui peut crafter quoi (métiers de tout le roster)."""
    _require_user(request)
    want_en = _user_locale(request).startswith("en")
    with _db_lock, _db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT realm, name, ts, data FROM char_professions").fetchall()]
        cls_by_name: dict[str, str] = {}
        for row in conn.execute("SELECT name, data FROM char_snapshots WHERE day = ?", (_snap_day(),)).fetchall():
            try:
                c = (json.loads(row["data"]) or {}).get("class")
            except (ValueError, TypeError):
                c = None
            if c:
                cls_by_name[row["name"]] = c
    disp: dict[str, str] = {}
    realms: dict[str, str] = {}
    try:
        roster, _t = bnet.roster()
        for m in (roster.get("members") or []):
            k = (m.get("name") or "").lower()
            if k:
                disp[k] = m.get("name") or k
                realms[k] = m.get("realm") or bnet.GUILD_REALM
    except bnet.BnetError:
        pass
    groups: dict[str, list] = {}
    with_profs = 0
    for r in rows:
        try:
            d = json.loads(r["data"]) or {}
        except (ValueError, TypeError):
            continue
        profs = d.get("profs") or []
        if not profs:
            continue
        with_profs += 1
        k = r["name"]
        for p in profs:
            nm = (_pick(p, "name", want_en)) or "?"
            if want_en:
                nm = PROF_EN.get(nm, nm)  # lignes pas encore rafraîchies : libellé FR → nom anglais
            groups.setdefault(nm, []).append({
                "name": disp.get(k) or k, "key": k,
                "realm": realms.get(k) or r["realm"],
                "class_key": CLASS_KEY_FR.get(cls_by_name.get(k) or ""),
                "points": p.get("points"), "max": p.get("max"), "tier": p.get("tier"),
            })
    order = {n: i for i, n in enumerate(
        [PROF_EN.get(x, x) if want_en else x for x in PROF_ORDER])}
    profs_out = []
    for nm, lst in groups.items():
        lst.sort(key=lambda x: (-(x.get("points") or 0), (x["name"] or "").lower()))
        profs_out.append({"name": nm, "members": lst})
    profs_out.sort(key=lambda g: (order.get(g["name"], 99), g["name"]))
    return {"built": time.time(), "chars": len(rows), "with_profs": with_profs, "professions": profs_out}


@router.get("/api/leaderboard")
def api_leaderboard(request: Request, refresh: int = 0):
    _require_user(request)
    now = time.time()
    with _db_lock:
        cached = _LB_CACHE["data"]
        age = now - _LB_CACHE["ts"]
    if cached is not None and ((not refresh and age < LB_TTL) or (refresh and age < 60)):
        return cached
    data = _build_leaderboard()
    with _db_lock:
        _LB_CACHE["ts"] = time.time()
        _LB_CACHE["data"] = data
    return data


# ---------------------------------------------------------------------------
# Succès fun (palmarès rigolo de la guilde)
# ---------------------------------------------------------------------------
_FUN_CACHE: dict = {"ts": 0.0, "data": None}
FUN_TTL = 1800.0


def _build_fun() -> dict:
    """Agrège le palmarès fun : morts WCL + stats internes (sims, présences, partage)."""
    out: dict = {
        "cemetery": [], "massacre": None, "first_blood": [], "cause": None,
        "intouchables": [], "scholars": [], "pillars": [], "collectors": [], "hearts": [],
        "built": time.time(),
    }
    try:
        rl, _ts = wcl.reports(limit=6)
        cemetery: dict = {}
        firsts: dict = {}
        participation: dict = {}
        causes: dict = {}
        massacre = None
        for rep in (rl.get("data") or []):
            code = rep.get("code")
            try:
                full, _t = wcl.report_full(code)
            except wcl.WclError:
                continue
            fights = {f["id"]: f for f in (full["report"].get("fights") or [])}
            if not fights:
                continue
            try:
                death_rows, _t2 = wcl.deaths(code)
            except wcl.WclError:
                death_rows = []
            per_fight: dict = {}
            first_ts: dict = {}
            for de in death_rows:
                nm = de.get("name")
                if not nm:
                    continue
                row = cemetery.setdefault(nm, {"name": nm, "class": de.get("class"),
                                               "spec": de.get("spec"), "deaths": 0})
                row["deaths"] += 1
                fid = de.get("fight")
                per_fight[fid] = per_fight.get(fid, 0) + 1
                ts = de.get("timestamp")
                if ts is not None and (fid not in first_ts or ts < first_ts[fid][1]):
                    first_ts[fid] = (nm, ts)
                killer = de.get("killer")
                if killer:
                    causes[killer] = causes.get(killer, 0) + 1
            for nm, _t3 in first_ts.values():
                firsts[nm] = firsts.get(nm, 0) + 1
            for fid, n in per_fight.items():
                if not massacre or n > massacre["deaths"]:
                    f = fights.get(fid) or {}
                    massacre = {"boss": f.get("name") or "?", "deaths": n, "kill": bool(f.get("kill")),
                                "report": code, "date": full["report"].get("startTime")}
            for _fid, entry in (full.get("rankings") or {}).items():
                if not entry.get("kill"):
                    continue
                for role in ("dps", "tanks", "healers"):
                    for c in (((entry.get("roles") or {}).get(role) or {}).get("characters") or []):
                        nm = c.get("name")
                        if nm:
                            participation[nm] = participation.get(nm, 0) + 1
        out["cemetery"] = sorted(cemetery.values(), key=lambda r: -r["deaths"])[:10]
        out["massacre"] = massacre
        out["first_blood"] = sorted(
            ({"name": k, "count": v} for k, v in firsts.items()), key=lambda r: -r["count"]
        )[:5]
        if causes:
            top_cause = max(causes.items(), key=lambda kv: kv[1])
            out["cause"] = {"name": top_cause[0], "count": top_cause[1]}
        else:
            out["cause"] = None
        tomb = set(cemetery)
        out["intouchables"] = sorted(
            ({"name": k, "fights": v} for k, v in participation.items() if v >= 5 and k not in tomb),
            key=lambda r: -r["fights"],
        )[:5]
    except wcl.WclError as exc:
        out["error"] = str(exc)
    try:
        with _db_lock, _db() as conn:
            out["scholars"] = [dict(r) for r in conn.execute(
                "SELECT user_name AS name, COUNT(*) AS n FROM sims "
                "WHERE user_name IS NOT NULL AND user_name<>'' GROUP BY user_name ORDER BY n DESC LIMIT 5"
            ).fetchall()]
            out["pillars"] = [dict(r) for r in conn.execute(
                "SELECT COALESCE(u.name, s.user_email) AS name, COUNT(*) AS n FROM raid_signups s "
                "LEFT JOIN users u ON u.email = s.user_email WHERE s.status='yes' "
                "GROUP BY s.user_email ORDER BY n DESC LIMIT 5"
            ).fetchall()]
            out["hearts"] = [dict(r) for r in conn.execute(
                "SELECT COALESCE(u.name, p.user_email) AS name, COUNT(*) AS n FROM profiles p "
                "LEFT JOIN users u ON u.email = p.user_email WHERE p.shared=1 "
                "GROUP BY p.user_email ORDER BY n DESC LIMIT 5"
            ).fetchall()]
            mains = conn.execute("SELECT realm, name, display FROM char_links WHERE is_main=1").fetchall()
        coll = []
        seen: set = set()
        for m in mains[:30]:
            k = (m["name"] or "").lower()
            if not k or k in seen:
                continue
            seen.add(k)
            try:
                ex, _t = bnet.extras(m["realm"], m["name"])
            except bnet.BnetError:
                continue
            if ex.get("mounts"):
                coll.append({"name": m["display"] or m["name"], "mounts": ex["mounts"],
                             "pets": ex.get("pets") or 0, "achv": ex.get("achv_points") or 0})
        coll.sort(key=lambda r: -r["mounts"])
        out["collectors"] = coll[:5]
    except Exception:  # noqa: BLE001
        pass
    return out


@router.get("/api/fun")
def api_fun(request: Request, refresh: int = 0):
    _require_user(request)
    now = time.time()
    with _db_lock:
        cached = _FUN_CACHE["data"]
        age = now - _FUN_CACHE["ts"]
    if cached is not None and ((not refresh and age < FUN_TTL) or (refresh and age < 60)):
        return cached
    data = _build_fun()
    with _db_lock:
        _FUN_CACHE["ts"] = time.time()
        _FUN_CACHE["data"] = data
    return data

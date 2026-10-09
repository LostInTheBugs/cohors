"""Cohors — Battle.net and Warcraft Logs data: roster, character sheets, guild progress, raid reports, comparator."""
from __future__ import annotations

import re
import threading
import time

from fastapi import APIRouter, HTTPException, Request

from app import bnet, wcl
from app.core.auth import _require_user, _user_locale
from app.core.db import _db, _db_lock
from app.core.util import CLASS_KEY_FR, _valid_char

router = APIRouter()


def _bnet_call(fn, realm: str, name: str, refresh: int = 0, locale: str | None = None) -> dict:
    _valid_char(realm, name)
    try:
        data, ts = fn(realm, name, force=bool(refresh), locale=locale)
    except bnet.BnetError as exc:
        raise HTTPException(exc.status if exc.status in (400, 404) else 502, str(exc))
    data = dict(data)
    if data.get("class"):
        data["class_key"] = CLASS_KEY_FR.get(data["class"]) or re.sub(r"\s+", "", data["class"])
    data["fetched_at"] = ts
    return data


@router.get("/api/roster")
def api_roster(request: Request, refresh: int = 0):
    _require_user(request)
    try:
        data, ts = bnet.roster(force=bool(refresh))
    except bnet.BnetError as exc:
        raise HTTPException(exc.status if exc.status in (400, 404) else 502, str(exc))
    return {
        "guild": data["guild"],
        "realm": data["realm"],
        "region": data["region"],
        "members": data["members"],
        "count": len(data["members"]),
        "fetched_at": ts,
    }


@router.get("/api/char/{realm}/{name}/summary")
def api_char_summary(realm: str, name: str, request: Request, refresh: int = 0):
    _require_user(request)
    return _bnet_call(bnet.character, realm, name, refresh, locale=_user_locale(request))


@router.get("/api/char/{realm}/{name}/extras")
def api_char_extras(realm: str, name: str, request: Request, refresh: int = 0):
    _require_user(request)
    return _bnet_call(bnet.extras, realm, name, refresh, locale=_user_locale(request))


@router.get("/api/char/{realm}/{name}/talents")
def api_char_talents(realm: str, name: str, request: Request, refresh: int = 0):
    _require_user(request)
    return _bnet_call(bnet.talents, realm, name, refresh, locale=_user_locale(request))


@router.get("/api/char/{realm}/{name}/raids-progress")
def api_char_raids_progress(realm: str, name: str, request: Request, refresh: int = 0):
    _require_user(request)
    return _bnet_call(bnet.raid_progress, realm, name, refresh, locale=_user_locale(request))


@router.get("/api/char/{realm}/{name}/dungeons-progress")
def api_char_dungeons_progress(realm: str, name: str, request: Request, refresh: int = 0):
    _require_user(request)
    return _bnet_call(bnet.dungeon_progress, realm, name, refresh, locale=_user_locale(request))


# ---- Progression de la guilde (v2026.09.154) ----
_GPROG: dict = {}
_GPROG_TTL = 900.0
_gprog_lock = threading.Lock()


def _guild_progress_aggregate(results: list, names: dict, mains: set) -> dict:
    """[(membre roster, progression bnet|None)] -> boss × difficulté × membres, et résumé par membre.

    Blizzard ne liste que les boss tués : l'ordre et les boss manquants viennent du journal (`names`).
    """
    order = (names or {}).get("order") or {}
    enc_names = (names or {}).get("enc") or {}
    insts: dict = {}
    diffs: dict = {}
    members = []
    expansion = ""
    for m, prog in results:
        if not prog:
            continue
        expansion = expansion or prog.get("expansion") or ""
        done: dict = {}
        for ins in prog.get("instances") or []:
            iid = ins.get("id")
            it = insts.setdefault(iid, {"id": iid, "name": ins.get("name") or "?", "total": 0, "bosses": {}})
            for mo in ins.get("modes") or []:
                d = mo.get("difficulty") or ""
                diffs.setdefault(d, mo.get("label") or d)
                it["total"] = max(it["total"], int(mo.get("total") or 0))
                done[d] = done.get(d, 0) + int(mo.get("done") or 0)
                for b in mo.get("bosses") or []:
                    bb = it["bosses"].setdefault(b.get("id"), {"id": b.get("id"), "name": b.get("name") or "?",
                                                               "kills": {}})
                    bb["kills"].setdefault(d, []).append(m["name"])
        members.append({"name": m["name"], "realm": m.get("realm") or "",
                        "main": (m["name"] or "").lower() in mains, "done": done})
    rank = {d: i for i, d in enumerate(bnet.DIFF_ORDER)}
    out_insts = []
    for iid, it in insts.items():
        ids = list((order.get(iid) or order.get(str(iid)) or []))
        ids += [bid for bid in it["bosses"] if bid not in ids]
        bosses = [it["bosses"].get(bid) or {"id": bid, "name": enc_names.get(bid) or "?", "kills": {}}
                  for bid in ids]
        out_insts.append({"id": iid, "name": it["name"], "total": max(it["total"], len(ids)),
                          "bosses": bosses})
    grand = sum(i["total"] for i in out_insts)
    for mm in members:
        best = None
        for d, n in mm["done"].items():
            if n > 0 and (best is None or rank.get(d, 99) > rank.get(best, 99)):
                best = d
        mm["best"] = ({"difficulty": best, "label": diffs.get(best, best), "done": mm["done"][best],
                       "total": grand} if best else None)
    members.sort(key=lambda x: (-(rank.get((x["best"] or {}).get("difficulty"), -1)),
                                -((x["best"] or {}).get("done") or 0), x["name"].lower()))
    diff_list = [{"difficulty": d, "label": diffs[d]}
                 for d in sorted(diffs, key=lambda d: rank.get(d, 99)) if d]
    return {"expansion": expansion, "diffs": diff_list, "instances": out_insts, "members": members}


def _guild_progress(kind: str, locale: str, force: bool = False) -> dict:
    """Agrège la progression des membres de niveau max du roster (cache 15 min)."""
    key = f"{kind}/{locale}"
    with _gprog_lock:
        hit = _GPROG.get(key)
    if hit and not force and time.time() - hit["ts"] < _GPROG_TTL:
        return hit["data"]
    roster, _ts = bnet.roster()
    mem = [m for m in roster.get("members") or [] if m.get("name")]
    top = max((int(m.get("level") or 0) for m in mem), default=0)
    mem = [m for m in mem if int(m.get("level") or 0) == top]
    with _db_lock, _db() as conn:
        mains = {str(r["name"] or "").lower()
                 for r in conn.execute("SELECT name FROM char_links WHERE is_main=1").fetchall()}
    fn = bnet.raid_progress if kind == "raid" else bnet.dungeon_progress

    def one(m):
        try:
            return m, fn(m.get("realm") or bnet.GUILD_REALM, m["name"], locale=locale)[0]
        except bnet.BnetError:
            return m, None

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, mem))
    # donjons : Blizzard ne suit que le dernier boss -> pas de liste de boss du journal
    names = bnet.journal_names(locale) if kind == "raid" else {}
    data = _guild_progress_aggregate(results, names, mains)
    data.update({"kind": kind, "level": top, "scanned": len(mem), "fetched_at": time.time()})
    with _gprog_lock:
        _GPROG[key] = {"ts": time.time(), "data": data}
    return data


@router.get("/api/guild/progress")
def api_guild_progress(request: Request, kind: str = "raid", refresh: int = 0):
    """Progression de la guilde (membres de niveau max) : boss × difficulté, meilleure progression par membre."""
    _require_user(request)
    if kind not in ("raid", "dungeon"):
        raise HTTPException(400, "kind doit valoir raid ou dungeon.")
    try:
        return _guild_progress(kind, _user_locale(request), force=bool(refresh))
    except bnet.BnetError as exc:
        raise HTTPException(exc.status if exc.status in (400, 404) else 502, str(exc))


@router.get("/api/char/{realm}/{name}/equipment")
def api_char_equipment(realm: str, name: str, request: Request, refresh: int = 0):
    _require_user(request)
    return _bnet_call(bnet.equipment, realm, name, refresh, locale=_user_locale(request))


# ---------------------------------------------------------------------------
# WCL API — rapports de raid (cache serveur)
# ---------------------------------------------------------------------------
_WCL_CODE_RE = re.compile(r"^[A-Za-z0-9]{12,24}$")


@router.get("/api/wcl/reports")
def api_wcl_reports(request: Request, refresh: int = 0, limit: int = 30):
    _require_user(request)
    try:
        data, ts = wcl.reports(limit=min(max(limit, 5), 50), force=bool(refresh))
    except wcl.WclError as exc:
        raise HTTPException(exc.status if exc.status in (400, 404) else 502, str(exc))
    return {"reports": data, "fetched_at": ts}


@router.get("/api/wcl/report/{code}")
def api_wcl_report(code: str, request: Request, refresh: int = 0):
    _require_user(request)
    if not _WCL_CODE_RE.match(code):
        raise HTTPException(400, "Code de rapport invalide.")
    try:
        data, ts = wcl.report_full(code, force=bool(refresh))
    except wcl.WclError as exc:
        raise HTTPException(exc.status if exc.status in (400, 404) else 502, str(exc))
    data = dict(data)
    data["fetched_at"] = ts
    return data


@router.get("/api/compare")
def api_compare(request: Request, chars: str = "", refresh: int = 0):
    _require_user(request)
    items = [c.strip() for c in chars.split(",") if c.strip()][:6]
    out = []
    for item in items:
        realm, _, name = item.partition(":")
        realm, name = realm.strip(), name.strip()
        if not realm or not name:
            continue
        _valid_char(realm, name)
        entry: dict = {"realm": realm.lower(), "name": name}
        try:
            summary, _ts = bnet.character(realm, name, force=bool(refresh), locale=_user_locale(request))
            if summary.get("class"):
                summary = dict(summary)
                summary["class_key"] = CLASS_KEY_FR.get(summary["class"]) or re.sub(r"\s+", "", summary["class"])
            entry["summary"] = summary
        except bnet.BnetError as exc:
            entry["summary"] = None
            entry["bnet_error"] = str(exc)
        try:
            zr, _ts = wcl.character_rankings(realm, name, force=bool(refresh))
            entry["wcl"] = zr
        except wcl.WclError as exc:
            entry["wcl"] = None
            entry["wcl_error"] = str(exc)
        out.append(entry)
    return {"chars": out, "zone_id": wcl.RAID_ZONE_ID, "zone_label": wcl.zone_label()}

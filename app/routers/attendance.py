"""Cohors — raid attendance from Warcraft Logs reports (cached)."""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Request

from app import bnet, wcl
from app.core.auth import _require_user, _user_locale
from app.core.db import _db, _db_lock
from app.core.util import CLASS_KEY_FR, _snap_day

router = APIRouter()


def _att_localized(data: dict, locale: str) -> dict:
    """Assiduité : sert la classe dans la langue demandée (les clés/parcours restent FR)."""
    if not locale.startswith("en"):
        return data
    out = dict(data)
    out["rows"] = [dict(r, **{"class": r.get("class_en") or r.get("class")}) for r in (data.get("rows") or [])]
    return out

_ATT_CACHE: dict = {"ts": 0.0, "days": 0, "data": None}
ATT_TTL = 900.0


@router.get("/api/attendance")
def api_attendance(request: Request, days: int = 30, refresh: int = 0):
    """Assiduité réelle aux soirées de raid (logs Warcraft Logs) sur les N derniers jours."""
    _require_user(request)
    days = days if days in (14, 30, 60) else 30
    now = time.time()
    if (not refresh and _ATT_CACHE["data"] is not None and _ATT_CACHE["days"] == days
            and now - _ATT_CACHE["ts"] < ATT_TTL):
        return _att_localized(_ATT_CACHE["data"], _user_locale(request))
    try:
        rl, _ts = wcl.reports(limit=50, force=bool(refresh))
    except wcl.WclError as exc:
        return {"error": str(exc)}
    cutoff = now - days * 86400
    cls_by_name: dict[str, str] = {}
    cls_en_by_name: dict[str, str] = {}
    with _db_lock, _db() as conn:
        for row in conn.execute(
            "SELECT name, data FROM char_snapshots WHERE day = ?", (_snap_day(),)
        ).fetchall():
            try:
                d_snap = json.loads(row["data"]) or {}
            except (ValueError, TypeError):
                d_snap = {}
            if d_snap.get("class"):
                cls_by_name[row["name"]] = d_snap["class"]
            if d_snap.get("class_en"):
                cls_en_by_name[row["name"]] = d_snap["class_en"]
    roster: dict[str, dict] = {}
    try:
        rl2, _t = bnet.roster()
        roster = {(m.get("name") or "").lower(): m for m in (rl2.get("members") or [])}
    except bnet.BnetError:
        pass
    evenings: list[dict] = []
    for r in rl.get("data") or []:
        st = (r.get("startTime") or 0) / 1000
        if st < cutoff:
            continue
        code = r.get("code")
        try:
            full, _t = wcl.report_full(code, force=bool(refresh))
            comb, _t2 = wcl.report_combatants(code, force=bool(refresh))
        except wcl.WclError as exc:
            print(f"[att] WCL {code}: {exc}")
            continue
        fights = (full.get("report") or {}).get("fights") or []
        boss = [f for f in fights if f.get("encounterID")]
        players = comb.get("players") or {}
        if not boss or not players:
            continue
        evenings.append({
            "code": code, "day": _snap_day(st), "ts": st,
            "zone": (r.get("zone") or {}).get("name") or "",
            "kills": sum(1 for f in boss if f.get("kill")),
            "bosses": len({f.get("encounterID") for f in boss}),
            "players": list(players),
        })
    evenings.sort(key=lambda e: e["ts"])
    total = len(evenings)
    seen: dict[str, dict] = {}
    for e in evenings:
        for pname in e["players"]:
            key = pname.lower()
            d = seen.setdefault(key, {"name": pname, "nights": 0, "last_day": None})
            d["nights"] += 1
            d["last_day"] = e["day"]
    rows = []
    for key, d in seen.items():
        mem = roster.get(key) or {}
        rows.append({
            "name": d["name"], "key": key,
            "realm": mem.get("realm") or bnet.GUILD_REALM,
            "class": cls_by_name.get(key), "class_en": cls_en_by_name.get(key),
            "class_key": CLASS_KEY_FR.get(cls_by_name.get(key) or ""),
            "nights": d["nights"], "pct": round(100 * d["nights"] / total) if total else 0,
            "last_day": d["last_day"], "guest": key not in roster,
        })
    rows.sort(key=lambda r: (-r["nights"], r["name"].lower()))
    data = {"days": days, "built": now, "total": total,
            "evenings": [{k: v for k, v in e.items() if k != "players"} for e in evenings],
            "rows": rows}
    _ATT_CACHE.update({"ts": now, "days": days, "data": data})
    return _att_localized(data, _user_locale(request))

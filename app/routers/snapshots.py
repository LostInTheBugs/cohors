"""Cohors — character history routes: daily snapshots, snapshot diff, manual WCL backfill."""
from __future__ import annotations

import json
import threading

from fastapi import APIRouter, HTTPException, Request

from app.core.auth import _require_admin, _require_user, _user_locale
from app.core.config import SNAP_KEEP_DAYS
from app.core.db import _db, _db_lock
from app.core.util import _pick, _snap_day
from app.services.snapshots import _is_tracked_char, _snap_backfill, _snap_capture, _snap_summary

router = APIRouter()


@router.get("/api/char/{realm}/{name}/history")
def api_char_history(realm: str, name: str, request: Request):
    """Relevés quotidiens (résumés) d'un personnage — du plus ancien au plus récent."""
    _require_user(request)
    want_en = _user_locale(request).startswith("en")
    realm, name = realm.lower(), name.lower()
    with _db_lock, _db() as conn:
        rows = conn.execute(
            "SELECT day, ts, data FROM char_snapshots WHERE realm=? AND name=? ORDER BY day",
            (realm, name),
        ).fetchall()
    days = []
    for r in rows:
        try:
            days.append(_snap_summary(r["day"], r["ts"], json.loads(r["data"]), want_en))
        except ValueError:
            continue
    # personnage suivi et relevé du jour manquant → capture à la volée (sans bloquer la réponse)
    if (not days or days[-1]["day"] != _snap_day()) and _is_tracked_char(realm, name):
        threading.Thread(target=_snap_capture, args=(realm, name), daemon=True).start()
    return {"ok": True, "days": days, "keep_days": SNAP_KEEP_DAYS}


@router.post("/api/admin/snap-backfill")
def admin_snap_backfill(request: Request, days: int = 30):
    """Relance manuelle du rétro-remplissage WCL (admin)."""
    _require_admin(request)
    return _snap_backfill(days=max(1, min(90, days)), force=True)


@router.get("/api/char/{realm}/{name}/snapdiff")
def api_char_snapdiff(realm: str, name: str, request: Request):
    """Différence entre deux relevés (?from=YYYY-MM-DD&to=YYYY-MM-DD ; défaut : début → fin)."""
    _require_user(request)
    want_en = _user_locale(request).startswith("en")
    realm, name = realm.lower(), name.lower()
    frm = (request.query_params.get("from") or "").strip()
    to = (request.query_params.get("to") or "").strip()
    with _db_lock, _db() as conn:
        rows = conn.execute(
            "SELECT day, ts, data FROM char_snapshots WHERE realm=? AND name=? ORDER BY day",
            (realm, name),
        ).fetchall()
    if not rows:
        raise HTTPException(404, "Aucun relevé pour ce personnage.")
    snaps = []
    for r in rows:
        try:
            snaps.append((r["day"], r["ts"], json.loads(r["data"])))
        except ValueError:
            continue
    if not snaps:
        raise HTTPException(404, "Aucun relevé pour ce personnage.")
    by_day = {d: (ts, data) for d, ts, data in snaps}
    a_day = frm if frm in by_day else snaps[0][0]
    b_day = to if to in by_day else snaps[-1][0]
    if a_day > b_day:
        a_day, b_day = b_day, a_day
    a_ts, a = by_day[a_day]
    b_ts, b = by_day[b_day]

    def delta(key: str):
        av, bv = a.get(key), b.get(key)
        if isinstance(av, (int, float)) and isinstance(bv, (int, float)):
            return round(bv - av, 2)
        return None

    a_items = {it.get("slot"): it for it in (a.get("items") or [])}
    b_items = {it.get("slot"): it for it in (b.get("items") or [])}

    def _loc_item(it):
        if not it:
            return it
        out = dict(it)
        out["slot"] = _pick(it, "slot", want_en)
        out["name"] = _pick(it, "name", want_en)
        return out

    items = []
    for slot in list(dict.fromkeys(list(a_items.keys()) + list(b_items.keys()))):
        fa, fb = a_items.get(slot), b_items.get(slot)
        slot_lbl = ((fa or {}).get("slot_en") or (fb or {}).get("slot_en") or slot) if want_en else slot
        changed = not (fa and fb and fa.get("id") == fb.get("id") and fa.get("ilvl") == fb.get("ilvl"))
        items.append({"slot": slot_lbl, "from": _loc_item(fa), "to": _loc_item(fb), "changed": changed})
    return {
        "ok": True,
        "from": _snap_summary(a_day, a_ts, a, want_en),
        "to": _snap_summary(b_day, b_ts, b, want_en),
        "deltas": {k: delta(k) for k in ("level", "ilvl", "ilvl_avg", "achv", "mounts", "pets", "mplus")},
        "items": items,
    }

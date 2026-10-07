"""Cohors — self-update: compare with the latest GitHub release, auto-check cadence and the
request file the host-side applier picks up (the app never restarts its own containers)."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from app.core.config import DATA_DIR, VERSION
from app.core.db import _db, _db_lock
from app.services.jobs import _job_status_set

# ---------------------------------------------------------------- mises à jour (v2026.09.151)
# L'app ne peut PAS redémarrer ses conteneurs (elle n'a ni socket Docker ni privilèges, et c'est
# un choix de sécurité assumé). Elle vérifie donc les versions (lecture GitHub) et DÉPOSE une
# demande dans DATA_DIR ; un petit service de l'hôte (deploy/apply-update.py + timer systemd)
# l'applique. L'app affiche le battement de cœur et le dernier résultat de cet applicateur.
UPD_REPO = "LostInTheBugs/cohors"
UPD_CHECK_VALUES = (0, 6, 12, 24, 48, 168)          # heures entre deux vérifications (0 = jamais)
UPD_DEFAULTS = {"upd_check_h": "24", "upd_apply_auto": "0"}
_UPD_REQUEST = DATA_DIR / "update-request.json"
_UPD_APPLIER = DATA_DIR / "update-applier.json"


def _upd_rows() -> dict:
    with _db_lock, _db() as conn:
        return {r["key"]: r["value"] for r in conn.execute("SELECT * FROM update_config").fetchall()}


def _upd_conf(key: str) -> str:
    return _upd_rows().get(key) or UPD_DEFAULTS.get(key, "")


def _upd_set(updates: dict) -> None:
    with _db_lock, _db() as conn:
        for key, value in updates.items():
            conn.execute(
                "INSERT OR REPLACE INTO update_config (key, value, updated) VALUES (?,?,?)",
                (str(key), str(value), time.time()))


def _upd_vtuple(version: str) -> tuple:
    """« 2026.09.150-c3 » → ((2026, 9, 150), 3) — comparer des versions du dépôt entre elles."""
    base, _, corr = str(version or "").strip().partition("-c")
    parts = []
    for piece in base.split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    try:
        corr_i = int(corr) if corr else 0
    except ValueError:
        corr_i = 0
    return (tuple(parts[:3]), corr_i)


def _upd_latest_release() -> dict:
    """Dernière release publiée du dépôt (réseau — un seul appel, anonyme)."""
    import urllib.request
    req = urllib.request.Request(
        f"https://api.github.com/repos/{UPD_REPO}/releases/latest",
        headers={"User-Agent": f"Cohors/{VERSION}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        raise ValueError("release sans étiquette de version")
    return {"version": tag,
            "published_at": str(data.get("published_at") or ""),
            "url": str(data.get("html_url") or f"https://github.com/{UPD_REPO}/releases/tag/{tag}")}


def _upd_read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _upd_sims_busy() -> bool:
    """Ne jamais demander un redémarrage pendant une simulation (elle serait coupée)."""
    try:
        with _db_lock, _db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM sims WHERE status IN ('queued','running')").fetchone()
        return int(row["n"] or 0) > 0
    except sqlite3.Error:
        return True


def _upd_state() -> dict:
    rows = _upd_rows()
    latest_v = str(rows.get("upd_latest") or "")
    req = _upd_read_json(_UPD_REQUEST)
    app = _upd_read_json(_UPD_APPLIER)
    available = bool(latest_v) and _upd_vtuple(latest_v) > _upd_vtuple(VERSION)
    try:
        check_h = int(float(_upd_conf("upd_check_h")))
    except (TypeError, ValueError):
        check_h = 24
    seen = float(app.get("seen_at") or 0)
    return {
        "current": VERSION,
        "latest": ({"version": latest_v,
                    "published_at": str(rows.get("upd_latest_at") or ""),
                    "url": str(rows.get("upd_latest_url") or "")} if latest_v else None),
        "available": available,
        "check": {"last_at": float(rows.get("upd_checked") or 0),
                  "error": str(rows.get("upd_error") or "")},
        "request": ({"version": str(req.get("version") or ""),
                     "requested_at": float(req.get("at") or 0),
                     "by": str(req.get("by") or "")} if req.get("version") else None),
        "applier": {"installed": seen > 0,
                    "seen_at": seen,
                    "applied": str(app.get("applied") or ""),
                    "applied_at": float(app.get("at") or 0),
                    "result": str(app.get("result") or ""),
                    "running": str(app.get("running") or ""),
                    "running_at": float(app.get("running_at") or 0)},
        "settings": {"upd_check_h": check_h,
                     "upd_apply_auto": _upd_conf("upd_apply_auto") == "1"},
        "sims_busy": _upd_sims_busy(),
    }


def _upd_run_check() -> dict:
    now = time.time()
    try:
        rel = _upd_latest_release()
        _upd_set({"upd_latest": rel["version"], "upd_latest_at": rel["published_at"],
                  "upd_latest_url": rel["url"], "upd_checked": now, "upd_error": ""})
    except Exception as exc:  # noqa: BLE001 — réseau/API : on note l'échec, rien ne casse
        _upd_set({"upd_checked": now, "upd_error": f"{type(exc).__name__} : {exc}"[:200]})
    return _upd_state()


def _upd_request_write(version: str, by: str) -> None:
    _UPD_REQUEST.write_text(
        json.dumps({"version": version, "at": time.time(), "by": by}, ensure_ascii=False),
        encoding="utf-8")


def _upd_tick() -> None:
    """Cadence de vérification + application automatique (appelé par la boucle de fond)."""
    try:
        check_h = int(float(_upd_conf("upd_check_h")))
    except (TypeError, ValueError):
        check_h = 24
    st = _upd_state()
    due = check_h > 0 and (st["check"]["last_at"] == 0
                           or time.time() - st["check"]["last_at"] >= check_h * 3600)
    if due:
        st = _upd_run_check()
        if st["latest"]:
            _job_status_set("update", detail=f"vérification OK — dernière {st['latest']['version']}")
    if st["available"] and st["settings"]["upd_apply_auto"] and not st["request"]:
        if _upd_sims_busy():
            _job_status_set("update", detail="mise à jour en attente — simulation en cours")
            return
        _upd_request_write(st["latest"]["version"], "application automatique")
        _job_status_set("update", detail=f"mise à jour {st['latest']['version']} demandée (automatique)")


def _update_loop() -> None:
    time.sleep(45)
    while True:
        try:
            _upd_tick()
        except Exception as exc:  # noqa: BLE001
            print(f"[updates] {exc}")
        time.sleep(600)

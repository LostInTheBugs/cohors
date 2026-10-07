"""Cohors — background jobs: admin-tunable settings (intervals, toggles) and last-run status."""
from __future__ import annotations

import time

from app.core.config import (
    BOT_POLL_S, PROF_REFRESH_DAYS, SNAP_KEEP_DAYS, SNAP_MAX_PER_TICK, SNAP_POLL_S, SNAP_REFRESH_MIN,
    SNAP_REFRESH_MIN_OTHER,
)
from app.core.db import _db, _db_lock


JOB_DEFAULTS = {
    # relevés des personnages (char_snapshots) + métiers
    "snap_enabled": "1",
    "snap_interval_min": str(max(1, int(SNAP_POLL_S // 60))),
    "snap_linked_h": str(max(1, int(SNAP_REFRESH_MIN // 60))),
    "snap_roster_h": str(max(1, int(SNAP_REFRESH_MIN_OTHER // 60))),
    "snap_max_tick": str(SNAP_MAX_PER_TICK),
    "snap_keep_days": str(SNAP_KEEP_DAYS),
    "prof_days": str(max(1, int(PROF_REFRESH_DAYS))),
    # bot Discord (rapports + mouvements de guilde)
    "bot_interval_min": str(max(1, BOT_POLL_S // 60)),
}


JOB_BOUNDS = {
    "snap_interval_min": (1, 1440), "snap_linked_h": (1, 168), "snap_roster_h": (1, 720),
    "snap_max_tick": (1, 500), "snap_keep_days": (2, 30), "prof_days": (1, 60),
    "bot_interval_min": (1, 1440),
}


def _job_rows() -> dict:
    with _db_lock, _db() as conn:
        return {r["key"]: r["value"] for r in conn.execute("SELECT * FROM job_config").fetchall()}


def _job_conf(key: str) -> str:
    """Réglage effectif : valeur de l'administration, sinon défaut (constante d'environnement)."""
    return _job_rows().get(key) or JOB_DEFAULTS.get(key, "")


def _job_int(key: str) -> int:
    try:
        return int(float(_job_conf(key)))
    except (TypeError, ValueError):
        return int(float(JOB_DEFAULTS.get(key, "0") or 0))


def _job_status_set(slug: str, detail: str = "", error: str = "") -> None:
    with _db_lock, _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO job_status (slug, last_run, detail, error) VALUES (?,?,?,?)",
            (slug, time.time(), detail[:200], error[:200]))


def _job_status_rows() -> dict:
    with _db_lock, _db() as conn:
        return {r["slug"]: {"last_run": r["last_run"], "detail": r["detail"], "error": r["error"]}
                for r in conn.execute("SELECT * FROM job_status").fetchall()}

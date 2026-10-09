"""Cohors — admin: background job settings, last-run status and on-demand snapshot pass."""
from __future__ import annotations

import threading
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.auth import _require_admin
from app.core.db import _db, _db_lock
from app.services.jobs import JOB_BOUNDS, JOB_DEFAULTS, _job_conf, _job_status_rows
from app.services.snapshots import _snap_tick_job

router = APIRouter()


@router.get("/api/admin/jobs")
def admin_jobs_get(request: Request):
    _require_admin(request)
    return {"config": {k: _job_conf(k) for k in JOB_DEFAULTS},
            "bounds": JOB_BOUNDS,
            "defaults": JOB_DEFAULTS,
            "status": _job_status_rows()}


class JobConfigRequest(BaseModel):
    values: dict[str, str] = {}


@router.post("/api/admin/jobs")
def admin_jobs_save(payload: JobConfigRequest, request: Request):
    _require_admin(request)
    saved = {}
    for key, value in (payload.values or {}).items():
        if key not in JOB_DEFAULTS:
            continue
        val = str(value).strip()
        if key.endswith("_enabled"):
            saved[key] = "1" if val in ("1", "true", "on", "yes") else "0"
            continue
        try:
            num = int(float(val))
        except (TypeError, ValueError):
            raise HTTPException(400, f"Valeur invalide pour {key}.")
        lo, hi = JOB_BOUNDS.get(key, (1, 100000))
        if not (lo <= num <= hi):
            raise HTTPException(400, f"{key} doit être entre {lo} et {hi}.")
        saved[key] = str(num)
    if saved:
        with _db_lock, _db() as conn:
            for k, v in saved.items():
                conn.execute("INSERT OR REPLACE INTO job_config (key, value, updated) VALUES (?,?,?)",
                             (k, v, time.time()))
    return {"ok": True, "saved": saved}


class JobRunRequest(BaseModel):
    slug: str = Field(..., max_length=30)


@router.post("/api/admin/jobs/run")
def admin_jobs_run(payload: JobRunRequest, request: Request):
    _require_admin(request)
    slug = payload.slug.strip().lower()
    if slug == "snapshots":
        threading.Thread(target=_snap_tick_job, daemon=True, name="snap-manual").start()
        return {"ok": True, "started": True}
    raise HTTPException(400, "Ce job ne peut pas être lancé à la demande.")

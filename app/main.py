"""LOTP Simulateur — web app (FastAPI).

V1: submit a `/simc` addon export, FIFO queue running one simulation at a
time, shared result cache (SHA-256 of input + iterations), per-IP anti-abuse
quotas, French UI. Simulations run in the official SimulationCraft Docker
image via `worker/simrun.py` (the app container mounts the host Docker socket).

Security note: the Docker socket gives root-equivalent power; this deployment
is meant to stay small and rate-limited until v2 moves sims to a dedicated
worker service.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from worker.simrun import run_sim

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DB_PATH = DATA_DIR / "wow.sqlite"
REPORTS_DIR = DATA_DIR / "reports"

SIMC_IMAGE = os.environ.get("SIMC_IMAGE", "simulationcraftorg/simc:latest")
SIM_TIMEOUT = int(os.environ.get("SIM_TIMEOUT", "900"))
QUEUE_MAX = int(os.environ.get("QUEUE_MAX", "20"))
PER_IP_ACTIVE = int(os.environ.get("PER_IP_ACTIVE", "3"))
PER_IP_COOLDOWN_S = int(os.environ.get("PER_IP_COOLDOWN_S", "15"))
ITER_CHOICES = (1000, 5000, 10000, 25000, 50000)
DEFAULT_ITERATIONS = 10000
MAX_INPUT_CHARS = 200_000

DATA_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

_version_file = Path(__file__).resolve().parent.parent / "VERSION"
VERSION = _version_file.read_text().strip() if _version_file.exists() else os.environ.get("APP_VERSION", "dev")

_db_lock = threading.Lock()
STATIC_DIR = Path(__file__).resolve().parent / "static"


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_db() -> None:
    with _db_lock, _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sims (
                id            TEXT PRIMARY KEY,
                created       REAL NOT NULL,
                ip            TEXT NOT NULL,
                label         TEXT NOT NULL DEFAULT '',
                iterations    INTEGER NOT NULL,
                status        TEXT NOT NULL,
                error         TEXT,
                input_hash    TEXT NOT NULL,
                input_file    TEXT NOT NULL,
                cached_from   TEXT,
                dps           REAL,
                dps_error_pct REAL,
                wall_s        REAL,
                report_html   TEXT,
                report_json   TEXT,
                started       REAL,
                finished      REAL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sims_status ON sims(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sims_hash ON sims(input_hash)")


# ---------------------------------------------------------------------------
# Worker (single simulation at a time — one sim saturates every core)
# ---------------------------------------------------------------------------
def _next_queued() -> str | None:
    with _db_lock, _db() as conn:
        running = conn.execute("SELECT COUNT(*) AS c FROM sims WHERE status='running'").fetchone()["c"]
        if running:
            return None
        row = conn.execute("SELECT id FROM sims WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
        if row is None:
            return None
        conn.execute("UPDATE sims SET status='running', started=? WHERE id=?", (time.time(), row["id"]))
        return row["id"]


def _run_one(sim_id: str) -> None:
    with _db_lock, _db() as conn:
        row = conn.execute("SELECT * FROM sims WHERE id=?", (sim_id,)).fetchone()
        if row is None or row["status"] != "running":
            return
        input_file = Path(row["input_file"])
        iterations = int(row["iterations"])

    try:
        res = run_sim(profile_path=input_file, iterations=iterations, outdir=input_file.parent, timeout=SIM_TIMEOUT)
        ok = bool(res.get("ok"))
        with _db_lock, _db() as conn:
            conn.execute(
                """UPDATE sims SET status=?, dps=?, dps_error_pct=?, wall_s=?, report_html=?, report_json=?,
                                    error=?, finished=? WHERE id=?""",
                (
                    "done" if ok else "failed",
                    res.get("dps"), res.get("dps_error_pct"), res.get("wall_s"),
                    res.get("html"), res.get("json"),
                    None if ok else (res.get("log_tail") or "échec de la simulation")[-2000:],
                    time.time(), sim_id,
                ),
            )
    except Exception as exc:  # noqa: BLE001
        with _db_lock, _db() as conn:
            conn.execute(
                "UPDATE sims SET status='failed', error=?, finished=? WHERE id=?",
                (str(exc)[-2000:], time.time(), sim_id),
            )


def _worker_loop() -> None:
    while True:
        sim_id = _next_queued()
        if sim_id is None:
            time.sleep(1.0)
            continue
        _run_one(sim_id)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _init_db()
    threading.Thread(target=_worker_loop, daemon=True, name="sim-worker").start()
    yield


app = FastAPI(title="LOTP Simulateur", version=VERSION, lifespan=_lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
class SimRequest(BaseModel):
    input: str = Field(..., min_length=30, max_length=MAX_INPUT_CHARS)
    iterations: int = DEFAULT_ITERATIONS
    label: str = ""


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


@app.post("/api/sim")
def submit_sim(payload: SimRequest, request: Request):
    if payload.iterations not in ITER_CHOICES:
        raise HTTPException(400, f"Valeurs d'itérations acceptées : {', '.join(map(str, ITER_CHOICES))}")
    text = payload.input.replace("\r\n", "\n").strip()
    label = payload.label.strip()[:60]
    ip = _client_ip(request)
    now = time.time()

    with _db_lock, _db() as conn:
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM sims WHERE ip=? AND status IN ('queued','running')", (ip,)
        ).fetchone()["c"]
        if active >= PER_IP_ACTIVE:
            raise HTTPException(429, f"Tu as déjà {active} simulation(s) en attente — patiente un peu.")
        last_ts = conn.execute("SELECT MAX(created) AS m FROM sims WHERE ip=?", (ip,)).fetchone()["m"]
        if last_ts and now - last_ts < PER_IP_COOLDOWN_S:
            wait = int(PER_IP_COOLDOWN_S - (now - last_ts)) + 1
            raise HTTPException(429, f"Doucement ! Réessaie dans {wait} s.")
        queue_len = conn.execute("SELECT COUNT(*) AS c FROM sims WHERE status IN ('queued','running')").fetchone()["c"]
        if queue_len >= QUEUE_MAX:
            raise HTTPException(503, "La file est pleine, réessaie dans quelques minutes.")

        input_hash = hashlib.sha256(f"{payload.iterations}\n{text}".encode()).hexdigest()
        cached = conn.execute(
            "SELECT * FROM sims WHERE input_hash=? AND status='done' ORDER BY finished DESC LIMIT 1", (input_hash,)
        ).fetchone()

        sim_id = uuid.uuid4().hex[:12]
        sim_dir = REPORTS_DIR / sim_id
        sim_dir.mkdir(parents=True, exist_ok=True)
        input_file = sim_dir / "input.simc"
        input_file.write_text(text)

        if cached:
            conn.execute(
                """INSERT INTO sims (id, created, ip, label, iterations, status, input_hash, input_file, cached_from,
                                     dps, dps_error_pct, wall_s, report_html, report_json, started, finished)
                   VALUES (?,?,?,?,?, 'done', ?,?,?,?,?,?,?,?,?,?)""",
                (sim_id, now, ip, label, payload.iterations, input_hash, str(input_file), cached["id"],
                 cached["dps"], cached["dps_error_pct"], cached["wall_s"], cached["report_html"], cached["report_json"],
                 now, now),
            )
            return {"id": sim_id, "status": "done", "cached": True}

        conn.execute(
            "INSERT INTO sims (id, created, ip, label, iterations, status, input_hash, input_file) VALUES (?,?,?,?,?, 'queued', ?, ?)",
            (sim_id, now, ip, label, payload.iterations, input_hash, str(input_file)),
        )
        return {"id": sim_id, "status": "queued", "cached": False, "position": queue_len + 1}


def _public_row(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "created": r["created"],
        "label": r["label"],
        "iterations": r["iterations"],
        "status": r["status"],
        "cached": bool(r["cached_from"]),
        "dps": r["dps"],
        "dps_error_pct": r["dps_error_pct"],
        "wall_s": r["wall_s"],
        "has_report": r["status"] == "done" and bool(r["report_html"]),
        "error": (r["error"] or "")[:300] if r["status"] == "failed" else None,
    }


@app.get("/api/sims")
def list_sims():
    with _db_lock, _db() as conn:
        rows = conn.execute("SELECT * FROM sims ORDER BY created DESC LIMIT 50").fetchall()
    return {"sims": [_public_row(r) for r in rows], "version": VERSION}


@app.get("/api/sims/{sim_id}")
def get_sim(sim_id: str):
    with _db_lock, _db() as conn:
        r = conn.execute("SELECT * FROM sims WHERE id=?", (sim_id,)).fetchone()
    if r is None:
        raise HTTPException(404, "Simulation inconnue")
    d = _public_row(r)
    d["cached_from"] = r["cached_from"]
    return d


@app.get("/reports/{sim_id}/report.html")
def report_html(sim_id: str):
    with _db_lock, _db() as conn:
        r = conn.execute("SELECT report_html FROM sims WHERE id=?", (sim_id,)).fetchone()
    if r is None or not r["report_html"] or not Path(r["report_html"]).exists():
        raise HTTPException(404, "Rapport introuvable")
    return FileResponse(r["report_html"], media_type="text/html")


@app.get("/reports/{sim_id}/report.json")
def report_json(sim_id: str):
    with _db_lock, _db() as conn:
        r = conn.execute("SELECT report_json FROM sims WHERE id=?", (sim_id,)).fetchone()
    if r is None or not r["report_json"] or not Path(r["report_json"]).exists():
        raise HTTPException(404, "Rapport introuvable")
    return FileResponse(r["report_json"], media_type="application/json")


@app.api_route("/api/health", methods=["GET", "HEAD"])
def health():
    with _db_lock, _db() as conn:
        queued = conn.execute("SELECT COUNT(*) AS c FROM sims WHERE status='queued'").fetchone()["c"]
        running = conn.execute("SELECT COUNT(*) AS c FROM sims WHERE status='running'").fetchone()["c"]
    return {"ok": True, "version": VERSION, "queued": queued, "running": bool(running), "simc_image": SIMC_IMAGE}


@app.api_route("/", methods=["GET", "HEAD"])
def index():
    return FileResponse(STATIC_DIR / "index.html")

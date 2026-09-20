#!/usr/bin/env python3
"""simworker — petit worker stdlib qui possède SEUL le socket Docker.

Pourquoi : monter /var/run/docker.sock dans l'app revient à lui donner les droits
root sur l'hôte (une faille de l'app compromet toute la machine). Ici l'app ne
parle plus au socket : elle soumet un job RESTREINT via un socket Unix (volume
partagé), et ce worker construit lui-même chaque `docker run` — image fixe,
montages fixes, identifiant de job généré ici. Les chemins d'entrée/sortie ne
viennent JAMAIS de l'app : seuls les chemins dérivés de l'id de job existent.

Défense en profondeur : le worker re-valide tout ce que l'app lui envoie
(taille du profil, directives interdites, bornes d'itérations, options) —
voir shared/simvalidate.py — et n'accepte que l'uid de l'app sur le socket
(SO_PEERCRED), en plus du mode 0660 réservé au groupe partagé.

Protocole : une ligne JSON en entrée, une ligne JSON en sortie.
  {"cmd":"ping"}                              → {"ok":true,"version":…,"jobs":N}
  {"cmd":"run","profile":"…","iterations":N,"extra":[…],"timeout":S}
  {"cmd":"run","container_profile":"profiles/….simc",…}
                                              → {"ok":true,"id":"…"}
  {"cmd":"status","id":"…"}                   → {"ok":true,"state":"queued|running|done|error",
                                                 "result":{…export simrun…},"error":…}
  {"cmd":"cancel","id":"…"}                   → {"ok":true}
"""
from __future__ import annotations

import json
import os
import queue
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from shared.simvalidate import (validate_container_profile, validate_extra,
                                validate_iterations, validate_profile_text)
from worker.simrun import cleanup_orphans, run_sim

VERSION = os.environ.get("COHORS_VERSION", "?")
try:
    _v = Path(__file__).resolve().parent.parent / "VERSION"
    if _v.exists():
        VERSION = _v.read_text(encoding="utf-8").strip() or VERSION
except Exception:  # noqa: BLE001
    pass

SOCK_PATH = os.environ.get("SIMWORKER_SOCK", "/run/cohors/simworker.sock")
APP_UID = int(os.environ.get("SIMWORKER_APP_UID", "1000"))
APP_GID = int(os.environ.get("SIMWORKER_APP_GID", "10001"))
JOBS_DIR = Path(os.environ.get("SIM_JOBS_DIR", "/sim-jobs"))
MAX_PROFILE_KB = int(os.environ.get("SIM_MAX_PROFILE_KB", "2048"))
MAX_ITERATIONS = int(os.environ.get("SIM_MAX_ITERATIONS", "200000"))
DEFAULT_TIMEOUT = int(os.environ.get("SIM_TIMEOUT", "900"))
MAX_TIMEOUT = int(os.environ.get("SIM_MAX_TIMEOUT", "3600"))
MAX_JOBS_KEPT = 200

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
QUEUE: "queue.Queue[tuple[str, dict]]" = queue.Queue()


def _set(job_id: str, **kw) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kw)


def _prune_jobs() -> None:
    """Garde au plus MAX_JOBS_KEPT entrées (les plus anciennes terminées d'abord)."""
    with JOBS_LOCK:
        if len(JOBS) <= MAX_JOBS_KEPT:
            return
        done = sorted((j for j in JOBS.items() if j[1].get("state") in ("done", "error")),
                      key=lambda kv: kv[1].get("created", 0))
        for job_id, _ in done[: len(JOBS) - MAX_JOBS_KEPT]:
            JOBS.pop(job_id, None)


def _execute(job_id: str, spec: dict) -> dict:
    """Exécute un job : le worker choisit TOUS les chemins (id de job généré ici)."""
    jobdir = JOBS_DIR / job_id
    jobdir.mkdir(parents=True, exist_ok=True)
    name = "sim-" + job_id
    if spec.get("container_profile"):
        res = run_sim(container_profile=spec["container_profile"], iterations=spec["iterations"],
                      outdir=jobdir, extra=spec["extra"], timeout=spec["timeout"], container_name=name)
    else:
        profile_file = jobdir / "input.simc"
        profile_file.write_text(spec["profile"], encoding="utf-8")
        res = run_sim(profile_path=profile_file, iterations=spec["iterations"],
                      outdir=jobdir, extra=spec["extra"], timeout=spec["timeout"], container_name=name)
    res["job"] = job_id
    return res


def _worker_loop() -> None:
    while True:
        job_id, spec = QUEUE.get()
        try:
            _set(job_id, state="running")
            result = _execute(job_id, spec)
            _set(job_id, state="done", result=result)
        except Exception as exc:  # noqa: BLE001
            _set(job_id, state="error", error=str(exc)[:500])
        finally:
            QUEUE.task_done()


def _handle_cmd(req: dict) -> dict:
    cmd = req.get("cmd")
    if cmd == "ping":
        with JOBS_LOCK:
            n = len(JOBS)
        return {"ok": True, "version": VERSION, "jobs": n,
                "max_iterations": MAX_ITERATIONS, "max_profile_kb": MAX_PROFILE_KB}
    if cmd == "run":
        # Re-validation complète : le worker ne fait JAMAIS confiance à l'app.
        ok, err = validate_iterations(req.get("iterations"), MAX_ITERATIONS)
        if not ok:
            return {"ok": False, "error": err}
        ok, err = validate_extra(req.get("extra"))
        if not ok:
            return {"ok": False, "error": err}
        try:
            timeout = max(10, min(int(req.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT))
        except (TypeError, ValueError):
            return {"ok": False, "error": "délai invalide"}
        spec = {"iterations": int(req["iterations"]), "extra": list(req.get("extra") or []),
                "timeout": timeout, "container_profile": None, "profile": None}
        if req.get("container_profile"):
            ok, err = validate_container_profile(req["container_profile"])
            if not ok:
                return {"ok": False, "error": err}
            spec["container_profile"] = req["container_profile"]
        else:
            ok, err = validate_profile_text(req.get("profile"), MAX_PROFILE_KB * 1024)
            if not ok:
                return {"ok": False, "error": err}
            spec["profile"] = req["profile"]
        job_id = uuid.uuid4().hex[:16]
        with JOBS_LOCK:
            JOBS[job_id] = {"state": "queued", "created": time.time()}
        QUEUE.put((job_id, spec))
        _prune_jobs()
        return {"ok": True, "id": job_id}
    if cmd == "status":
        job_id = str(req.get("id") or "")
        with JOBS_LOCK:
            job = dict(JOBS.get(job_id) or {})
        if not job:
            return {"ok": False, "error": "job inconnu"}
        return {"ok": True, "state": job.get("state"), "result": job.get("result"),
                "error": job.get("error")}
    if cmd == "cancel":
        job_id = str(req.get("id") or "")
        try:
            subprocess.run(["docker", "kill", "sim-" + job_id], capture_output=True, timeout=60)
            subprocess.run(["docker", "rm", "-f", "sim-" + job_id], capture_output=True, timeout=60)
        except Exception:  # noqa: BLE001
            pass
        _set(job_id, state="error", error="annulé")
        return {"ok": True}
    return {"ok": False, "error": "commande inconnue"}


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        # SO_PEERCRED : seul l'uid de l'app est accepté (en plus du 0660/groupe).
        peer_uid = -1
        try:
            creds = self.connection.getsockopt(socket.SOL_SOCKET,
                                               socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, peer_uid, _gid = struct.unpack("3i", creds)
        except OSError:
            pass
        if peer_uid != APP_UID:
            self._reply({"ok": False, "error": f"accès refusé (uid {peer_uid})"})
            return
        line = self.rfile.readline(4 * 1024 * 1024)
        if not line:
            return
        try:
            req = json.loads(line.decode("utf-8", "replace"))
            if not isinstance(req, dict):
                raise ValueError("requête non-objet")
            rep = _handle_cmd(req)
        except Exception as exc:  # noqa: BLE001
            rep = {"ok": False, "error": f"requête illisible : {exc}"}
        self._reply(rep)

    def _reply(self, rep: dict) -> None:
        try:
            self.wfile.write((json.dumps(rep, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            pass


def main() -> int:
    sock = Path(SOCK_PATH)
    sock.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(sock.parent, 0o770)
        os.chown(sock.parent, -1, APP_GID)
    except (PermissionError, OSError):
        pass
    try:
        sock.unlink()
    except FileNotFoundError:
        pass

    orphans = cleanup_orphans()
    if orphans:
        print(f"simworker: ménage — {len(orphans)} conteneur(s) orphelin(s) supprimé(s) : "
              f"{', '.join(orphans)}", flush=True)

    server = socketserver.ThreadingUnixStreamServer(str(sock), Handler)
    server.daemon_threads = True
    os.chmod(sock, 0o660)
    try:
        os.chown(sock, -1, APP_GID)   # groupe partagé avec l'app uniquement
    except PermissionError:
        print("simworker: chown du socket impossible (pas root ?) — 0660 conservé",
              file=sys.stderr, flush=True)

    threading.Thread(target=_worker_loop, daemon=True).start()
    print(f"simworker prêt — {sock} (0660, groupe {APP_GID}) · uid app autorisé : {APP_UID}"
          f" · jobs : {JOBS_DIR} (max {MAX_ITERATIONS} itérations, profil ≤ {MAX_PROFILE_KB} Ko)",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

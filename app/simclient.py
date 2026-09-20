"""Client du worker de simulation (socket Unix) — remplace le `docker run` direct.

L'app ne voit plus /var/run/docker.sock : elle lit le profil, l'envoie au worker
(volume partagé, socket 0660 réservé à son groupe) et attend le résultat. Les
chemins de sortie (rapports html/json) ne viennent JAMAIS d'ici : le worker les
dérive de SON identifiant de job — on ne fait que relayer ce qu'il renvoie.

`outdir` est accepté pour compatibilité avec les appels historiques mais IGNORÉ
(les rapports atterrissent dans le dossier du job, sous DATA_DIR, chemin identique
des deux côtés : l'app peut les servir directement).
"""
from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

SOCK_PATH = os.environ.get("SIMWORKER_SOCK", "/run/cohors/simworker.sock")
POLL_S = 1.0


class WorkerError(RuntimeError):
    """Problème de transport ou refus du worker (pas un échec de simulation)."""


def _call(payload: dict, timeout: float = 30.0) -> dict:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(SOCK_PATH)
            s.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except (OSError, socket.timeout) as exc:
        raise WorkerError(f"worker injoignable ({SOCK_PATH}) : {exc}") from exc
    if not buf.strip():
        raise WorkerError("réponse vide du worker")
    try:
        return json.loads(buf.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise WorkerError(f"réponse illisible du worker : {exc}") from exc


def run_sim(
    profile_path: Path | None = None,
    container_profile: str | None = None,
    iterations: int = 10000,
    outdir: Path | None = None,          # ignoré : le worker choisit ses chemins
    extra: list[str] | None = None,
    timeout: int = 900,
) -> dict:
    """Soumet une simulation au worker et attend son résultat.

    Même forme de retour que l'ancien `worker.simrun.run_sim` (ok/rc/dps/html/json/
    log_tail/scale_factors/gear/group…), donc les appelants ne changent pas.
    Un échec de SIMULATION est retourné (ok=False), pas levé : seuls les problèmes
    de transport (worker absent, refus, timeout du client) lèvent WorkerError.
    """
    if bool(profile_path) == bool(container_profile):
        raise ValueError("exactly one of profile_path / container_profile is required")
    if profile_path is not None:
        text = Path(profile_path).read_text(encoding="utf-8", errors="replace")
        payload = {"cmd": "run", "profile": text, "iterations": int(iterations),
                   "extra": [str(x) for x in (extra or [])], "timeout": int(timeout)}
    else:
        payload = {"cmd": "run", "container_profile": str(container_profile),
                   "iterations": int(iterations), "extra": [str(x) for x in (extra or [])],
                   "timeout": int(timeout)}

    rep = _call(payload)
    if not rep.get("ok"):
        raise WorkerError(rep.get("error") or "le worker a refusé le job")
    job_id = rep["id"]

    deadline = time.monotonic() + int(timeout) + 120  # le worker coupe déjà à `timeout`
    while time.monotonic() < deadline:
        st = _call({"cmd": "status", "id": job_id})
        if not st.get("ok"):
            raise WorkerError(st.get("error") or "job perdu par le worker")
        state = st.get("state")
        if state == "done":
            result = st.get("result") or {}
            result.setdefault("ok", False)
            return result
        if state == "error":
            return {"ok": False, "rc": None, "dps": None, "dps_error_pct": None,
                    "iterations": int(iterations), "wall_s": None, "html": None, "json": None,
                    "log_tail": "worker : " + str(st.get("error") or "erreur inconnue")[:1500],
                    "scale_factors": None, "gear": None, "group": None}
        time.sleep(POLL_S)

    try:  # client à bout de patience : on demande au worker d'arrêter le job
        _call({"cmd": "cancel", "id": job_id}, timeout=10)
    except WorkerError:
        pass
    raise WorkerError(f"délai client dépassé pour le job {job_id}")

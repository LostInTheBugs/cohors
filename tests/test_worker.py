"""Tests du worker de simulation (v2026.09.148, suivi de revue externe du 20/09).

Couvre : annulation d'un job encore en file (ne doit JAMAIS s'exécuter), annulation d'un job en
cours (l'état « annulé » ne doit pas être écrasé), purge des dossiers de jobs par âge, limite de
requête dérivée de SIM_MAX_PROFILE_KB, et le message app côté « job inconnu » (worker redémarré).
"""
import importlib
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fresh_worker(tmp_path, monkeypatch, **env):
    """Ré-importe worker.simworker avec un JOBS_DIR jetable et l'env voulu."""
    monkeypatch.setenv("SIM_JOBS_DIR", str(tmp_path))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for m in [m for m in sys.modules if m in ("worker", "worker.simworker", "worker.simrun")]:
        sys.modules.pop(m, None)
    return importlib.import_module("worker.simworker")


def test_cancel_queued_job_is_not_executed(tmp_path, monkeypatch):
    """Un job annulé pendant qu'il est encore en file ne doit pas être lancé plus tard."""
    w = _fresh_worker(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(w, "_execute", lambda jid, spec: calls.append(jid) or {"ok": True})
    w.JOBS["abc"] = {"state": "queued", "created": time.time()}
    rep = w._handle_cmd({"cmd": "cancel", "id": "abc"})
    assert rep["ok"] is True
    assert w.JOBS["abc"]["state"] == "error"
    assert "annul" in str(w.JOBS["abc"].get("error")).lower()
    # le job arrive dans la file APRÈS l'annulation (il y était en attente) : la boucle doit l'ignorer
    w.QUEUE.put(("abc", {"iterations": 1, "extra": [], "timeout": 10,
                         "container_profile": None, "profile": "x"}))
    threading.Thread(target=w._worker_loop, daemon=True).start()
    time.sleep(0.3)
    assert calls == [], "un job annulé en file ne doit PAS être exécuté"
    assert w.JOBS["abc"]["state"] == "error"
    assert "abc" not in w.CANCELLED


def test_cancel_running_job_keeps_annule(tmp_path, monkeypatch):
    """Annulation pendant l'exécution : l'état « annulé » ne doit pas être écrasé par la fin du job."""
    w = _fresh_worker(tmp_path, monkeypatch)
    monkeypatch.setattr(w, "_execute", lambda jid, spec: (time.sleep(0.3), {"ok": True})[1])
    w.JOBS["r1"] = {"state": "queued", "created": time.time()}
    threading.Thread(target=w._worker_loop, daemon=True).start()
    w.QUEUE.put(("r1", {"iterations": 1, "extra": [], "timeout": 10,
                        "container_profile": None, "profile": "x"}))
    for _ in range(60):
        if w.JOBS["r1"].get("state") == "running":
            break
        time.sleep(0.02)
    assert w.JOBS["r1"]["state"] == "running"
    w._handle_cmd({"cmd": "cancel", "id": "r1"})
    assert w.JOBS["r1"]["state"] == "error"
    time.sleep(0.6)
    assert w.JOBS["r1"]["state"] == "error", "la fin du job ne doit pas écraser « annulé »"
    assert "r1" not in w.CANCELLED


def test_cancel_unknown_or_done_is_noop(tmp_path, monkeypatch):
    """Annuler un id inconnu ou un job terminé : sans effet (on n'efface pas un résultat)."""
    w = _fresh_worker(tmp_path, monkeypatch)
    assert w._handle_cmd({"cmd": "cancel", "id": "inconnu"})["ok"] is True
    assert "inconnu" not in w.CANCELLED
    w.JOBS["d1"] = {"state": "done", "created": time.time(), "result": {"ok": True}}
    assert w._handle_cmd({"cmd": "cancel", "id": "d1"})["ok"] is True
    assert w.JOBS["d1"]["state"] == "done"


def test_purge_job_dirs_removes_old_only(tmp_path):
    """Purge par âge : les vieux dossiers (rapports) partent, les récents restent."""
    from worker.simrun import purge_job_dirs

    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "report.html").write_text("x", encoding="utf-8")
    old_ts = time.time() - 200 * 3600
    os.utime(old, (old_ts, old_ts))
    assert purge_job_dirs(tmp_path, 168) == 1
    assert not old.exists()
    assert new.exists()


def test_readline_limit_derives_from_max_profile(tmp_path, monkeypatch):
    """La limite de lecture du worker suit SIM_MAX_PROFILE_KB (plus de 4 Mo figés)."""
    w = _fresh_worker(tmp_path, monkeypatch, SIM_MAX_PROFILE_KB="2048")
    assert w._readline_limit() == 2048 * 1024 * 4 + 65536
    monkeypatch.setattr(w, "MAX_PROFILE_KB", 8192)
    assert w._readline_limit() == 8192 * 1024 * 4 + 65536


def test_simclient_reports_worker_restart(monkeypatch):
    """« job inconnu » (worker redémarré) → message compréhensible côté app, pas le code brut."""
    import app.simclient as sc

    profile = Path("/tmp/cohors-test-simclient.simc")
    profile.write_text("mage=Tester\n", encoding="utf-8")
    seq = [{"ok": True, "id": "j1"}, {"ok": False, "error": "job inconnu"}]
    monkeypatch.setattr(sc, "_call", lambda payload, timeout=30.0: seq.pop(0))
    with pytest.raises(sc.WorkerError) as ei:
        sc.run_sim(profile_path=profile, iterations=100)
    assert "redémarré" in str(ei.value) and "relance" in str(ei.value)

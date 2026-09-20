#!/usr/bin/env python3
"""Cohors — applicateur de mise à jour (côté hôte).

La demande est déposée par l'administration du site dans `data/update-request.json`.
Le conteneur de l'app n'a ni socket Docker ni privilèges (choix de sécurité) : il ne fait
que DEMANDER. Ce script, lancé par le timer systemd `cohors-update.timer`, applique.

    python3 deploy/apply-update.py            # applique la demande en attente (s'il y en a)
    python3 deploy/apply-update.py --dry-run  # ne change rien : montre ce qui serait fait

Modes :
  - images  (`image:` dans docker-compose.yml) : `docker compose pull` puis `up -d` ;
  - sources (`build:` dans docker-compose.yml) : archive de la version demandée téléchargée
    depuis GitHub, synchronisée (rsync, hors `.env`/`data/`), puis `up -d --build`.

Écrit `data/update-applier.json` (battement de cœur + dernier résultat, lu par le site)
et journalise dans `data/update.log`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """Le MÊME dossier de données que le déploiement : variable d'environnement, sinon
    `DATA_DIR=` du `.env` posé à côté du compose, sinon `./data`. (Se tromper de dossier
    ferait écrire le battement de cœur là où le site ne le lit pas — vécu en prod.)"""
    from_env = os.environ.get("DATA_DIR", "").strip()
    if from_env:
        return Path(from_env)
    try:
        for line in (APP_DIR / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DATA_DIR="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    return Path(value)
    except OSError:
        pass
    return APP_DIR / "data"


DATA_DIR = data_dir()
REQ = DATA_DIR / "update-request.json"
STATUS = DATA_DIR / "update-applier.json"
LOG = DATA_DIR / "update.log"
REPO = "LostInTheBugs/cohors"
VERSION_RE = re.compile(r"^\d{4}\.\d{2}\.\d{3}(?:-c\d+)?$")
DRY = "--dry-run" in sys.argv
STATS_PASSES = "--verbose" in sys.argv   # journalise aussi les passages sans demande


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"[{stamp}] {message}\n")
    print(f"[{stamp}] {message}", flush=True)


def status(**updates) -> None:
    data = {}
    try:
        loaded = json.loads(STATUS.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        data = {}
    data["seen_at"] = time.time()
    data.update(updates)
    STATUS.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def run(cmd: list[str]) -> None:
    log("exécute : " + " ".join(cmd))
    if DRY:
        return
    res = subprocess.run(cmd, cwd=APP_DIR, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} — code {res.returncode} : {(res.stderr or res.stdout).strip()[-400:]}")


def source_mode() -> bool:
    try:
        text = (APP_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    except OSError:
        return False
    return re.search(r"^[ \t]+build:", text, re.M) is not None


def apply_images() -> None:
    run(["docker", "compose", "pull"])
    run(["docker", "compose", "up", "-d", "--remove-orphans"])


def apply_sources(version: str) -> None:
    if shutil.which("rsync") is None:
        raise RuntimeError("rsync absent sur l'hôte")
    url = f"https://github.com/{REPO}/archive/refs/tags/{version}.tar.gz"
    if DRY:
        log(f"dry-run : téléchargerait {url}, puis rsync + docker compose up -d --build")
        return
    log(f"télécharge {url}")
    tmp = Path(tempfile.mkdtemp(prefix="cohors-update-"))
    archive = tmp / "src.tar.gz"
    urllib.request.urlretrieve(url, archive)
    with tarfile.open(archive) as tar:
        try:
            tar.extractall(tmp / "src", filter="data")
        except TypeError:      # Python antérieur à 3.12
            tar.extractall(tmp / "src")
    roots = [p for p in (tmp / "src").iterdir() if p.is_dir()]
    if not roots or not (roots[0] / "docker-compose.yml").exists():
        raise RuntimeError("archive inattendue (docker-compose.yml absent)")
    run(["rsync", "-a", "--delete",
         "--exclude", ".env", "--exclude", "data/", "--exclude", ".git",
         "--exclude", "__pycache__", "--exclude", "docker-compose.override.yml",
         f"{roots[0]}/", f"{APP_DIR}/"])
    run(["docker", "compose", "up", "-d", "--build"])
    shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not REQ.exists():
        if STATS_PASSES:            # journal discret : un passage sans demande n'a rien à faire
            log("aucune demande en attente")
        status(result="attente")
        return 0
    try:
        payload = json.loads(REQ.read_text(encoding="utf-8"))
        version = str(payload.get("version") or "").strip()
        by = str(payload.get("by") or "?")
    except (OSError, ValueError) as exc:
        log(f"demande illisible : {exc}")
        REQ.unlink(missing_ok=True)
        status(result="demande illisible")
        return 1
    if not VERSION_RE.match(version):
        log(f"version refusée : {version!r}")
        REQ.unlink(missing_ok=True)
        status(result=f"version refusée : {version}"[:300])
        return 1
    mode = "sources" if source_mode() else "images"
    log(f"demande de {by} vers {version} (mode {mode}{', dry-run' if DRY else ''})")
    status(result="en cours", running=version)
    try:
        if mode == "sources":
            apply_sources(version)
        else:
            apply_images()
    except Exception as exc:  # noqa: BLE001 — on note l'échec, la demande est conservée
        log(f"ÉCHEC : {exc}")
        status(result=f"échec : {exc}"[:300])
        return 1
    log(f"mise à jour vers {version} terminée")
    if not DRY:
        REQ.unlink(missing_ok=True)
    status(applied=version, at=time.time(), result="ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())

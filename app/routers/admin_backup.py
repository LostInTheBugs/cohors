"""Cohors — admin: downloadable backup (database + guild identity) and validated restore."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response

from app.core.auth import _require_admin
from app.core.brand import _img_type
from app.core.config import BRAND_DIR, DATA_DIR, DB_PATH, VERSION
from app.core.db import _db, _db_lock
from app.core.schema import _init_db
from app.services.api_keys import _apply_api_keys
from app.services.guild_settings import _apply_guild_config
from app.services.mail_settings import _apply_mail_config

router = APIRouter()

# L'irremplaçable tient dans la base (comptes, personnages, recettes, réglages) et l'identité
# (logo/fond : fichiers dans DATA_DIR/branding). Rapports de simulation, dossiers sim-jobs et
# fichiers vocaux sont régénérables ou volumineux — exclus de l'archive (petite, rechargeable).
BACKUP_MAX_MB = int(os.environ.get("BACKUP_MAX_MB", "128"))
BACKUP_MAX_UNPACKED_MB = int(os.environ.get("BACKUP_MAX_UNPACKED_MB", "512"))


def _backup_manifest() -> dict:
    with _db_lock, _db() as conn:
        counts = {}
        for t in ("users", "char_professions", "craft_recipes", "sims"):
            try:
                counts[t] = conn.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
            except sqlite3.Error:
                counts[t] = None
    return {"app": "Cohors", "version": VERSION, "created_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
            "created": time.time(), "counts": counts}


def _snapshot_db_to(dest: Path) -> None:
    """Copie cohérente de la base (API backup de sqlite — jamais un cp brut en pleine écriture)."""
    with _db_lock, _db() as src:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()


def _build_backup() -> tuple[bytes, str]:
    """Archive .tar.gz : manifest + base + fichiers d'identité. Retourne (octets, nom)."""
    # secret.key is intentionally excluded: it encrypts API secrets, the bot token and
    # the SMTP password with this instance's key. If restored on another instance,
    # those secrets must be re-entered with the new instance's key (COHORS_SECRET_KEY
    # or DATA_DIR/secret.key).
    import io
    import tarfile
    import tempfile

    buf = io.BytesIO()
    with tempfile.TemporaryDirectory(prefix="cohors-backup-") as tmp:
        tmpd = Path(tmp)
        db_copy = tmpd / "wow.sqlite"
        _snapshot_db_to(db_copy)
        man = tmpd / "manifest.json"
        man.write_text(json.dumps(_backup_manifest(), ensure_ascii=False, indent=1), encoding="utf-8")
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(man, arcname="manifest.json")
            tar.add(db_copy, arcname="wow.sqlite")
            if BRAND_DIR.is_dir():
                for f in sorted(BRAND_DIR.iterdir()):
                    if f.is_file() and not f.name.startswith("."):
                        tar.add(f, arcname="branding/" + f.name)
    name = "cohors-backup-" + time.strftime("%Y%m%d-%H%M%S") + ".tar.gz"
    return buf.getvalue(), name


@router.get("/api/admin/backup")
def api_admin_backup(request: Request):
    """Sauvegarde téléchargeable (admin) : base + identité — à recharger après un redéploiement."""
    _require_admin(request)
    data, name = _build_backup()
    return Response(data, media_type="application/gzip",
                    headers={"Content-Disposition": 'attachment; filename="' + name + '"',
                             "Cache-Control": "no-store"})


async def _read_restore_upload(request: Request) -> bytes:
    cap = BACKUP_MAX_MB * 1024 * 1024
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > cap:
        raise HTTPException(413, "sauvegarde trop volumineuse (max %d Mo)" % BACKUP_MAX_MB)
    data = await request.body()
    if not data:
        raise HTTPException(400, "aucun fichier reçu")
    if len(data) > cap:
        raise HTTPException(413, "sauvegarde trop volumineuse (max %d Mo)" % BACKUP_MAX_MB)
    return data


def _extract_backup(data: bytes, dest: Path) -> tuple[dict, Path, list]:
    """Extrait et VALIDE l'archive dans un dossier temporaire : retourne (manifest, base extraite).

    Rien n'est appliqué ici : chemins contrôlés (pas de « .. » ni d'absolu), manifest + base
    exigés, integrity_check + tables vitales présentes. Une archive douteuse → 400 explicite.
    """
    import io
    import tarfile

    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except tarfile.TarError as exc:
        raise HTTPException(400, "archive illisible (%s)" % exc)
    members = {}
    unpacked_cap = BACKUP_MAX_UNPACKED_MB * 1024 * 1024
    total = 0
    with tar:
        for m in tar.getmembers():
            name = m.name.lstrip("./")
            if name.startswith("/") or ".." in name.split("/"):
                raise HTTPException(400, "archive refusée (chemin non sûr)")
            if not m.isfile():
                continue
            total += m.size
            if total > unpacked_cap:
                raise HTTPException(400, "archive trop volumineuse une fois décompressée "
                                         "(max %d Mo)" % BACKUP_MAX_UNPACKED_MB)
            if name.startswith("branding/"):
                ef = tar.extractfile(m)     # None pour un membre exotique : 400 propre, pas 500
                head = ef.read(12) if ef is not None else b""
                if not _branding_member_ok(name, head):
                    raise HTTPException(400, "fichier d'identité refusé (%s) — images "
                                             "png/jpg/gif/webp attendues" % name)
            members[name] = m
        if "manifest.json" not in members or "wow.sqlite" not in members:
            raise HTTPException(400, "archive invalide : manifest.json et wow.sqlite attendus")
        dest.mkdir(parents=True, exist_ok=True)
        try:
            tar.extractall(dest, members=list(members.values()), filter="data")
        except TypeError:   # Python < 3.12 : pas de filtre natif (nos contrôles suffisent)
            tar.extractall(dest, members=list(members.values()))
    db = dest / "wow.sqlite"
    try:
        chk = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        try:
            if chk.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise HTTPException(400, "base corrompue (integrity_check)")
            tables = {r[0] for r in chk.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            chk.close()
    except sqlite3.Error as exc:
        raise HTTPException(400, "base illisible (%s)" % exc)
    for need in ("users", "sessions", "sims"):
        if need not in tables:
            raise HTTPException(400, "base invalide : table %s manquante" % need)
    try:
        manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(400, "manifest illisible (%s)" % exc)
    if not isinstance(manifest, dict) or manifest.get("app") != "Cohors":
        raise HTTPException(400, "ce fichier n'est pas une sauvegarde Cohors")
    warnings = []
    v_arch, v_cur = str(manifest.get("version", "")), VERSION
    if _ver_tuple(v_arch) > _ver_tuple(v_cur):
        warnings.append("sauvegarde créée par une version plus récente (%s > %s)" % (v_arch, v_cur))
    elif _ver_tuple(v_arch) < _ver_tuple(v_cur):
        warnings.append("sauvegarde plus ancienne (%s) — migrations de schéma rejouées à la restauration" % v_arch)
    return manifest, db, warnings


def _swap_file(src: Path, dst: Path) -> None:
    """Remplace dst par src. os.replace d'abord ; si src est sur un autre FS que dst
    (EXDEV — vécu le 20/09 : /tmp du conteneur ≠ volume de données), copie puis renomme."""
    try:
        os.replace(str(src), str(dst))
        return
    except OSError:
        pass
    incoming = dst.parent / (dst.name + ".incoming")
    incoming.write_bytes(src.read_bytes())
    os.replace(str(incoming), str(dst))
    try:
        src.unlink()
    except FileNotFoundError:
        pass


def _branding_member_ok(name: str, raw: bytes) -> bool:
    """Fichier d'identité d'une sauvegarde : nom PLAT, extension d'image, signature réelle
    et cohérente. Bloque un .html/.svg/.php qui finirait servi même origine (XSS/CSP)."""
    rest = name[len("branding/"):]
    if "/" in rest or rest.startswith("."):
        return False
    ext = rest.rsplit(".", 1)[-1].lower() if "." in rest else ""
    if ext not in ("png", "jpg", "jpeg", "gif", "webp"):
        return False
    det, _ = _img_type(raw)
    return det != "" and det == ("jpg" if ext == "jpeg" else ext)


def _ver_tuple(v) -> tuple:
    """« 2026.09.149-c1 » -> (2026, 9, 149) ; valeurs manquantes complétées par des zéros."""
    import re as _re
    parts = _re.findall(r"\d+", str(v))[:3]
    return tuple(int(x) for x in parts) + (0,) * (3 - len(parts))


def _counts_of(db: Path) -> dict:
    out = {}
    try:
        c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        try:
            for t in ("users", "char_professions", "craft_recipes", "sims"):
                try:
                    out[t] = c.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
                except sqlite3.Error:
                    out[t] = None
        finally:
            c.close()
    except sqlite3.Error:
        pass
    return out


@router.post("/api/admin/restore/preview")
async def api_admin_restore_preview(request: Request):
    """Vérifie une sauvegarde AVANT restauration : manifest + comptages — rien n'est modifié."""
    _require_admin(request)
    import tempfile

    data = await _read_restore_upload(request)
    with tempfile.TemporaryDirectory(prefix="cohors-restore-") as tmp:
        manifest, db, warnings = _extract_backup(data, Path(tmp))
        counts = _counts_of(db)
    return {"ok": True, "manifest": manifest, "counts": counts, "warnings": warnings}


@router.post("/api/admin/restore")
async def api_admin_restore(request: Request):
    """Restaure une sauvegarde : remplace la base et les fichiers d'identité, puis ré-applique
    la configuration (comme au démarrage). Les sessions de l'ancienne base disparaissent —
    il faut éventuellement se reconnecter."""
    user = _require_admin(request)
    import shutil
    import uuid

    data = await _read_restore_upload(request)
    # Staging DANS DATA_DIR : os.replace exige le même système de fichiers que la base
    # (le /tmp du conteneur est un autre montage — vécu le 20/09, erreur EXDEV).
    staging = DATA_DIR / (".restore-" + uuid.uuid4().hex[:8])
    try:
        manifest, db, warnings = _extract_backup(data, staging)
        counts = _counts_of(db)
        with _db_lock:
            try:    # fige l'ancienne base dans son fichier principal avant le filet
                with _db() as ck:
                    ck.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            if DB_PATH.exists():    # filets horodatés : retour arrière = renommer le fichier
                rolled = Path(str(DB_PATH) + ".pre-restore-" + time.strftime("%Y%m%d-%H%M%S"))
                if rolled.exists():     # deux restaurations dans la même seconde
                    rolled = Path(str(rolled) + "-" + uuid.uuid4().hex[:4])
                os.replace(str(DB_PATH), str(rolled))
                # ne garde que les 3 filets les plus récents — en ignorant les fichiers
                # annexes (-shm/-wal que sqlite peut créer à côté d'un filet rouvert)
                caps = [p for p in sorted(DATA_DIR.glob("wow.sqlite.pre-restore-*"))
                        if not p.name.endswith(("-shm", "-wal", "-journal"))]
                for old in caps[:-3]:
                    for path in (old, Path(str(old) + "-shm"), Path(str(old) + "-wal")):
                        try:
                            path.unlink()   # le filet purgé et ses annexes éventuelles
                        except OSError:
                            pass
            _swap_file(db, DB_PATH)
            for suffix in ("-wal", "-shm"):     # résidus de l'ancienne base (mode WAL)
                try:
                    Path(str(DB_PATH) + suffix).unlink()
                except FileNotFoundError:
                    pass
        # la base restaurée peut précéder la version courante : rejouer le schéma AVANT la config
        _init_db()
        restored_brand = 0
        bdir = staging / "branding"
        if bdir.is_dir():
            BRAND_DIR.mkdir(parents=True, exist_ok=True)
            for f in sorted(bdir.iterdir()):
                if f.is_file():
                    (BRAND_DIR / f.name).write_bytes(f.read_bytes())
                    restored_brand += 1
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    for fn in (_apply_api_keys, _apply_guild_config, _apply_mail_config):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            print("[backup] re-apply %s : %s" % (fn.__name__, exc))
    print("[backup] restauration par %s — %s" % (user["email"], counts), flush=True)
    return {"ok": True, "manifest": manifest, "counts": counts, "branding_files": restored_brand,
            "warnings": warnings,
            "notice": "Sauvegarde restaurée — recharge la page (reconnexion possible)."}

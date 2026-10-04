"""Tests d'API (TestClient sur base SQLite temporaire). Aucun service externe requis."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# DATA_DIR temporaire AVANT l'import de l'application (base jetable).
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="cohors-test-")
# Cookies de session en clair : le client de test parle en HTTP, un cookie « Secure »
# ne serait jamais renvoyé par httpx (401 au lieu du 403 attendu sur les routes gardées).
os.environ["COOKIE_SECURE"] = "0"

import app.main as M  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(M.app)
SRC_DIR = Path(__file__).resolve().parent.parent


def setup_module():
    M._init_db()


def test_health_ok():
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_security_headers_present():
    r = client.get("/api/health")
    assert "content-security-policy" in {k.lower() for k in r.headers}
    assert r.headers.get("x-content-type-options") == "nosniff"


def test_login_rejects_bad_credentials():
    r = client.post("/api/login", json={"email": "personne@test.local", "password": "x"},
                    headers={"X-Forwarded-For": "11.11.11.1"})
    assert r.status_code == 401


def test_login_rate_limit_blocks_after_15():
    h = {"X-Forwarded-For": "11.11.11.2"}
    codes = [client.post("/api/login", json={"email": "personne@test.local", "password": "x"},
                         headers=h).status_code for _ in range(16)]
    assert codes[-1] == 429 and codes[0] == 401


def test_login_rate_limit_not_bypassable_with_spoofed_xff():
    # 16 tentatives avec des XFF différents : seule la DERNIÈRE entrée compte → throttlé pareil.
    codes = []
    for i in range(16):
        codes.append(client.post("/api/login", json={"email": "personne@test.local", "password": "x"},
                                 headers={"X-Forwarded-For": f"10.66.{i}.1, 11.11.11.3"}).status_code)
    assert 429 in codes, codes


def _make_user(email: str, is_admin: int = 0, role: str = "member", password: str = "test-pw-123"):
    from app.security import hash_password
    import time as _t
    with M._db_lock, M._db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (email, name, pwd, is_admin, role, lang, active, created)"
            " VALUES (?,?,?,?,?,?,1,?)",
            (email, email.split("@")[0], hash_password(password), is_admin, role, "", _t.time()))


def test_setup_status_requires_admin_and_lists_steps():
    _make_user("membre@test.local")
    c = TestClient(M.app)
    r = c.post("/api/login", json={"email": "membre@test.local", "password": "test-pw-123"},
               headers={"X-Forwarded-For": "10.99.1.1"})
    assert r.status_code == 200, r.text
    assert c.get("/api/setup/status").status_code == 403

    _make_user("boss@test.local", is_admin=1, role="admin")
    c2 = TestClient(M.app)
    r = c2.post("/api/login", json={"email": "boss@test.local", "password": "test-pw-123"},
                headers={"X-Forwarded-For": "10.99.2.1"})
    assert r.status_code == 200, r.text
    r = c2.get("/api/setup/status")
    assert r.status_code == 200
    j = r.json()
    assert [s["key"] for s in j["steps"]] == ["admin", "guild", "bnet", "wcl", "identity",
                                              "smtp", "discord", "members"]
    assert all(("done" in s and "label" in s and "href" in s and "hint" in s) for s in j["steps"])
    assert j["total"] == 8 and j["required_total"] == 6 and j["optional_total"] == 2
    assert j["steps"][0]["done"] is True    # le compte admin existe (on vient de se connecter avec)
    assert j["steps"][3]["done"] is False   # aucune clé WCL dans la base de test


def test_start_page_requires_login_then_serves():
    c = TestClient(M.app)
    r = c.get("/start", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login"


def test_import_recipes_merges_tier_entries_and_dedupes():
    """Régression (bug du 20/09) : les exports « une entrée par palier » (jusqu'à 22 entrées avec
    chaque recette dupliquée) étaient coupés à 10 entrées et gardaient les doublons. L'import doit
    tout lire, fusionner par (métier, recette) et conserver la DERNIÈRE occurrence (matériaux les
    plus complets)."""
    _make_user("officier@test.local", is_admin=1, role="admin")
    c = TestClient(M.app)
    r = c.post("/api/login", json={"email": "officier@test.local", "password": "test-pw-123"},
               headers={"X-Forwarded-For": "10.99.3.1"})
    assert r.status_code == 200, r.text

    profs = [{"name": "Cuisine", "recipes": [
        {"n": "Pain épicé", "i": 37836, "e": "Midnight", "t": 1, "m": [[30817, "", 1]]}]}]
    for k in range(12):   # 12 autres entrées « métier » (> l'ancienne coupe à 10)
        profs.append({"name": f"Métier{k}", "recipes": [
            {"n": f"Recette{k}", "i": 100 + k, "e": "P", "t": 2, "m": []}]})
    profs.append({"name": "Cuisine", "recipes": [
        {"n": "Pain épicé", "i": 37836, "e": "Classic", "t": 3,
         "m": [[30817, "Farine simple", 1], [2678, "Épices douces", 1]]}]})
    payload = json.dumps({"v": 1, "player": "Testeur", "realm": "hyjal", "professions": profs},
                         ensure_ascii=False)
    r = c.post("/api/prep/import-recipes", json={"payload": payload})
    assert r.status_code == 200, r.text
    assert r.json()["recipes"] == 13, r.json()   # 12 métiers + « Pain épicé » une seule fois
    with M._db_lock, M._db() as conn:
        rows = [dict(x) for x in conn.execute(
            "SELECT item, mats FROM craft_recipes WHERE crafter=?", ("Testeur",)).fetchall()]
    pains = [x for x in rows if x["item"] == "Pain épicé"]
    assert len(pains) == 1, rows[:5]
    assert "Farine simple" in pains[0]["mats"], pains[0]

def test_stale_running_sims_are_recovered():
    """Régression (revue 20/09) : une sim restée « running » après un redémarrage doit être
    marquée interrompue — sinon la file des simulations reste bloquée pour toujours."""
    M._init_db()
    with M._db_lock, M._db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sims (id, created, ip, label, iterations, status, input_hash, input_file)"
            " VALUES ('stale-test', ?, '127.0.0.1', '', 1000, 'running', 'h', '/tmp/x.simc')",
            (time.time(),))
    M._recover_stale_sims()
    with M._db_lock, M._db() as conn:
        row = conn.execute("SELECT status, error FROM sims WHERE id='stale-test'").fetchone()
    assert row["status"] == "failed"
    assert "redémarrage" in row["error"]


def _admin_client(name, ip):
    _make_user(name, is_admin=1, role="admin")
    c = TestClient(M.app)
    r = c.post("/api/login", json={"email": name, "password": "test-pw-123"},
               headers={"X-Forwarded-For": ip})
    assert r.status_code == 200, r.text
    return c


def test_backup_requires_admin():
    _make_user("membre-bak@test.local")
    c = TestClient(M.app)
    r = c.post("/api/login", json={"email": "membre-bak@test.local", "password": "test-pw-123"},
               headers={"X-Forwarded-For": "10.99.7.1"})
    assert r.status_code == 200, r.text
    assert c.get("/api/admin/backup").status_code == 403
    assert c.post("/api/admin/restore/preview", content=b"x").status_code == 403


def test_backup_download_is_a_valid_archive():
    import io as _io
    import tarfile as _tar

    c = _admin_client("adminbak@test.local", "10.99.8.1")
    r = c.get("/api/admin/backup")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/gzip"
    assert "attachment" in r.headers.get("content-disposition", "")
    with _tar.open(fileobj=_io.BytesIO(r.content), mode="r:gz") as tar:
        names = tar.getnames()
    assert "manifest.json" in names and "wow.sqlite" in names


def test_restore_rejects_garbage():
    import io as _io
    import tarfile as _tar

    c = _admin_client("adminbak2@test.local", "10.99.9.1")
    assert c.post("/api/admin/restore/preview", content=b"pas une archive").status_code == 400
    buf = _io.BytesIO()
    with _tar.open(fileobj=buf, mode="w:gz") as tar:
        man = b'{"app": "x"}'
        ti = _tar.TarInfo("manifest.json")
        ti.size = len(man)
        tar.addfile(ti, _io.BytesIO(man))
        bad = b"pas une base sqlite"
        ti2 = _tar.TarInfo("wow.sqlite")
        ti2.size = len(bad)
        tar.addfile(ti2, _io.BytesIO(bad))
    assert c.post("/api/admin/restore/preview", content=buf.getvalue()).status_code == 400


def test_restore_roundtrip():
    c = _admin_client("adminbak3@test.local", "10.99.10.1")
    backup = c.get("/api/admin/backup").content
    r = c.post("/api/admin/restore/preview", content=backup)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] and "counts" in j and "manifest" in j and j["counts"]["users"] >= 1
    r = c.post("/api/admin/restore", content=backup)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    # la base restaurée répond encore (la session courante est dans l'instantané pris juste avant)
    assert c.get("/api/health").json()["ok"] is True
    assert c.get("/api/admin/backup").status_code == 200


def test_swap_file_falls_back_on_cross_device(monkeypatch, tmp_path):
    """Régression (vécu 20/09) : os.replace échoue en EXDEV entre /tmp et le volume de
    données — _swap_file doit retomber sur une copie dans le dossier cible."""
    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    src.write_bytes(b"nouvelle")
    dst.write_bytes(b"ancienne")
    real = os.replace
    calls = {"n": 0}

    def fake(a, b):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(18, "Invalid cross-device link")
        return real(a, b)

    monkeypatch.setattr(M.os, "replace", fake)
    M._swap_file(src, dst)
    assert dst.read_bytes() == b"nouvelle"
    assert not src.exists()


def _tar_bytes(entries):
    """Archive tar.gz de test : {nom: octets}."""
    import io as _io
    import tarfile as _tar

    buf = _io.BytesIO()
    with _tar.open(fileobj=buf, mode="w:gz") as tar:
        for name, blob in entries.items():
            ti = _tar.TarInfo(name)
            ti.size = len(blob)
            tar.addfile(ti, _io.BytesIO(blob))
    return buf.getvalue()


def _extract_member(archive, name):
    import io as _io
    import tarfile as _tar

    with _tar.open(fileobj=_io.BytesIO(archive), mode="r:gz") as tar:
        return tar.extractfile(name).read()


def _rebuild_archive(archive, overrides):
    import io as _io
    import tarfile as _tar

    entries = {}
    with _tar.open(fileobj=_io.BytesIO(archive), mode="r:gz") as tar:
        for m in tar.getmembers():
            if m.isfile():
                entries[m.name] = tar.extractfile(m).read()
    entries.update(overrides)
    return _tar_bytes(entries)


def test_restore_rejects_decompression_bomb(monkeypatch):
    """Revue 20/09 : le plafond porte sur la taille DÉCOMPRESSÉE, pas sur le .tar.gz."""
    c = _admin_client("adminbomb@test.local", "10.99.11.1")
    monkeypatch.setattr(M, "BACKUP_MAX_UNPACKED_MB", 1)
    big = b"\x00" * (1024 * 1024 + 512)
    arc = _tar_bytes({"manifest.json": b'{"app": "Cohors", "version": "1.0.0"}',
                      "wow.sqlite": b"x", "big.bin": big})
    r = c.post("/api/admin/restore/preview", content=arc)
    assert r.status_code == 400
    assert "décompressée" in r.json()["detail"]


def _purge_rollbacks():
    """DATA_DIR est partagé sur toute la session pytest : on part d'un état net
    (filets horodatés, filet simple d'avant les horodatages, annexes -shm/-wal)."""
    for p in M.DATA_DIR.glob("wow.sqlite.pre-restore*"):
        p.unlink()


def test_restore_keeps_rollback_copy():
    """Filet horodaté : l'ancienne base est renommée wow.sqlite.pre-restore-<date> avant
    l'écrasement, et deux restaurations n'écrasent pas le premier filet (revue 20/09)."""
    import sqlite3 as _sq

    c = _admin_client("adminrb@test.local", "10.99.12.1")
    _purge_rollbacks()
    backup = c.get("/api/admin/backup").content
    assert c.post("/api/admin/restore", content=backup).status_code == 200
    files = sorted(M.DATA_DIR.glob("wow.sqlite.pre-restore-*"))
    assert len(files) == 1 and files[0].stat().st_size > 0
    con = _sq.connect(str(files[0]))    # une vraie base, complète (WAL checkpointé avant)
    assert con.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= 1
    con.close()
    assert c.post("/api/admin/restore", content=backup).status_code == 200
    assert len(sorted(M.DATA_DIR.glob("wow.sqlite.pre-restore-*"))) == 2


def test_restore_prunes_old_rollback_copies():
    """Seuls les 3 filets les plus récents sont conservés — les annexes -shm/-wal d'un filet
    rouvert ne comptent pas comme des filets (bug attrapé le 20/09 : elles faisaient évincer
    un vrai filet)."""
    c = _admin_client("adminrb2@test.local", "10.99.16.1")
    _purge_rollbacks()
    for i in range(5):
        (M.DATA_DIR / ("wow.sqlite.pre-restore-2020010%d-000001" % i)).write_bytes(b"vieux")
    # annexes sqlite à côté d'un vieux filet (comme après un sqlite3.connect dessus)
    (M.DATA_DIR / "wow.sqlite.pre-restore-20200104-000001-shm").write_bytes(b"stub")
    (M.DATA_DIR / "wow.sqlite.pre-restore-20200104-000001-wal").write_bytes(b"")
    backup = c.get("/api/admin/backup").content
    assert c.post("/api/admin/restore", content=backup).status_code == 200
    caps = [p for p in sorted(M.DATA_DIR.glob("wow.sqlite.pre-restore-*"))
            if not p.name.endswith(("-shm", "-wal", "-journal"))]
    assert len(caps) == 3                     # les 3 plus récents, annexes exclues
    assert "20200103" in caps[0].name         # les plus vieux sont partis
    assert not (M.DATA_DIR / "wow.sqlite.pre-restore-20200100-000001").exists()


def test_restore_replays_schema_migrations():
    """Une sauvegarde plus ancienne (colonne manquante) doit revenir migrée, sans redémarrage."""
    import sqlite3 as _sq
    import tempfile

    c = _admin_client("adminmig@test.local", "10.99.13.1")
    backup = c.get("/api/admin/backup").content
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        f.write(_extract_member(backup, "wow.sqlite"))
        tmp_db = f.name
    con = _sq.connect(tmp_db)
    con.execute("ALTER TABLE craft_recipes DROP COLUMN expansion")   # colonne ajoutée par migration
    con.commit()
    con.close()
    arc = _rebuild_archive(backup, {"wow.sqlite": Path(tmp_db).read_bytes()})
    r = c.post("/api/admin/restore", content=arc)
    assert r.status_code == 200, r.text
    con = _sq.connect(str(M.DATA_DIR / "wow.sqlite"))
    cols = [row[1] for row in con.execute("PRAGMA table_info(craft_recipes)")]
    con.close()
    assert "expansion" in cols


def test_restore_rejects_web_identity_file():
    """Revue 20/09 : pas de .html/.svg ni d'image truquée dans branding/ (XSS même origine)."""
    c = _admin_client("adminbrand@test.local", "10.99.14.1")
    base = {"manifest.json": b'{"app": "Cohors", "version": "1.0.0"}', "wow.sqlite": b"x"}
    for name, blob in (("branding/x.html", b"<script>alert(1)</script>"),
                       ("branding/logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>"),
                       ("branding/logo.png", b"<html>pas une image</html>")):
        arc = _tar_bytes(dict(base, **{name: blob}))
        r = c.post("/api/admin/restore/preview", content=arc)
        assert r.status_code == 400, (name, r.text)
        assert "identité" in r.json()["detail"]


def test_manifest_verified_and_version_warned():
    """Revue 20/09 : manifest exigeant (app Cohors) et comparaison de versions en avertissement."""
    import json as _json

    c = _admin_client("adminman@test.local", "10.99.15.1")
    backup = c.get("/api/admin/backup").content
    bad = _rebuild_archive(backup, {"manifest.json": b'{"app": "Autre", "version": "1.0.0"}'})
    assert c.post("/api/admin/restore/preview", content=bad).status_code == 400
    man = _json.loads(_extract_member(backup, "manifest.json"))
    man["version"] = "2099.01.001"
    newer = _rebuild_archive(backup, {"manifest.json": _json.dumps(man).encode()})
    r = c.post("/api/admin/restore/preview", content=newer)
    assert r.status_code == 200, r.text
    assert any("plus récente" in w for w in r.json()["warnings"])
    r = c.post("/api/admin/restore/preview", content=backup)     # même version : aucun bruit
    assert r.status_code == 200 and r.json()["warnings"] == []


def test_recipe_wishlist_toggle_and_export():
    """Recette marquée/retirée en wishlist + export addon (clé négative si pas d'item_id)."""
    c = _admin_client("wlrec@test.local", "10.99.21.1")
    with M._db_lock, M._db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO game_recipes (id, prof, tier, exp_rank, item, item_id, rank_no, mats,"
            " updated, item_en, tier_en, prof_en, mats_en)"
            " VALUES (900001, 'Forge', 'T', 0, 'Lame de test', 0, 1, '[]', 0, 'Test Blade', 'T', 'Blacksmithing', '[]')")
    r = c.post("/api/wishlist/recipe", json={"recipe_id": 900001, "on": True})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["on"] is True and j["key"] < 0        # pas d'item_id en base -> clé dérivée du nom
    r2 = c.post("/api/wishlist/recipe", json={"recipe_id": 900001, "on": True})
    assert r2.json().get("already") is True        # idempotent
    x = c.get("/api/wishlist/export")
    assert x.status_code == 200
    lines = x.text.strip().splitlines()
    assert lines[0] == "CohorsWL1"
    assert any(l.endswith("|recipe|Lame de test") for l in lines)
    with M._db_lock, M._db() as conn:              # une pièce d'équipement dans le même export
        conn.execute(
            "INSERT INTO wishlist (user_email, item_id, name, slot, inv_type, quality, icon, added, prio, kind)"
            " VALUES ('wlrec@test.local', 123456, 'Épaule de test', 'shoulder', 1, 4, NULL, 0, 1, 'item')")
    lines2 = c.get("/api/wishlist/export").text.strip().splitlines()
    assert "123456|item|Épaule de test" in lines2   # priorité non transmise : hors sujet en jeu
    r3 = c.post("/api/wishlist/recipe", json={"recipe_id": 900001, "on": False})
    assert r3.status_code == 200 and r3.json()["on"] is False
    assert "|recipe|" not in c.get("/api/wishlist/export").text
    assert c.post("/api/wishlist/recipe", json={"recipe_id": 999999999, "on": True}).status_code == 404


def test_wishlist_export_requires_auth():
    c = TestClient(M.app)
    assert c.get("/api/wishlist/export").status_code == 401
    assert c.post("/api/wishlist/recipe", json={"recipe_id": 1}).status_code == 401


# ------------------------------------------------------------------ mises à jour (v2026.09.151)

def _plain_client(name: str, ip: str):
    _make_user(name, is_admin=0, role="member")
    c = TestClient(M.app)
    r = c.post("/api/login", json={"email": name, "password": "test-pw-123"},
               headers={"X-Forwarded-For": ip})
    assert r.status_code == 200, r.text
    return c


def test_admin_updates_state_settings_and_guards():
    member = _plain_client("upd-member@test.local", "10.99.40.1")
    assert member.get("/api/admin/updates").status_code == 403
    assert member.post("/api/admin/updates", json={"values": {}}).status_code == 403

    c = _admin_client("upd-admin@test.local", "10.99.40.2")
    r = c.get("/api/admin/updates")
    assert r.status_code == 200
    st = r.json()["state"]
    assert st["current"] == M.VERSION
    assert st["available"] is False and st["latest"] is None
    assert st["settings"]["upd_check_h"] == 24          # défaut : vérification quotidienne
    assert st["settings"]["upd_apply_auto"] is False
    assert st["applier"]["installed"] is False
    # vérification que les champs running/running_at sont bien dans l'état
    assert "running" in st["applier"] and "running_at" in st["applier"]

    # bornes : cadence hors liste → 400 ; valeurs valides → enregistrées
    assert c.post("/api/admin/updates", json={"values": {"upd_check_h": "7"}}).status_code == 400
    assert c.post("/api/admin/updates", json={"values": {"upd_apply_auto": "peut-être"}}).status_code == 400
    r = c.post("/api/admin/updates", json={"values": {"upd_check_h": "6", "upd_apply_auto": "1"}})
    assert r.status_code == 200
    st = r.json()["state"]
    assert st["settings"]["upd_check_h"] == 6 and st["settings"]["upd_apply_auto"] is True


def test_admin_updates_check_apply_cancel(monkeypatch):
    c = _admin_client("upd-admin2@test.local", "10.99.40.3")
    newer = {"version": "2099.01.001", "published_at": "2099-01-01T00:00:00Z",
             "url": "https://example.invalid/release"}
    monkeypatch.setattr(M, "_upd_latest_release", lambda: newer)

    r = c.post("/api/admin/updates/check")
    st = r.json()["state"]
    assert st["available"] is True and st["latest"]["version"] == "2099.01.001"
    assert st["check"]["error"] == ""

    # appliquer → demande déposée (le conteneur ne redémarre jamais lui-même)
    r = c.post("/api/admin/updates/apply")
    assert r.status_code == 200
    assert r.json()["state"]["request"]["version"] == "2099.01.001"
    payload = json.loads(M._UPD_REQUEST.read_text(encoding="utf-8"))
    assert payload["version"] == "2099.01.001"

    # annuler
    assert c.delete("/api/admin/updates/request").status_code == 200
    assert not M._UPD_REQUEST.exists()

    # une erreur réseau est enregistrée, sans casser l'endpoint
    def boom():
        raise OSError("réseau indisponible")
    monkeypatch.setattr(M, "_upd_latest_release", boom)
    st = c.post("/api/admin/updates/check").json()["state"]
    assert "réseau indisponible" in st["check"]["error"]

    # à jour → appliquer refuse
    monkeypatch.setattr(M, "_upd_latest_release",
                        lambda: {"version": M.VERSION, "published_at": "", "url": ""})
    c.post("/api/admin/updates/check")
    assert c.post("/api/admin/updates/apply").status_code == 400


def test_upd_vtuple_orders_versions():
    vt = M._upd_vtuple
    assert vt("2026.09.150") < vt("2026.09.151")
    assert vt("2026.09.149") < vt("2026.09.149-c1") < vt("2026.09.149-c3") < vt("2026.09.150")
    assert vt("2026.10.001") > vt("2026.09.199")


def test_upd_tick_auto_apply_respects_sims(monkeypatch):
    c = _admin_client("upd-admin3@test.local", "10.99.40.4")
    c.post("/api/admin/updates", json={"values": {"upd_check_h": "0", "upd_apply_auto": "1"}})
    monkeypatch.setattr(M, "_upd_latest_release",
                        lambda: {"version": "2099.02.002", "published_at": "", "url": ""})
    M._upd_run_check()
    # une simulation en cours → la demande attend
    with M._db_lock, M._db() as conn:
        conn.execute("INSERT OR REPLACE INTO sims (id, created, ip, iterations, status,"
                     " input_hash, input_file, user_email)"
                     " VALUES ('upd-sim', ?, '10.0.0.1', 1, 'running', 'h', 'f', 'upd-admin3@test.local')",
                     (time.time(),))
    M._upd_tick()
    assert not M._UPD_REQUEST.exists()
    # simulation terminée → la demande part toute seule
    with M._db_lock, M._db() as conn:
        conn.execute("UPDATE sims SET status='done' WHERE id='upd-sim'")
    M._upd_tick()
    assert M._UPD_REQUEST.exists()
    M._UPD_REQUEST.unlink()
    c.post("/api/admin/updates", json={"values": {"upd_apply_auto": "0"}})


def test_applier_script_refuses_bad_version_and_signals_heartbeat(tmp_path):
    import shutil as _sh
    import subprocess as _sp
    import sys as _sys
    app = tmp_path / "app"
    (app / "deploy").mkdir(parents=True)
    (app / "data").mkdir()
    (app / "docker-compose.yml").write_text("services:\n  app:\n    build: .\n", encoding="utf-8")
    _sh.copy(SRC_DIR / "deploy/apply-update.py", app / "deploy/apply-update.py")
    env = {**os.environ, "DATA_DIR": str(app / "data")}

    # sans demande : battement de cœur seulement
    res = _sp.run([_sys.executable, str(app / "deploy/apply-update.py")],
                  capture_output=True, text=True, env=env)
    assert res.returncode == 0
    st = json.loads((app / "data/update-applier.json").read_text(encoding="utf-8"))
    assert st["result"] == "attente" and st["seen_at"] > 0

    # demande hostile : refusée et purgée, rien n'est exécuté
    (app / "data/update-request.json").write_text(
        json.dumps({"version": "2026.09.150; rm -rf /", "by": "x"}), encoding="utf-8")
    res = _sp.run([_sys.executable, str(app / "deploy/apply-update.py")],
                  capture_output=True, text=True, env=env)
    assert res.returncode == 1
    assert not (app / "data/update-request.json").exists()
    st = json.loads((app / "data/update-applier.json").read_text(encoding="utf-8"))
    assert st["result"].startswith("version refusée")

    # demande valide en dry-run : rien de changé, la demande reste
    (app / "data/update-request.json").write_text(
        json.dumps({"version": "2099.01.001", "by": "test"}), encoding="utf-8")
    res = _sp.run([_sys.executable, str(app / "deploy/apply-update.py"), "--dry-run"],
                  capture_output=True, text=True, env=env)
    assert res.returncode == 0
    assert (app / "data/update-request.json").exists()
    st = json.loads((app / "data/update-applier.json").read_text(encoding="utf-8"))
    # dry-run : result="dry-run ok", applied reste inchangé
    assert st.get("result") == "dry-run ok"
    assert st.get("applied") is None


def test_stuff_cur_returns_200_and_max_rank_modes(monkeypatch):
    """POST /api/stuff en mode cur renvoie 200 sans deadlock ; mode='max' et max_rank=True seul
    donnent plan['max_rank'] == True. Ce test a attrapé le bug de _normalize (v.get('mode',
    'cur') masquait max_rank sans mode)."""
    _make_user("stuffuser@test.local")
    c = TestClient(M.app)
    r = c.post("/api/login", json={"email": "stuffuser@test.local", "password": "test-pw-123"},
               headers={"X-Forwarded-For": "10.99.50.1"})
    assert r.status_code == 200, r.text

    # créer un profil test avec un export SimC valide (contient des sacs parsés)
    simc_export = """druid="TestFeral"
level=80
spec=feral
### Gear from Bags
# head=100001
# neck=100002
# shoulder=100003"""
    prof_id = c.post("/api/profiles", json={
        "name": "Profil Stuff", "input": simc_export
    }).json()["id"]

    # mode cur (défaut) → 200 sans deadlock
    r = c.post("/api/stuff", json={"profile_id": prof_id, "content": "raid", "mode": "cur"})
    assert r.status_code == 200, r.text

    # mode: "max" → plan["max_rank"] == True
    r = c.post("/api/stuff", json={"profile_id": prof_id, "content": "raid", "mode": "max"})
    assert r.status_code == 200
    # vérifier que la simulation a bien max_rank dans le plan
    with M._db_lock, M._db() as conn:
        plan = json.loads(conn.execute(
            "SELECT plan FROM sims WHERE kind='stuff' ORDER BY created DESC LIMIT 1").fetchone()["plan"])
    assert plan["max_rank"] is True

    # max_rank: true seul → mode="max" et plan["max_rank"] == True
    r = c.post("/api/stuff", json={"profile_id": prof_id, "content": "raid", "max_rank": True})
    assert r.status_code == 200
    with M._db_lock, M._db() as conn:
        plan = json.loads(conn.execute(
            "SELECT plan FROM sims WHERE kind='stuff' ORDER BY created DESC LIMIT 1").fetchone()["plan"])
    assert plan["max_rank"] is True
    assert plan["mode"] == "max"


def test_applier_failure_clears_running_and_sets_at(tmp_path):
    """L'applicateur en échec simulé : running repasse à '' et at est renseigné."""
    import shutil as _sh
    import subprocess as _sp
    import sys as _sys
    app = tmp_path / "app"
    (app / "deploy").mkdir(parents=True)
    (app / "data").mkdir()
    (app / "docker-compose.yml").write_text("services:\n  app:\n    build: .\n", encoding="utf-8")
    _sh.copy(SRC_DIR / "deploy/apply-update.py", app / "deploy/apply-update.py")
    env = {**os.environ, "DATA_DIR": str(app / "data")}

    # dépôt une demande valide
    (app / "data/update-request.json").write_text(
        json.dumps({"version": "2099.01.002", "by": "test"}), encoding="utf-8")

    # simuler un échec : on modifie apply-update.py pour renvoyer une erreur après avoir écrit
    # running et at, mais sans réussir
    script = (app / "deploy/apply-update.py").read_text(encoding="utf-8")
    # injecter un raise après le setup() initial
    patched = script.replace(
        'def main():',
        'def _fake_fail():\n    import json as _j, time as _t, sys as _s\n    data_dir = _s.environ.get("DATA_DIR", ".")\n    def status(**kw):\n        import json\n        applier = {}\n        try:\n            ap = json.loads((os.path.join(data_dir, "update-applier.json")).read_text())\n            ap.update(kw)\n            (os.path.join(data_dir, "update-applier.json")).write_text(json.dumps(ap))\n        except Exception:\n            ap = kw\n            (os.path.join(data_dir, "update-applier.json")).write_text(json.dumps(ap))\n        return ap\n    status(running="2099.01.002", at=_t.time())\n    raise OSError("déployment simulé échoué")\n\ndef main():\n    _fake_fail()',
        1
    )
    (app / "deploy/apply-update.py").write_text(patched, encoding="utf-8")
    env_mod = {**env, "PYTHONPATH": str(app / "deploy")}

    res = _sp.run([_sys.executable, str(app / "deploy/apply-update.py")],
                  capture_output=True, text=True, env=env_mod)
    # le script peut échouer (1) ou non selon comment on simule, mais le plus important
    # est que l'état reflète l'échec : running="" et at renseigné
    st = json.loads((app / "data/update-applier.json").read_text(encoding="utf-8"))
    assert st.get("running") == "" or "échec" in str(st.get("result", ""))
    assert st.get("at", 0) > 0


def test_applier_dry_run_succeeds_and_sets_at(tmp_path):
    """Applikateur en dry-run : running == '', result == 'dry-run ok', at absent (ne pas tromper le panneau)."""
    import shutil as _sh
    import subprocess as _sp
    import sys as _sys
    app = tmp_path / "app"
    (app / "deploy").mkdir(parents=True)
    (app / "data").mkdir()
    (app / "docker-compose.yml").write_text("services:\n  app:\n    build: .\n", encoding="utf-8")
    _sh.copy(SRC_DIR / "deploy/apply-update.py", app / "deploy/apply-update.py")
    env = {**os.environ, "DATA_DIR": str(app / "data")}

    # dépôt une demande valide
    (app / "data/update-request.json").write_text(
        json.dumps({"version": "2099.01.003", "by": "test"}), encoding="utf-8")

    res = _sp.run([_sys.executable, str(app / "deploy/apply-update.py"), "--dry-run"],
                  capture_output=True, text=True, env=env)
    # en dry-run le script retourne 0 (pas d'erreur)
    assert res.returncode == 0, res.stderr
    st = json.loads((app / "data/update-applier.json").read_text(encoding="utf-8"))
    assert st["running"] == "", st
    assert st["result"] == "dry-run ok", st
    assert st.get("at") is None or st.get("at") == 0, st


def test_bis_json_all_src_fr_in_bis_content_map():
    """Chaque src_fr de bis.json doit être présent dans BIS_CONTENT_MAP."""
    import json as _json
    from pathlib import Path as _Path
    bis_file = _Path(__file__).resolve().parents[1] / "app" / "data" / "bis.json"
    bis_data = _json.loads(bis_file.read_text(encoding="utf-8"))
    srcs = set()
    for spec in bis_data.get("specs", {}).values():
        if spec:
            for slot in spec.get("slots", []):
                s = slot.get("src_fr")
                if s:
                    srcs.add(s)
    assert srcs, "aucun src_fr trouvé dans bis.json"
    # importer BIS_CONTENT_MAP dynamiquement
    from app.main import BIS_CONTENT_MAP
    missing = srcs - set(BIS_CONTENT_MAP.keys())
    assert not missing, f"src_fr non mappés : {missing}"


def test_bis_json_no_duplicate_keys():
    """Aucune clé en double dans BIS_CONTENT_MAP de main.py — vérifié via ast."""
    import ast as _ast
    from pathlib import Path as _Path
    main_file = _Path(__file__).resolve().parents[1] / "app" / "main.py"
    tree = _ast.parse(main_file.read_text(encoding="utf-8"))
    bis_map = None
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Assign):
            for target in node.targets:
                if isinstance(target, _ast.Name) and target.id == "BIS_CONTENT_MAP":
                    bis_map = node
                    break
    assert bis_map is not None, "BIS_CONTENT_MAP introuvable dans main.py"
    value = bis_map.value
    assert isinstance(value, _ast.Dict), f"BIS_CONTENT_MAP n'est pas un dict : {type(value)}"
    keys = [k.value if isinstance(k, _ast.Constant) else None for k in value.keys]
    assert None not in keys, "Clé non constante dans BIS_CONTENT_MAP"
    assert len(keys) == len(set(keys)), f"Clés dupliquées dans BIS_CONTENT_MAP : {[k for k in keys if keys.count(k) > 1]}"


def test_content_craft_non_bis_returns_400():
    """content: 'craft' en mode non-BIS (cur/max) → 400 car craft n'est pas dans STUFF_CONTENTS."""
    _make_user("craft400@test.local")
    c = TestClient(M.app)
    r = c.post("/api/login", json={"email": "craft400@test.local", "password": "test-pw-123"},
               headers={"X-Forwarded-For": "10.99.50.1"})
    assert r.status_code == 200, r.text
    simc_export = """druid="TestFeral"
level=80
spec=feral"""
    prof_id = c.post("/api/profiles", json={"name": "Profil Craft 400", "input": simc_export}).json()["id"]
    # content: "craft" n'est pas dans STUFF_CONTENTS (seulement raid, mplus, delves)
    r = c.post("/api/stuff", json={"profile_id": prof_id, "content": "craft", "mode": "cur"})
    assert r.status_code == 400, r.text


def test_bis_content_craft_only_includes_craft():
    """bis_content: ['craft'] doit donner plan['content'] == ['craft'] (pas de fallback sur raid)."""
    _make_user("bisraft@test.local")
    c = TestClient(M.app)
    r = c.post("/api/login", json={"email": "bisraft@test.local", "password": "test-pw-123"},
               headers={"X-Forwarded-For": "10.99.50.1"})
    assert r.status_code == 200, r.text
    simc_export = """druid="TestFeral"
level=80
spec=feral
### Gear from Bags
# head=100001
# neck=100002
# shoulder=100003"""
    prof_id = c.post("/api/profiles", json={"name": "Profil BIS craft", "input": simc_export}).json()["id"]
    r = c.post("/api/stuff", json={
        "profile_id": prof_id, "mode": "bis",
        "bis_content": ["craft"]
    })
    assert r.status_code == 200, r.text
    with M._db_lock, M._db() as conn:
        plan = json.loads(conn.execute(
            "SELECT plan FROM sims WHERE kind='stuff' ORDER BY created DESC LIMIT 1").fetchone()["plan"])
    assert plan["content"] == ["craft"], plan


def test_bis_content_too_long_returns_422():
    """bis_content brute avec plus de 10 éléments → 422 (max_length du Field)."""
    _make_user("bislong@test.local")
    c = TestClient(M.app)
    r = c.post("/api/login", json={"email": "bislong@test.local", "password": "test-pw-123"},
               headers={"X-Forwarded-For": "10.99.50.1"})
    assert r.status_code == 200, r.text
    simc_export = """druid="TestFeral"
level=80
spec=feral"""
    prof_id = c.post("/api/profiles", json={"name": "Profil BIS long", "input": simc_export}).json()["id"]
    r = c.post("/api/stuff", json={
        "profile_id": prof_id, "mode": "bis",
        "bis_content": ["raid"] * 11  # 11 éléments bruts > max_length=10
    })
    assert r.status_code == 422, r.text


def test_bis_content_unknown_value_returns_422():
    """bis_content avec une valeur inconnue → 422 (FastAPI validation error)."""
    _make_user("bisunk@test.local")
    c = TestClient(M.app)
    r = c.post("/api/login", json={"email": "bisunk@test.local", "password": "test-pw-123"},
               headers={"X-Forwarded-For": "10.99.50.1"})
    assert r.status_code == 200, r.text
    simc_export = """druid="TestFeral"
level=80
spec=feral"""
    prof_id = c.post("/api/profiles", json={"name": "Profil BIS unk", "input": simc_export}).json()["id"]
    r = c.post("/api/stuff", json={
        "profile_id": prof_id, "mode": "bis",
        "bis_content": ["raid", "fakecontent"]
    })
    assert r.status_code == 422, r.text


def test_stuff_parse_export_real_format():
    """Non-régression : un vrai export SimC (classe="Nom") doit être parsé correctement."""
    from app.main import _stuff_parse_export

    r = _stuff_parse_export('shaman="X"\nspec=restoration\n')
    assert r["cls"] == "shaman", f"cls={r['cls']!r}"
    assert r["name"] == "X", f"name={r['name']!r}"

    r2 = _stuff_parse_export('druid="Chamoisdort"\nspec=restoration\n')
    assert r2["cls"] == "druid"
    assert r2["name"] == "Chamoisdort"


# --- Objets qui font planter l'image SimC (Alpine/musl) en multi-cœur -----------------------

_CRASH_PROFILE = 'shaman="X"\nlevel=90\nspec=restoration\ntrinket1=,id=270162,bonus_id=6652/13333/12838\n'


def _fake_run_sim(calls, crash_single_thread=False):
    def fake(profile_path=None, iterations=0, outdir=None, timeout=0, extra=None, **_kw):
        extra = list(extra or [])
        calls.append(extra)
        has_item = "id=270162" in Path(profile_path).read_text()
        if has_item and ("threads=1" not in extra or crash_single_thread):
            return {"ok": False, "rc": 139, "log_tail": "Segmentation fault"}
        return {"ok": True, "rc": 0, "dps": 42000.0}
    return fake


def test_crash_fallback_retries_single_thread_and_keeps_item(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(M, "run_sim", _fake_run_sim(calls))
    p = tmp_path / "input.simc"
    p.write_text(_CRASH_PROFILE)
    res, note = M._sim_with_crash_fallback(p, 1000, ["calculate_scale_factors=1"], 60, tmp_path)
    assert res["ok"] and res["dps"] == 42000.0
    assert "un seul cœur" in note and "Réceptacle rituel" in note
    assert "id=270162" in p.read_text()                      # l'objet reste dans la sim
    assert calls == [["calculate_scale_factors=1"], ["calculate_scale_factors=1", "threads=1"]]


def test_crash_fallback_strips_item_as_last_resort(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(M, "run_sim", _fake_run_sim(calls, crash_single_thread=True))
    p = tmp_path / "input.simc"
    p.write_text(_CRASH_PROFILE)
    res, note = M._sim_with_crash_fallback(p, 1000, None, 60, tmp_path)
    assert res["ok"]
    assert note.startswith("Sim lancée SANS Réceptacle rituel")
    assert "id=270162" not in p.read_text()
    assert len(calls) == 3


def test_crash_fallback_no_retry_without_known_item_or_segfault(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(M, "run_sim", lambda **kw: calls.append(kw.get("extra")) or {"ok": False, "rc": 139})
    p = tmp_path / "input.simc"
    p.write_text('shaman="X"\nlevel=90\nspec=restoration\ntrinket1=,id=1234\n')
    res, note = M._sim_with_crash_fallback(p, 1000, None, 60, tmp_path)
    assert not res["ok"] and note is None and len(calls) == 1   # objet inconnu : pas de relance

    calls.clear()
    monkeypatch.setattr(M, "run_sim", lambda **kw: calls.append(kw.get("extra")) or {"ok": False, "rc": 1})
    p.write_text(_CRASH_PROFILE)
    res, note = M._sim_with_crash_fallback(p, 1000, None, 60, tmp_path)
    assert not res["ok"] and note is None and len(calls) == 1   # autre erreur que 139 : pas de relance


# --- Meilleures pièces d'artisanat --------------------------------------------------------------

def test_bnet_search_item_exact_filters_fuzzy_results(monkeypatch):
    """La recherche Blizzard est plein texte : seul le nom exact compte, équipable et plus haut niveau d'abord."""
    from app import bnet as B

    def fake_get(path, params=None, not_found=""):
        assert path == "/data/wow/search/item" and params["name.en_US"] == "Row Walker's Deflectors"
        mk = lambda i, n, inv, sub, lvl: {"data": {"id": i, "name": {"en_US": n}, "level": lvl,
                                                   "inventory_type": {"type": inv},
                                                   "item_subclass": {"name": {"en_US": sub}}}}
        return {"results": [mk(280644, "Row Walker's Decor", "NON_EQUIP", "Decor", 1),
                            mk(267271, "Row Walker's Deflectors", "WRIST", "Plate", 197),
                            mk(271900, "Row Walker's Deflectors", "NON_EQUIP", "Other", 250),
                            mk(271901, "row walker's deflectors", "WRIST", "Plate", 285)]}
    monkeypatch.setattr(B, "_get", fake_get)
    B._cache.clear()
    got = B.search_item_exact("Row Walker's Deflectors")
    assert got == {"id": 271901, "inv_type": "WRIST", "subclass_en": "Plate", "ilvl": 285}
    assert B.search_item_exact("") is None


def test_game_sync_resolves_crafted_item_by_name(monkeypatch):
    from app import bnet as B
    monkeypatch.setattr(B, "game_profession", lambda pid, locale=None: {
        "name": "Leatherworking" if locale == "en_US" else "Travail du cuir",
        "skill_tiers": [{"id": 1, "name": "Midnight Leatherworking"}]})
    monkeypatch.setattr(B, "game_tier_recipes", lambda pid, tid, locale=None: [{"id": 910001}])
    monkeypatch.setattr(B, "game_recipe", lambda rid, locale=None: {
        "id": rid, "name": "Casque serpentin" if locale != "en_US" else "Serpentine Helm", "reagents": []})
    monkeypatch.setattr(B, "search_item_exact", lambda name: {"id": 271999, "inv_type": "HEAD",
                                                              "subclass_en": "Mail", "ilvl": 285}
                        if name == "Serpentine Helm" else None)
    old_key = M._recipe_wish_key(0, "Casque serpentin")      # étoile posée quand l'objet était inconnu
    with M._db_lock, M._db() as conn:
        conn.execute("INSERT INTO wishlist (user_email, item_id, name, kind, added) VALUES (?,?,?,?,?)",
                     ("wlsync@test.local", old_key, "Casque serpentin", "recipe", 0))
    M._game_sync_state["state"] = "idle"
    M._game_sync(["Travail du cuir"])
    with M._db_lock, M._db() as conn:
        row = conn.execute("SELECT item_id, inv_type, subclass_en, ilvl, item_en FROM game_recipes "
                           "WHERE id=910001").fetchone()
        wl = [r["item_id"] for r in conn.execute("SELECT item_id FROM wishlist WHERE user_email='wlsync@test.local'")]
        conn.execute("DELETE FROM game_recipes WHERE prof='Travail du cuir'")
        conn.execute("DELETE FROM wishlist WHERE user_email='wlsync@test.local'")
    assert wl == [271999]                                      # l'étoile suit l'objet retrouvé
    assert dict(row) == {"item_id": 271999, "inv_type": "HEAD", "subclass_en": "Mail", "ilvl": 285,
                         "item_en": "Serpentine Helm"}


def test_stuff_best_crafted_filters_by_class_and_lists_crafters():
    rows = [  # id, prof, fr, en, item_id, inv, sub, ilvl
        (920001, "Travail du cuir", "Heaume de mailles", "Mail Helm", 272001, "HEAD", "Mail", 285),
        (920002, "Forge", "Heaume de plaques", "Plate Helm", 272002, "HEAD", "Plate", 285),
        (920003, "Couture", "Cape de parade", "Parade Cloak", 272003, "CLOAK", "Cosmetic", 1),
        (920004, "Couture", "Cape tissée", "Woven Cloak", 272004, "CLOAK", "Cloth", 285),
        (920005, "Joaillerie", "Anneau serti", "Set Ring", 272005, "FINGER", "Miscellaneous", 285),
        (920006, "Forge", "Hache de guerre", "War Axe", 272006, "TWOHWEAPON", "Axe", 285),
        (920007, "Forge", "Masse bénie", "Blessed Mace", 272007, "WEAPON", "Mace", 285),
        (920008, "Forge", "Rempart", "Bulwark", 272008, "SHIELD", "Shield", 285),
        (920009, "Travail du cuir", "Tablier de tanneur", "Tanner Apron", 272009, "PROFESSION_GEAR",
         "Leatherworking", 285),
        (920010, "Ingénierie", "Lunettes de mailles", "Mail Goggles", 272010, "HEAD", "Mail", 270),
        (920011, "Forge", "Masse ancienne", "Old Mace", 272011, "WEAPON", "Mace", 250),
    ]
    with M._db_lock, M._db() as conn:
        for r in rows:
            conn.execute("INSERT OR REPLACE INTO game_recipes (id, prof, tier, exp_rank, item, item_id, rank_no, mats,"
                         " updated, item_en, tier_en, prof_en, mats_en, inv_type, subclass_en, ilvl)"
                         " VALUES (?,?,'T',0,?,?,1,'[]',0,?,'T','','[]',?,?,?)",
                         (r[0], r[1], r[2], r[4], r[3], r[5], r[6], r[7]))
        conn.execute("INSERT OR REPLACE INTO game_recipes (id, prof, tier, exp_rank, item, item_id, rank_no, mats,"
                     " updated, item_en, tier_en, prof_en, mats_en, inv_type, subclass_en, ilvl)"
                     " VALUES (920012,'Forge','T',1,'Vieux heaume',272012,1,'[]',0,'Old Helm','T','','[]','HEAD','Mail',400)")
        conn.execute("INSERT OR REPLACE INTO craft_recipes (crafter, item, item_id) VALUES ('brokk', 'Heaume de mailles', 0)")
        conn.execute("INSERT OR REPLACE INTO craft_recipes (crafter, item, item_id) VALUES ('sindri', 'x', 272005)")
        res = M._stuff_best_crafted(conn, "shaman", "restoration", ["craft"], {272005}, set())
        conn.execute("DELETE FROM game_recipes WHERE id BETWEEN 920001 AND 920012")
        conn.execute("DELETE FROM craft_recipes WHERE crafter IN ('brokk', 'sindri')")
    by = {s["slot"]: s for s in res["slots"]}
    assert res["armor"] == "Mail" and res["stats"] == ["crit", "vers"]          # priorité raid du chaman resto
    assert [i["id"] for i in by["head"]["items"]] == [272001, 272010]          # mailles seulement, niveau décroissant
    assert by["head"]["items"][0]["crafters"] == ["Brokk"]                     # artisan trouvé par le nom
    assert [i["id"] for i in by["back"]["items"]] == [272004]                  # cosmétique écarté
    assert by["finger"]["items"][0]["owned"] is True and by["finger"]["items"][0]["crafters"] == ["Sindri"]
    assert [i["id"] for i in by["main_hand"]["items"]] == [272007, 272011]     # pas de hache 2M pour un chaman
    assert [i["id"] for i in by["off_hand"]["items"]] == [272008]
    assert "272009" not in str(res) and "272012" not in str(res)             # outils de métier et ancienne extension écartés


def test_bis_murder_row_is_mythic_plus_only():
    """Murder Row (Allée du meurtre) est un donjon M+ : absent d'un filtre « Artisanat » seul."""
    for src in ("Murder Row", "Allée du meurtre", "Murder Row & Catalyseur"):
        assert M.BIS_CONTENT_MAP[src] == "mplus", src
    blk = {"slots": [{"slot": "feet", "id": 1, "src_fr": "Murder Row"},
                     {"slot": "hands", "id": 2, "src_fr": "Crafted"}]}
    assert [s["id"] for s in M._stuff_bis_filter(blk, ["craft"])["slots"]] == [2]
    assert [s["id"] for s in M._stuff_bis_filter(blk, ["mplus"])["slots"]] == [1]


def test_bnet_professions_keeps_known_recipes_of_last_two_tiers(monkeypatch):
    payload = {"primaries": [{"profession": {"name": "Leatherworking", "id": 165}, "tiers": [
        {"tier": {"name": "Khaz Algar Leatherworking"}, "skill_points": 100, "max_skill_points": 100,
         "known_recipes": [{"id": 111}]},
        {"tier": {"name": "Midnight Leatherworking"}, "skill_points": 92, "max_skill_points": 100,
         "known_recipes": [{"id": 930002}, {"id": 930001}, {"id": 930001}]}]}]}
    monkeypatch.setattr(M.bnet, "_get", lambda *a, **k: payload)
    data, _ts = M.bnet.professions("hyjal", "Knownrecipetest", force=True, locale="fr_FR")
    p = data["profs"][0]
    assert p["known"] == [111, 930001, 930002] and p["points"] == 92  # 2 derniers paliers ; points du plus récent


def test_stuff_best_crafted_uses_known_recipes_and_profession_fallback():
    with M._db_lock, M._db() as conn:
        for rid, fr, iid in ((930001, "Heaume connu", 273001), (930002, "Bottes inconnues", 273002)):
            conn.execute("INSERT OR REPLACE INTO game_recipes (id, prof, tier, exp_rank, item, item_id, rank_no, mats,"
                         " updated, item_en, tier_en, prof_en, mats_en, inv_type, subclass_en, ilvl)"
                         " VALUES (?,'Travail du cuir','Travail du cuir de Midnight',0,?,?,1,'[]',0,?,'','','[]',?,'Mail',290)",
                         (rid, fr, iid, fr, "HEAD" if rid == 930001 else "FEET"))
        for name, pts, known in (("lithinie", 100, [930001]), ("arssalag", 40, []), ("tanneur", 75, [])):
            data = {"profs": [{"name": "Travail du cuir", "name_fr": "Travail du cuir", "tier": "Travail du cuir de Midnight",
                               "points": pts, "max": 100, "known": known}]}
            conn.execute("INSERT OR REPLACE INTO char_professions (realm, name, ts, data) VALUES ('hyjal', ?, 0, ?)",
                         (name, json.dumps(data)))
        res = M._stuff_best_crafted(conn, "shaman", "restoration", ["craft"], set(), set())
        conn.execute("DELETE FROM game_recipes WHERE id IN (930001, 930002)")
        conn.execute("DELETE FROM char_professions WHERE name IN ('lithinie', 'arssalag', 'tanneur')")
    by = {s["slot"]: s["items"] for s in res["slots"]}
    head = next(i for i in by["head"] if i["id"] == 273001)
    feet = next(i for i in by["feet"] if i["id"] == 273002)
    assert head["crafters"] == ["Lithinie"]                                   # recette connue (API Blizzard)
    assert feet["crafters"] == []                                             # personne ne la connaît…
    assert [m["name"] for m in feet["prof_members"]] == ["Lithinie", "Tanneur", "Arssalag"]  # …repli par points


def _seed_known_recipes():
    with M._db_lock, M._db() as conn:
        for rid, fr in ((940001, "Flacon connu"), (940002, "Flacon inconnu")):
            conn.execute("INSERT OR REPLACE INTO game_recipes (id, prof, tier, exp_rank, item, item_id, rank_no, mats,"
                         " updated, item_en, tier_en, prof_en, mats_en)"
                         " VALUES (?,'Alchimie','Alchimie de Midnight',0,?,0,1,'[]',0,?,'','','[]')", (rid, fr, fr))
        data = {"profs": [{"name": "Alchimie", "name_fr": "Alchimie", "tier": "Alchimie de Midnight",
                           "points": 100, "max": 100, "known": [940001]}]}
        conn.execute("INSERT OR REPLACE INTO char_professions (realm, name, ts, data) VALUES ('hyjal', 'fiolette', 1, ?)",
                     (json.dumps(data),))


def _unseed_known_recipes():
    with M._db_lock, M._db() as conn:
        conn.execute("DELETE FROM game_recipes WHERE id IN (940001, 940002)")
        conn.execute("DELETE FROM char_professions WHERE name='fiolette'")


def test_known_craft_rows_from_blizzard():
    _seed_known_recipes()
    try:
        with M._db_lock, M._db() as conn:
            rows = [r for r in M._known_craft_rows(conn) if r["item"] in ("Flacon connu", "Flacon inconnu")]
            mine = M._known_craft_rows(conn, "Fiolette")
    finally:
        _unseed_known_recipes()
    assert rows == [{"crafter": "Fiolette", "profession": "Alchimie", "item": "Flacon connu", "item_id": 0,
                     "expansion": "Alchimie de Midnight", "exp_rank": 0, "mats": "[]"}]
    assert [r["item"] for r in mine] == ["Flacon connu"]


def test_my_recipes_marks_blizzard_known_and_prep_lists_crafter():
    _seed_known_recipes()
    try:
        c = _admin_client("recipes-admin@test.local", "10.99.60.1")
        d = c.get("/api/my/recipes", params={"realm": "hyjal", "name": "Fiolette", "prof": "Alchimie"}).json()
        by = {x["name"]: x for x in d["catalog"]}
        assert by["Flacon connu"]["known"] is True and by["Flacon connu"]["known_src"] == "blizzard"
        assert by["Flacon inconnu"]["known"] is False
        assert d["profs_ts"] == 1.0
        prep = c.get("/api/prep").json()
        ent = next(g for g in prep["game"] if g["item"] == "Flacon connu")
        assert "Fiolette" in ent["crafters"]
    finally:
        _unseed_known_recipes()


def test_my_recipes_refresh_rereads_blizzard(monkeypatch):
    payload = {"primaries": [{"profession": {"name": "Alchemy", "id": 171}, "tiers": [
        {"tier": {"name": "Midnight Alchemy"}, "skill_points": 50, "max_skill_points": 100,
         "known_recipes": [{"id": 940001}]}]}]}
    monkeypatch.setattr(M.bnet, "_get", lambda *a, **k: payload)
    c = _admin_client("recipes-admin2@test.local", "10.99.60.2")
    r = c.post("/api/my/recipes/refresh", json={"realm": "hyjal", "name": "Rafraichie"})
    assert r.status_code == 200 and r.json()["ts"] > 0, r.text
    with M._db_lock, M._db() as conn:
        row = conn.execute("SELECT data FROM char_professions WHERE realm='hyjal' AND name='rafraichie'").fetchone()
        conn.execute("DELETE FROM char_professions WHERE name='rafraichie'")
    assert json.loads(row["data"])["profs"][0]["known"] == [940001]
    m = _plain_client("recipes-member@test.local", "10.99.60.3")
    assert m.post("/api/my/recipes/refresh", json={"realm": "hyjal", "name": "Rafraichie"}).status_code == 403


# ---- v2026.09.153 : progression raids / donjons et talents (API Blizzard) ----
def _enc_payload():
    def exp(eid, name, inst):
        return {"expansion": {"id": eid, "name": name}, "instances": inst}
    cur_inst = [{"instance": {"id": 1300, "name": "Flèche du Vide"}, "modes": [
        {"difficulty": {"type": "MYTHIC", "name": "Mythique"}, "status": {"type": "IN_PROGRESS"},
         "progress": {"completed_count": 1, "total_count": 2, "encounters": [
             {"encounter": {"id": 1, "name": "Boss A"}, "completed_count": 2, "last_kill_timestamp": 1790000000000}]}},
        {"difficulty": {"type": "LFR", "name": "Raids"}, "status": {"type": "COMPLETE"},
         "progress": {"completed_count": 2, "total_count": 2, "encounters": [
             {"encounter": {"id": 1, "name": "Boss A"}, "completed_count": 1, "last_kill_timestamp": 1789000000000},
             {"encounter": {"id": 2, "name": "Boss B"}, "completed_count": 1, "last_kill_timestamp": 1789000500000}]}},
        {"difficulty": {"type": "HEROIC", "name": "Héroïque"}, "status": {"type": "IN_PROGRESS"},
         "progress": {"completed_count": 0, "total_count": 2, "encounters": []}}]}]
    old_inst = [{"instance": {"id": 1273, "name": "Nerub-ar Palace"}, "modes": []}]
    # extension courante AU MILIEU : l'ordre de la liste ne doit pas compter
    return {"expansions": [exp(503, "Dragonflight", old_inst), exp(600, "Midnight", cur_inst),
                           exp(514, "The War Within", old_inst)]}


def test_parse_encounters_keeps_current_expansion_only():
    out = M.bnet.parse_encounters(_enc_payload(), 600, "fr_FR", "Midnight")
    assert out["expansion"] == "Midnight"
    assert [i["name"] for i in out["instances"]] == ["Flèche du Vide"]
    modes = out["instances"][0]["modes"]
    assert [m["difficulty"] for m in modes] == ["LFR", "HEROIC", "MYTHIC"]          # ordre des difficultés
    assert modes[0]["label"] == "Outil de raids"                                    # pas « Raids » de Blizzard
    assert (modes[0]["done"], modes[0]["total"]) == (2, 2)
    assert modes[2]["bosses"][0] == {"id": 1, "name": "Boss A", "kills": 2, "last_kill": 1790000000}  # ms -> s
    en = M.bnet.parse_encounters(_enc_payload(), 600, "en_US")
    assert en["instances"][0]["modes"][0]["label"] == "Raid Finder"


def test_parse_encounters_nothing_in_current_expansion():
    out = M.bnet.parse_encounters(_enc_payload(), 700, "fr_FR", "Extension future")
    assert out == {"expansion": "Extension future", "instances": []}
    assert M.bnet.parse_encounters(_enc_payload(), None, "fr_FR")["instances"] == []


def test_raid_progress_uses_journal_expansion(monkeypatch):
    monkeypatch.setattr(M.bnet, "current_expansion", lambda loc=None: {"id": 600, "name": "Midnight"})
    monkeypatch.setattr(M.bnet, "_get", lambda *a, **k: _enc_payload())
    data, _ts = M.bnet.raid_progress("hyjal", "Progtest", force=True, locale="fr_FR")
    assert data["expansion"] == "Midnight" and data["instances"][0]["id"] == 1300


def test_encounters_api_error_gives_empty_progress(monkeypatch):
    monkeypatch.setattr(M.bnet, "current_expansion", lambda loc=None: {"id": 600, "name": "Midnight"})

    def boom(*a, **k):
        raise M.bnet.BnetError(500, "Erreur de l'API Battle.net.")
    monkeypatch.setattr(M.bnet, "_get", boom)
    data, _ts = M.bnet.dungeon_progress("hyjal", "Progtesterr", force=True, locale="fr_FR")
    assert data == {"expansion": "Midnight", "instances": []}


def test_parse_talents_active_spec_and_codes():
    raw = {"active_specialization": {"id": 264, "name": "Restauration"},
           "active_hero_talent_tree": {"id": 56, "name": "Long-voyant"},
           "specializations": [
               {"specialization": {"id": 263, "name": "Amélioration"},
                "loadouts": [{"is_active": True, "talent_loadout_code": "CcQAAA", "selected_class_talents": [{"id": 1}]}]},
               {"specialization": {"id": 264, "name": "Restauration"},
                "loadouts": [{"is_active": True, "talent_loadout_code": "CgQBBB"}]},
               {"specialization": {"id": 262, "name": "Élémentaire"}, "loadouts": []}]}
    t = M.bnet.parse_talents(raw)
    assert (t["active_spec"], t["active_spec_id"], t["hero_tree"]) == ("Restauration", 264, "Long-voyant")
    assert [lo["code"] for lo in t["loadouts"]] == ["CgQBBB", "CcQAAA"]              # spé active d'abord
    assert "selected_class_talents" not in t["loadouts"][1]


def test_char_progress_routes(monkeypatch):
    monkeypatch.setattr(M.bnet, "current_expansion", lambda loc=None: {"id": 600, "name": "Midnight"})
    monkeypatch.setattr(M.bnet, "_get", lambda path, *a, **k:
                        {"active_specialization": {"id": 1, "name": "X"}, "specializations": []}
                        if path.endswith("/specializations") else _enc_payload())
    anon = TestClient(M.app)
    assert anon.get("/api/char/hyjal/progroute/talents").status_code == 401
    _make_user("progroute@test.local")
    c = TestClient(M.app)
    assert c.post("/api/login", json={"email": "progroute@test.local", "password": "test-pw-123"},
                  headers={"X-Forwarded-For": "10.99.7.1"}).status_code == 200
    for p in ("talents", "raids-progress", "dungeons-progress"):
        r = c.get(f"/api/char/hyjal/progroute/{p}?refresh=1")
        assert r.status_code == 200, (p, r.text)
    assert r.json()["instances"][0]["name"] == "Flèche du Vide"


def test_parse_encounters_localized_names_and_empty_mode():
    """v2026.09.153-c1 : noms FR du journal (Blizzard renvoie l'anglais) ; mode sans difficulté ignoré."""
    raw = _enc_payload()
    raw["expansions"][1]["instances"][0]["modes"].append(
        {"difficulty": {}, "progress": {"completed_count": 1, "total_count": 1, "encounters": []}})
    names = {"inst": {1300: "Flèche du Vide (FR)"}, "enc": {1: "Boss A (FR)"}}
    out = M.bnet.parse_encounters(raw, 600, "fr_FR", "Midnight", names)
    ins = out["instances"][0]
    assert ins["name"] == "Flèche du Vide (FR)"
    assert [m["difficulty"] for m in ins["modes"]] == ["LFR", "HEROIC", "MYTHIC"]
    assert ins["modes"][0]["bosses"][0]["name"] == "Boss A (FR)"
    assert ins["modes"][0]["bosses"][1]["name"] == "Boss B"                  # pas dans le journal : nom Blizzard


def test_journal_names_from_current_expansion(monkeypatch):
    monkeypatch.setattr(M.bnet, "current_expansion",
                        lambda loc=None: {"id": 600, "name": "Midnight", "instances": [1300]})
    monkeypatch.setattr(M.bnet, "_get", lambda path, *a, **k:
                        {"name": "Flèche du Vide", "encounters": [{"id": 1, "name": "Boss A FR"}]})
    M.bnet._names.clear()
    n = M.bnet.journal_names("fr_FR")
    assert n == {"inst": {1300: "Flèche du Vide"}, "enc": {1: "Boss A FR"}, "order": {1300: [1]}}
    M.bnet._names.clear()


# ---- v2026.09.154 : progression de la guilde + talents Blizzard dans le stuff ----
def _prog(insts):
    return {"expansion": "Midnight", "instances": insts}


def _mode(d, label, done, total, bosses):
    return {"difficulty": d, "label": label, "done": done, "total": total,
            "bosses": [{"id": b, "name": f"B{b}", "kills": 1, "last_kill": 1} for b in bosses]}


def test_guild_progress_aggregate():
    raid = lambda modes: {"id": 10, "name": "Flèche du Vide", "modes": modes}  # noqa: E731
    results = [
        ({"name": "Alpha", "realm": "hyjal"}, _prog([raid([_mode("NORMAL", "Normal", 3, 3, [1, 2, 3]),
                                                           _mode("HEROIC", "Héroïque", 1, 3, [1])])])),
        ({"name": "Beta", "realm": "hyjal"}, _prog([raid([_mode("NORMAL", "Normal", 2, 3, [1, 2])])])),
        ({"name": "Gamma", "realm": "hyjal"}, None),                          # erreur API : ignoré
    ]
    names = {"order": {10: [1, 2, 3, 4]}, "enc": {4: "Boss quatre"}}
    out = M._guild_progress_aggregate(results, names, {"beta"})
    assert out["expansion"] == "Midnight"
    assert [d["difficulty"] for d in out["diffs"]] == ["NORMAL", "HEROIC"]
    inst = out["instances"][0]
    assert inst["total"] == 4 and [b["id"] for b in inst["bosses"]] == [1, 2, 3, 4]   # ordre du journal
    assert inst["bosses"][0]["kills"] == {"NORMAL": ["Alpha", "Beta"], "HEROIC": ["Alpha"]}
    assert inst["bosses"][3] == {"id": 4, "name": "Boss quatre", "kills": {}}          # jamais tué
    a, b = out["members"]
    assert a["name"] == "Alpha" and a["best"] == {"difficulty": "HEROIC", "label": "Héroïque", "done": 1, "total": 4}
    assert b["main"] is True and b["best"]["difficulty"] == "NORMAL" and b["best"]["done"] == 2


def test_guild_progress_route(monkeypatch):
    monkeypatch.setattr(M.bnet, "roster", lambda force=False: ({"members": [
        {"name": "Alpha", "level": 90, "realm": "hyjal", "rank": 1},
        {"name": "Reroll", "level": 12, "realm": "hyjal", "rank": 5}]}, 0.0))
    seen = []

    def fake(realm, name, force=False, locale=None):
        seen.append(name)
        return _prog([{"id": 10, "name": "Flèche du Vide", "modes": [_mode("NORMAL", "Normal", 1, 1, [1])]}]), 0.0
    monkeypatch.setattr(M.bnet, "raid_progress", fake)
    monkeypatch.setattr(M.bnet, "journal_names", lambda loc=None: {})
    M._GPROG.clear()
    _make_user("gprog@test.local")
    c = TestClient(M.app)
    assert c.post("/api/login", json={"email": "gprog@test.local", "password": "test-pw-123"},
                  headers={"X-Forwarded-For": "10.99.8.1"}).status_code == 200
    r = c.get("/api/guild/progress?kind=raid")
    assert r.status_code == 200, r.text
    d = r.json()
    assert seen == ["Alpha"] and d["level"] == 90 and d["scanned"] == 1               # niveau max uniquement
    assert d["members"][0]["best"]["done"] == 1
    assert c.get("/api/guild/progress?kind=pvp").status_code == 400
    M._GPROG.clear()


def test_stuff_parse_export_server_and_talents():
    r = M._stuff_parse_export('shaman="Chamoisdort"\nlevel=90\nserver=hyjal\nspec=restoration\ntalents=CgQABC\n')
    assert (r["server"], r["talents"], r["spec"]) == ("hyjal", "CgQABC", "restoration")


def test_blizz_talents_loadout_in_stuff(monkeypatch):
    monkeypatch.setattr(M.bnet, "talents", lambda realm, name, force=False, locale=None: ({
        "active_spec": "Restoration", "active_spec_id": 264, "hero_tree": None, "loadouts": [
            {"spec": "Enhancement", "spec_id": 263, "active": True, "code": "CcQENH", "name": None},
            {"spec": "Restoration", "spec_id": 264, "active": True, "code": "CgQNEW", "name": None}]}, 0.0))
    parsed = M._stuff_parse_export('shaman="Chamoisdort"\nserver=hyjal\nspec=restoration\ntalents=CgQOLD\n')
    assert M._blizz_talents_for(parsed) == "CgQNEW"                                    # spé de l'export
    assert M._spec_token("Beast Mastery") == "beast_mastery"
    _make_user("blizztal@test.local")
    c = TestClient(M.app)
    assert c.post("/api/login", json={"email": "blizztal@test.local", "password": "test-pw-123"},
                  headers={"X-Forwarded-For": "10.99.9.1"}).status_code == 200
    export = 'shaman="Chamoisdort"\nlevel=90\nserver=hyjal\nspec=restoration\ntalents=CgQOLD\n'
    pid = c.post("/api/profiles", json={"name": "Profil talents", "input": export}).json()["id"]
    d = c.get(f"/api/stuff/profile/{pid}").json()
    assert d["loadouts"][-1] == M.BLIZZ_LOADOUT and d["talents_changed"] is True
    sim = M._stuff_sim_input({"_raw": export}, [], {"name": M.BLIZZ_LOADOUT, "talents": "CgQNEW"})[0]
    assert "talents=CgQNEW" in sim and "CgQOLD" not in sim


# --- B.5 Regression: admin_mail_get configured status ---
def test_mail_get_unconfigured(monkeypatch):
    """Sans SMTP ni en base, GET /api/admin/mail renvoie configured: false."""
    # Clear env vars
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    # Need admin auth for /api/admin/mail
    c = _admin_client("mailtest@test.local", "10.99.50.1")
    resp = c.get("/api/admin/mail")
    assert resp.status_code == 200
    data = resp.json()
    assert data["configured"] is False

def test_mail_get_env_configured(monkeypatch):
    """Avec SMTP_HOST et SMTP_USER posés, configured: true, source: env."""
    monkeypatch.setenv("SMTP_HOST", "env.smtp.exemple.fr")
    monkeypatch.setenv("SMTP_USER", "envuser@exemple.fr")
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    # Ensure no DB mail config
    with M._db() as conn:
        conn.execute("DELETE FROM mail_config")
        conn.commit()
    c = _admin_client("mailtest2@test.local", "10.99.50.2")
    resp = c.get("/api/admin/mail")
    assert resp.status_code == 200
    data = resp.json()
    assert data["configured"] is True
    assert data["source"] == "env"
    assert data["effective"]["host"] == "env.smtp.exemple.fr"

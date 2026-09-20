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
    assert "Farine simple" in pains[0]["mats"], pains[0]   # dernière occurrence conservée

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
    st = r.json()
    assert st["current"] == M.VERSION
    assert st["available"] is False and st["latest"] is None
    assert st["settings"]["upd_check_h"] == 24          # défaut : vérification quotidienne
    assert st["settings"]["upd_apply_auto"] is False
    assert st["applier"]["installed"] is False

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
    assert st["result"] == "ok" and st["applied"] == "2099.01.001"

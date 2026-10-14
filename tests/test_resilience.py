"""Robustesse : migration d'une ancienne base, pannes des API externes (Blizzard, Warcraft Logs)."""
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import tempfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# base jetable AVANT l'import de l'application (si ce fichier est lancé seul)
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cohors-test-res-"))
os.environ["COOKIE_SECURE"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

import app.main as M  # noqa: E402
from app import bnet, wcl  # noqa: E402


def setup_module():
    M._init_db()


def _admin(email: str, ip: str) -> TestClient:
    from app.security import hash_password
    with M._db_lock, M._db() as conn:
        conn.execute("INSERT OR REPLACE INTO users (email, name, pwd, is_admin, role, lang, active, created)"
                     " VALUES (?,?,?,1,'admin','',1,0)", (email, email.split("@")[0], hash_password("test-pw-123")))
    c = TestClient(M.app)
    assert c.post("/api/login", json={"email": email, "password": "test-pw-123"},
                  headers={"X-Forwarded-For": ip}).status_code == 200
    return c

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

# Script lancé dans un processus à part (la base est choisie à l'import, par DATA_DIR) : migre la base
# copiée, relance la migration, puis parcourt l'application avec les comptes de l'ancienne base.
_MIGRATE = r'''
import json, sys, time
import app.main as M
from fastapi.testclient import TestClient
M._init_db()
M._init_db()                                   # idempotent : une 2e passe ne doit rien casser
out = {}
with M._db_lock, M._db() as conn:
    out["tables"] = sorted(r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    out["cols"] = {t: sorted(c["name"] for c in conn.execute(f"PRAGMA table_info({t})")) for t in out["tables"]}
    out["counts"] = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in out["tables"]}
st = {}
m = TestClient(M.app)
st["login_member"] = m.post("/api/login", json={"email": "mig-member@test.local", "password": "test-pw-123"},
                            headers={"X-Forwarded-For": "10.77.0.1"}).status_code
me = m.get("/api/me")
st["me"] = me.status_code
out["me"] = me.json() if me.status_code == 200 else {}
wl = m.get("/api/wishlist")
st["wishlist"] = wl.status_code
out["wish"] = [(i["item_id"], i.get("track"), i.get("prio")) for i in (wl.json().get("items") or [])] if wl.status_code == 200 else []
for path in ("/api/gcal", "/api/readiness", "/api/readiness/mine", "/api/me/onboarding", "/api/me/notifs",
             "/dashboard", "/calendar", "/wishlist", "/settings"):
    st[path] = m.get(path).status_code
a = TestClient(M.app)
st["login_admin"] = a.post("/api/login", json={"email": "mig-admin@test.local", "password": "test-pw-123"},
                           headers={"X-Forwarded-For": "10.77.0.2"}).status_code
for path in ("/api/admin/users", "/api/admin/usage", "/api/loot/export", "/api/setup/status", "/api/admin/backup"):
    st[path] = a.get(path).status_code
out["status"] = st
print("RESULT " + json.dumps(out, ensure_ascii=False))
'''


def _run(data_dir: Path) -> dict:
    env = dict(os.environ, DATA_DIR=str(data_dir), COOKIE_SECURE="0")
    p = subprocess.run([sys.executable, "-c", _MIGRATE], cwd=str(ROOT), env=env, capture_output=True,
                       text=True, timeout=180)
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line, f"migration en échec :\n{p.stdout[-3000:]}\n{p.stderr[-3000:]}"
    return json.loads(line[7:])


@pytest.mark.parametrize("fixture", sorted(p.name for p in FIXTURES.glob("db-*.sqlite")))
def test_old_database_migrates_to_current_schema(fixture, tmp_path):
    old, fresh = tmp_path / "old", tmp_path / "fresh"
    old.mkdir()
    fresh.mkdir()
    shutil.copyfile(FIXTURES / fixture, old / "wow.sqlite")
    import sqlite3
    src = sqlite3.connect(str(FIXTURES / fixture))
    before = {t: src.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for (t,) in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    src.close()
    got, ref = _run(old), _run(fresh)
    # même schéma qu'une installation neuve : toutes les tables et toutes les colonnes
    assert set(ref["tables"]) <= set(got["tables"]), set(ref["tables"]) - set(got["tables"])
    for t in ref["tables"]:
        assert set(ref["cols"][t]) <= set(got["cols"][t]), (t, set(ref["cols"][t]) - set(got["cols"][t]))
    # aucune ligne perdue
    lost = {t: (n, got["counts"].get(t)) for t, n in before.items() if (got["counts"].get(t) or 0) < n}
    assert not lost, lost
    st = got["status"]
    bad = {k: v for k, v in st.items() if v != 200}
    assert not bad, bad
    assert got["me"]["chars"] == 2 and got["me"]["role"] == "member"
    assert (4242, "myth", True) in [tuple(w) for w in got["wish"]]


# ------------------------------------------------------------------ pannes des API externes
def _get_routes():
    """Toutes les routes GET de l'application sans paramètre de chemin (pages comprises)."""
    out = []
    for r in M.app.routes:
        path = getattr(r, "path", "")
        if ("GET" not in (getattr(r, "methods", None) or set()) or "{" in path or path.startswith("/__voice")
                or path in ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")):
            continue
        out.append(path)
    return sorted(set(out))


CALLS = {"n": 0}


def _boom(kind):
    def urlopen(req, *a, **k):
        CALLS["n"] += 1
        url = getattr(req, "full_url", str(req))
        if kind == "down":
            raise urllib.error.URLError("réseau indisponible (test)")
        code = {"unavailable": 503, "revoked": 401, "ratelimit": 429}[kind]
        raise urllib.error.HTTPError(url, code, "test", {}, io.BytesIO(b"{}"))
    return urlopen


@pytest.mark.parametrize("kind", ["down", "unavailable", "revoked", "ratelimit"])
def test_no_server_error_when_blizzard_or_wcl_fail(kind, monkeypatch):
    """Blizzard / Warcraft Logs en panne, clé révoquée, quota dépassé : aucune page ni API ne plante (500)."""
    monkeypatch.setattr(urllib.request, "urlopen", _boom(kind))
    bnet.set_credentials("cle-test", "secret-test")
    wcl.set_credentials("cle-test", "secret-test")
    bnet.set_guild_info(region="eu", realm="hyjal", slug="guilde-test", locale="fr_FR")
    wcl.set_guild_info(region="eu", name="Guilde Test", realm="hyjal")
    bnet._token["value"], bnet._token["expires"] = None, 0.0
    for mod in (bnet, wcl):
        cache = getattr(mod, "_cache", None)
        if isinstance(cache, dict):
            cache.clear()
    c = _admin(f"res-{kind}@test.local", "10.99.70.%d" % (1 + ["down", "unavailable", "revoked", "ratelimit"].index(kind)))
    CALLS["n"] = 0
    try:
        errors = {}
        routes = _get_routes()
        assert len(routes) > 25, len(routes)
        # routes à paramètres qui interrogent Blizzard / WCL
        routes += ["/api/char/hyjal/inconnu/equipment", "/api/char/hyjal/inconnu", "/api/item/19019",
                   "/api/readiness/char/hyjal/inconnu", "/char/hyjal/inconnu", "/item/19019",
                   "/api/wcl/report/ABCDEF", "/api/attendance?refresh=1", "/api/search?q=test",
                   "/api/craft/search?q=flacon"]
        for path in routes:
            if path in ("/api/logout", "/logout"):
                continue
            try:
                r = c.get(path)
            except Exception as exc:  # noqa: BLE001 — exception non gérée = plantage
                errors[path] = ("exception", f"{type(exc).__name__}: {exc}")
                continue
            # 502/503 avec un message = panne signalée proprement ; 500 = plantage
            if r.status_code == 500 or (r.status_code >= 500 and "detail" not in r.text):
                errors[path] = (r.status_code, r.text[:200])
            # une clé révoquée (401 chez Blizzard) ne doit jamais faire croire au navigateur que le
            # membre est déconnecté (401 = renvoi vers la page de connexion)
            if r.status_code == 401:
                errors[path] = (401, r.text[:200])
        assert not errors, errors
        assert CALLS["n"] > 0           # les appels sortants ont bien été tentés (et ont échoué)
    finally:
        bnet.set_credentials(None, None)
        wcl.set_credentials(None, None)
        bnet.set_guild_info()
        wcl.set_guild_info()

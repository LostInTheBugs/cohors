"""Tests d'API (TestClient sur base SQLite temporaire). Aucun service externe requis."""
import os
import sys
import tempfile
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

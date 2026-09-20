"""Tests d'API (TestClient sur base SQLite temporaire). Aucun service externe requis."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# DATA_DIR temporaire AVANT l'import de l'application (base jetable).
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="cohors-test-")

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

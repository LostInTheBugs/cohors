"""Partie A — Hashage des tokens de session (SHA-256)."""
import hashlib
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="cohors-hash-test-")
os.environ["COOKIE_SECURE"] = "0"

import app.main as M  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.security import hash_password  # noqa: E402

M._init_db()  # Initialiser la base AVANT de créer le TestClient
client = TestClient(M.app)


def _session_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _make_user(email: str, is_admin: int = 0, password: str = "test-pw-123"):
    with M._db_lock, M._db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (email, name, pwd, is_admin, role, lang, active, created)"
            " VALUES (?,?,?,?,?,?,1,?)",
            (email, email.split("@")[0], hash_password(password), is_admin, "member", "", time.time()))


def _login(email: str, ip: str = "10.1.1.1") -> TestClient:
    c = TestClient(M.app)
    r = c.post("/api/login", json={"email": email, "password": "test-pw-123"},
               headers={"X-Forwarded-For": ip})
    assert r.status_code == 200, r.text
    return c


# ---------------------------------------------------------------------------
# A.1 — Après login, la valeur en base ≠ la valeur du cookie, et = sha256(cookie)
# ---------------------------------------------------------------------------
def test_session_hashed_in_db():
    """Après login, le hash en base = sha256(cookie), et la valeur brute ≠ hash."""
    _make_user("hashed@test.local", is_admin=1)
    c = _login("hashed@test.local")

    cookies = dict(c.cookies)
    assert "cohors_session" in cookies, f"Cookie absent, got: {cookies}"
    raw_token = cookies["cohors_session"]

    # En base, le token stocké est le hash
    with M._db_lock, M._db() as conn:
        user = conn.execute("SELECT id FROM users WHERE email=?", ("hashed@test.local",)).fetchone()
        row = conn.execute("SELECT token, hashed FROM sessions WHERE user_id=?", (user["id"],)).fetchone()
    assert row is not None
    assert row["token"] == _session_key(raw_token)
    assert row["token"] != raw_token  # Le stocké ≠ le brut
    assert row["hashed"] == 1


# ---------------------------------------------------------------------------
# A.2 — Migration idempotente : ligne insérée « à l'ancienne » → le cookie authentifie
# ---------------------------------------------------------------------------
def test_migration_plain_session():
    """Insère un token en clair (hashed=0), puis migration : le cookie correspond authentifie."""
    _make_user("migrate@test.local", is_admin=0)

    with M._db_lock, M._db() as conn:
        user = conn.execute("SELECT id FROM users WHERE email=?", ("migrate@test.local",)).fetchone()
        user_id = user["id"]
        raw_token = "plain-token-from-old-instance"
        conn.execute(
            "INSERT INTO sessions (token, user_id, created, last_seen, expires, hashed)"
            " VALUES (?,?,?,?,?,0)",
            (raw_token, user_id, time.time(), time.time(), time.time() + 86400))

    # La migration s'exécute au _init_db
    M._init_db()

    expected_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    with M._db_lock, M._db() as conn:
        row = conn.execute("SELECT token, hashed FROM sessions WHERE token=?", (expected_hash,)).fetchone()
    assert row is not None, "La migration n'a pas hashé le token en clair"
    assert row["token"] == expected_hash
    assert row["hashed"] == 1

    # Un client avec le cookie cohors_session=raw-token doit obtenir 200 sur une route protégée
    c = TestClient(M.app)
    c.cookies.set("cohors_session", raw_token, domain="")
    assert c.get("/api/me").status_code == 200

    # Un client sans cookie doit obtenir 401
    c2 = TestClient(M.app)
    assert c2.get("/api/me").status_code == 401


def test_migration_idempotent():
    """Un second _init_db ne change rien (plus de ligne avec hashed=0)."""
    # Migration déjà passée → hashed=1 partout
    M._init_db()

    with M._db_lock, M._db() as conn:
        rows = conn.execute("SELECT token FROM sessions WHERE hashed = 0").fetchall()
    assert len(rows) == 0, "Après migration idempotente, aucune ligne avec hashed=0"


# ---------------------------------------------------------------------------
# A.3 — Logout supprime bien la session
# ---------------------------------------------------------------------------
def test_logout_removes_session():
    _make_user("logout@test.local", is_admin=1)
    c = _login("logout@test.local")

    cookies = dict(c.cookies)
    assert "cohors_session" in cookies
    raw_token = cookies["cohors_session"]

    with M._db_lock, M._db() as conn:
        pre = conn.execute("SELECT COUNT(*) FROM sessions WHERE token=?", (_session_key(raw_token),)).fetchone()[0]
    assert pre == 1

    r = c.post("/api/logout")
    assert r.status_code == 200

    with M._db_lock, M._db() as conn:
        post = conn.execute("SELECT COUNT(*) FROM sessions WHERE token=?", (_session_key(raw_token),)).fetchone()[0]
    assert post == 0, "La session n'a pas été supprimée après logout"


# ---------------------------------------------------------------------------
# A.4 — "Déconnecter mes autres sessions" garde la courante et supprime les autres
# ---------------------------------------------------------------------------
def test_disconnect_other_sessions():
    _make_user("multi@test.local", is_admin=1)

    # Créer plusieurs sessions pour le même user
    with M._db_lock, M._db() as conn:
        user = conn.execute("SELECT id FROM users WHERE email='multi@test.local'").fetchone()
        user_id = user["id"]
        for i in range(3):
            tok = secrets.token_urlsafe(16)
            conn.execute(
                "INSERT INTO sessions (token, user_id, created, last_seen, expires)"
                " VALUES (?,?,?,?,?)",
                (_session_key(tok), user_id, time.time(), time.time(), time.time() + 86400))

    # Se connecter
    c = _login("multi@test.local")
    cookies = dict(c.cookies)
    current_cookie = cookies.get("cohors_session", "")
    assert current_cookie, "Cookie de session manquant après login"

    with M._db_lock, M._db() as conn:
        pre = conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (user_id,)).fetchone()[0]
    assert pre == 4, f"Attendu 4 sessions (3 créées + 1 login), got {pre}"

    # "Déconnecter mes autres sessions" via /api/me/password
    r = c.post("/api/me/password", json={"new": "new-pass-456", "current": "test-pw-123"})
    assert r.status_code == 200, r.text

    with M._db_lock, M._db() as conn:
        post = conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (user_id,)).fetchone()[0]
    assert post == 1, f"Attendu 1 session restante (la courante), got {post}"

    # Vérifions que la session courante est bien la seule restante
    with M._db_lock, M._db() as conn:
        row = conn.execute("SELECT token FROM sessions WHERE user_id=? AND token=?",
                           (user_id, _session_key(current_cookie))).fetchone()
    assert row is not None, "La session courante a été supprimée à tort"


# ---------------------------------------------------------------------------
# A.5 — Le helper _session_key est correct
# ---------------------------------------------------------------------------
def test_session_key_helper():
    tok = "test-token-abc123"
    expected = hashlib.sha256(tok.encode()).hexdigest()
    assert M._session_key(tok) == expected
    assert M._session_key(tok) == M._session_key(tok)
    assert M._session_key("aaa") != M._session_key("bbb")

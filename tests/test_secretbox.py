"""Tests 1-3 for B.1 — secretbox module."""
import os
import sys
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

from app import secretbox

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# DATA_DIR temporaire AVANT l'import de l'application
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="cohors-test-secretbox-")
os.environ["COOKIE_SECURE"] = "0"

import app.main as M  # noqa: E402
from app.security import hash_password  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import time  # noqa: E402

client = TestClient(M.app)
SRC_DIR = Path(__file__).resolve().parent.parent


def setup_module():
    M._init_db()


def _data_dir() -> Path:
    d = Path(tempfile.mkdtemp())
    os.environ["DATA_DIR"] = str(d)
    return d


def test_encrypt_decrypt_roundtrip(monkeypatch) -> None:
    """Test 1 — encrypt/decrypt round-trip."""
    data_dir = _data_dir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    secretbox.reset_box()
    plain = "my-super-secret-token"
    enc = secretbox.encrypt(plain)
    assert enc != plain
    assert enc.startswith(secretbox.PREFIX)
    assert secretbox.decrypt(enc) == plain
    # idempotent
    assert secretbox.encrypt(enc) == enc
    # decrypt non-prefixed returns as-is
    assert secretbox.decrypt("plain-no-prefix") == "plain-no-prefix"
    # empty strings
    assert secretbox.encrypt("") == ""
    assert secretbox.decrypt("") == ""
    # clean up
    import shutil
    shutil.rmtree(data_dir, ignore_errors=True)


def test_wrong_key(monkeypatch) -> None:
    """Test 2 — with a different key, decrypt returns '' and undecryptable is True."""
    data_dir = _data_dir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    # Force a key first
    secretbox.reset_box()
    plain = "my-super-secret-token"
    enc = secretbox.encrypt(plain)

    # Now force a different key
    different_key = Fernet.generate_key()
    secretbox.reset_box()
    secretbox._box = Fernet(different_key)

    # decrypt should return ""
    assert secretbox.decrypt(enc) == ""
    # undecryptable should be True
    assert secretbox.undecryptable(enc) is True
    # no exception raised
    import shutil
    shutil.rmtree(data_dir, ignore_errors=True)


def _make_admin(name: str) -> TestClient:
    """Create an admin user and login, return a TestClient with session cookie."""
    with M._db_lock, M._db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (email, name, pwd, is_admin, role, lang, active, created) "
            "VALUES (?,?,?,?,?,?,1,?)",
            (name, name.split("@")[0], hash_password("test-pw-123"), 1, "admin", "", time.time()))
    c = TestClient(M.app)
    r = c.post("/api/login", json={"email": name, "password": "test-pw-123"},
               headers={"X-Forwarded-For": "10.99.99.1"})
    assert r.status_code == 200, r.text
    return c


def test_secrets_encrypted_on_write(monkeypatch) -> None:
    """Test 3 — write each secret via admin route, verify encryption in DB and plain-text readback."""
    import sqlite3

    # Mock external API validation so we can test encryption without real credentials
    monkeypatch.setattr(M.bnet, "check", lambda cid, secret: {"ok": True, "detail": "mocked"})
    monkeypatch.setattr(M.bnet, "set_credentials", lambda cid, secret: None)
    monkeypatch.setattr(M.wcl, "check", lambda cid, secret: {"ok": True, "detail": "mocked"})
    monkeypatch.setattr(M.wcl, "set_credentials", lambda cid, secret: None)
    monkeypatch.setattr(M.discord_bot, "me", lambda token: {"username": "TestBot"})
    monkeypatch.setattr(M.discord_bot, "invite_url", lambda app_id: f"https://discord.gg/{app_id}")
    monkeypatch.setattr(M.mailer, "check", lambda host, port, mode, user, pw, sender, helo: {"ok": True, "detail": "mocked"})

    c = _make_admin("secret-test@test.local")

    # --- 1. API key (bnet) ---
    r = c.post("/api/admin/api-keys", json={
        "provider": "bnet",
        "client_id": "bnet-client-123",
        "client_secret": "bnet-secret-plain-text-xyz",
    })
    assert r.status_code == 200, r.text

    with M._db_lock, M._db() as conn:
        row = conn.execute("SELECT client_secret FROM api_keys WHERE provider=?", ("bnet",)).fetchone()
    assert row is not None, "bnet api_key not found"
    raw_secret = row["client_secret"]
    assert raw_secret.startswith(secretbox.PREFIX), f"api_keys.client_secret not encrypted: {raw_secret[:20]}"
    assert "bnet-secret-plain-text-xyz" not in raw_secret, "plaintext found in api_keys.client_secret"

    # Readback via accessor should return plaintext
    rows = M._api_keys_rows()
    assert rows["bnet"]["client_secret"] == "bnet-secret-plain-text-xyz"

    # --- 2. Bot config token ---
    r = c.post("/api/admin/bot", json={
        "enabled": True,
        "token": "discord-bot-token-plain-abc",
        "app_id": "123456789",
        "channel_id": "987654321",
    })
    assert r.status_code == 200, r.text

    with M._db_lock, M._db() as conn:
        row = conn.execute("SELECT token FROM bot_config WHERE id=1").fetchone()
    assert row is not None, "bot_config token not found"
    raw_token = row["token"]
    assert raw_token.startswith(secretbox.PREFIX), f"bot_config.token not encrypted: {raw_token[:20]}"
    assert "discord-bot-token-plain-abc" not in raw_token, "plaintext found in bot_config.token"

    # Readback via accessor should return plaintext
    cfg = M._bot_config()
    assert cfg["token"] == "discord-bot-token-plain-abc"

    # --- 3. Mail config password ---
    r = c.post("/api/admin/mail", json={
        "values": {
            "host": "smtp.example.com",
            "user": "user@example.com",
            "password": "smtp-pass-plain-123",
            "port": "587",
            "mode": "starttls",
            "sender": "noreply@example.com",
        }
    })
    assert r.status_code == 200, r.text

    with M._db_lock, M._db() as conn:
        row = conn.execute("SELECT value FROM mail_config WHERE key='password'").fetchone()
    assert row is not None, "mail_config password not found"
    raw_pw = row["value"]
    assert raw_pw.startswith(secretbox.PREFIX), f"mail_config.password not encrypted: {raw_pw[:20]}"
    assert "smtp-pass-plain-123" not in raw_pw, "plaintext found in mail_config.password"

    # Readback via accessor should return plaintext
    mrows = M._mail_rows()
    assert mrows["password"] == "smtp-pass-plain-123"

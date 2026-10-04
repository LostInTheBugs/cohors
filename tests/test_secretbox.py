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


def test_migration_from_plaintext(monkeypatch) -> None:
    """Test 4 — insert plaintext secrets, call _init_db(), verify encryption and idempotency."""
    import sqlite3
    import shutil

    # Set up fresh DATA_DIR
    data_dir = _data_dir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    secretbox.reset_box()
    M._init_db()

    # Insert plaintext secrets directly into DB
    with M._db_lock, M._db() as conn:
        conn.execute("DELETE FROM api_keys")
        conn.execute("DELETE FROM bot_config WHERE id != 1")
        conn.execute("INSERT OR REPLACE INTO bot_config (id, updated) VALUES (1, 0)")
        conn.execute("DELETE FROM mail_config")
        conn.execute("INSERT OR IGNORE INTO mail_config (key, value, updated) VALUES (?, '', 0)", ("password",))
        conn.execute(
            "INSERT INTO api_keys (provider, client_id, client_secret, updated) VALUES (?, ?, ?, ?)",
            ("bnet", "bnet-id", "plaintext-bnet-secret", time.time()))
        conn.execute(
            "UPDATE bot_config SET token=? WHERE id=1", ("plaintext-discord-token",))
        conn.execute(
            "UPDATE mail_config SET value=? WHERE key='password'", ("plaintext-smtp-pass",))

    # Verify plaintext is in DB before migration
    with M._db_lock, M._db() as conn:
        raw = conn.execute("SELECT client_secret FROM api_keys WHERE provider='bnet'").fetchone()
        assert raw["client_secret"] == "plaintext-bnet-secret", "api_keys should be plaintext before migration"
        raw = conn.execute("SELECT token FROM bot_config WHERE id=1").fetchone()
        assert raw["token"] == "plaintext-discord-token", "bot_config should be plaintext before migration"
        raw = conn.execute("SELECT value FROM mail_config WHERE key='password'").fetchone()
        assert raw["value"] == "plaintext-smtp-pass", "mail_config should be plaintext before migration"

    # Call _init_db() to trigger migration
    M._init_db()

    # Verify all three are now encrypted
    with M._db_lock, M._db() as conn:
        row = conn.execute("SELECT client_secret FROM api_keys WHERE provider='bnet'").fetchone()
        assert row["client_secret"].startswith(secretbox.PREFIX), f"api_keys.client_secret not encrypted after migration: {row['client_secret'][:20]}"
        assert "plaintext-bnet-secret" not in row["client_secret"], "plaintext still in api_keys after migration"

        row = conn.execute("SELECT token FROM bot_config WHERE id=1").fetchone()
        assert row["token"].startswith(secretbox.PREFIX), f"bot_config.token not encrypted after migration: {row['token'][:20]}"
        assert "plaintext-discord-token" not in row["token"], "plaintext still in bot_config after migration"

        row = conn.execute("SELECT value FROM mail_config WHERE key='password'").fetchone()
        assert row["value"].startswith(secretbox.PREFIX), f"mail_config.password not encrypted after migration: {row['value'][:20]}"
        assert "plaintext-smtp-pass" not in row["value"], "plaintext still in mail_config after migration"

    # Verify accessors return plaintext
    assert M._api_keys_rows()["bnet"]["client_secret"] == "plaintext-bnet-secret"
    assert M._bot_config()["token"] == "plaintext-discord-token"
    assert M._mail_rows()["password"] == "plaintext-smtp-pass"

    # Store raw values for idempotency check
    with M._db_lock, M._db() as conn:
        raw_before = {
            "api_secret": conn.execute("SELECT client_secret FROM api_keys WHERE provider='bnet'").fetchone()["client_secret"],
            "bot_token": conn.execute("SELECT token FROM bot_config WHERE id=1").fetchone()["token"],
            "mail_pass": conn.execute("SELECT value FROM mail_config WHERE key='password'").fetchone()["value"],
        }

    # Call _init_db() a second time — should not change raw values (idempotent)
    M._init_db()

    with M._db_lock, M._db() as conn:
        raw_after = {
            "api_secret": conn.execute("SELECT client_secret FROM api_keys WHERE provider='bnet'").fetchone()["client_secret"],
            "bot_token": conn.execute("SELECT token FROM bot_config WHERE id=1").fetchone()["token"],
            "mail_pass": conn.execute("SELECT value FROM mail_config WHERE key='password'").fetchone()["value"],
        }

    assert raw_before == raw_after, f"Raw values changed on second _init_db(): {raw_before} != {raw_after}"

    shutil.rmtree(data_dir, ignore_errors=True)


def test_undecryptable_and_no_leak_in_responses(monkeypatch) -> None:
    """Test 5 — GET endpoints never leak plaintext or enc:v1:; undecryptable=true for wrong-key ciphertext."""
    import sqlite3
    import shutil

    # Set up fresh DATA_DIR
    data_dir = _data_dir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    secretbox.reset_box()
    M._init_db()

    # Insert encrypted secrets (with current key)
    with M._db_lock, M._db() as conn:
        conn.execute("DELETE FROM api_keys")
        conn.execute("DELETE FROM bot_config WHERE id != 1")
        conn.execute("INSERT OR REPLACE INTO bot_config (id, updated) VALUES (1, 0)")
        conn.execute("DELETE FROM mail_config")
        conn.execute("INSERT OR IGNORE INTO mail_config (key, value, updated) VALUES (?, '', 0)", ("password",))

        enc_api = secretbox.encrypt("bnet-secret-plain")
        conn.execute(
            "INSERT INTO api_keys (provider, client_id, client_secret, updated) VALUES (?, ?, ?, ?)",
            ("bnet", "bnet-id", enc_api, time.time()))
        enc_bot = secretbox.encrypt("discord-bot-token")
        conn.execute("UPDATE bot_config SET token=? WHERE id=1", (enc_bot,))
        enc_mail = secretbox.encrypt("smtp-pass")
        conn.execute("UPDATE mail_config SET value=? WHERE key='password'", (enc_mail,))

    # Mock external calls
    monkeypatch.setattr(M.bnet, "check", lambda cid, secret: {"ok": True, "detail": "mocked"})
    monkeypatch.setattr(M.bnet, "set_credentials", lambda cid, secret: None)
    monkeypatch.setattr(M.wcl, "check", lambda cid, secret: {"ok": True, "detail": "mocked"})
    monkeypatch.setattr(M.wcl, "set_credentials", lambda cid, secret: None)
    monkeypatch.setattr(M.discord_bot, "me", lambda token: {"username": "TestBot"})
    monkeypatch.setattr(M.discord_bot, "invite_url", lambda app_id: f"https://discord.gg/{app_id}")
    monkeypatch.setattr(M.mailer, "check", lambda host, port, mode, user, pw, sender, helo: {"ok": True, "detail": "mocked"})

    c = _make_admin("undecrypt-test@test.local")

    # --- GET /api/admin/api-keys ---
    r = c.get("/api/admin/api-keys")
    assert r.status_code == 200, r.text
    data = r.json()
    bnet_data = next(p for p in data["providers"] if p["provider"] == "bnet")
    # Should NOT contain plaintext
    assert "bnet-secret-plain" not in str(data), "plaintext leaked in api_keys response"
    # Should NOT contain enc:v1: prefix
    assert "enc:v1:" not in str(data), "enc:v1: prefix leaked in api_keys response"
    # undecryptable should be False for correctly encrypted value
    assert bnet_data.get("undecryptable") is False, "undecryptable should be False for correct key"

    # --- GET /api/admin/bot ---
    r = c.get("/api/admin/bot")
    assert r.status_code == 200, r.text
    data = r.json()
    assert "discord-bot-token" not in str(data), "plaintext leaked in bot response"
    assert "enc:v1:" not in str(data), "enc:v1: prefix leaked in bot response"
    assert data.get("undecryptable") is False, "undecryptable should be False for correct key"

    # --- GET /api/admin/mail ---
    r = c.get("/api/admin/mail")
    assert r.status_code == 200, r.text
    data = r.json()
    assert "smtp-pass" not in str(data), "plaintext leaked in mail response"
    assert "enc:v1:" not in str(data), "enc:v1: prefix leaked in mail response"
    assert data.get("undecryptable") is False, "undecryptable should be False for correct key"

    # --- Now: switch key to make existing ciphertext undecryptable ---
    from cryptography.fernet import Fernet
    wrong_key = Fernet.generate_key()
    secretbox.reset_box()
    secretbox._box = Fernet(wrong_key)

    # Re-login (cookie is still valid)
    c2 = _make_admin("undecrypt-test2@test.local")

    # GET /api/admin/api-keys — should still 200, but undecryptable=True
    r = c2.get("/api/admin/api-keys")
    assert r.status_code == 200, r.text
    data = r.json()
    bnet_data = next(p for p in data["providers"] if p["provider"] == "bnet")
    assert bnet_data.get("undecryptable") is True, "undecryptable should be True for wrong key"
    assert "bnet-secret-plain" not in str(data), "plaintext leaked in api_keys response with wrong key"
    assert "enc:v1:" not in str(data), "enc:v1: prefix leaked in api_keys response with wrong key"

    # GET /api/admin/bot — should still 200, but undecryptable=True
    r = c2.get("/api/admin/bot")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("undecryptable") is True, "undecryptable should be True for wrong key"
    assert "discord-bot-token" not in str(data), "plaintext leaked in bot response with wrong key"
    assert "enc:v1:" not in str(data), "enc:v1: prefix leaked in bot response with wrong key"

    # GET /api/admin/mail — should still 200, but undecryptable=True
    r = c2.get("/api/admin/mail")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("undecryptable") is True, "undecryptable should be True for wrong key"
    assert "smtp-pass" not in str(data), "plaintext leaked in mail response with wrong key"
    assert "enc:v1:" not in str(data), "enc:v1: prefix leaked in mail response with wrong key"

    shutil.rmtree(data_dir, ignore_errors=True)

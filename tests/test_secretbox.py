"""Tests 1-2 for B.1 — secretbox module."""
import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

from app import secretbox


def _data_dir() -> Path:
    d = Path(tempfile.mkdtemp())
    os.environ["DATA_DIR"] = str(d)
    return d


def test_encrypt_decrypt_roundtrip():
    """Test 1 — encrypt/decrypt round-trip."""
    data_dir = _data_dir()
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


def test_wrong_key():
    """Test 2 — with a different key, decrypt returns '' and undecryptable is True."""
    data_dir = _data_dir()
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

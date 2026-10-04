"""Cohors — encryption of secrets at rest (Fernet)."""
from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

PREFIX = "enc:v1:"

_box: Fernet | None = None
_cache_key: bytes | None = None


def _load_key(data_dir: Path) -> bytes:
    env = os.environ.get("COHORS_SECRET_KEY", "").strip()
    if env:
        return env.encode()
    path = data_dir / "secret.key"
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        key = Fernet.generate_key()
        os.write(fd, key)
        os.close(fd)
        return key
    except FileExistsError:
        return path.read_bytes()


def _get_box(data_dir: Path) -> Fernet:
    global _box, _cache_key
    if _box is not None:
        return _box
    if _cache_key is None:
        _cache_key = _load_key(data_dir)
    _box = Fernet(_cache_key)
    return _box


def reset_box() -> None:
    """Reset the cached box — for testing only."""
    global _box, _cache_key
    _box = None
    _cache_key = None


def encrypt(s: str) -> str:
    """Return *s* encrypted with the PREFIX, or *s* unchanged."""
    if not s:
        return ""
    if s.startswith(PREFIX):
        return s
    box = _get_box(Path(os.environ.get("DATA_DIR", "./data")))
    return PREFIX + box.encrypt(s.encode()).decode()


def decrypt(s: str) -> str:
    """Return *s* decrypted, or *s* unchanged if not prefixed."""
    if not s:
        return ""
    if not s.startswith(PREFIX):
        return s
    box = _get_box(Path(os.environ.get("DATA_DIR", "./data")))
    try:
        return box.decrypt(s[len(PREFIX):].encode()).decode()
    except InvalidToken:
        logger.warning("Failed to decrypt a secret (wrong key or corrupted data)")
        return ""


def undecryptable(s: str) -> bool:
    """Return True if *s* is prefixed but cannot be decrypted."""
    if not s or not s.startswith(PREFIX):
        return False
    box = _get_box(Path(os.environ.get("DATA_DIR", "./data")))
    try:
        box.decrypt(s[len(PREFIX):].encode())
        return False
    except InvalidToken:
        return True

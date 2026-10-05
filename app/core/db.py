"""Cohors — SQLite connection helper and its global lock."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

from app.core.config import DB_PATH

_db_lock = threading.Lock()


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

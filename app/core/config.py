"""Cohors — configuration (environment variables, paths, version)."""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
BRAND_DIR = DATA_DIR / "branding"
DB_PATH = DATA_DIR / "wow.sqlite"
REPORTS_DIR = DATA_DIR / "reports"

SIMC_IMAGE = os.environ.get("SIMC_IMAGE", "simulationcraftorg/simc:1210-2026-10-04-2d54d82")
SIM_TIMEOUT = int(os.environ.get("SIM_TIMEOUT", "900"))
QUEUE_MAX = int(os.environ.get("QUEUE_MAX", "20"))
PER_IP_ACTIVE = int(os.environ.get("PER_IP_ACTIVE", "3"))
PER_IP_COOLDOWN_S = int(os.environ.get("PER_IP_COOLDOWN_S", "15"))
PER_USER_ACTIVE = int(os.environ.get("PER_USER_ACTIVE", "3"))
ITER_CHOICES = (1000, 5000, 10000, 25000, 50000)
DEFAULT_ITERATIONS = 10000
MAX_INPUT_CHARS = 200_000

SESSION_COOKIE = "cohors_session"
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "30"))
INVITE_TTL_DAYS = int(os.environ.get("INVITE_TTL_DAYS", "7"))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") not in ("0", "false", "no", "")
COOKIE_DOMAIN = os.environ.get("COOKIE_DOMAIN", "").strip() or None
BOT_POLL_S = int(os.environ.get("BOT_POLL_S", "300"))  # intervalle du bot Discord (secondes)
# Relevés quotidiens (évolution des personnages liés) — v2026.09.054.
# TTL max 30 jours : Blizzard Developer API ToU §18 (« retain data ... no longer than 30 days »).
SNAP_POLL_S = float(os.environ.get("SNAPSHOT_POLL_S", "900"))       # tick de la boucle (s)
SNAP_REFRESH_MIN = float(os.environ.get("SNAPSHOT_REFRESH_MIN", "360"))  # re-relevé si dernier > 6 h
SNAP_KEEP_DAYS = min(30, max(2, int(os.environ.get("SNAPSHOT_KEEP_DAYS", "30"))))
SNAP_REFRESH_MIN_OTHER = float(os.environ.get("SNAPSHOT_REFRESH_MIN_OTHER", "1200"))  # roster : 20 h
SNAP_MAX_PER_TICK = int(os.environ.get("SNAPSHOT_MAX_PER_TICK", "60"))  # borne le temps du passage
PROF_REFRESH_DAYS = float(os.environ.get("PROFESSIONS_REFRESH_DAYS", "7"))  # métiers : 7 j

DATA_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

_version_file = Path(__file__).resolve().parents[2] / "VERSION"
VERSION = _version_file.read_text().strip() if _version_file.exists() else os.environ.get("APP_VERSION", "dev")

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"

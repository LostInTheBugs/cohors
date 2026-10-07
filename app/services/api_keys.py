"""Cohors — Battle.net / Warcraft Logs API keys: providers, stored keys (secret decrypted), effective keys, masking."""
from __future__ import annotations

from app import bnet, secretbox, wcl
from app.core.db import _db, _db_lock

API_PROVIDERS = {
    "bnet": {"label": "Battle.net (Blizzard)",
             "help_url": "https://develop.battle.net/access/clients",
             "help_fr": "Portail développeurs Blizzard → Clients API → « Create Client » (type Client Credentials).",
             "module": "bnet"},
    "wcl": {"label": "Warcraft Logs",
            "help_url": "https://www.warcraftlogs.com/api/clients",
            "help_fr": "Warcraft Logs → ton profil → API Clients → « Create Client » (Client Credentials).",
            "module": "wcl"},
}


def _api_keys_rows() -> dict:
    with _db_lock, _db() as conn:
        rows = {r["provider"]: dict(r) for r in conn.execute("SELECT * FROM api_keys").fetchall()}
    for prov, row in rows.items():
        row["client_secret"] = secretbox.decrypt(row.get("client_secret") or "")
    return rows


def _apply_api_keys() -> None:
    """Recopie les clés stockées vers les clients (prioritaires sur l'environnement)."""
    rows = _api_keys_rows()
    for prov, mod in (("bnet", bnet), ("wcl", wcl)):
        row = rows.get(prov)
        mod.set_credentials(row["client_id"] if row else None,
                            row["client_secret"] if row else None)


def _api_effective(prov: str) -> tuple[str, str, str]:
    """(client_id, secret, source) effectifs pour un fournisseur — source : admin / env / aucune."""
    rows = _api_keys_rows()
    row = rows.get(prov)
    if row and (row["client_id"] or row["client_secret"]):
        return (row["client_id"], row["client_secret"], "admin")
    cid, secret = (bnet.credentials() if prov == "bnet" else wcl.credentials())
    if cid or secret:
        return (cid, secret, "env")
    return ("", "", "")


def _mask(value: str, keep: int = 4) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    return ("•" * 6 + v[-keep:]) if len(v) > keep else "•" * 6

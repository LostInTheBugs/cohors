"""Cohors — guild identity settings (region, realm, Warcraft Logs) stored by the admin and applied to the API clients."""
from __future__ import annotations

from app import bnet, wcl
from app.core.db import _db, _db_lock


def _guild_rows() -> dict:
    with _db_lock, _db() as conn:
        return {r["key"]: r["value"] for r in conn.execute("SELECT * FROM guild_config").fetchall()}


def _guild_effective() -> dict:
    """Valeurs en vigueur : administration appliquée, sinon fichier serveur."""
    return {"region": bnet.REGION, "realm": bnet.GUILD_REALM, "slug": bnet.GUILD_SLUG,
            "locale": bnet.LOCALE, "wcl_region": wcl.REGION, "wcl_name": wcl.GUILD_NAME}


def _apply_guild_config() -> None:
    """Recopie l'identité de guilde vers les clients (prioritaire sur l'environnement)."""
    rows = _guild_rows()
    bnet.set_guild_info(region=rows.get("region"), realm=rows.get("realm"),
                        slug=rows.get("slug"), locale=rows.get("locale"))
    wcl.set_guild_info(region=rows.get("wcl_region"), name=rows.get("wcl_name"),
                       realm=rows.get("realm"))

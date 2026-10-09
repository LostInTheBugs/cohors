"""Cohors — first-run setup checklist shown on /start (admins only)."""
from __future__ import annotations

import os

from fastapi import APIRouter, Request

from app.core.auth import _require_admin
from app.core.brand import _brand_files, _brand_row
from app.core.db import _db, _db_lock
from app.services.api_keys import _api_effective
from app.services.bot import _bot_config
from app.services.guild_settings import _guild_effective
from app.services.mail_settings import _mail_rows

router = APIRouter()


@router.get("/api/setup/status")
def api_setup_status(request: Request):
    """État des étapes de mise en route de la guilde (réservé aux administrateurs)."""
    _require_admin(request)
    with _db_lock, _db() as conn:
        n_admins = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE active=1 AND (is_admin=1 OR role='admin')"
        ).fetchone()["c"]
        n_users = conn.execute("SELECT COUNT(*) AS c FROM users WHERE active=1").fetchone()["c"]
        n_invites = conn.execute("SELECT COUNT(*) AS c FROM invites WHERE used IS NULL").fetchone()["c"]
        brand = dict(_brand_row(conn))
    cfg = _bot_config()
    bot = cfg
    g = _guild_effective()
    bnet_id, bnet_secret, _s1 = _api_effective("bnet")
    wcl_id, wcl_secret, _s2 = _api_effective("wcl")
    mail_host = (_mail_rows().get("host") or os.environ.get("SMTP_HOST", "")).strip()
    guild_txt = " · ".join(x for x in (str(g.get("realm") or ""), str(g.get("slug") or "")) if x)
    brand_name = (brand.get("guild_name") or "").strip()
    members_txt = f"{n_users} membre" + ("s" if n_users > 1 else "")
    if n_invites:
        members_txt += f" · {n_invites} invitation" + ("s" if n_invites > 1 else "") + " en attente"
    steps = [
        {"key": "admin", "label": "Compte administrateur", "done": n_admins > 0, "optional": False,
         "hint": "Créé au premier démarrage avec ADMIN_EMAIL / ADMIN_PASSWORD.", "detail": "",
         "href": "/settings#comptes"},
        {"key": "guild", "label": "Guilde du serveur", "done": bool(g.get("realm") and g.get("slug")),
         "optional": False,
         "hint": "Royaume, région, slug Battle.net et nom Warcraft Logs — bouton 🔎 Vérifier.",
         "detail": guild_txt, "href": "/settings#guilde"},
        {"key": "bnet", "label": "Clés API Battle.net", "done": bool(bnet_id and bnet_secret),
         "optional": False,
         "hint": "Portail développeurs Blizzard → Clients API (roster et fiches de personnages).",
         "detail": "", "href": "/settings#api"},
        {"key": "wcl", "label": "Clés API Warcraft Logs", "done": bool(wcl_id and wcl_secret),
         "optional": False,
         "hint": "Warcraft Logs → API Clients (page Rapports).", "detail": "", "href": "/settings#api"},
        {"key": "identity", "label": "Identité du site", "done": bool(brand_name or "logo" in _brand_files()),
         "optional": False,
         "hint": "Nom de guilde, nom court, logo et fond.", "detail": brand_name,
         "href": "/settings#identite"},
        {"key": "smtp", "label": "✉️ E-mail (SMTP)", "done": bool(mail_host), "optional": True,
         "hint": "Optionnel — pour envoyer les invitations par e-mail.", "detail": mail_host,
         "href": "/settings#mail"},
        {"key": "discord", "label": "Bot Discord",
         "done": bool(bot and (bot["token"] or "").strip()), "optional": True,
         "hint": "Optionnel — jeton du bot pour les annonces de rapports et de mouvements.", "detail": "",
         "href": "/settings#bot"},
        {"key": "members", "label": "Premiers membres", "done": n_users > 1 or n_invites > 0,
         "optional": False,
         "hint": "Crée une invitation, envoie le lien, ils s'inscrivent eux-mêmes.", "detail": members_txt,
         "href": "/settings#invitations"},
    ]
    required = [s for s in steps if not s["optional"]]
    return {"steps": steps, "done": sum(1 for s in steps if s["done"]), "total": len(steps),
            "required_done": sum(1 for s in required if s["done"]), "required_total": len(required),
            "optional_done": sum(1 for s in steps if s["optional"] and s["done"]),
            "optional_total": sum(1 for s in steps if s["optional"])}

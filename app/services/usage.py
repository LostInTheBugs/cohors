"""Cohors — page usage for administrators: which pages members actually open.

One row per (day, page, account) — the number of members who opened a page on a given day, never a
click trail: no time of day, no sequence, no external service. Rows older than USAGE_KEEP_DAYS are
dropped. Only HTML pages are counted (not the API), under their first path segment
(/char/hyjal/flavia → /char).
"""
from __future__ import annotations

import json
import time

from app.core.auth import _session_key
from app.core.db import _db, _db_lock
from app.core.util import _snap_day

USAGE_KEEP_DAYS = 90
PAGE_LABELS = {
    "/dashboard": "🏠 Tableau de bord", "/": "⚔️ Simulateur", "/gear": "🧰 Top Stuff",
    "/stuff": "🎒 Stuff conseillé", "/compare": "⚖️ Comparateur", "/characters": "👥 Personnages",
    "/mains": "⭐ Mains & alts", "/rankings": "🏆 Classements", "/fun": "🎉 Succès fun",
    "/craft": "🔨 Artisanat", "/guild": "🛡️ Infos & liens", "/raids": "📊 Rapports",
    "/calendar": "🗓️ Calendrier", "/prep": "🧪 Préparation de raid", "/readiness": "🩺 Bilan des joueurs",
    "/mplus": "⚔️ MM+", "/voice": "🎧 Panneau vocal", "/music": "🎵 Musique", "/help": "📖 Aide",
    "/settings": "⚙️ Paramètres", "/mespersos": "🪪 Mes personnages", "/mesrecettes": "📖 Mes recettes",
    "/messtats": "📊 Mes statistiques", "/alertes": "🔔 Alertes MM+", "/mesindispos": "🚫 Mes indispos",
    "/wishlist": "🎯 Ma wishlist", "/commandes": "🧾 Mes commandes", "/char": "🧙 Fiche personnage",
    "/item": "🎁 Fiche objet", "/start": "🚀 Première configuration", "/moi": "🙋 Moi",
}
_SKIP = ("/api", "/static", "/branding", "/invite", "/login", "/__voice", "/sw.js", "/manifest")
_SEEN: set = set()          # (jour, page, compte) déjà notés depuis le démarrage — évite une écriture par vue
_SEEN_DAY = {"day": ""}


def page_key(path: str) -> str | None:
    """Page comptée pour un chemin, ou None (API, fichiers, connexion…)."""
    if not path or path.startswith(_SKIP) or "." in path.rsplit("/", 1)[-1]:
        return None
    first = "/" + path.strip("/").split("/", 1)[0] if path.strip("/") else "/"
    return first if first in PAGE_LABELS else None


def record(token: str, path: str) -> None:
    """Note qu'un compte a ouvert une page aujourd'hui (une fois par jour et par page)."""
    page = page_key(path)
    if page is None or not token:
        return
    day = _snap_day()
    if _SEEN_DAY["day"] != day:
        _SEEN.clear()
        _SEEN_DAY["day"] = day
        purge = True
    else:
        purge = False
    with _db_lock, _db() as conn:
        row = conn.execute("SELECT user_id FROM sessions WHERE token=? AND expires > ?",
                           (_session_key(token), time.time())).fetchone()
        if row is None:
            return
        k = (day, page, row["user_id"])
        if k in _SEEN:
            return
        _SEEN.add(k)
        conn.execute("INSERT OR IGNORE INTO page_usage (day, page, user_id) VALUES (?,?,?)", k)
        if purge:
            conn.execute("DELETE FROM page_usage WHERE day < ?", (_snap_day(time.time() - USAGE_KEEP_DAYS * 86400),))


def report(conn) -> dict:
    """Pour l'administration : membres actifs, usage par page (7 / 30 jours), parcours d'accueil."""
    d7, d30 = _snap_day(time.time() - 6 * 86400), _snap_day(time.time() - 29 * 86400)
    act = lambda since: conn.execute(  # noqa: E731
        "SELECT COUNT(DISTINCT user_id) AS n FROM page_usage WHERE day >= ?", (since,)).fetchone()["n"]
    pages = []
    for r in conn.execute(
            "SELECT page, COUNT(DISTINCT CASE WHEN day >= ? THEN user_id END) AS u7, "
            "COUNT(DISTINCT user_id) AS u30, COUNT(*) AS d30 FROM page_usage WHERE day >= ? GROUP BY page",
            (d7, d30)).fetchall():
        pages.append({"page": r["page"], "label": PAGE_LABELS.get(r["page"], r["page"]),
                      "u7": r["u7"], "u30": r["u30"], "d30": r["d30"]})
    pages.sort(key=lambda p: (-p["u30"], -p["d30"], p["page"]))
    seen = {p["page"] for p in pages}
    unused = sorted((p, lab) for p, lab in PAGE_LABELS.items() if p not in seen and p not in ("/moi", "/start"))
    users = conn.execute("SELECT email, onboard FROM users WHERE active=1").fetchall()
    linked = {r["user_email"] for r in conn.execute("SELECT DISTINCT user_email FROM char_links").fetchall()}
    mained = {r["user_email"] for r in conn.execute("SELECT DISTINCT user_email FROM char_links WHERE is_main=1").fetchall()}
    wished = {r["user_email"] for r in conn.execute("SELECT DISTINCT user_email FROM wishlist").fetchall()}

    def _addon(u) -> bool:
        try:
            return bool((json.loads(u["onboard"] or "{}") or {}).get("addon"))
        except (ValueError, TypeError):
            return False
    funnel = {"accounts": len(users),
              "chars": sum(1 for u in users if u["email"] in linked),
              "main": sum(1 for u in users if u["email"] in mained),
              "wishlist": sum(1 for u in users if u["email"] in wished),
              "addon": sum(1 for u in users if _addon(u))}
    first = conn.execute("SELECT MIN(day) AS d FROM page_usage").fetchone()["d"]
    return {"active7": act(d7), "active30": act(d30), "since": first, "pages": pages,
            "unused": [{"page": p, "label": lab} for p, lab in unused], "funnel": funnel}

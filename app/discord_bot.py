"""Client Discord (REST v10) pour le bot d'annonces de la guilde.

Aucune dépendance externe : appels HTTP simples (urllib). Le bot n'utilise pas
la passerelle temps réel (pas de commandes, pas d'intents) — uniquement l'envoi
de messages dans un salon via l'API web.

Permissions demandées à l'invitation : Voir le salon (1024) + Envoyer des
messages (2048) + Liens intégrés (16384) = 19456.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

API = "https://discord.com/api/v10"
INVITE_PERMS = 19456

COLOR_CRIMSON = 0xB1002E
COLOR_GOLD = 0xDFA55A
COLOR_MUTED = 0x8C96AD


class DiscordError(Exception):
    """Erreur API Discord (statut HTTP + message court, en français)."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)


def invite_url(app_id: str) -> str:
    """Lien d'invitation OAuth2 (scope bot) pour ajouter le bot à un serveur."""
    return (
        f"https://discord.com/oauth2/authorize?client_id={app_id.strip()}"
        f"&scope=bot&permissions={INVITE_PERMS}"
    )


def _req(method: str, path: str, token: str, body: dict | None = None, retry: bool = True) -> dict:
    req = urllib.request.Request(
        API + path,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "LOTP-Simulateur (lotp.gensbien.fr)",
        },
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return json.loads(raw.decode() or "{}") if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode() or "{}")
        except Exception:  # noqa: BLE001
            payload = {}
        if exc.code == 429 and retry:
            delay = float(payload.get("retry_after") or 1.0)
            time.sleep(min(delay + 0.2, 5.0))
            return _req(method, path, token, body, retry=False)
        if exc.code == 401:
            raise DiscordError(401, "Token du bot refusé par Discord (invalide ou révoqué ?).") from exc
        if exc.code == 403:
            raise DiscordError(403, "Discord a refusé l'action (permissions du bot insuffisantes ?).") from exc
        msg = str(payload.get("message") or "").strip()[:160] or "erreur Discord"
        raise DiscordError(exc.code, f"Discord : {msg}") from exc
    except urllib.error.URLError as exc:
        raise DiscordError(0, f"Discord injoignable ({getattr(exc, 'reason', exc)}).") from exc


def me(token: str) -> dict:
    """Identité du bot (valide le token)."""
    return _req("GET", "/users/@me", token)


def guilds(token: str) -> list[dict]:
    """Serveurs où le bot est présent."""
    out = _req("GET", "/users/@me/guilds", token)
    return out if isinstance(out, list) else []


def channels(token: str, guild_id: str) -> list[dict]:
    """Salons texte du serveur (id + nom), triés par position."""
    out = _req("GET", f"/guilds/{guild_id}/channels", token)
    if not isinstance(out, list):
        return []
    texts = [c for c in out if int(c.get("type") or -1) in (0, 5)]  # texte / annonces
    texts.sort(key=lambda c: (int(c.get("position") or 0), str(c.get("name"))))
    return [{"id": str(c.get("id")), "name": str(c.get("name"))} for c in texts]


def send(token: str, channel_id: str, embeds: list[dict] | None = None, content: str = "") -> dict:
    """Poste un message (embeds) dans un salon."""
    body: dict = {}
    if content:
        body["content"] = content[:1900]
    if embeds:
        body["embeds"] = embeds[:10]
    return _req("POST", f"/channels/{channel_id}/messages", token, body)


# ---------------------------------------------------------------------------
# Embeds « annonces de guilde » (français)
# ---------------------------------------------------------------------------
def report_embed(report: dict) -> dict:
    """Embed « nouveau rapport de raid » (Warcraft Logs)."""
    code = str(report.get("code") or "")
    zone = (report.get("zone") or {}).get("name") or "Raid"
    start_ms = float(report.get("startTime") or 0.0)
    date_txt = time.strftime("%d/%m/%Y à %H:%M", time.localtime(start_ms / 1000)) if start_ms else "?"
    return {
        "title": "📊 Nouveau rapport de raid",
        "url": f"https://www.warcraftlogs.com/reports/{code}",
        "description": f"**{str(report.get('title') or 'Rapport')[:200]}**",
        "color": COLOR_CRIMSON,
        "fields": [
            {"name": "Zone", "value": zone, "inline": True},
            {"name": "Date", "value": date_txt, "inline": True},
        ],
        "footer": {"text": "LOTP Simulateur · Warcraft Logs"},
    }


def roster_embed(kind: str, member: dict) -> dict:
    """Embed « arrivée » / « départ » de guilde."""
    name = str(member.get("name") or "?")
    if kind == "join":
        fields = []
        if member.get("level"):
            fields.append({"name": "Niveau", "value": str(member["level"]), "inline": True})
        return {
            "title": "👋 Nouveau membre",
            "description": f"**{name}** rejoint la guilde !",
            "color": COLOR_GOLD,
            "fields": fields,
            "footer": {"text": "Lords Of The Pit · roster Battle.net"},
        }
    return {
        "title": "😢 Départ",
        "description": f"**{name}** a quitté la guilde.",
        "color": COLOR_MUTED,
        "footer": {"text": "Lords Of The Pit · roster Battle.net"},
    }


def char_embed(name: str, changes: dict) -> dict:
    """Embed « progression de personnage » (palier d'iLvl, nouvelles montures / mascottes)."""
    bits = []
    if changes.get("ilvl_from") or changes.get("ilvl_to"):
        bits.append(f"iLvl **{changes.get('ilvl_from')} → {changes.get('ilvl_to')}**")
    if changes.get("mounts"):
        bits.append(f"🐎 +{int(changes['mounts'])} monture(s)")
    if changes.get("pets"):
        bits.append(f"🐾 +{int(changes['pets'])} mascotte(s)")
    return {
        "title": "📈 Progression de personnage",
        "description": f"**{str(name)[:80]}** — " + " · ".join(bits),
        "color": COLOR_GOLD,
        "footer": {"text": "Lords Of The Pit · suivi quotidien"},
    }


def weekly_embed(fields: list[dict], link: str = "") -> dict:
    """Embed « récap hebdomadaire » de la guilde."""
    desc = "La semaine de la guilde en un coup d'œil :"
    if link:
        desc += f"\n\n👉 [Voir les classements]({link})"
    return {
        "title": "📰 Récap hebdo — Lords Of The Pit",
        "description": desc,
        "color": COLOR_CRIMSON,
        "fields": fields[:6],
        "footer": {"text": "Lords Of The Pit · récap hebdomadaire"},
    }


# ---------------------------------------------------------------------------
# Embeds « calendrier des raids » (français)
# ---------------------------------------------------------------------------
def raid_embed(raid: dict, link: str = "") -> dict:
    """Embed « nouveau raid planifié »."""
    starts = float(raid.get("starts") or 0.0)
    date_txt = time.strftime("%d/%m/%Y à %H:%M", time.localtime(starts)) if starts else "?"
    title = str(raid.get("title") or "Raid de guilde")[:200]
    fields = [{"name": "Début", "value": date_txt, "inline": True}]
    if raid.get("duration_min"):
        fields.append({"name": "Durée", "value": f"{int(raid['duration_min'])} min", "inline": True})
    desc = f"**{title}**"
    if raid.get("note"):
        desc += f"\n{str(raid['note'])[:300]}"
    if link:
        desc += f"\n\n👉 [Répondre présent sur le site]({link})"
    return {
        "title": "🗓️ Nouveau raid planifié",
        "description": desc,
        "color": COLOR_GOLD,
        "fields": fields,
        "footer": {"text": "Lords Of The Pit · Calendrier"},
    }


def raid_reminder_embed(raid: dict, counts: dict | None = None, link: str = "") -> dict:
    """Embed « rappel de raid » (moins d'une heure avant le début)."""
    starts = float(raid.get("starts") or 0.0)
    date_txt = time.strftime("%d/%m/%Y à %H:%M", time.localtime(starts)) if starts else "?"
    title = str(raid.get("title") or "Raid de guilde")[:200]
    desc = f"**{title}** commence bientôt !"
    if counts:
        desc += f"\n✅ {counts.get('yes', 0)} · ❓ {counts.get('maybe', 0)} · ❌ {counts.get('no', 0)}"
    if link:
        desc += f"\n\n👉 [Répondre sur le site]({link})"
    return {
        "title": "⏰ Rappel — raid dans moins d'une heure",
        "description": desc,
        "color": COLOR_CRIMSON,
        "fields": [{"name": "Début", "value": date_txt, "inline": True}],
        "footer": {"text": "Lords Of The Pit · Calendrier"},
    }

"""Envoi d'e-mails du site (invitations) — SMTP configurable depuis l'administration.

Priorité : valeurs enregistrées dans l'administration (`set_config`), sinon variables
d'environnement SMTP_*.  Sans configuration, les envois échouent proprement (MailError)
et l'administration reste fonctionnelle en mode « lien à copier ».

Variables d'environnement reconnues : SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD,
SMTP_FROM, SMTP_MODE (starttls | ssl | none), SMTP_HELO.
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

_override: dict | None = None


class MailError(Exception):
    """Erreur d'envoi (SMTP non configuré ou refusé par le serveur)."""


def set_config(cfg: dict | None) -> None:
    """Configuration enregistrée dans l'administration (prioritaire sur l'environnement)."""
    global _override
    cfg = dict(cfg or {})
    _override = cfg if cfg.get("host") and cfg.get("user") else None


def _config() -> dict | None:
    c: dict
    if _override is not None:
        c = dict(_override)
    else:
        host = os.environ.get("SMTP_HOST", "").strip()
        user = os.environ.get("SMTP_USER", "").strip()
        password = os.environ.get("SMTP_PASSWORD", "")
        if not (host and user):
            return None
        c = {
            "host": host,
            "port": os.environ.get("SMTP_PORT", "587"),
            "user": user,
            "password": password,
            "sender": os.environ.get("SMTP_FROM", "").strip(),
            "mode": os.environ.get("SMTP_MODE", "starttls"),
            "helo": os.environ.get("SMTP_HELO", "").strip(),
        }
    c["port"] = int(c.get("port") or 587)
    c["mode"] = (c.get("mode") or "starttls").strip().lower()
    c["sender"] = (c.get("sender") or "").strip() or c["user"]
    c["helo"] = (c.get("helo") or "").strip() or c["sender"].split("@")[-1] or "localhost"
    return c


def smtp_configured() -> bool:
    return _config() is not None


def _open(cfg: dict):
    if cfg["mode"] == "ssl":
        return smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=25,
                                local_hostname=cfg["helo"], context=ssl.create_default_context())
    smtp = smtplib.SMTP(cfg["host"], cfg["port"], timeout=25, local_hostname=cfg["helo"])
    if cfg["mode"] == "starttls":
        smtp.starttls(context=ssl.create_default_context())
    return smtp


def check(host: str, port: int | str, mode: str, user: str, password: str,
          sender: str = "", helo: str = "") -> dict:
    """Teste une configuration SMTP (connexion + authentification) sans envoyer d'e-mail."""
    cfg = {"host": (host or "").strip(), "port": port, "mode": (mode or "starttls").strip().lower(),
           "user": (user or "").strip(), "password": password or "",
           "sender": (sender or "").strip(), "helo": (helo or "").strip()}
    if not (cfg["host"] and cfg["user"]):
        return {"ok": False, "detail": "Serveur et identifiant sont obligatoires."}
    cfg["port"] = int(cfg["port"] or 587)
    cfg["sender"] = cfg["sender"] or cfg["user"]
    cfg["helo"] = cfg["helo"] or cfg["sender"].split("@")[-1] or "localhost"
    try:
        smtp = _open(cfg)
        try:
            if cfg["password"]:
                smtp.login(cfg["user"], cfg["password"])
            smtp.noop()
        finally:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"Connexion refusée : {exc}"}
    return {"ok": True, "detail": "Connexion et authentification réussies."}


def send_mail(to: str, subject: str, text: str, html: str | None = None) -> None:
    cfg = _config()
    if cfg is None:
        raise MailError("SMTP non configuré (administration ou variables SMTP_*).")
    msg = EmailMessage()
    msg["From"] = cfg["sender"]
    msg["To"] = to if "<" in to else f"<{to}>"
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg["user"].split("@")[-1])
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        smtp = _open(cfg)
        try:
            if cfg["password"]:
                smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        raise MailError(f"Envoi SMTP impossible : {exc}") from exc


def invite_mail(link: str, expires_days: int, guild_name: str = "", short_name: str = "",
                base_url: str = "") -> tuple[str, str]:
    """Construit (texte brut, html) de l'e-mail d'invitation, aux couleurs de la guilde."""
    guild = (guild_name or "la guilde").strip()
    title = f"{(short_name or guild).strip()} Simulateur"
    footer = ("À bientôt" + (f" sur {base_url} !" if base_url else " !") + "\n")
    text = (
        "Bonjour,\n\n"
        f"Tu es invité(e) à rejoindre « {guild} » sur {title} :\n"
        "simulateur SimulationCraft, roster de la guilde, rapports de raid et\n"
        "comparateur de personnages.\n\n"
        f"Crée ton compte ici : {link}\n\n"
        f"Ce lien est valable {expires_days} jours et utilisable une seule fois.\n\n"
        + footer
    )
    link_line = (f'<p style="margin:12px 0 0;color:#6b6b6b;font-size:13px;">'
                 f'À bientôt sur <a href="{base_url}" style="color:#b1002e;">{base_url}</a> !</p>'
                 if base_url else "")
    html = f"""<!DOCTYPE html>
<html lang="fr"><body style="margin:0;background:#f4f4f7;font-family:system-ui,'Segoe UI',Roboto,sans-serif;">
<div style="max-width:520px;margin:24px auto;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e4e4ea;">
  <div style="background:#0b0e14;padding:18px 24px;">
    <span style="color:#dfa55a;font-size:20px;font-weight:700;">{title}</span><br>
    <span style="color:#8c96ad;font-size:13px;">{guild} — outil de guilde</span>
  </div>
  <div style="padding:24px;color:#232323;font-size:15px;line-height:1.6;">
    <p style="margin:0 0 12px;">Bonjour,</p>
    <p style="margin:0 0 16px;">Tu es invité(e) à rejoindre « {guild} » : simulateur SimulationCraft, roster de la guilde, rapports de raid et comparateur de personnages.</p>
    <p style="margin:0 0 20px;text-align:center;">
      <a href="{link}" style="display:inline-block;background:#b1002e;color:#ffffff;text-decoration:none;font-weight:600;padding:12px 22px;border-radius:10px;">Créer mon compte</a>
    </p>
    <p style="margin:0 0 6px;color:#6b6b6b;font-size:13px;">Ou copie ce lien dans ton navigateur :<br>
      <a href="{link}" style="color:#b1002e;word-break:break-all;">{link}</a></p>
    <p style="margin:12px 0 0;color:#6b6b6b;font-size:13px;">Lien valable {expires_days} jours, utilisable une seule fois.</p>
    {link_line}
  </div>
  <div style="background:#f4f4f7;padding:12px 24px;color:#8c98a8;font-size:11.5px;">
    Outil non affilié à Blizzard Entertainment · Données de jeu fournies par Blizzard Entertainment
  </div>
</div>
</body></html>"""
    return text, html

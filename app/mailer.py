"""Envoi d'e-mails du site (invitations) via le SMTP mailcow — expéditeur noreply@.

Config par variables d'environnement : SMTP_HOST, SMTP_PORT, SMTP_USER,
SMTP_PASSWORD, SMTP_FROM.  Sans config, les envois échouent proprement
(MailError) et l'admin reste fonctionnel en mode « lien à copier ».
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage


class MailError(Exception):
    """Erreur d'envoi (SMTP non configuré ou refusé par le serveur)."""


def _config() -> dict | None:
    host = os.environ.get("SMTP_HOST", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    if not (host and user and password):
        return None
    return {
        "host": host,
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "user": user,
        "password": password,
        "sender": os.environ.get("SMTP_FROM", "").strip() or user,
    }


def smtp_configured() -> bool:
    return _config() is not None


def send_mail(to: str, subject: str, text: str, html: str | None = None) -> None:
    cfg = _config()
    if cfg is None:
        raise MailError("SMTP non configuré sur le serveur (variables SMTP_* absentes).")
    msg = EmailMessage()
    msg["From"] = cfg["sender"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=25) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
    except Exception as exc:  # noqa: BLE001
        raise MailError(f"Envoi SMTP impossible : {exc}") from exc


def invite_mail(link: str, expires_days: int) -> tuple[str, str]:
    """Construit (texte brut, html) de l'e-mail d'invitation."""
    text = (
        "Bonjour,\n\n"
        "Tu es invité(e) à rejoindre le site de guilde Lords of the Pit :\n"
        "simulateur SimulationCraft, roster de la guilde, rapports de raid et\n"
        "comparateur de personnages.\n\n"
        f"Crée ton compte ici : {link}\n\n"
        f"Ce lien est valable {expires_days} jours et utilisable une seule fois.\n\n"
        "À bientôt sur https://lotp.gensbien.fr !\n"
    )
    html = f"""<!DOCTYPE html>
<html lang="fr"><body style="margin:0;background:#f4f4f7;font-family:system-ui,'Segoe UI',Roboto,sans-serif;">
<div style="max-width:520px;margin:24px auto;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e4e4ea;">
  <div style="background:#0b0e14;padding:18px 24px;">
    <span style="color:#dfa55a;font-size:20px;font-weight:700;">LOTP <span style="color:#ffffff;">Simulateur</span></span><br>
    <span style="color:#8c96ad;font-size:13px;">Lords of the Pit — outil de guilde</span>
  </div>
  <div style="padding:24px;color:#232323;font-size:15px;line-height:1.6;">
    <p style="margin:0 0 12px;">Bonjour,</p>
    <p style="margin:0 0 16px;">Tu es invité(e) à rejoindre le site de guilde : simulateur SimulationCraft, roster de la guilde, rapports de raid et comparateur de personnages.</p>
    <p style="margin:0 0 20px;text-align:center;">
      <a href="{link}" style="display:inline-block;background:#b1002e;color:#ffffff;text-decoration:none;font-weight:600;padding:12px 22px;border-radius:10px;">Créer mon compte</a>
    </p>
    <p style="margin:0 0 6px;color:#6b6b6b;font-size:13px;">Ou copie ce lien dans ton navigateur :<br>
      <a href="{link}" style="color:#b1002e;word-break:break-all;">{link}</a></p>
    <p style="margin:12px 0 0;color:#6b6b6b;font-size:13px;">Lien valable {expires_days} jours, utilisable une seule fois.</p>
  </div>
  <div style="background:#f4f4f7;padding:12px 24px;color:#8c98a8;font-size:11.5px;">
    Outil non affilié à Blizzard Entertainment · Données de jeu fournies par Blizzard Entertainment
  </div>
</div>
</body></html>"""
    return text, html

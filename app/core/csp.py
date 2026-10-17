"""Cohors — Content-Security-Policy.

Les pages de l'interface (app/static/*.html) gardent chacune leur bloc <script> embarqué. Au lieu
d'autoriser tout script inline ('unsafe-inline'), la CSP liste l'empreinte sha256 de CHAQUE bloc,
calculée au démarrage depuis les fichiers servis : un script injecté (oubli d'échappement dans un
innerHTML, par exemple) ou un attribut on…= n'a pas d'empreinte connue et est bloqué par le
navigateur. Des empreintes plutôt que des nonces : les pages sont des fichiers fixes, et le service
worker en garde des copies hors ligne — une empreinte reste valable, un nonce figé non.

Deux exceptions, chacune avec sa propre politique :
  * le portail vocal (vhost VOICE_PUBLIC_HOST) relaie une application tierce dont on ne maîtrise
    pas le HTML : il garde l'ancienne politique ('unsafe-inline') ;
  * les rapports SimulationCraft (/reports/<id>/report.html) sont produits par SimC, avec leurs
    propres scripts : ils sont servis en `sandbox` (origine opaque — aucun accès aux cookies ni à
    l'API du site), scripts inline autorisés DANS ce bac à sable seulement.

Les styles gardent 'unsafe-inline' (attributs style= partout dans l'interface ; risque faible).
"""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

# Bloc <script> SANS attribut src (les <script src=…> relèvent de 'self').
_INLINE_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script\s*>", re.S | re.I)


def inline_script_hashes(static_dir: Path) -> list[str]:
    """Sources CSP `'sha256-…'` de tous les blocs <script> embarqués des pages HTML."""
    out: set[str] = set()
    for page in sorted(Path(static_dir).glob("*.html")):
        # Le navigateur normalise les fins de ligne (CRLF → LF) avant de calculer l'empreinte.
        html = page.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
        for m in _INLINE_SCRIPT_RE.finditer(html):
            digest = base64.b64encode(hashlib.sha256(m.group(1).encode("utf-8")).digest()).decode()
            out.add(f"'sha256-{digest}'")
    return sorted(out)


def _common(voice_host: str) -> str:
    frame_src = "'self'" + (f" https://{voice_host}" if voice_host else "")
    return ("img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; media-src 'self' blob:; "
            f"frame-src {frame_src}; object-src 'none'; base-uri 'self'; form-action 'self'; "
            "frame-ancestors 'self'; worker-src 'self'")


def page_csp(static_dir: Path, voice_host: str = "") -> str:
    """Politique des pages Cohors : scripts du site + empreintes des blocs embarqués, rien d'autre."""
    scripts = " ".join(["'self'", *inline_script_hashes(static_dir)])
    return (f"default-src 'self'; script-src {scripts}; style-src 'self' 'unsafe-inline'; "
            + _common(voice_host))


def voice_csp(voice_host: str = "") -> str:
    """Portail vocal (HTML tiers relayé) : politique historique, scripts inline autorisés."""
    return ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            + _common(voice_host))


# Rapports SimC : bac à sable (origine opaque), rien ne sort, rien d'externe ne se charge.
REPORT_CSP = ("sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox; default-src 'none'; "
              "script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; "
              "font-src 'self' data:; frame-ancestors 'self'; base-uri 'none'; form-action 'none'")

"""Cohors — sécurité : mots de passe (scrypt) et garde-fous des profils SimulationCraft.

Module volontairement sans dépendance applicative (stdlib uniquement) : il s'importe
et se teste sans FastAPI ni base de données (voir tests/).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

# ---------------------------------------------------------------------------
# Mots de passe — scrypt (n=2^14, r=8, p=1) + sel aléatoire par utilisateur,
# comparaison à temps constant. Format : scrypt$n$r$p$sel_hex$hash_hex
# ---------------------------------------------------------------------------

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_hex, dk_hex = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                            n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Profils SimulationCraft — SimulationCraft honore certaines options écrites
# DANS le fichier de profil (vérifié sur l'image officielle : `input=` lit un
# fichier du conteneur, `output=` y écrit un fichier). Un export /simc n'en
# contient jamais : on refuse ces directives avant d'écrire le fichier.
# ---------------------------------------------------------------------------

_BLOCKED_KEYS = ("input", "output", "html", "json", "json2", "apikey")
_PROFILE_BLOCK_RE = re.compile(r"^\s*(" + "|".join(_BLOCKED_KEYS) + r")\s*=", re.IGNORECASE)


def real_client_ip(xff: str | None, peer: str | None) -> str:
    """Adresse du client derrière notre reverse proxy (Apache, même hôte).

    Le conteneur n'est jamais exposé directement : le seul intermédiaire est notre proxy,
    qui AJOUTE l'adresse qu'il voit en DERNIÈRE position d'X-Forwarded-For. Toutes les
    entrées précédentes viennent du client et sont donc forgeables — lire la première
    (comme avant) permettait de contourner le rate-limit de connexion en changeant
    d'en-tête à chaque requête (reproduit en direct sur la démo le 2026-09-20). On ne lit
    donc que la dernière entrée non vide, avec repli sur l'adresse de la socket.
    """
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return peer or "?"


def check_profile(text: str) -> str | None:
    """Retourne la directive interdite trouvée dans le profil, sinon None."""
    for line in text.splitlines():
        m = _PROFILE_BLOCK_RE.match(line)
        if m:
            return m.group(1).lower()
    return None

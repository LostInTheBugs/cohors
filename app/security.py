"""Cohors — sécurité : mots de passe (scrypt) et garde-fous des profils SimulationCraft.

Module volontairement sans dépendance applicative (stdlib uniquement) : il s'importe
et se teste sans FastAPI ni base de données (voir tests/).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from shared.simvalidate import check_profile  # noqa: F401  (re-export)

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
# Profils SimulationCraft — le garde-fou (directives input=/output=/html=/…)
# vit désormais dans shared/simvalidate.py, copié dans les DEUX images : l'app
# l'applique à l'entrée, le worker le re-applique de son côté. Il est
# re-exporté ci-dessus pour les appelants historiques (app/main.py).
# ---------------------------------------------------------------------------


def real_client_ip(xff: str | None, peer: str | None, hops: int = 1) -> str:
    """Adresse du client derrière notre chaîne de proxys de confiance.

    Chaque proxy de la chaîne AJOUTE l'adresse qu'il voit en fin d'X-Forwarded-For ;
    toutes les entrées écrites par le client lui-même sont donc forgeables — lire la
    première (comportement d'origine) permettait de contourner le rate-limit de connexion
    en changeant d'en-tête à chaque requête (reproduit en direct sur la démo le 2026-09-20).

    `hops` = nombre de proxys de confiance devant l'app (variable TRUSTED_PROXY_HOPS,
    défaut 1 : notre Apache sur le même hôte). On lit l'entrée située `hops` positions
    depuis la fin ; si l'en-tête est plus court que prévu, on prend la plus ancienne
    disponible. Sans en-tête, repli sur l'adresse de la socket.

    ⚠️ Si un proxy supplémentaire est ajouté devant (Cloudflare, load balancer), penser à
    augmenter TRUSTED_PROXY_HOPS, sinon tous les visiteurs partagent le même compteur.
    Et l'app doit rester liée à 127.0.0.1 derrière le proxy : exposée directement,
    l'en-tête redevient contrôlé par le client.
    """
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            idx = min(max(1, hops), len(parts))
            return parts[-idx]
    return peer or "?"

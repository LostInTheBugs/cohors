"""Cohors — validation partagée des entrées SimulationCraft (stdlib uniquement, ZÉRO dépendance).

Ce module est copié tel quel dans les DEUX images (app et worker : voir les deux
Dockerfile). L'app refuse les entrées invalides le plus tôt possible, et le worker
re-vérifie TOUT avant de construire le `docker run` — il ne fait jamais confiance
à ce que l'app lui envoie (défense en profondeur : une app compromise reste dans
son bac à sable).

Rappel du risque traité : un profil SimulationCraft n'est pas de la donnée inerte,
il accepte des directives comme `input=` (lit un fichier du conteneur) ou `output=`
(écrit un fichier) — vérifié sur l'image officielle en 2026-09. Les profils venant
d'un export /simc n'en contiennent jamais : on les rejette à l'entrée.
"""
from __future__ import annotations

import re

# Directives SimC qui touchent au système de fichiers ou à la facturation d'API.
BLOCKED_KEYS = ("input", "output", "html", "json", "json2", "apikey")

# Détection large : la directive peut être précédée d'un préfixe SimC
# (« profileset_x+=output=… ») ou collée à un séparateur, pas seulement en début
# de ligne — le garde-fou v2026.09.141 ne voyait que « ^clé= » (échappable).
_ANY_BLOCK_RE = re.compile(r"(?:^|[^\w])(" + "|".join(BLOCKED_KEYS) + r")\s*=", re.IGNORECASE)

# Profil livré DANS l'image (mode CLI de confort) : chemin relatif strict, pas de « .. ».
_STOCK_RE = re.compile(r"^profiles/[A-Za-z0-9_][A-Za-z0-9_./-]*\.simc$")

_EXTRA_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EXTRA_VAL_RE = re.compile(r"^[A-Za-z0-9_.,:+-]*$")


def check_profile(text: str) -> str | None:
    """Retourne la directive interdite trouvée dans le profil, sinon None.

    (Fonction re-exportée par app/security.py pour les appelants historiques.)
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _ANY_BLOCK_RE.search(line)
        if m:
            return m.group(1).lower()
    return None


def validate_profile_text(text: str | None, max_bytes: int) -> tuple[bool, str | None]:
    """Profil fourni en texte : taille maximale + directives interdites."""
    if not isinstance(text, str) or text.strip() == "":
        return False, "profil vide"
    if len(text.encode("utf-8", "replace")) > max_bytes:
        return False, f"profil trop volumineux (max {max_bytes // 1024} Ko)"
    hit = check_profile(text)
    if hit:
        return False, f"directive interdite dans le profil : {hit}="
    return True, None


def validate_iterations(value, max_iterations: int) -> tuple[bool, str | None]:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return False, "nombre d'itérations invalide"
    if not (1 <= n <= max_iterations):
        return False, f"itérations hors bornes (1 à {max_iterations})"
    return True, None


def validate_extra(extra) -> tuple[bool, str | None]:
    """Options passées telles quelles à SimC : `clé=valeur`, clé jamais interdite."""
    if extra is None:
        return True, None
    if not isinstance(extra, (list, tuple)):
        return False, "options invalides"
    for tok in extra:
        if not isinstance(tok, str) or "=" not in tok or len(tok) > 120:
            return False, f"option invalide : {tok!r}"
        key, _, val = tok.partition("=")
        if not _EXTRA_KEY_RE.match(key) or not _EXTRA_VAL_RE.match(val):
            return False, f"option invalide : {tok!r}"
        if key.lower() in BLOCKED_KEYS:
            return False, f"option interdite : {key}="
    return True, None


def validate_container_profile(path) -> tuple[bool, str | None]:
    """Chemin d'un profil interne à l'image (jamais fourni par un utilisateur final)."""
    if not isinstance(path, str) or not _STOCK_RE.match(path) or ".." in path:
        return False, "chemin de profil interne invalide"
    return True, None

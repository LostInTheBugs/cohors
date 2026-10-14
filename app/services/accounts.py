"""Cohors — accounts: e-mail change (the e-mail is the key of many tables) and the newcomer checklist."""
from __future__ import annotations

import json
import re

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Toutes les colonnes qui portent l'e-mail d'un compte (table, colonne). L'ajout d'une table qui stocke
# un e-mail doit compléter cette liste (test : test_email_columns_are_all_listed).
EMAIL_COLUMNS = (
    ("sims", "user_email"), ("profiles", "user_email"), ("char_links", "user_email"),
    ("wishlist", "user_email"), ("raids", "created_by"), ("raid_signups", "user_email"),
    ("guild_info", "updated_by"), ("branding", "updated_by"), ("prep_plan", "updated_by"),
    ("prep_bank", "updated_by"), ("unavails", "email"), ("gcal_meta", "updated_by"),
    ("mplus_alerts", "email"), ("notifs", "email"), ("craft_orders", "requester"),
    ("craft_orders", "crafter_email"), ("loot_history", "added_by"),
)


def normalize_email(email: str) -> str:
    e = str(email or "").strip().lower()
    if len(e) > 200 or not EMAIL_RE.match(e):
        raise ValueError("Adresse e-mail invalide.")
    return e


def change_email(conn, user_id: int, new_email: str) -> tuple[str, str]:
    """Change l'e-mail d'un compte et le reporte partout où il sert de clé. → (ancien, nouveau).

    ValueError si l'adresse est invalide ou déjà prise. Les restes d'un ancien compte supprimé qui
    porteraient la nouvelle adresse sont écrasés (UPDATE OR REPLACE) plutôt que de bloquer."""
    new = normalize_email(new_email)
    row = conn.execute("SELECT email FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        raise LookupError("Compte inconnu")
    old = row["email"]
    if new == old:
        return old, new
    if conn.execute("SELECT 1 AS x FROM users WHERE lower(email)=? AND id != ?", (new, user_id)).fetchone():
        raise ValueError("Cette adresse est déjà utilisée par un autre compte.")
    conn.execute("UPDATE users SET email=? WHERE id=?", (new, user_id))
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for table, col in EMAIL_COLUMNS:
        if table in tables:
            conn.execute(f"UPDATE OR REPLACE {table} SET {col}=? WHERE {col}=?", (new, old))  # noqa: S608
    # invitations / liens de réinitialisation en attente pour l'ancienne adresse : caducs
    conn.execute("DELETE FROM invites WHERE email=? AND used IS NULL", (old,))
    return old, new


# ------------------------------------------------------------------ accueil d'un nouveau membre
def _flags(user) -> dict:
    try:
        return json.loads(user["onboard"] or "{}") or {}
    except (ValueError, TypeError, IndexError, KeyError):
        return {}


def onboarding(conn, user) -> dict:
    """Les 3 étapes d'accueil d'un membre : personnages liés (et main), wishlist, addon installé."""
    flags = _flags(user)
    email = user["email"]
    n_chars = conn.execute("SELECT COUNT(*) AS n FROM char_links WHERE user_email=?", (email,)).fetchone()["n"]
    has_main = conn.execute("SELECT 1 AS x FROM char_links WHERE user_email=? AND is_main=1", (email,)).fetchone()
    n_wish = conn.execute("SELECT COUNT(*) AS n FROM wishlist WHERE user_email=?", (email,)).fetchone()["n"]
    steps = [
        {"key": "chars", "done": bool(n_chars and has_main), "n": int(n_chars or 0)},
        {"key": "wishlist", "done": bool(n_wish), "n": int(n_wish or 0)},
        {"key": "addon", "done": bool(flags.get("addon"))},
    ]
    done = sum(1 for s in steps if s["done"])
    return {"steps": steps, "done": done, "total": len(steps), "hidden": bool(flags.get("hide"))}


def onboarding_set(conn, user, addon: bool | None = None, hide: bool | None = None) -> None:
    flags = _flags(user)
    if addon is not None:
        flags["addon"] = bool(addon)
    if hide is not None:
        flags["hide"] = bool(hide)
    conn.execute("UPDATE users SET onboard=? WHERE id=?", (json.dumps(flags), user["id"]))


def onboarding_counts(conn) -> dict:
    """Récap hebdo : comptes actifs sans personnage lié / avec personnages mais sans wishlist."""
    users = [r["email"] for r in conn.execute("SELECT email FROM users WHERE active=1").fetchall()]
    linked = {r["user_email"] for r in conn.execute("SELECT DISTINCT user_email FROM char_links").fetchall()}
    wished = {r["user_email"] for r in conn.execute("SELECT DISTINCT user_email FROM wishlist").fetchall()}
    return {"accounts": len(users), "no_chars": sum(1 for u in users if u not in linked),
            "no_wishlist": sum(1 for u in users if u in linked and u not in wished)}

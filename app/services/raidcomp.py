"""Cohors — raid composition: raid buff coverage (Midnight) and the Discord summary of a raid night."""
from __future__ import annotations

# Buffs et débuffs de raid apportés par chaque classe (Midnight) + utilitaires clés.
# (libellé FR, libellé EN, classes qui l'apportent — clés de CLASS_KEY_FR)
RAID_BUFFS = (
    ("Intelligence (Mage)", "Intellect (Mage)", ("Mage",)),
    ("Endurance (Prêtre)", "Stamina (Priest)", ("Priest",)),
    ("Puissance d'attaque (Guerrier)", "Attack power (Warrior)", ("Warrior",)),
    ("Polyvalence (Druide)", "Versatility (Druid)", ("Druid",)),
    ("Maîtrise (Chaman)", "Mastery (Shaman)", ("Shaman",)),
    ("Mobilité (Évocateur)", "Movement (Evoker)", ("Evoker",)),
    ("Dégâts subis (Chasseur)", "Damage taken (Hunter)", ("Hunter",)),
    ("Dégâts magiques subis (Chasseur de démons)", "Magic damage taken (Demon Hunter)", ("DemonHunter",)),
    ("Dégâts physiques subis (Moine)", "Physical damage taken (Monk)", ("Monk",)),
    ("Aura de dévotion (Paladin)", "Devotion Aura (Paladin)", ("Paladin",)),
    ("Poison atrophiant (Voleur)", "Atrophic Poison (Rogue)", ("Rogue",)),
    ("Furie sanguinaire", "Bloodlust", ("Shaman", "Mage", "Hunter", "Evoker")),
    ("Résurrection en combat", "Battle resurrection", ("Druid", "DeathKnight", "Warlock", "Paladin")),
)

ROLE_ORDER = (("tank", "🛡️ Tanks"), ("heal", "💚 Heals"), ("dps", "⚔️ DPS"))


def raid_buffs(want_en: bool = False) -> list[list]:
    """Table des buffs pour l'interface : [[libellé, [classes…]], …] dans la langue demandée."""
    return [[en if want_en else fr, list(cls)] for fr, en, cls in RAID_BUFFS]


def buff_coverage(class_keys) -> tuple[list[str], list[str]]:
    """(buffs couverts, buffs manquants) — libellés FR — pour un ensemble de classes présentes."""
    have = {c for c in class_keys if c}
    ok, missing = [], []
    for fr, _en, cls in RAID_BUFFS:
        (ok if any(c in have for c in cls) else missing).append(fr)
    return ok, missing


def event_status(inv: dict, ovr: dict) -> str:
    """Réponse effective d'un invité : forcée par un officier, sinon celle du jeu."""
    n = inv.get("n")
    if ovr.get(n):
        return ovr[n]
    s = inv.get("s")
    return "ok" if s in (1, 3) else "maybe" if s == 8 else "no" if s == 2 else "wait"


def _ck_of(name: str, class_keys: dict) -> str:
    k = str(name or "").lower()
    return class_keys.get(k) or (class_keys.get(k.rsplit("-", 1)[0]) if "-" in k else "") or ""


def _clip(text: str, n: int = 1024) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def compo_embed(ev: dict, class_keys: dict, guild: str = "la guilde", link: str = "",
                color: int = 0xDFA55A) -> dict:
    """Embed Discord (français) : présents par rôle, peut-être, buffs manquants, pièces voulues."""
    meta = ev.get("meta") or {}
    roles, ovr = meta.get("roles") or {}, meta.get("ovr") or {}
    ok, maybe = [], []
    for i in ev.get("inv") or []:
        st = event_status(i, ovr)
        if st == "ok":
            ok.append(i.get("n") or "?")
        elif st == "maybe":
            maybe.append(i.get("n") or "?")
    fields = []
    lines = []
    for role, label in ROLE_ORDER:
        who = [n for n in ok if roles.get(n) == role]
        if who:
            lines.append(f"**{label} ({len(who)})** : {', '.join(who)}")
    no_role = [n for n in ok if roles.get(n) not in ("tank", "heal", "dps")]
    if no_role:
        lines.append((f"**Sans rôle ({len(no_role)})** : " if len(no_role) < len(ok) else "") + ", ".join(no_role))
    fields.append({"name": f"✅ Présents ({len(ok)})", "value": _clip("\n".join(lines) or "Personne pour l'instant.")})
    if maybe:
        fields.append({"name": f"❓ Peut-être ({len(maybe)})", "value": _clip(", ".join(maybe))})
    known = [_ck_of(n, class_keys) for n in ok]
    if any(known):
        _cov, missing = buff_coverage(known)
        unknown = [n for n, k in zip(ok, known) if not k]
        txt = ("⚠️ " + " · ".join(missing)) if missing else "Tous couverts ✅"
        if unknown:
            txt += f"\n_Classe inconnue : {', '.join(unknown)}_"
        fields.append({"name": "🧩 Buffs de raid", "value": _clip(txt)})
    if meta.get("raids") or meta.get("bosses"):
        fields.append({"name": "🗺️ Programme", "value": _clip(
            " · ".join(meta.get("raids") or []) + (f"\n⚔️ {' · '.join(meta.get('bosses') or [])}" if meta.get("bosses") else "")
            + (f"\n🚩 Départ : {meta['start']}" if meta.get("start") else ""))})
    for w in (ev.get("wanted") or [])[:5]:
        items = " · ".join(("⭐ " if it.get("prio") else "") + str(it.get("name") or "")
                           + (f" ({', '.join(it.get('who') or [])})" if it.get("who") else "")
                           for it in w.get("items") or [])
        if items:
            fields.append({"name": f"🎁 {w.get('boss') or 'Boss'}", "value": _clip(items)})
    try:
        ts = int(float(ev.get("ts") or 0))
    except (TypeError, ValueError):
        ts = 0
    when = f"<t:{ts}:F> (<t:{ts}:R>)" if ts > 0 else str(ev.get("date") or "")
    return {
        "title": f"📋 {ev.get('title') or 'Raid'}",
        "description": when + (f"\n👉 [Calendrier]({link})" if link else ""),
        "color": color,
        "fields": fields[:25],
        "footer": {"text": f"{guild} · composition du raid"},
    }


def _base(name: str) -> str:
    """Nom de personnage sans le royaume (« Nom-Royaume » → « nom »), en minuscules."""
    k = str(name or "").strip().lower()
    return k.rsplit("-", 1)[0] if "-" in k else k


def fill_candidates(ev: dict, chars: list[dict], owner_of: dict, unavailable: set, want_en: bool = False) -> dict:
    """Qui peut compléter une soirée : personnages actifs ni inscrits (présent / peut-être / absent),
    ni indisponibles ce jour-là, ni joués par un compte déjà présent.

    `chars` : [{key, class_key, ...}] (derniers relevés) ; `owner_of` : nom → e-mail du compte ;
    `unavailable` : e-mails indisponibles ce jour-là. Chaque candidat indique les buffs manquants
    qu'il apporterait (`brings`) ; ceux qui en apportent le plus viennent en premier.
    """
    meta = ev.get("meta") or {}
    ovr = meta.get("ovr") or {}
    status = {}
    for i in ev.get("inv") or []:
        status[_base(i.get("n"))] = event_status(i, ovr)
    class_of = {c["key"]: c.get("class_key") or "" for c in chars}
    coming = [k for k, st in status.items() if st in ("ok", "maybe")]
    coming_owners = {owner_of[k] for k in coming if owner_of.get(k)}
    _ok, missing = buff_coverage(class_of.get(k, "") for k, st in status.items() if st == "ok")
    missing = set(missing)
    label = {fr: (en if want_en else fr) for fr, en, _c in RAID_BUFFS}
    out = []
    for c in chars:
        k = c["key"]
        st = status.get(k)
        if st in ("ok", "maybe", "no"):
            continue
        owner = owner_of.get(k)
        if owner and (owner in coming_owners or owner in unavailable):
            continue
        brings = [label[fr] for fr, _en, cls in RAID_BUFFS if fr in missing and c.get("class_key") in cls]
        out.append({**c, "invited": st == "wait", "brings": brings})
    out.sort(key=lambda c: (-len(c["brings"]), not c.get("main"), -(c.get("ilvl") or 0), c.get("name", "").lower()))
    return {"candidates": out, "missing": [label[fr] for fr, _en, _c in RAID_BUFFS if fr in missing]}

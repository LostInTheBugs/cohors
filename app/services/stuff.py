"""Cohors — "Stuff conseillé": SimC export parsing, BIS guide, crafted gear, item stats and ranking."""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import httpx

from app.simclient import run_sim

logger = logging.getLogger(__name__)


# =====================================================================
# v2026.09.119 — « Stuff conseillé » : quoi porter, depuis ce qu'on possède.
#
# L'export SimC de l'addon contient l'équipement porté, les pièces des sacs
# (« ### Gear from Bags ») et les builds sauvegardés (« # Saved Loadout: »).
# On s'en sert pour répondre à : « avec ce que j'ai, que devrais-je porter
# pour tel contenu ? » — sans rien coller.
#
# Méthodes :
#  * Spés de soin → SimulationCraft ne simule pas les soins : classement par
#    niveau d'objet puis par priorité des statistiques du guide de la spé
#    (les stats de chaque pièce sont extraites du moteur, bonus inclus).
#  * Spés DPS → vraies simulations : une profileset par pièce (comme le
#    Top Stuff), sur le contenu choisi.
# =====================================================================
STUFF_SLOTS = ("head", "neck", "shoulder", "back", "chest", "wrist", "hands", "waist",
               "legs", "feet", "finger1", "finger2", "trinket1", "trinket2", "main_hand", "off_hand")
STUFF_ITERATIONS = 800
STUFF_MAX_CANDIDATES = 30
STUFF_CONTENTS = {
    "mplus": {"opts": ["fight_style=DungeonSlice"], "label_fr": "Mythique+", "label_en": "Mythic+",
              "sim_fr": "donjon (DungeonSlice)", "sim_en": "dungeon (DungeonSlice)"},
    "raid": {"opts": ["fight_style=Patchwerk", "max_time=300"], "label_fr": "Raid", "label_en": "Raid",
             "sim_fr": "combat de 5 min, buffs de raid", "sim_en": "5 min fight, raid buffs"},
    "delves": {"opts": ["fight_style=Patchwerk", "max_time=90", "optimal_raid=0"],
               "label_fr": "Gouffres", "label_en": "Delves",
               "sim_fr": "combat court (90 s), sans buffs de raid (solo)",
               "sim_en": "short fight (90 s), no raid buffs (solo)"},
}
# Contenus valables pour bis_content (sur-ensemble incluant les sources non-simulables)
BIS_CONTENTS = {
    "raid": {"label_fr": "Raid"},
    "mplus": {"label_fr": "Mythique+"},
    "delves": {"label_fr": "Gouffres"},
    "worldboss": {"label_fr": "Boss mondial"},
    "craft": {"label_fr": "Artisanat"},
}
# Spés de soin (le moteur ne les simule pas) — classement par stats pondérées.
HEAL_SPECS = {"restoration", "holy", "discipline", "mistweaver", "preservation"}
# Priorité des stats secondaires par spé et par contenu : niveau d'objet d'abord,
# puis cet ordre (les deux premières stats portées pèsent le plus).
# Sources : guides Wowhead « Stat Priority » (patch 12.1, août-sept. 2026) ;
# pour le chaman, croisé avec Icy Veins « Restoration Shaman Stat Priority » (12.1).
# Les gouffres n'ont pas de priorité publiée : on utilise celle du Mythique+ (combats courts).
STUFF_HEAL_PRIO = {
    "shaman/restoration": {
        "orders": {"raid": ["crit", "vers", "haste", "mast"],
                   "mplus": ["crit", "haste", "vers", "mast"],
                   "delves": ["crit", "haste", "vers", "mast"]},
        "source": "https://www.wowhead.com/guide/classes/shaman/restoration/stat-priority-pve-healer",
        "source_fr": "Guide Wowhead — Chaman Restauration (12.1)",
        "note_fr": "Critique d'abord ; Hâte et Polyvalence très proches ; Maîtrise en dernier. Identiques en Totémique et Farseer. La Hâte est mise en avant en Mythique+ et en gouffres (combats courts).",
    },
    "druid/restoration": {
        "orders": {"raid": ["haste", "mast", "vers", "crit"],
                   "mplus": ["haste", "mast", "vers", "crit"],
                   "delves": ["haste", "mast", "vers", "crit"]},
        "source": "https://www.wowhead.com/guide/classes/druid/restoration/stat-priority-pve-healer",
        "source_fr": "Guide Wowhead — Druide Restauration (12.1)",
        "note_fr": "Hâte d'abord, puis Maîtrise, Polyvalence, et Critique en dernier.",
    },
    "paladin/holy": {
        "orders": {"raid": ["mast", "haste", "crit", "vers"],
                   "mplus": ["mast", "haste", "crit", "vers"],
                   "delves": ["mast", "haste", "crit", "vers"]},
        "source": "https://www.wowhead.com/guide/classes/paladin/holy/stat-priority-pve-healer",
        "source_fr": "Guide Wowhead — Paladin Sacré (12.1)",
        "note_fr": "Maîtrise d'abord, puis Hâte et Critique (à égalité), Polyvalence en dernier.",
    },
    "priest/holy": {
        "orders": {"raid": ["crit", "vers", "mast", "haste"],
                   "mplus": ["vers", "crit", "haste", "mast"],
                   "delves": ["vers", "crit", "haste", "mast"]},
        "source": "https://www.wowhead.com/guide/classes/priest/holy/stat-priority-pve-healer",
        "source_fr": "Guide Wowhead — Prêtre Sacré (12.1)",
        "note_fr": "En raid : Critique puis Polyvalence/Maîtrise. En Mythique+/gouffres : Polyvalence d'abord (survie et dégâts), puis Critique.",
    },
    "priest/discipline": {
        "orders": {"raid": ["haste", "mast", "crit", "vers"],
                   "mplus": ["haste", "mast", "crit", "vers"],
                   "delves": ["haste", "mast", "crit", "vers"]},
        "source": "https://www.wowhead.com/guide/classes/priest/discipline/stat-priority-pve-healer",
        "source_fr": "Guide Wowhead — Prêtre Discipline (12.1)",
        "note_fr": "Hâte d'abord, puis Maîtrise, Critique, Polyvalence. Identiques en Oracle et Voidweaver.",
    },
    "monk/mistweaver": {
        "orders": {"raid": ["haste", "crit", "vers", "mast"],
                   "mplus": ["haste", "mast", "crit", "vers"],
                   "delves": ["haste", "mast", "crit", "vers"]},
        "source": "https://www.wowhead.com/guide/classes/monk/mistweaver/stat-priority-pve-healer",
        "source_fr": "Guide Wowhead — Moine Tisse-brume (12.1)",
        "note_fr": "Hâte d'abord. En raid : Critique puis Polyvalence. En Mythique+/gouffres : Maîtrise puis Critique (dégâts).",
    },
    "evoker/preservation": {
        "orders": {"raid": ["crit", "mast", "haste", "vers"],
                   "mplus": ["crit", "haste", "mast", "vers"],
                   "delves": ["crit", "haste", "mast", "vers"]},
        "source": "https://www.wowhead.com/guide/classes/evoker/preservation/stat-priority-pve-healer",
        "source_fr": "Guide Wowhead — Évocateur Préservation (12.1)",
        "note_fr": "Critique d'abord. En raid : Maîtrise avant Hâte. En Mythique+/gouffres : Hâte avant Maîtrise.",
    },
}


# Niveau maximum d'une pièce : Wowhead publie, pour chaque objet, la version la plus
# haute qui existe (« Item Level X · Upgrade Level: <Piste> Y/6 »). On s'en sert pour le
# mode « tout au rang maximum » (aucune table de saison inventée : les valeurs viennent
# de l'objet lui-même).
WH_TOOLTIP = "https://nether.wowhead.com/tooltip/item/{iid}?dataEnv=1&locale=0"
WH_ILVL_RE = re.compile(r"Item Level <!--ilvl-->(\d+)")
WH_UP_RE = re.compile(r"Upgrade Level: ([A-Za-z ]+) <!--uindex-->(\d+)/(\d+)")


# Liste BIS : instantané des guides embarqué (voir app/data/bis.json) — le serveur ne peut
# pas récupérer les pages de guides (Cloudflare 403), donc la liste est versionnée avec l'app.
BIS_FILE = Path(__file__).resolve().parents[1] / "data" / "bis.json"
BIS_CACHE: dict = {"mtime": 0.0, "data": {}}


# Mapping source_fr -> contenu (raid, mplus, delves, worldboss, craft, ou None = multi/non-classifié)
BIS_CONTENT_MAP = {
    # Raid uniquement
    "Catalyseur & Mythic+ & Coffre": "raid",
    "Catalyseur & Raid & Coffre": "raid",
    "Raid & Coffre": "raid",
    "The Coiled Altar (Raid) & Catalyseur": "raid",
    # M+ uniquement (donjons mythique+)
    "Blinding Vale": "mplus",
    "Catalyseur & Ruby Life Pools": "mplus",
    "Catalyseur & The Coiled Altar": "mplus",
    "Catalyseur & Voidscar Arena": "mplus",
    "Entombed Sentinels": "mplus",
    "Galvazzt": "mplus",
    "Mor'zahi": "mplus",
    "Murder Row": "mplus",
    "Allée du meurtre": "mplus",   # nom FR de Murder Row (donjon M+)
    "Murder Row & Catalyseur": "mplus",
    "Ruby Life Pools": "mplus",
    "Temple of Sethraliss": "mplus",
    "Temple of Sethraliss & Catalyseur": "mplus",
    "The Blinding Vale": "mplus",
    "The Blinding Vale & Catalyseur": "mplus",
    "The Coiled Altar": "mplus",
    "The Coiled Altar & Catalyseur": "mplus",
    "The Coiled Alter": "mplus",
    "The Coiled Alter & Catalyseur": "mplus",
    "Tier Set & The Coiled Altar": "raid",
    "Tier Set & Voidscar Arena": "mplus",
    "Token & Entombed Sentinels": "mplus",
    "Voidscar Arena": "mplus",
    "Voidscar Arena & Catalyseur": "mplus",
    # Delves uniquement
    "Antre de Nalorakk": "delves",
    "Den of Nalorakk": "delves",
    "Den of Nalorakk & Catalyseur": "delves",
    "L'Ophidien ondulant": "delves",
    "The Lost Explorers": "delves",
    "The Lost Explorers (Raid)": "delves",
    "The Twin Fangs": "delves",
    # World Boss uniquement
    "Catalyseur & Nek'zali the Soulcoiler": "worldboss",
    "Catalyseur & Ula'tek": "worldboss",
    "King's Rest": "worldboss",
    "King's Rest & Catalyseur": "worldboss",
    "Kings Rest & Catalyseur": "worldboss",
    "Kings' Rest": "worldboss",
    "Kings' Rest & Catalyseur": "worldboss",
    "Nek'zali l'Entortillâme": "worldboss",
    "Nek'zali the Soulcoiler": "worldboss",
    "Nek'zali the Soulcoiler & Catalyseur": "worldboss",
    "Nek'zali the Soulcoiler (Raid)": "worldboss",
    "Nymrissa Mande-vagues": "worldboss",
    "Nymrissa Wavebinder": "worldboss",
    "Nymrissa Wavebinder (Raid)": "worldboss",
    "Nymrissa Wavecaller": "worldboss",
    "Rav'i": "worldboss",
    "Repos des rois": "worldboss",
    "Roi Dazar": "worldboss",
    "Seigneur des maléfices Malacrass": "worldboss",
    "Souffle d'Ula'tek": "worldboss",
    "Tier Set & King's Rest": "worldboss",
    "Tier Set & Nek'zali the Soulcoiler": "worldboss",
    "Tier Set & Ula'tek": "worldboss",
    "Tier Set & Vashnik the Malignant": "worldboss",
    "Ula'tek": "worldboss",
    "Ula'tek & Catalyseur": "worldboss",
    "Ula'tek (Raid)": "worldboss",
    "Vashnik": "worldboss",
    "Vashnik the Malignant": "worldboss",
    "Vexhul": "worldboss",
    # Craft uniquement
    "Blacksmithing": "craft",
    "Crafted": "craft",
    "Crafting": "craft",
    "Crafting Blacksmithing": "craft",
    "Crafting/Misc": "craft",
    "Forge": "craft",
    "Jewelcrafting": "craft",
    "Leatherworking": "craft",
    "Travail du cuir": "craft",
    # Multi-contenus / non classifiés (toujours inclus)
    "Catalyseur": None,
    "Altar of Fangs": None,
    "Arène de la Cicatrice du Vide": None,
    "BoE Trash Drop": None,
    "Entomed Sentinels": None,
    "Tier Set": None,
    # Boss de raid (Szorak = Temple of Sethraliss)
    # bis.json contains "Sszorak" (double S)
    "Sszorak": "raid",
    "Sszorak (Raid)": "raid",
    "Tier Set & Sszorak": "raid",
}


def _stuff_bis_filter(blk: dict, contents: list[str]) -> dict:
    """Filtrer les slots BIS pour ne garder que ceux dont la source est valide pour les contenus demandés.

    - Source absente de BIS_CONTENT_MAP → inclure le slot (pas de correspondance connue, mais pas à exclure)
      et émettre un log.warning.
    - Source mappée à None (multi-contenus) → toujours inclure (catalyseur, tier set, etc.)
    - Source mappée à une valeur → inclure uniquement si cette valeur est dans contents
    """
    if not contents:
        return blk
    contents_set = set(contents)
    filtered_slots = []
    for slot in (blk.get("slots") or []):
        src = slot.get("src_fr", "")
        if src not in BIS_CONTENT_MAP:
            # Source absente du mapping → inclure mais noter
            logger.warning("Src_fr non mappée dans bis_content : %s", src)
            filtered_slots.append(slot)
        else:
            mapped = BIS_CONTENT_MAP[src]
            if mapped is None:
                # Multi-contenus / non classifiés : toujours inclus
                filtered_slots.append(slot)
            elif mapped in contents_set:
                filtered_slots.append(slot)
    return {**blk, "slots": filtered_slots}


# ---------------------------------------------------------------------------
# Meilleures pièces d'artisanat (mode BIS, case « Artisanat »)
#
# Le guide BIS ne donne qu'un objet par emplacement — rarement fabriqué. Ici on part des
# recettes du jeu (game_recipes, objet retrouvé par son nom lors de la synchro) : pour
# chaque emplacement, les objets fabricables de l'extension en cours que la classe peut
# porter, avec les artisans de la guilde qui connaissent la recette.
# ---------------------------------------------------------------------------
CLASS_ARMOR = {"mage": "Cloth", "priest": "Cloth", "warlock": "Cloth",
               "druid": "Leather", "rogue": "Leather", "monk": "Leather", "demonhunter": "Leather",
               "hunter": "Mail", "shaman": "Mail", "evoker": "Mail",
               "warrior": "Plate", "paladin": "Plate", "deathknight": "Plate"}
CRAFT_INV_SLOT = {"HEAD": "head", "NECK": "neck", "SHOULDER": "shoulder", "CLOAK": "back", "BACK": "back",
                  "CHEST": "chest", "ROBE": "chest", "WRIST": "wrist", "HAND": "hands", "HANDS": "hands",
                  "WAIST": "waist", "LEGS": "legs", "FEET": "feet", "FINGER": "finger", "TRINKET": "trinket",
                  "WEAPON": "main_hand", "TWOHWEAPON": "main_hand", "WEAPONMAINHAND": "main_hand",
                  "RANGED": "main_hand", "RANGEDRIGHT": "main_hand",
                  "HOLDABLE": "off_hand", "SHIELD": "off_hand", "WEAPONOFFHAND": "off_hand"}
CRAFT_SLOT_ORDER = ["head", "neck", "shoulder", "back", "chest", "wrist", "hands", "waist", "legs", "feet",
                    "finger", "trinket", "main_hand", "off_hand"]
CRAFT_ARMOR_SLOTS = {"head", "shoulder", "chest", "wrist", "hands", "waist", "legs", "feet"}
# Armes maniables par classe (noms anglais des sous-classes Blizzard) ; « Miscellaneous » = main gauche tenue.
CLASS_WEAPONS = {
    "warrior": {"Axe", "Mace", "Sword", "Polearm", "Staff", "Dagger", "Fist Weapon", "Shield"},
    "paladin": {"Axe", "Mace", "Sword", "Polearm", "Shield", "Miscellaneous"},
    "hunter": {"Bow", "Gun", "Crossbow", "Polearm", "Staff", "Axe", "Sword", "Dagger", "Fist Weapon"},
    "rogue": {"Dagger", "Fist Weapon", "Axe", "Mace", "Sword"},
    "priest": {"Mace", "Dagger", "Staff", "Wand", "Miscellaneous"},
    "shaman": {"Axe", "Mace", "Fist Weapon", "Dagger", "Staff", "Shield", "Miscellaneous"},
    "mage": {"Sword", "Dagger", "Staff", "Wand", "Miscellaneous"},
    "warlock": {"Sword", "Dagger", "Staff", "Wand", "Miscellaneous"},
    "monk": {"Fist Weapon", "Axe", "Mace", "Sword", "Polearm", "Staff", "Miscellaneous"},
    "druid": {"Dagger", "Fist Weapon", "Mace", "Polearm", "Staff", "Miscellaneous"},
    "demonhunter": {"Warglaives", "Fist Weapon", "Axe", "Sword"},
    "deathknight": {"Axe", "Mace", "Sword", "Polearm"},
    "evoker": {"Axe", "Dagger", "Fist Weapon", "Mace", "Sword", "Staff", "Miscellaneous"},
}
# Spés à Intelligence : armes de lanceur de sorts seulement (pas de hache 2M pour un chaman Restauration).
CASTER_WEAPONS = {"Staff", "Mace", "Dagger", "Sword", "Wand", "Miscellaneous", "Shield"}
CASTER_CLASSES = {"mage", "warlock", "evoker"}
CASTER_SPECS = {"restoration", "holy", "discipline", "mistweaver", "preservation", "shadow", "elemental", "balance"}
CRAFT_SLOT_FR = {"head": "Tête", "neck": "Cou", "shoulder": "Épaules", "back": "Dos", "chest": "Torse",
                 "wrist": "Poignets", "hands": "Mains", "waist": "Taille", "legs": "Jambes", "feet": "Pieds",
                 "finger": "Anneau", "trinket": "Bijou", "main_hand": "Arme", "off_hand": "Main gauche"}
CRAFT_MAX_PER_SLOT = 3


def _stuff_best_crafted(conn, cls: str, spec: str, contents: list[str], owned_ids: set[int],
                        equipped_ids: set[int]) -> dict:
    """Objets fabricables (extension en cours) utiles au personnage, par emplacement.

    Filtre : type d'armure de la classe pour les pièces d'armure, armes maniables par la
    classe (armes de lanceur de sorts pour les spés à Intelligence), bijoux/anneaux/cou/dos pour tous ; outils et tenues de métier, objets cosmétiques
    écartés. Classement : niveau d'objet de base, puis nom. Pour les soigneurs, les deux stats
    secondaires à demander à l'artisan viennent de la priorité connue de la spé.
    """
    cls = (cls or "").lower()
    armor = CLASS_ARMOR.get(cls, "")
    weapons = set(CLASS_WEAPONS.get(cls, set()))
    if cls in CASTER_CLASSES or (spec or "").lower() in CASTER_SPECS:
        weapons &= CASTER_WEAPONS
    else:
        weapons -= {"Wand", "Miscellaneous"}
    rows = conn.execute(
        "SELECT id, prof, tier, item, item_en, item_id, inv_type, subclass_en, ilvl FROM game_recipes "
        "WHERE exp_rank=0 AND item_id>0 AND inv_type<>''").fetchall()
    crafters: dict[str, set[str]] = {}
    # 1) recettes envoyées par l'add-on (« Mes recettes »)
    for c in conn.execute("SELECT crafter, item, item_id FROM craft_recipes").fetchall():
        for k in (f'id:{int(c["item_id"] or 0)}', f'nm:{(c["item"] or "").strip().casefold()}'):
            if k not in ("id:0", "nm:"):
                crafters.setdefault(k, set()).add((c["crafter"] or "").strip().title())
    # 2) recettes connues d'après l'API Blizzard (métiers des personnages du roster) ;
    #    + repli : membres ayant le métier au palier de l'extension, par points décroissants
    prof_members: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for pr in conn.execute("SELECT name, data FROM char_professions").fetchall():
        try:
            profs = (json.loads(pr["data"]) or {}).get("profs") or []
        except (ValueError, TypeError):
            continue
        who_name = (pr["name"] or "").strip().title()
        for p in profs:
            for rid in p.get("known") or []:
                crafters.setdefault(f"rc:{int(rid)}", set()).add(who_name)
            key = ((p.get("name_fr") or p.get("name") or ""), (p.get("tier") or ""))
            prof_members.setdefault(key, []).append((int(p.get("points") or 0), who_name))
    by_slot: dict[str, dict[int, dict]] = {}
    for r in rows:
        slot = CRAFT_INV_SLOT.get(r["inv_type"] or "")
        sub = r["subclass_en"] or ""
        if not slot or sub == "Cosmetic":
            continue
        if slot in CRAFT_ARMOR_SLOTS and sub != armor:
            continue
        if slot in ("main_hand", "off_hand") and sub not in weapons:
            continue
        iid = int(r["item_id"])
        names = {(r["item"] or "").strip().casefold(), (r["item_en"] or "").strip().casefold()} - {""}
        who = set(crafters.get(f"id:{iid}", set())) | crafters.get(f"rc:{int(r['id'])}", set())
        members = sorted(prof_members.get((r["prof"] or "", r["tier"] or ""), []), key=lambda m: (-m[0], m[1]))
        for n in names:
            who |= crafters.get(f"nm:{n}", set())
        cur = by_slot.setdefault(slot, {}).get(iid)
        if cur is None:
            by_slot[slot][iid] = {"id": iid, "name_fr": r["item"] or r["item_en"], "name_en": r["item_en"] or r["item"],
                                  "prof": r["prof"], "ilvl": int(r["ilvl"] or 0),
                                  "crafters": sorted(who - {""}),
                                  "prof_members": [{"name": n, "points": pts} for pts, n in members[:3] if n],
                                  "owned": iid in owned_ids, "equipped": iid in equipped_ids}
        else:
            cur["crafters"] = sorted(set(cur["crafters"]) | (who - {""}))
    out = []
    for slot in CRAFT_SLOT_ORDER:
        cands = sorted(by_slot.get(slot, {}).values(), key=lambda c: (-c["ilvl"], c["name_fr"].casefold()))
        if cands:
            out.append({"slot": slot, "slot_fr": CRAFT_SLOT_FR[slot], "items": cands[:CRAFT_MAX_PER_SLOT],
                        "more": max(0, len(cands) - CRAFT_MAX_PER_SLOT)})
    stats = []
    sim_content = next((c for c in contents if c in STUFF_CONTENTS), "raid")
    prio = (STUFF_HEAL_PRIO.get(f"{cls}/{(spec or '').lower()}") or {}).get("orders", {}).get(sim_content)
    if prio:
        stats = list(prio[:2])
    armor_fr = {"Cloth": "Tissu", "Leather": "Cuir", "Mail": "Mailles", "Plate": "Plaques"}.get(armor, armor)
    return {"slots": out, "armor": armor, "armor_fr": armor_fr, "stats": stats,
            "stats_content": sim_content if stats else ""}


def _stuff_bis_list(cls: str, spec: str) -> dict | None:
    """Bloc BIS d'une spécialisation depuis l'instantané embarqué."""
    try:
        mtime = BIS_FILE.stat().st_mtime
    except OSError:
        return None
    if BIS_CACHE["mtime"] != mtime:
        try:
            BIS_CACHE["data"] = json.loads(BIS_FILE.read_text(encoding="utf-8"))
            BIS_CACHE["mtime"] = mtime
        except Exception:  # noqa: BLE001
            return None
    return ((BIS_CACHE["data"].get("specs") or {}).get(f"{cls}/{spec}"))


def _stuff_wh_version(iid: int) -> dict | None:
    """Version maximum publiée par Wowhead pour une pièce : {ilvl, track, rank, label}."""
    try:
        r = httpx.get(WH_TOOLTIP.format(iid=int(iid)), timeout=20,
                      headers={"User-Agent": "Cohors-Stuff/1.0"})
        r.raise_for_status()
        tip = (r.json() or {}).get("tooltip") or ""
    except Exception:  # noqa: BLE001
        return None
    mi = WH_ILVL_RE.search(tip)
    if not mi:
        return None
    out = {"ilvl": int(mi.group(1)), "track": "", "rank": 0, "label": ""}
    mu = WH_UP_RE.search(tip)
    if mu:
        out["track"] = mu.group(1).strip()
        out["rank"] = int(mu.group(2))
        out["label"] = f'{out["track"]} {mu.group(2)}/{mu.group(3)}'
    return out


def _stuff_max_levels(items: list[dict], conn) -> tuple[int, list[str]]:
    """Complète chaque pièce avec son niveau maximum (cache en base). Renvoie (nb modifiés, exemples)."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS item_max_ilvl (
               item_id   INTEGER PRIMARY KEY,
               ilvl      INTEGER NOT NULL,
               label     TEXT NOT NULL DEFAULT '',
               fetched   REAL NOT NULL DEFAULT 0)""")
    ids = sorted({int(re.search(r"id=(\d+)", it.get("opts") or "").group(1))
                  for it in items if re.search(r"id=(\d+)", it.get("opts") or "")})
    known = {r["item_id"]: {"ilvl": r["ilvl"], "label": r["label"]}
             for r in conn.execute("SELECT * FROM item_max_ilvl").fetchall()}
    missing = [i for i in ids if i not in known]
    if missing:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(_stuff_wh_version, missing))
        for iid, res in zip(missing, results):
            if res:
                conn.execute(
                    "INSERT OR REPLACE INTO item_max_ilvl (item_id, ilvl, label, fetched) VALUES (?,?,?,?)",
                    (iid, res["ilvl"], res["label"], time.time()))
                known[iid] = {"ilvl": res["ilvl"], "label": res["label"]}
        conn.commit()
    changed, examples = 0, []
    for it in items:
        m = re.search(r"id=(\d+)", it.get("opts") or "")
        if not m:
            continue
        info = known.get(int(m.group(1)))
        it["max_ilvl"] = (info or {}).get("ilvl")
        it["max_label"] = (info or {}).get("label") or ""
        cur = int(it.get("ilvl") or 0)
        if it["max_ilvl"] and it["max_ilvl"] > cur:
            it["was_ilvl"] = cur
            it["ilvl"] = it["max_ilvl"]
            it["opts"] = f'{it["opts"]},ilevel={it["max_ilvl"]}'
            changed += 1
            if len(examples) < 3:
                examples.append(f'{it.get("name") or "?"} {cur}→{it["max_ilvl"]}')
    return changed, examples


def _stuff_parse_export(txt: str) -> dict:
    """Analyse un export SimC : classe/spé, builds sauvegardés, équipé, pièces des sacs."""
    out = {"cls": "", "name": "", "spec": "", "level": 0, "loadouts": [],
           "equipped": [], "bags": [], "server": "", "talents": ""}
    pending = None
    cur_loadout = None
    in_bags = False
    for ln in txt.replace("\r\n", "\n").splitlines():
        if not out["cls"]:
            m = re.match(r'^([a-z_]+)="([^"]*)"\s*$', ln)
            if m:
                out["cls"], out["name"] = m.group(1), m.group(2)
                continue
        if ln.strip().startswith("### Gear from Bags"):
            in_bags = True
            continue
        if not out["spec"]:
            m = re.match(r"^spec=(\S+)", ln)
            if m:
                out["spec"] = m.group(1)
                continue
        if not out["level"]:
            m = re.match(r"^level=(\d+)", ln)
            if m:
                out["level"] = int(m.group(1))
                continue
        if not out["server"]:
            m = re.match(r"^server=(\S+)", ln)
            if m:
                out["server"] = m.group(1)
                continue
        if not out["talents"]:
            m = re.match(r"^talents=(\S+)", ln)
            if m:
                out["talents"] = m.group(1)
                continue
        m = re.match(r"^#\s*Saved Loadout:\s*(.+?)\s*$", ln)
        if m:
            cur_loadout = m.group(1)
            continue
        m = re.match(r"^#\s*talents=(\S+)\s*$", ln)
        if m:
            if cur_loadout:
                out["loadouts"].append({"name": cur_loadout, "talents": m.group(1)})
                cur_loadout = None
            continue
        m = re.match(r"^#\s*(.+?)\s*\((\d+)\)\s*$", ln)
        if m:
            pending = {"name": m.group(1), "ilvl": int(m.group(2))}
            continue
        m = re.match(r"^(#\s*)?(" + "|".join(STUFF_SLOTS) + r")\s*=\s*(.+?)\s*$", ln)
        if m:
            item = {"slot": m.group(2), "opts": m.group(3),
                    "name": (pending or {}).get("name") or "", "ilvl": (pending or {}).get("ilvl") or 0}
            dest = out["bags"] if (in_bags or m.group(1)) else out["equipped"]
            dest.append(item)
            pending = None
    return out


def _stuff_gear_stats(json_path: str | None) -> dict:
    """Stats des pièces depuis le JSON d'une sim d'acteurs (un acteur par pièce)."""
    if not json_path:
        return {}
    try:
        data = json.loads(Path(json_path).read_text())
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, dict] = {}
    for p in (data.get("sim", {}).get("players") or []):
        nm = str(p.get("name") or "")
        if not re.match(r"^C\d+$", nm):
            continue
        for _slot, it in (p.get("gear") or {}).items():
            if it.get("name"):
                out[nm] = {"ilvl": it.get("ilevel"), "int": it.get("intellect") or it.get("agiint"),
                           "crit": it.get("crit_rating"), "haste": it.get("haste_rating"),
                           "mast": it.get("mastery_rating"), "vers": it.get("versatility_rating"),
                           "stam": it.get("stamina")}
    return out


def _stuff_fetch_stats(items: list[dict], cls: str, spec: str, workdir: Path) -> tuple[dict, int, list]:
    """Stats de chaque pièce via des acteurs SimC, par lots.

    Retire les pièces que le personnage ne peut pas porter (le moteur répond
    « Invalid type ») et isole par dichotomie celles qui font planter le moteur.
    Renvoie (stats par acteur, nb de pièces écartées, pièces qui plantent).
    """
    for k, c in enumerate(items, 1):
        c["actor"] = f"C{k:03d}"
    stats: dict[str, dict] = {}
    dropped = 0
    crashed: list[dict] = []
    queue = [list(items)]
    guard = 0
    while queue and guard < 80:
        guard += 1
        batch = queue.pop(0)
        if not batch:
            continue
        lines = []
        for c in batch:
            lines += [f'{cls or "shaman"}="{c["actor"]}"', f'level={c.get("level") or 90}',
                      "region=eu", f'spec={spec or "restoration"}', f'{c["slot"]}={c["opts"]}', ""]
        profile = workdir / "stats.simc"
        profile.write_text("\n".join(lines))
        # threads=1 : une seule itération, rien à paralléliser — et évite le plantage des
        # objets comme le Réceptacle rituel sur l'image Alpine (pile des threads trop petite).
        res = run_sim(profile_path=profile, iterations=1, outdir=workdir, extra=["max_time=1", "threads=1"], timeout=300)
        got = _stuff_gear_stats(res.get("json"))
        if got:
            stats.update(got)
            continue
        log = res.get("log_tail") or ""
        bad = set(re.findall(r"Player '(C\d+)'", log))
        if bad:
            keep = [c for c in batch if c["actor"] not in bad]
            dropped += len(batch) - len(keep)
            if keep:
                queue.append(keep)
            continue
        if res.get("rc") == 139:
            if len(batch) > 1:
                mid = len(batch) // 2
                queue += [batch[:mid], batch[mid:]]
            else:
                crashed.append(batch[0])
            continue
        dropped += len(batch)
    return stats, dropped, crashed


def _stuff_score(item: dict, order: list[str]) -> tuple:
    """Clé de classement : niveau d'objet d'abord, puis stats dans l'ordre de priorité."""
    st = item.get("stats") or {}
    return (int(item.get("ilvl") or 0),) + tuple(int(st.get(k) or 0) for k in order)


def _stuff_item_public(item: dict) -> dict:
    st = item.get("stats") or {}
    return {"name": item.get("name") or "?", "ilvl": item.get("ilvl"),
            "int": st.get("int"), "crit": st.get("crit"), "haste": st.get("haste"),
            "mast": st.get("mast"), "vers": st.get("vers")}


def _stuff_rank_heal(parsed: dict, items: list[dict], prio: dict, content: str) -> dict:
    """Classement des pièces pour une spé de soin (niveau d'objet puis priorité des stats)."""
    orders = prio.get("orders") or {}
    order = orders.get(content) or orders.get("raid") or []
    by_slot_cur: dict[str, dict] = {}
    by_slot_bags: dict[str, list[dict]] = {}
    for it in items:
        if not it.get("stats"):
            continue
        if it.get("equipped"):
            by_slot_cur[it["slot"]] = it
        else:
            by_slot_bags.setdefault(it["slot"], []).append(it)
    slots = []
    for slot in STUFF_SLOTS:
        cur = by_slot_cur.get(slot)
        cands = by_slot_bags.get(slot) or []
        if not cur and not cands:
            continue
        best = None
        if cands:
            best = max(cands, key=lambda i: _stuff_score(i, order))
        entry = {"slot": slot,
                 "current": _stuff_item_public(cur) if cur else None,
                 "best": _stuff_item_public(best) if best else None,
                 "swap": False, "reason": None}
        if best is not None:
            if cur is None:
                entry["swap"] = True
                entry["reason"] = {"kind": "empty"}
            else:
                sb, sc = _stuff_score(best, order), _stuff_score(cur, order)
                if sb > sc:
                    entry["swap"] = True
                    if int(best.get("ilvl") or 0) != int(cur.get("ilvl") or 0):
                        entry["reason"] = {"kind": "ilvl",
                                           "diff": int(best.get("ilvl") or 0) - int(cur.get("ilvl") or 0)}
                    else:
                        bs, cs = best.get("stats") or {}, cur.get("stats") or {}
                        stat = next((k for k in order if int(bs.get(k) or 0) != int(cs.get(k) or 0)), None)
                        entry["reason"] = {"kind": "stats", "stat": stat} if stat else {"kind": "tie"}
        slots.append(entry)
    return {"mode": "heal", "content": content, "priority": order,
            "source": prio.get("source"), "source_fr": prio.get("source_fr"),
            "slots": slots}


def _stuff_sim_input(parsed: dict, items: list[dict], loadout: dict | None) -> str:
    """Profil pour les spés DPS : export nettoyé + talents du build + une profileset par pièce."""
    lines = [ln for ln in parsed.get("_raw", "").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    if loadout and loadout.get("talents"):
        lines = [ln for ln in lines if not ln.startswith("talents=")]
        lines.insert(1, "talents=" + loadout["talents"])
    cands = [it for it in items if not it.get("equipped") and it.get("stats")]
    cands.sort(key=lambda i: -int(i.get("ilvl") or 0))
    cands = cands[:STUFF_MAX_CANDIDATES]
    ps = [f'profileset."{(it.get("name") or "?")[:40]} [{it["slot"]}]"={it["slot"]}={it["opts"]}'
          for it in cands]
    return "\n".join(lines) + "\n\n" + "\n".join(ps) + "\n", len(cands)


def _stuff_plausible(items: list, equipped_by_slot: dict) -> list:
    """Ne garde que les pièces qui peuvent battre (ou presque) l'équipé en niveau d'objet."""
    out = []
    for it in items:
        if it.get("equipped") or not it.get("stats"):
            continue
        cur = equipped_by_slot.get(it["slot"])
        ilvl = int(it.get("ilvl") or 0)
        if cur is None or ilvl >= int(cur.get("ilvl") or 0) - 6:
            out.append(it)
    out.sort(key=lambda i: -int(i.get("ilvl") or 0))
    return out[:STUFF_MAX_CANDIDATES]

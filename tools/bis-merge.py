#!/usr/bin/env python3
"""Merge per-spec BiS lists into app/data/bis.json and verify every item id.

One JSON file per spec in the input directory (default: `./bis-in`, override with `BIS_IN`):

    {
      "cls": "mage",
      "spec": "fire",
      "source_url": "https://www.wowhead.com/fr/guide/classes/mage/fire/bis-gear",
      "source_label_fr": "Wowhead (fr) — guide BiS Feu",
      "updated_fr": "2026-09-19",
      "slots": [{"slot": "head", "id": 271474, "src_fr": "…", "src_en": "…"}, …]
    }

Run:

    python3 tools/bis-merge.py

Every item id is then resolved through the Battle.net API inside a running app container
(`COHORS_CONTAINER`, default `cohors-app`) so names and inventory types match what the app
shows — target `0 anomaly(ies)`. Without a reachable container the merge still writes the
file and reports the check as skipped. Full procedure: `tools/refresh-bis.md`.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIS = pathlib.Path(os.environ.get("BIS_FILE", ROOT / "app" / "data" / "bis.json"))
INDIR = pathlib.Path(os.environ.get("BIS_IN", ROOT / "bis-in"))
CONTAINER = os.environ.get("COHORS_CONTAINER", "cohors-app")

SLOTS_OK = ["head", "neck", "shoulder", "back", "chest", "wrist", "hands", "waist",
            "legs", "feet", "finger1", "finger2", "trinket1", "trinket2", "main_hand", "off_hand"]
# Tolerances for Blizzard's inventory types (checked 2026-09-19 — 40 specs, 0 anomaly).
OK_TYPES = {
    "head": {"HEAD"}, "neck": {"NECK"}, "shoulder": {"SHOULDER"}, "back": {"BACK", "CLOAK"},
    "chest": {"CHEST", "ROBE"}, "wrist": {"WRIST"}, "hands": {"HANDS", "HAND"}, "waist": {"WAIST"},
    "legs": {"LEGS"}, "feet": {"FEET"}, "finger1": {"FINGER"}, "finger2": {"FINGER"},
    "trinket1": {"TRINKET"}, "trinket2": {"TRINKET"},
    "main_hand": {"WEAPON", "TWOHWEAPON", "WEAPONMAINHAND", "TWOHAND", "RANGED", "RANGEDRIGHT"},
    "off_hand": {"SHIELD", "OFFHAND", "HOLDABLE", "WEAPONOFFHAND", "WEAPON", "WEAPONMAINHAND", "TWOHWEAPON"},
}
# French labels that leak into src_en when the guides are read in French → English counterpart.
FR_EN = {
    "Travail du cuir": "Leatherworking", "Travail du métal": "Blacksmithing",
    "Repos des rois": "Kings' Rest", "Allée du meurtre": "Murder Row",
    "Antre de Nalorakk": "Den of Nalorakk", "Arène de la Cicatrice du Vide": "Voidscar Arena",
    "Seigneur des maléfices Malacrass": "Hex Lord Malacrass",
    "Nek'zali l'Entortillâme": "Nek'zali the Soulcoiler",
    "Nymrissa Mande-vagues": "Nymrissa Wavebinder",
    "Souffle d'Ula'tek": "Ula'tek's Breath", "Forge": "Crafting",
}

# Runs inside the app container (keys come from its own environment).
_CHECK_SCRIPT = (
    "import json,sys\n"
    "sys.path.insert(0,'/app/app')\n"
    "import bnet\n"
    "data=json.loads(sys.argv[1])\n"
    "out={}\n"
    "for key,slots in data.items():\n"
    "  rows=[]\n"
    "  for s in slots:\n"
    "    try:\n"
    "      it=bnet.item(int(s['id']), locale='fr_FR')\n"
    "      rows.append([s['slot'], s['id'], it.get('name'), it.get('inv_type')])\n"
    "    except Exception as e:\n"
    "      rows.append([s['slot'], s['id'], 'ERREUR', str(e)[:60]])\n"
    "  out[key]=rows\n"
    "print(json.dumps(out, ensure_ascii=False))\n"
)


def _container_check(to_check: dict) -> dict | None:
    """Resolve item ids through the app container; None if it is unreachable."""
    tmp = pathlib.Path(tempfile.mkdtemp()) / "_bis_check.py"
    tmp.write_text(_CHECK_SCRIPT, encoding="utf-8")
    try:
        subprocess.run(["docker", "cp", str(tmp), f"{CONTAINER}:/tmp/_bis_check.py"],
                       check=True, capture_output=True)
        r = subprocess.run(["docker", "exec", CONTAINER, "python3", "/tmp/_bis_check.py",
                            json.dumps(to_check)], capture_output=True, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"!! item check skipped ({CONTAINER} unreachable): {e}")
        return None
    if r.returncode != 0:
        print("!! item check failed in container:", r.stderr[:400])
        return None
    return json.loads(r.stdout)


def main() -> int:
    if not INDIR.is_dir():
        print(f"nothing to merge: {INDIR} does not exist (see tools/refresh-bis.md)")
        return 1
    data = json.loads(BIS.read_text(encoding="utf-8"))
    specs = data.setdefault("specs", {})
    added, dropped = [], []
    for f in sorted(INDIR.glob("*.json")):
        if f.name.startswith("_"):
            continue
        blk = json.loads(f.read_text(encoding="utf-8"))
        key = f"{blk['cls']}/{blk['spec']}"
        slots, seen = [], set()
        for s in blk.get("slots") or []:
            sl = s.get("slot")
            if sl not in SLOTS_OK:
                dropped.append(f"{key}:{sl}:{s.get('id')}")
                continue
            if sl in seen:
                dropped.append(f"{key}:DUPLICATE:{sl}")
                continue
            seen.add(sl)
            en = s.get("src_en") or ""
            for fr, e in FR_EN.items():
                en = en.replace(fr, e)
            slots.append({"slot": sl, "id": int(s["id"]), "src_fr": s.get("src_fr", ""), "src_en": en})
        if not slots:
            dropped.append(f"{key}:EMPTY")
            continue
        specs[key] = {"source_url": blk.get("source_url", ""),
                      "source_label_fr": blk.get("source_label_fr", ""),
                      "updated_fr": blk.get("updated_fr", ""), "slots": slots}
        added.append(f"{key}({len(slots)})")

    if added:
        data["snapshot"] = __import__("datetime").date.today().isoformat()

    touched = [a.split("(")[0] for a in added]
    to_check = {k: v["slots"] for k, v in specs.items() if k in touched}
    report = _container_check(to_check) if to_check else None
    if report is None and to_check:
        data["_verify"] = "check skipped (app container unreachable)"
        bad = total = 0
    else:
        bad = 0
        total = 0
        for key, rows in (report or {}).items():
            issues = [f"{slot}:{iid}({itype})" for slot, iid, name, itype in rows
                      if itype not in OK_TYPES.get(slot, set())]
            if issues:
                bad += len(issues)
                print(f"!! {key}: {issues}")
            total += len(rows)
        if report is not None:
            data["_verify"] = f"{bad} anomalie(s) sur {total} entrées vérifiées"

    BIS.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nspecs merged: {len(added)} -> {', '.join(added) or '—'}")
    print(f"slots ignored: {len(dropped)}" + (f" ({', '.join(dropped[:8])})" if dropped else ""))
    print(f"total specs: {len(specs)} | {data.get('_verify', '?')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Diagnostic des endpoints encounters/talents pour un personnage."""
import os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from bnet import BnetClient, current_expansion

bnet = BnetClient(os.environ)
realm, name = "hyjal", "chamoisdort"

print("=== Diagnostics", realm, name, "===")

# 1. Journal expansion (courant)
try:
    jexp, _ = current_expansion()
    print(f"\n--- Journal Expansion (current) ---")
    print(f"id: {jexp.get('id')}, name: {jexp.get('name')} ({jexp.get('start_date', '')[:10]})")
except Exception as e:
    print(f"\n--- Journal Expansion: ERREUR {e}")

# 2. Specializations (expansions)
try:
    data, _ = bnet.specializations(realm, name)
    print(f"\n--- Expansions from /specializations ---")
    for e in (data or {}).get("expansions", []):
        print(f"  id={e['expansion']['id']}, name={e['expansion']['name']}")
except Exception as e:
    print(f"\n--- Specializations: ERREUR {e}")

# 3. Encounters raids
try:
    data, _ = bnet.raid_progress(realm, name)
    print(f"\n--- Expansions from /encounters/raids ---")
    for e in (data or {}).get("expansions", []):
        print(f"  id={e['expansion']['id']}, name={e['expansion']['name']}")
    print(f"\n--- Raid Progression (current expansion) ---")
    for inst in (data or {}).get("instances", [])[:2]:
        print(f"  {inst['name']}: {inst.get('progress', {})}")
except Exception as e:
    print(f"\n--- Raid Progression: ERREUR {e}")

# 4. Encounters dungeons
try:
    data, _ = bnet.dungeon_progress(realm, name)
    print(f"\n--- Expansions from /encounters/dungeons ---")
    for e in (data or {}).get("expansions", []):
        print(f"  id={e['expansion']['id']}, name={e['expansion']['name']}")
    print(f"\n--- Dungeon Progression (current expansion) ---")
    print(f"  {len((data or {}).get('instances', []))} donjons")
except Exception as e:
    print(f"\n--- Dungeon Progression: ERREUR {e}")

# 5. Talents détails
try:
    data, _ = bnet.talents(realm, name)
    print(f"\n--- Talents (extraits) ---")
    print(f"  active_spec: {data.get('active_spec')} (id={data.get('active_spec_id')})")
    print(f"  hero_tree: {data.get('hero_tree')}")
    lo = data.get("loadouts", [])
    print(f"  loadouts: {len(lo)} loadouts trouvés")
    if lo:
        l = lo[0]
        print(f"    spec={l.get('spec')}, code={l.get('code')[:20]}...")
except Exception as e:
    print(f"\n--- Talents: ERREUR {e}")

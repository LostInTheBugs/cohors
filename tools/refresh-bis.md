# Refreshing the embedded BiS lists

`app/data/bis.json` is a **dated snapshot** of per-spec Best-in-Slot guides (Wowhead FR, cross-checked
against Icy Veins / Archon during collection). It is embedded because the application server cannot
fetch guide pages itself — Cloudflare answers `403` from the containers — so the snapshot is refreshed
from a machine that can open the guides (browser tooling / `web_extract`).

## File layout

- `snapshot` — date of the last refresh (stamped by the merge script);
- `_verify` — outcome of the last automated item check;
- `specs["<class>/<spec>"]` — `source_url`, `source_label_fr`, `updated_fr` (the guide's own
  "updated on" date, shown in the app on the *Stuff conseillé* page), and the 16 `slots`
  (`head` … `off_hand`), each with the item id and its source (boss, crafting…).

## Procedure

1. For each spec, open the guide:
   `https://www.wowhead.com/fr/guide/classes/<class>/<spec>/bis-gear`
   and read the "Best in Slot Gear" table (columns `Slot | Item | Source`). Note the page's
   "Actualisé" date. Slot mapping: Helm→head, Neck→neck, Shoulders→shoulder, Cape→back,
   Chest→chest, Bracers→wrist, Gloves→hands, Belt→waist, Legs→legs, Boots→feet,
   Ring→finger1 + finger2, Trinkets→trinket1 + trinket2, 1h/2h Weapon→main_hand,
   Shield / Off-hand→off_hand. Some tables carry a third trinket/weapon row — ignore it
   (the schema has exactly 16 slots).
2. Write **one JSON file per spec** (schema in the `tools/bis-merge.py` docstring) into a
   directory — `bis-in/` by default, never committed — e.g. `bis-in/mage__fire.json`.
3. Merge and verify:

   ```bash
   python3 tools/bis-merge.py
   ```

   The script merges the files into `app/data/bis.json`, stamps `snapshot` with the run date,
   then resolves **every item id** through the Battle.net API inside a running app container
   (`COHORS_CONTAINER`, default `cohors-app`, which needs the keys configured) so names and
   inventory types match what the app shows. Target: `0 anomaly(ies)`. Known tolerances
   (CLOAK for back, HAND for hands, ROBE for chest, RANGED for hunter weapons, dual-wield
   off-hands…) are listed in the script. Without a reachable container, the merge still writes
   the file and reports the check as skipped.
4. Rebuild/redeploy the app image, then check one spec end-to-end: **Stuff conseillé** →
   mode *BiS* (the method line should show the new "mise à jour" date).

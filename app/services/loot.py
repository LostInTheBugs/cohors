"""Cohors — raid/dungeon loot table (game journal) synced into item_loot, on demand and every 6 h."""
from __future__ import annotations

import time

from app import bnet
from app.core.db import _db, _db_lock


LOOT_SYNC_TTL = 7 * 86400.0
_loot_sync_state = {"state": "idle", "ts": 0.0, "error": ""}


def _loot_sync() -> None:
    """Synchronise le butin des raids et donjons (journal de jeu) dans item_loot."""
    st = _loot_sync_state
    if st["state"] == "running":
        return
    st.update({"state": "running", "error": ""})
    try:
        rows = bnet.journal_loot()
        now = time.time()
        with _db_lock, _db() as conn:
            conn.execute("DELETE FROM item_loot")
            conn.executemany(
                "INSERT INTO item_loot (item_id, kind, inst_fr, inst_en, boss_fr, boss_en, name_fr, name_en, updated) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [(r["item_id"], r["kind"], r["inst_fr"], r["inst_en"], r["boss_fr"], r["boss_en"],
                  r.get("name_fr") or "", r.get("name_en") or "", now)
                 for r in rows])
        st.update({"state": "idle", "ts": now})
        print(f"[loot] butin synchronisé : {len(rows)} ligne(s)", flush=True)
    except Exception as exc:  # noqa: BLE001
        st.update({"state": "idle", "error": str(exc)})
        print(f"[loot] synchro : {exc}", flush=True)


def _loot_loop() -> None:
    """Au démarrage puis toutes les 6 h : synchro du butin si vide ou trop ancien."""
    time.sleep(70)
    while True:
        try:
            with _db_lock, _db() as conn:
                row = conn.execute("SELECT COUNT(*) AS n, MAX(updated) AS ts FROM item_loot").fetchone()
            n, ts = int(row["n"] or 0), float(row["ts"] or 0)
            if _loot_sync_state["state"] != "running" and (n == 0 or time.time() - ts > LOOT_SYNC_TTL):
                _loot_sync()
        except Exception as exc:  # noqa: BLE001
            print(f"[loot] boucle : {exc}", flush=True)
        time.sleep(6 * 3600)

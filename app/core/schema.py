"""Cohors — SQLite schema creation and idempotent migrations (run at startup and after a restore)."""
from __future__ import annotations

import time

from app import secretbox
from app.core.auth import _session_key
from app.core.db import _db, _db_lock


def _init_db() -> None:
    with _db_lock, _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sims (
                id            TEXT PRIMARY KEY,
                created       REAL NOT NULL,
                ip            TEXT NOT NULL,
                label         TEXT NOT NULL DEFAULT '',
                iterations    INTEGER NOT NULL,
                status        TEXT NOT NULL,
                error         TEXT,
                input_hash    TEXT NOT NULL,
                input_file    TEXT NOT NULL,
                cached_from   TEXT,
                dps           REAL,
                dps_error_pct REAL,
                wall_s        REAL,
                report_html   TEXT,
                report_json   TEXT,
                started       REAL,
                finished      REAL,
                user_email    TEXT,
                user_name     TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                email      TEXT UNIQUE NOT NULL,
                name       TEXT NOT NULL DEFAULT '',
                pwd        TEXT NOT NULL,
                is_admin   INTEGER NOT NULL DEFAULT 0,
                role       TEXT NOT NULL DEFAULT 'member',
                lang       TEXT NOT NULL DEFAULT '',
                active     INTEGER NOT NULL DEFAULT 1,
                created    REAL NOT NULL,
                last_login REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token     TEXT PRIMARY KEY,
                user_id   INTEGER NOT NULL,
                created   REAL NOT NULL,
                last_seen REAL NOT NULL,
                expires   REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS invites (
                token   TEXT PRIMARY KEY,
                email   TEXT,
                note    TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL,
                expires REAL NOT NULL,
                used    REAL,
                used_by INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email TEXT NOT NULL,
                user_name  TEXT NOT NULL,
                name       TEXT NOT NULL,
                input      TEXT NOT NULL,
                shared     INTEGER NOT NULL DEFAULT 0,
                created    REAL NOT NULL,
                updated    REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_config (
                key     TEXT PRIMARY KEY,
                value   TEXT NOT NULL DEFAULT '',
                updated REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mail_config (
                key     TEXT PRIMARY KEY,
                value   TEXT NOT NULL DEFAULT '',
                updated REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_config (
                key     TEXT PRIMARY KEY,
                value   TEXT NOT NULL DEFAULT '',
                updated REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_status (
                slug     TEXT PRIMARY KEY,
                last_run REAL NOT NULL DEFAULT 0,
                detail   TEXT NOT NULL DEFAULT '',
                error    TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                provider      TEXT PRIMARY KEY,
                client_id     TEXT NOT NULL DEFAULT '',
                client_secret TEXT NOT NULL DEFAULT '',
                updated       REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_config (
                id             INTEGER PRIMARY KEY CHECK (id = 1),
                enabled        INTEGER NOT NULL DEFAULT 0,
                token          TEXT NOT NULL DEFAULT '',
                app_id         TEXT NOT NULL DEFAULT '',
                channel_id     TEXT NOT NULL DEFAULT '',
                channel_name   TEXT NOT NULL DEFAULT '',
                notify_reports INTEGER NOT NULL DEFAULT 1,
                notify_roster  INTEGER NOT NULL DEFAULT 1,
                last_report_t  REAL NOT NULL DEFAULT 0,
                roster_snap    TEXT NOT NULL DEFAULT '[]',
                last_message   TEXT NOT NULL DEFAULT '',
                last_error     TEXT NOT NULL DEFAULT '',
                updated        REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("INSERT OR IGNORE INTO bot_config (id, updated) VALUES (1, 0)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS char_links (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email TEXT NOT NULL,
                realm      TEXT NOT NULL,
                name       TEXT NOT NULL,
                display    TEXT NOT NULL,
                is_main    INTEGER NOT NULL DEFAULT 0,
                created    REAL NOT NULL
            )
            """
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_char_links_uniq ON char_links(user_email, realm, name)")
        # v2026.09.018 — UN SEUL « main » par compte : normalise les doublons éventuels
        # (on garde le plus récent) puis verrouille par index partiel unique.
        rows = conn.execute(
            "SELECT user_email, id FROM char_links WHERE is_main=1 ORDER BY created DESC, id DESC"
        ).fetchall()
        seen_main: set = set()
        for r in rows:
            if r["user_email"] in seen_main:
                conn.execute("UPDATE char_links SET is_main=0 WHERE id=?", (r["id"],))
            else:
                seen_main.add(r["user_email"])
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_char_links_one_main ON char_links(user_email) WHERE is_main=1"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wishlist (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email TEXT NOT NULL,
                item_id    INTEGER NOT NULL,
                name       TEXT NOT NULL,
                slot       TEXT NOT NULL DEFAULT '',
                inv_type   TEXT NOT NULL DEFAULT '',
                quality    TEXT NOT NULL DEFAULT 'COMMON',
                icon       TEXT,
                added      REAL NOT NULL
            )
            """
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_wishlist_uniq ON wishlist(user_email, item_id)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_events (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                kind    TEXT NOT NULL,
                member  TEXT NOT NULL,
                created REAL NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_guild_events_created ON guild_events(created)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS raids (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                title        TEXT NOT NULL DEFAULT '',
                starts       REAL NOT NULL,
                duration_min INTEGER NOT NULL DEFAULT 180,
                note         TEXT NOT NULL DEFAULT '',
                created_by   TEXT NOT NULL DEFAULT '',
                created      REAL NOT NULL,
                announced    INTEGER NOT NULL DEFAULT 0,
                reminded     INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_raids_starts ON raids(starts)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS raid_signups (
                raid_id    INTEGER NOT NULL,
                user_email TEXT NOT NULL,
                status     TEXT NOT NULL,
                updated    REAL NOT NULL,
                PRIMARY KEY (raid_id, user_email)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_info (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL DEFAULT '',
                updated    REAL NOT NULL DEFAULT 0,
                updated_by TEXT NOT NULL DEFAULT ''
            )
            """
        )
        for k in ("intro", "discord_url", "discord_note", "ts_host", "ts_password", "ts_note", "web_url", "web_note"):
            conn.execute("INSERT OR IGNORE INTO guild_info (key, value, updated) VALUES (?, '', 0)", (k,))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS char_snapshots (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                realm  TEXT NOT NULL,
                name   TEXT NOT NULL,
                day    TEXT NOT NULL,
                ts     REAL NOT NULL,
                data   TEXT NOT NULL,
                UNIQUE (realm, name, day)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_char_snapshots_lookup ON char_snapshots(realm, name, day)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS char_professions (
                realm TEXT NOT NULL,
                name  TEXT NOT NULL,
                ts    REAL NOT NULL,
                data  TEXT NOT NULL,
                PRIMARY KEY (realm, name)
            )
            """
        )
        # v2026.09.015 — rôles (membre / officier / administrateur).
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "role" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'member'")
        conn.execute("UPDATE users SET role='admin' WHERE is_admin=1 AND role != 'admin'")
        if "lang" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN lang TEXT NOT NULL DEFAULT ''")
        if "voice_nick" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN voice_nick TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sims_status ON sims(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sims_hash ON sims(input_hash)")
        # migrations (idempotent)
        for table, col in (("sims", "user_email"), ("sims", "user_name"),
                           ("sims", "kind"), ("sims", "weights"), ("sims", "gear"),
                           ("sims", "plan")):
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
        # v2026.09.062 — alertes « paliers » + récap hebdo du bot.
        bcols = [r["name"] for r in conn.execute("PRAGMA table_info(bot_config)").fetchall()]
        for bcol in ("notify_chars", "notify_weekly"):
            if bcol not in bcols:
                conn.execute(f"ALTER TABLE bot_config ADD COLUMN {bcol} INTEGER NOT NULL DEFAULT 1")
        # v2026.09.109 — wishlist : pièces prioritaires (⭐) + BIS déduits des sims « Top Stuff ».
        wcols = [r["name"] for r in conn.execute("PRAGMA table_info(wishlist)").fetchall()]
        if "prio" not in wcols:
            conn.execute("ALTER TABLE wishlist ADD COLUMN prio INTEGER NOT NULL DEFAULT 0")
        # v2026.09.150 — wishlist : recettes de métier (kind='recipe') en plus des pièces d'équipement.
        if "kind" not in wcols:
            conn.execute("ALTER TABLE wishlist ADD COLUMN kind TEXT NOT NULL DEFAULT 'item'")
        # v2026.09.151 — mises à jour de l'application : réglages + état de la dernière vérification.
        conn.execute("CREATE TABLE IF NOT EXISTS update_config (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '', updated REAL NOT NULL DEFAULT 0)")
        # v2026.09.111 — identité de la guilde (logo, nom, fond) pour réutiliser le site avec une autre guilde.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS branding ("
            " id INTEGER PRIMARY KEY CHECK (id = 1),"
            " guild_name TEXT NOT NULL DEFAULT '',"
            " guild_short TEXT NOT NULL DEFAULT '',"
            " logo TEXT NOT NULL DEFAULT '',"
            " bg TEXT NOT NULL DEFAULT '',"
            " bg_color TEXT NOT NULL DEFAULT '',"
            " updated REAL NOT NULL DEFAULT 0,"
            " updated_by TEXT NOT NULL DEFAULT '')")
        if "last_recap" not in bcols:
            conn.execute("ALTER TABLE bot_config ADD COLUMN last_recap REAL NOT NULL DEFAULT 0")
        # v2026.10.001 — hash session tokens (SHA-256) in place.
        scols = [r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
        if "hashed" not in scols:
            conn.execute("ALTER TABLE sessions ADD COLUMN hashed INTEGER NOT NULL DEFAULT 0")
        for row in conn.execute("SELECT token FROM sessions WHERE hashed = 0").fetchall():
            raw = row["token"]
            conn.execute("UPDATE sessions SET token=?, hashed=1 WHERE token=?", (_session_key(raw), raw))
        # v2026.09.064 — import du calendrier in-game (addon Cohors).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gcal_import (
                id     INTEGER PRIMARY KEY CHECK (id = 1),
                ts     REAL NOT NULL,
                player TEXT NOT NULL DEFAULT '',
                data   TEXT NOT NULL
            )
            """
        )
        # v2026.09.079 — préparation de raid (recettes d'objets, plan, apports des membres).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prep_recipes (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                name    TEXT NOT NULL,
                mats    TEXT NOT NULL DEFAULT '[]',
                created REAL NOT NULL DEFAULT 0,
                updated REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prep_plan (
                id         INTEGER PRIMARY KEY CHECK (id = 1),
                title      TEXT NOT NULL DEFAULT '',
                event_ts   REAL NOT NULL DEFAULT 0,
                items      TEXT NOT NULL DEFAULT '[]',
                updated    REAL NOT NULL DEFAULT 0,
                updated_by TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prep_claims (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                mat     TEXT NOT NULL,
                qty     REAL NOT NULL DEFAULT 0,
                user    TEXT NOT NULL,
                name    TEXT NOT NULL DEFAULT '',
                updated REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_prep_claims ON prep_claims(mat, user)")
        # v2026.09.093 — stock en banque de guilde par compos (préparation de raid).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prep_bank (
                mat        TEXT PRIMARY KEY,
                qty        REAL NOT NULL DEFAULT 0,
                updated    REAL NOT NULL DEFAULT 0,
                updated_by TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # v2026.09.080 — recettes des artisans (export addon /cohors recettes).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS craft_recipes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                crafter    TEXT NOT NULL,
                realm      TEXT NOT NULL DEFAULT '',
                profession TEXT NOT NULL DEFAULT '',
                item       TEXT NOT NULL,
                item_id    INTEGER NOT NULL DEFAULT 0,
                mats       TEXT NOT NULL DEFAULT '[]',
                updated    REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_craft_recipes ON craft_recipes(crafter, item)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS game_recipes (
                id       INTEGER PRIMARY KEY,
                prof     TEXT NOT NULL,
                tier     TEXT NOT NULL DEFAULT '',
                exp_rank INTEGER NOT NULL DEFAULT 0,
                item     TEXT NOT NULL,
                item_id  INTEGER NOT NULL DEFAULT 0,
                rank_no  INTEGER NOT NULL DEFAULT 1,
                mats     TEXT NOT NULL DEFAULT '[]',
                updated  REAL NOT NULL DEFAULT 0,
                item_en  TEXT NOT NULL DEFAULT '',
                tier_en  TEXT NOT NULL DEFAULT '',
                prof_en  TEXT NOT NULL DEFAULT '',
                mats_en  TEXT NOT NULL DEFAULT '[]'
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_game_recipes_prof ON game_recipes(prof)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mplus_posts (
                user    TEXT PRIMARY KEY,
                name    TEXT NOT NULL DEFAULT '',
                roles   TEXT NOT NULL DEFAULT '[]',
                slots   TEXT NOT NULL DEFAULT '[]',
                keys    TEXT NOT NULL DEFAULT '[]',
                updated REAL NOT NULL DEFAULT 0
            )
            """
        )
        # v2026.09.090 — périodes d'indisponibilité des membres (croisées avec le raid en préparation).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS unavails (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                email    TEXT NOT NULL,
                day_from TEXT NOT NULL,
                day_to   TEXT NOT NULL,
                note     TEXT NOT NULL DEFAULT '',
                created  REAL NOT NULL DEFAULT 0
            )
            """
        )
        # v2026.09.102 — visées des officiers par événement du calendrier in-game (raids/boss/départ).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gcal_meta (
                event_key  TEXT PRIMARY KEY,
                data       TEXT NOT NULL DEFAULT '{}',
                updated    REAL NOT NULL DEFAULT 0,
                updated_by TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # v2026.09.104 — butin (journal de jeu) : où trouver quoi (raids, donjons/MM+).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS item_loot (
                item_id INTEGER NOT NULL,
                kind    TEXT NOT NULL DEFAULT '',
                inst_fr TEXT NOT NULL DEFAULT '',
                inst_en TEXT NOT NULL DEFAULT '',
                boss_fr TEXT NOT NULL DEFAULT '',
                boss_en TEXT NOT NULL DEFAULT '',
                updated REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_item_loot_item ON item_loot(item_id)")
        # v2026.09.087 — alertes MM+ (clés recherchées) & notifications personnelles.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mplus_alerts (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                email     TEXT NOT NULL,
                dungeon   TEXT NOT NULL DEFAULT '',
                min_level INTEGER NOT NULL DEFAULT 2,
                created   REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notifs (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                email   TEXT NOT NULL,
                kind    TEXT NOT NULL DEFAULT '',
                data    TEXT NOT NULL DEFAULT '{}',
                seen    INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL DEFAULT 0
            )
            """
        )
        for _stmt in (
            "ALTER TABLE craft_recipes ADD COLUMN expansion TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE craft_recipes ADD COLUMN exp_rank INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE game_recipes ADD COLUMN item_en TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE game_recipes ADD COLUMN tier_en TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE game_recipes ADD COLUMN prof_en TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE game_recipes ADD COLUMN mats_en TEXT NOT NULL DEFAULT '[]'",
            # v2026.09.152-c26 — objet fabriqué retrouvé par son nom (l'API « recipe » ne le donne plus)
            "ALTER TABLE game_recipes ADD COLUMN inv_type TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE game_recipes ADD COLUMN subclass_en TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE game_recipes ADD COLUMN ilvl INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE prep_plan ADD COLUMN raids TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE prep_plan ADD COLUMN bosses TEXT NOT NULL DEFAULT '[]'",
            # v2026.10.020 — noms des objets du butin (fiche objet, recherche)
            "ALTER TABLE item_loot ADD COLUMN name_fr TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE item_loot ADD COLUMN name_en TEXT NOT NULL DEFAULT ''",
        ):
            try:
                conn.execute(_stmt)
            except Exception:
                pass
        # v2026.09.085 — données bilingues : force un re-relevé (noms EN) des métiers et recettes déjà stockés.
        for _key, _stmt in (
            ("loc_en_profs_v1", "UPDATE char_professions SET ts = 0"),
            ("loc_en_recipes_v1", "UPDATE game_recipes SET updated = 0"),
            # objets fabriqués + Couture/Joaillerie : force un re-relevé complet
            ("craft_items_v1", "UPDATE game_recipes SET updated = 0"),
            # recettes connues par personnage (API Blizzard) : re-relevé des métiers
            ("known_recipes_v1", "UPDATE char_professions SET ts = 0"),
            ("known_recipes_v2", "UPDATE char_professions SET ts = 0"),  # + palier précédent
            ("loot_names_v1", "UPDATE item_loot SET updated = 0"),  # re-synchro du butin avec les noms
        ):
            if conn.execute("SELECT value FROM meta WHERE key=?", (_key,)).fetchone() is None:
                conn.execute(_stmt)
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (_key, str(int(time.time()))))
        # v2026.10.002 — migration des secrets en clair vers enc:v1:
        # api_keys.client_secret
        for row in conn.execute("SELECT provider, client_secret FROM api_keys WHERE client_secret != '' AND client_secret NOT LIKE 'enc:v1:%'").fetchall():
            conn.execute("UPDATE api_keys SET client_secret=? WHERE provider=?", (secretbox.encrypt(row["client_secret"]), row["provider"]))
        # bot_config.token
        row = conn.execute("SELECT token FROM bot_config WHERE id=1 AND token != '' AND token NOT LIKE 'enc:v1:%'").fetchone()
        if row:
            conn.execute("UPDATE bot_config SET token=? WHERE id=1", (secretbox.encrypt(row["token"]),))
        # mail_config.value WHERE key='password'
        row = conn.execute("SELECT value FROM mail_config WHERE key='password' AND value != '' AND value NOT LIKE 'enc:v1:%'").fetchone()
        if row:
            conn.execute("UPDATE mail_config SET value=? WHERE key='password'", (secretbox.encrypt(row["value"]),))

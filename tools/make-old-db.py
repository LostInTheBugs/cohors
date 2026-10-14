"""Fabrique une base « ancienne » pour le test de migration (tests/fixtures/db-<version>.sqlite).

À lancer DEPUIS UN CHECKOUT DE L'ANCIENNE VERSION (git worktree add ../old <tag>), avec le chemin du
fichier à écrire :

    cd ../old && DATA_DIR=$(mktemp -d) python3 /chemin/vers/tools/make-old-db.py /chemin/vers/tests/fixtures/db-<tag>.sqlite

La base est créée par le _init_db de cette version, puis remplie de fausses données : quelques lignes
réalistes dans les tables principales et une ligne générique dans toutes les autres. Aucune donnée
réelle (pas de copie de lotp).
"""
import json
import os
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, os.getcwd())
import app.main as M  # noqa: E402

try:
    from app.security import hash_password
except ImportError:                       # très anciennes versions
    hash_password = M._hash_password

out = sys.argv[1]
M._init_db()
db = sqlite3.connect(str(M.DB_PATH))
db.row_factory = sqlite3.Row
now = time.time()
pwd = hash_password("test-pw-123")
tables = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def cols(t):
    return [r["name"] for r in db.execute(f"PRAGMA table_info({t})")]


def ins(t, row):
    if t not in tables:
        return
    row = {k: v for k, v in row.items() if k in cols(t)}
    db.execute(f"INSERT OR IGNORE INTO {t} ({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))


day = time.strftime("%Y-%m-%d")
ins("users", {"email": "mig-admin@test.local", "name": "MigAdmin", "pwd": pwd, "is_admin": 1, "role": "admin",
              "active": 1, "created": now, "lang": "fr"})
ins("users", {"email": "mig-member@test.local", "name": "MigMembre", "pwd": pwd, "is_admin": 0, "role": "member",
              "active": 1, "created": now, "lang": ""})
for i, (n, cls, spec) in enumerate((("migprêtre", "Prêtre", "Ombre"), ("migguerrier", "Guerrier", "Protection"))):
    ins("char_links", {"user_email": "mig-member@test.local", "realm": "hyjal", "name": n, "display": n.title(),
                       "is_main": 1 if i == 0 else 0, "created": now})
    ins("char_snapshots", {"realm": "hyjal", "name": n, "day": day, "ts": now, "data": json.dumps(
        {"class": cls, "spec": spec, "level": 90, "ilvl": 300 + i, "items": [
            {"slot": "Tête", "st": "HEAD", "ilvl": 310, "ench": False, "sock": 1, "gems": 0}]})})
ins("wishlist", {"user_email": "mig-member@test.local", "item_id": 4242, "name": "Casque migré", "slot": "Tête",
                 "quality": "EPIC", "added": now, "prio": 1, "kind": "item"})
ins("unavails", {"email": "mig-member@test.local", "day_from": day, "day_to": day, "note": "migration", "created": now})
ins("gcal_import", {"id": 1, "ts": now, "player": "MigOfficier", "data": json.dumps({"events": [
    {"id": 777, "title": "Raid migré", "ts": now + 86400, "inv": [{"n": "Migprêtre", "s": 1}]}]})})
ins("invites", {"token": "mig-token", "email": "", "note": "migration", "created": now, "expires": now + 86400})

# une ligne générique dans chaque table restée vide (types simples, JSON vide pour les colonnes de données)
for t in sorted(tables):
    if t.startswith("sqlite_") or db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]:
        continue
    row = {}
    for c in db.execute(f"PRAGMA table_info({t})"):
        name, typ, pk = c["name"], (c["type"] or "").upper(), c["pk"]
        if pk and "INT" in typ:
            continue
        if "email" in name or name in ("requester", "created_by", "updated_by", "added_by"):
            row[name] = "mig-member@test.local"
        elif name in ("data", "meta"):
            row[name] = "{}"
        elif name in ("inv", "mats", "mats_en", "items"):
            row[name] = "[]"
        elif "INT" in typ:
            row[name] = 1
        elif "REAL" in typ:
            row[name] = now
        else:
            row[name] = f"mig-{name}"
    ins(t, row)
db.commit()
db.execute("VACUUM")
db.close()
shutil.copyfile(str(M.DB_PATH), out)
ver = open("VERSION").read().strip()
print(f"{out} — base {ver}, {len(tables)} tables")

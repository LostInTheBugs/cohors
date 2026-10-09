"""Cohors — Discord bot loop: report, raid and roster announcements, roster movements, weekly recap."""
from __future__ import annotations

import json
import time
from datetime import datetime

from app import bnet, discord_bot, wcl
from app.core.brand import _brand_identity
from app.core.config import PUBLIC_BASE_URL
from app.core.db import _db, _db_lock
from app.services.bot import _bot_config, _bot_save
from app.services.jobs import _job_int, _job_status_set
from app.services.progression import _progression_data


def _weekly_recap_embed() -> dict | None:
    """Embed du récap hebdo : progressions (7 j), raids, mouvements de guilde."""
    fields: list[dict] = []
    try:
        prog = _progression_data(7)
        top = [r for r in (prog.get("rows") or []) if r.get("d_ilvl")][:5]
        if top:
            lines = [f"**{r['name']}** +{r['d_ilvl']} iLvl ({r.get('ilvl0')} → {r.get('ilvl1')})" for r in top]
            fields.append({"name": "🏆 Progressions de la semaine", "value": "\n".join(lines)[:1024]})
    except Exception as exc:  # noqa: BLE001
        print(f"[bot] récap progression: {exc}")
    nights = kills = 0
    try:
        rl, _ts = wcl.reports(limit=30)
        cutoff = time.time() - 7 * 86400
        for rep in rl.get("data") or []:
            if (rep.get("startTime") or 0) / 1000 < cutoff:
                continue
            full, _t = wcl.report_full(rep["code"])
            boss = [f for f in ((full.get("report") or {}).get("fights") or []) if f.get("encounterID")]
            if boss:
                nights += 1
                kills += sum(1 for f in boss if f.get("kill"))
        if nights:
            fields.append({"name": "⚔️ Raids", "value": f"{nights} soirée(s) · {kills} boss tué(s)", "inline": True})
    except wcl.WclError:
        pass
    try:
        with _db_lock, _db() as conn:
            ev = conn.execute(
                "SELECT kind, COUNT(*) AS c FROM guild_events WHERE created > ? GROUP BY kind",
                (time.time() - 7 * 86400,),
            ).fetchall()
        mov = {row["kind"]: row["c"] for row in ev}
        if mov.get("join") or mov.get("leave"):
            fields.append({"name": "👋 Mouvements",
                           "value": f"+{mov.get('join', 0)} / −{mov.get('leave', 0)}", "inline": True})
    except Exception:  # noqa: BLE001
        pass
    if not fields:
        return None
    return discord_bot.weekly_embed(fields, f"{PUBLIC_BASE_URL}/rankings" if PUBLIC_BASE_URL else "",
                                   _brand_identity()["guild_name"])


def _bot_tick() -> None:
    """Un passage : mouvements de roster (suivi continu) + annonces Discord (si actif)."""
    cfg = _bot_config()
    if cfg is None:
        return
    updates: dict = {}
    notes: list[str] = []
    errs: list[str] = []
    token, channel = (cfg["token"] or "").strip(), (cfg["channel_id"] or "").strip()
    bot_on = bool(cfg["enabled"]) and bool(token) and bool(channel)

    if bot_on and cfg["notify_reports"]:
        try:
            data, _ts = wcl.reports(limit=30)
            rows = data.get("data") or []
            newest = max((float(r.get("startTime") or 0.0) for r in rows), default=0.0)
            last = float(cfg["last_report_t"] or 0.0)
            if newest and last <= 0:
                updates["last_report_t"] = newest  # premier passage : référence, pas d'annonce rétroactive
            elif newest > last:
                fresh = sorted(
                    (r for r in rows if float(r.get("startTime") or 0.0) > last),
                    key=lambda r: float(r.get("startTime") or 0.0),
                )
                for r in fresh[:5]:
                    discord_bot.send(token, channel, embeds=[discord_bot.report_embed(r, _brand_identity()["guild_name"])])
                updates["last_report_t"] = newest
                notes.append(f"{min(len(fresh), 5)} annonce(s) « rapport »")
        except Exception as exc:  # noqa: BLE001
            errs.append(f"rapports — {exc}")

    # Raids planifiés : annonce à la création, rappel ~1 h avant (si le bot est actif).
    if bot_on:
        try:
            now = time.time()
            link = f"{PUBLIC_BASE_URL or ''}/calendar"
            with _db_lock, _db() as conn:
                to_announce = conn.execute(
                    "SELECT * FROM raids WHERE announced=0 AND starts > ? ORDER BY starts", (now,)
                ).fetchall()
                to_remind = conn.execute(
                    "SELECT * FROM raids WHERE announced=1 AND reminded=0 AND starts > ? AND starts <= ?",
                    (now, now + 3600),
                ).fetchall()
            for r in to_announce:
                discord_bot.send(token, channel, embeds=[discord_bot.raid_embed(dict(r), link, _brand_identity()["guild_name"])])
                with _db_lock, _db() as conn:
                    if float(r["starts"]) <= now + 3600:
                        conn.execute("UPDATE raids SET announced=1, reminded=1 WHERE id=?", (r["id"],))
                    else:
                        conn.execute("UPDATE raids SET announced=1 WHERE id=?", (r["id"],))
                notes.append("annonce « raid »")
            for r in to_remind:
                with _db_lock, _db() as conn:
                    su = conn.execute(
                        "SELECT status, COUNT(*) AS c FROM raid_signups WHERE raid_id=? GROUP BY status",
                        (r["id"],),
                    ).fetchall()
                counts = {x["status"]: x["c"] for x in su}
                discord_bot.send(token, channel, embeds=[discord_bot.raid_reminder_embed(dict(r), counts, link, _brand_identity()["guild_name"])])
                with _db_lock, _db() as conn:
                    conn.execute("UPDATE raids SET reminded=1 WHERE id=?", (r["id"],))
                notes.append("rappel « raid »")
        except Exception as exc:  # noqa: BLE001
            errs.append(f"raids — {exc}")

    # Mouvements de guilde : suivis en continu (tableau de bord), annoncés si le bot est actif.
    try:
        data, _ts = bnet.roster()
        members = {m["name"]: m for m in (data.get("members") or []) if m.get("name")}
        snap = set(json.loads(cfg["roster_snap"] or "[]"))
        if not snap:
            updates["roster_snap"] = json.dumps(sorted(members))
        else:
            added = sorted(set(members) - snap)
            gone = sorted(snap - set(members))
            if added or gone:
                now = time.time()
                with _db_lock, _db() as conn:
                    for n in added:
                        conn.execute(
                            "INSERT INTO guild_events (kind, member, created) VALUES ('join',?,?)", (n, now)
                        )
                    for n in gone:
                        conn.execute(
                            "INSERT INTO guild_events (kind, member, created) VALUES ('leave',?,?)", (n, now)
                        )
                updates["roster_snap"] = json.dumps(sorted(members))
                notes.append(f"roster : +{len(added)} / -{len(gone)}")
                if bot_on and cfg["notify_roster"]:
                    for n in added[:5]:
                        discord_bot.send(token, channel, embeds=[discord_bot.roster_embed("join", members[n], _brand_identity()["guild_name"])])
                    for n in gone[:5]:
                        discord_bot.send(token, channel, embeds=[discord_bot.roster_embed("leave", {"name": n}, _brand_identity()["guild_name"])])
    except Exception as exc:  # noqa: BLE001
        errs.append(f"roster — {exc}")

    # Récap hebdo (lundi matin, heure de Paris) — une fois par semaine si activé.
    if bot_on and cfg["notify_weekly"]:
        try:
            now = time.time()
            try:
                from zoneinfo import ZoneInfo
                local_now = datetime.now(ZoneInfo("Europe/Paris"))
            except Exception:  # noqa: BLE001
                local_now = datetime.now()
            if (local_now.weekday() == 0 and local_now.hour >= 9
                    and now - float(cfg["last_recap"] or 0) > 6 * 86400):
                emb = _weekly_recap_embed()
                if emb is not None:
                    discord_bot.send(token, channel, embeds=[emb])
                    updates["last_recap"] = now
                    notes.append("récap hebdo")
        except Exception as exc:  # noqa: BLE001
            errs.append(f"récap — {exc}")

    if updates or notes or errs or cfg["last_error"]:
        updates["last_message"] = " ; ".join(notes)[:300] if notes else (cfg["last_message"] or "")
        updates["last_error"] = " ; ".join(errs)[:300]
        _bot_save(updates)


def _bot_loop() -> None:
    time.sleep(15)
    while True:
        try:
            _bot_tick()
            _job_status_set("bot", detail="passage OK")
        except Exception as exc:  # noqa: BLE001
            print(f"[bot] tick: {exc}")
            _job_status_set("bot", error=str(exc))
        time.sleep(max(60, _job_int("bot_interval_min") * 60))

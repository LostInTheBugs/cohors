"""Calendrier in-game : import de l'export de l'addon, visées des officiers, relances Discord."""
from __future__ import annotations

import json
import re
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import bnet, discord_bot
from app.core.auth import _require_officer, _require_user, _user_locale
from app.core.brand import _brand_identity
from app.core.config import PUBLIC_BASE_URL
from app.core.db import _db, _db_lock
from app.core.util import CLASS_KEY_FR, _int_any, _lua_unescape, _snap_day
from app.services.bot import _bot_config
from app.services.raidcomp import compo_embed, raid_buffs
from app.services.wishlist import _bis_by_user

router = APIRouter()


class GcalImportRequest(BaseModel):
    payload: str = Field("", max_length=2_000_000)


def _gcal_parse(text: str) -> dict:
    """Extrait les données d'un import : JSON brut (chaîne collée) ou fichier SavedVariables (Cohors.lua)."""
    t = (text or "").strip()
    if not t:
        raise HTTPException(400, "Contenu vide.")
    raw = None
    if t.startswith("{"):
        raw = t
    else:
        ls = re.search(r'\["export"\]\s*=\s*\[(=*)\[(.*?)\]\1\]', t, re.S)
        st = re.search(r'\["export"\]\s*=\s*"((?:[^"\\]|\\.)*)"', t, re.S)
        if ls:
            raw = ls.group(2)
        elif st:
            raw = _lua_unescape(st.group(1))
        else:
            # tolérance : chaîne d'export noyée dans du texte copié avec (résumé, etc.)
            j = t.find('{"v":')
            k = t.rfind("}")
            if j >= 0 and k > j:
                raw = t[j:k + 1]
    if raw is None:
        snippet = " ".join(t[:90].split())
        raise HTTPException(400, "Format non reconnu (reçu : %d caractères — « %s… »). Copie la chaîne qui commence par "
                                 "{\"v\":1 avec le bouton « Exporter » de l'addon (Ctrl+A puis Ctrl+C), ou choisis le "
                                 "fichier WTF/Account/<compte>/SavedVariables/Cohors.lua." % (len(t), snippet))
    # tolérance : le client WoW écrit certains ids 64 bits en hexadécimal (0x1F45…), invalide en JSON strict
    raw = re.sub(r"(\s*:\s*)0x([0-9A-Fa-f]+)", r'\1"0x\2"', raw)
    try:
        data = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Données illisibles (JSON invalide) — recopie la chaîne avec « Exporter » "
                                 "(Ctrl+A puis Ctrl+C) ou importe le fichier Cohors.lua.")
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise HTTPException(400, "Données inattendues (aucun événement).")
    return data


def _gcal_key(e: dict) -> str:
    """Clé stable d'un événement du calendrier in-game (id Blizzard, sinon horodatage)."""
    try:
        if e.get("id"):
            return "id:" + str(int(e["id"]))
    except (TypeError, ValueError):
        pass
    try:
        return "ts:" + str(int(float(e.get("ts") or 0)))
    except (TypeError, ValueError):
        return "ts:0"


@router.get("/api/gcal")
def api_gcal_get(request: Request):
    """Dernier import du calendrier in-game (addon) + indispos + visées des officiers."""
    _require_user(request)
    with _db_lock, _db() as conn:
        row = conn.execute("SELECT ts, player, data FROM gcal_import WHERE id=1").fetchone()
        unavails = [dict(r) for r in conn.execute(
            "SELECT email, day_from, day_to, note FROM unavails ORDER BY day_from").fetchall()]
        links = {r["name"]: r["user_email"] for r in conn.execute(
            "SELECT user_email, name FROM char_links").fetchall()}
        unames = {(r["name"] or "").lower(): r["email"] for r in conn.execute(
            "SELECT email, name FROM users WHERE active=1").fetchall()}
        uname_by_email = {r["email"]: (r["name"] or r["email"]) for r in conn.execute(
            "SELECT email, name FROM users").fetchall()}
        metas = {r["event_key"]: dict(r) for r in conn.execute(
            "SELECT event_key, data, updated, updated_by FROM gcal_meta").fetchall()}
        wish_rows = conn.execute("SELECT item_id, name, user_email, prio FROM wishlist").fetchall()
        bis_all = _bis_by_user(conn)
        loot_rows2 = conn.execute(
            "SELECT item_id, kind, inst_fr, inst_en, boss_fr, boss_en FROM item_loot").fetchall()
        main_chars = {r["user_email"]: (r["display"] or r["name"]) for r in conn.execute(
            "SELECT user_email, display, name FROM char_links WHERE is_main=1").fetchall()}
        class_rows = conn.execute(
            "SELECT name, json_extract(data, '$.class') AS cfr, "
            "json_extract(data, '$.class_en') AS cen FROM char_snapshots "
            "WHERE id IN (SELECT MAX(id) FROM char_snapshots GROUP BY name)").fetchall()
    by_email: dict = {}
    for urow in unavails:
        by_email.setdefault(urow["email"], []).append(urow)
    if row is None:
        events = []
    else:
        try:
            data = json.loads(row["data"])
        except ValueError:
            data = {}
        events = data.get("events") or []
    loot_by_item: dict = {}
    for lr2 in loot_rows2:
        loot_by_item.setdefault(int(lr2["item_id"]), []).append(dict(lr2))
    want: dict = {}
    for wr in wish_rows:
        ent = want.setdefault(int(wr["item_id"]), {"name": wr["name"], "who": [], "prio": False})
        disp = (main_chars.get(wr["user_email"])
                or (uname_by_email.get(wr["user_email"]) or wr["user_email"]).split("@")[0])
        if disp and disp not in ent["who"]:
            ent["who"].append(disp)
        if wr["prio"] or int(wr["item_id"]) in (bis_all.get(wr["user_email"]) or {}):
            ent["prio"] = True
    en_wl = _user_locale(request).startswith("en")
    if en_wl:
        for iid2 in list(want):
            try:
                want[iid2]["name"] = (bnet.item(iid2, locale=_user_locale(request)).get("name")
                                      or want[iid2]["name"])
            except bnet.BnetError:
                pass
    status_map = {1: "ok", 3: "ok", 2: "no", 8: "maybe"}
    conflicts = 0
    for e in events:
        rows = []
        try:
            ts = float(e.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0
        key = _gcal_key(e)
        e["key"] = key
        mrow = metas.get(key)
        try:
            e["meta"] = json.loads(mrow["data"]) if mrow else None
        except (ValueError, TypeError):
            e["meta"] = None
        md2 = e.get("meta") or {}
        ev_raids = {str(x).strip().casefold() for x in (md2.get("raids") or []) if str(x).strip()}
        ev_bosses = {str(x).strip().casefold() for x in (md2.get("bosses") or []) if str(x).strip()}
        if ev_raids or ev_bosses:
            groups: dict = {}
            for iid3, went in want.items():
                for lr2 in loot_by_item.get(iid3, []):
                    in_f = str(lr2["inst_fr"] or "").strip().casefold()
                    in_e = str(lr2["inst_en"] or "").strip().casefold()
                    bo_f = str(lr2["boss_fr"] or "").strip().casefold()
                    bo_e = str(lr2["boss_en"] or "").strip().casefold()
                    if ev_raids and not (in_f in ev_raids or in_e in ev_raids):
                        continue
                    if ev_bosses and not (bo_f in ev_bosses or bo_e in ev_bosses):
                        continue
                    disp_b = None
                    for b in (md2.get("bosses") or []):
                        if str(b).strip().casefold() in (bo_f, bo_e):
                            disp_b = str(b).strip()
                            break
                    if disp_b is None:
                        disp_b = (lr2["boss_en"] if en_wl else lr2["boss_fr"]) or ""
                    g = groups.setdefault(disp_b, [])
                    ex = next((x for x in g if x["name"] == went["name"]), None)
                    if ex is not None:
                        ex["prio"] = ex["prio"] or bool(went["prio"])
                    elif len(g) < 5:
                        g.append({"name": went["name"], "who": went["who"][:3],
                                  "prio": bool(went["prio"])})
            if groups:
                e["wanted"] = [{"boss": b, "items": its} for b, its in groups.items()]
        if ts > 0:
            day = _snap_day(ts)
            ovr_map = (e.get("meta") or {}).get("ovr") or {}
            for mem in (e.get("inv") or []):
                nm = str(mem.get("n") or "").strip()
                if not nm:
                    continue
                owner = links.get(nm.lower()) or unames.get(nm.lower())
                periods = [x for x in by_email.get(owner, [])
                           if x["day_from"] <= day <= x["day_to"]] if owner else []
                if not periods:
                    continue
                st = ovr_map.get(nm) or status_map.get(mem.get("s"), "wait")
                conf = st == "ok"
                if conf:
                    conflicts += 1
                rows.append({"n": nm, "status": st, "conflict": conf,
                             "periods": [{"from": x["day_from"], "to": x["day_to"], "note": x["note"] or ""}
                                         for x in periods]})
        rows.sort(key=lambda r: (not r["conflict"], r["n"].casefold()))
        e["unav"] = rows
        e["unav_conflicts"] = sum(1 for r in rows if r["conflict"])
    today = _snap_day()
    limit = _snap_day(time.time() + 14 * 86400)
    urows = []
    for email, periods in by_email.items():
        ps = [x for x in periods if x["day_to"] >= today and x["day_from"] <= limit]
        if ps:
            urows.append({"name": uname_by_email.get(email, email),
                          "periods": [{"from": x["day_from"], "to": x["day_to"], "note": x["note"] or ""}
                                      for x in ps]})
    urows.sort(key=lambda r: r["periods"][0]["from"])
    try:
        raid_catalog, _jts = bnet.journal_raids(_user_locale(request))
    except bnet.BnetError:
        raid_catalog = {"expansion": "", "raids": []}
    en_loc = _user_locale(request).startswith("en")
    classes: dict = {}
    class_keys: dict = {}   # clé de classe indépendante de la langue (couverture des buffs de raid)
    for cr in class_rows:
        nmk = str(cr["name"] or "").strip().lower()
        if not nmk or nmk in classes:
            continue
        cname = (cr["cen"] if en_loc else cr["cfr"]) or cr["cfr"] or ""
        if cname:
            classes[nmk] = cname
        ck = CLASS_KEY_FR.get(cr["cfr"] or "")
        if ck:
            class_keys[nmk] = ck
    return {"imported_at": row["ts"] if row else 0, "player": row["player"] if row else "",
            "events": events, "raid_catalog": raid_catalog, "classes": classes, "class_keys": class_keys,
            "raid_buffs": raid_buffs(en_loc),
            "unavail": {"rows": urows, "counts": {"members": len(urows), "conflict": conflicts}}}


class GcalEventMetaRequest(BaseModel):
    raids: list[str] = []
    bosses: list[str] = []
    start: str = ""
    roles: dict[str, str] = {}
    ovr: dict[str, str] = {}


@router.post("/api/gcal/event/{key}")
def api_gcal_event_meta(key: str, body: GcalEventMetaRequest, request: Request):
    """Officiers : raids et boss visés pour une date (🚩 raid de départ inclus)."""
    user = _require_officer(request)
    key = (key or "").strip()[:48]
    if not (key.startswith("id:") or key.startswith("ts:")):
        raise HTTPException(400, "Clé d'événement invalide.")
    raids = [str(x).strip()[:80] for x in (body.raids or []) if str(x).strip()][:30]
    bosses = [str(x).strip()[:80] for x in (body.bosses or []) if str(x).strip()][:60]
    start = (body.start or "").strip()[:80]
    roles = {str(k).strip()[:60]: str(v).strip().lower() for k, v in (body.roles or {}).items()
             if str(k).strip() and str(v).strip().lower() in ("tank", "heal", "dps")}
    ovr = {str(k).strip()[:60]: str(v).strip().lower() for k, v in (body.ovr or {}).items()
           if str(k).strip() and str(v).strip().lower() in ("ok", "no")}
    roles, ovr = dict(list(roles.items())[:120]), dict(list(ovr.items())[:120])
    with _db_lock, _db() as conn:
        if not raids and not bosses and not start and not roles and not ovr:
            conn.execute("DELETE FROM gcal_meta WHERE event_key=?", (key,))
        else:
            conn.execute(
                "INSERT INTO gcal_meta (event_key, data, updated, updated_by) VALUES (?,?,?,?) "
                "ON CONFLICT(event_key) DO UPDATE SET data=excluded.data, updated=excluded.updated, "
                "updated_by=excluded.updated_by",
                (key, json.dumps({"raids": raids, "bosses": bosses, "start": start,
                                  "roles": roles, "ovr": ovr}, ensure_ascii=False),
                 time.time(), user["email"]))
    return {"ok": True, "key": key}


@router.post("/api/gcal/import")
def api_gcal_import(payload: GcalImportRequest, request: Request):
    """Importe un export du calendrier in-game (JSON collé ou fichier SavedVariables Cohors.lua)."""
    _require_officer(request)
    data = _gcal_parse(payload.payload)
    events: list[dict] = []
    responses = 0
    for e in (data.get("events") or []):
        if not isinstance(e, dict):
            continue
        inv = []
        for i in (e.get("inv") or []):
            if not isinstance(i, dict) or not i.get("n"):
                continue
            try:
                st = int(i.get("s")) if i.get("s") is not None else -1
            except (TypeError, ValueError):
                st = -1
            inv.append({"n": str(i["n"])[:60], "s": st,
                        "t": (i.get("t") if isinstance(i.get("t"), (int, float)) else None)})
        responses += len(inv)
        try:
            ts = float(e.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        events.append({
            "id": _int_any(e.get("id")),
            "title": str(e.get("title") or "?")[:200],
            "date": str(e.get("date") or "")[:20],
            "ts": ts,
            "type": _int_any(e.get("type")),
            "inv": inv,
        })
    blob = json.dumps({"events": events}, ensure_ascii=False)
    with _db_lock, _db() as conn:
        conn.execute(
            "INSERT INTO gcal_import (id, ts, player, data) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET ts=excluded.ts, player=excluded.player, data=excluded.data",
            (time.time(), str(data.get("player") or "")[:60], blob),
        )
    return {"ok": True, "events": len(events), "responses": responses}


@router.post("/api/gcal/relance/{event_id}")
def api_gcal_relance(event_id: int, request: Request):
    """Poste sur Discord une relance pour les membres sans réponse (événement in-game)."""
    _require_officer(request)
    cfg = _bot_config()
    token, channel = (cfg["token"] or "").strip(), (cfg["channel_id"] or "").strip()
    if not (cfg["enabled"] and token and channel):
        raise HTTPException(400, "Bot Discord non configuré ou inactif (Admin → Bot Discord).")
    with _db_lock, _db() as conn:
        row = conn.execute("SELECT data FROM gcal_import WHERE id=1").fetchone()
        mrow = conn.execute("SELECT data FROM gcal_meta WHERE event_key=?",
                            ("id:" + str(event_id),)).fetchone()
    if row is None:
        raise HTTPException(404, "Aucun import du calendrier in-game.")
    try:
        events = (json.loads(row["data"]) or {}).get("events") or []
    except ValueError:
        events = []
    ev = next((e for e in events if _int_any(e.get("id")) == event_id), None)
    if ev is None:
        raise HTTPException(404, "Événement introuvable dans le dernier import.")
    forced = {}
    if mrow is not None:
        try:
            forced = (json.loads(mrow["data"]) or {}).get("ovr") or {}
        except ValueError:
            forced = {}
    waiting = [i.get("n") for i in (ev.get("inv") or [])
               if int(i.get("s", -1)) not in (1, 2, 3, 8) and i.get("n") not in forced]
    if not waiting:
        raise HTTPException(400, "Tout le monde a répondu 👍")
    link = f"{PUBLIC_BASE_URL}/calendar" if PUBLIC_BASE_URL else ""
    emb = {
        "title": "⏰ Il manque des réponses",
        "description": (f"**{ev.get('title') or 'Raid'}** — {ev.get('date') or ''}\n"
                        f"En attente de réponse : **{', '.join(waiting[:40])}**")
                       + (f"\n\n👉 [Répondre sur le site]({link})" if link else ""),
        "color": discord_bot.COLOR_CRIMSON,
        "footer": {"text": f"{_brand_identity()['guild_name']} · calendrier"},
    }
    try:
        discord_bot.send(token, channel, embeds=[emb])
    except discord_bot.DiscordError as exc:
        raise HTTPException(400, f"Discord — {exc}")
    return {"ok": True, "count": len(waiting)}


@router.post("/api/gcal/post/{key}")
def api_gcal_post_compo(key: str, request: Request):
    """Publie sur Discord la composition d'une soirée : présents par rôle, buffs manquants, pièces voulues."""
    _require_officer(request)
    cfg = _bot_config() or {}
    token, channel = (cfg.get("token") or "").strip(), (cfg.get("channel_id") or "").strip()
    if not (cfg.get("enabled") and token and channel):
        raise HTTPException(400, "Bot Discord non configuré ou inactif (Admin → Bot Discord).")
    data = api_gcal_get(request)
    ev = next((e for e in data.get("events") or [] if e.get("key") == key), None)
    if ev is None:
        raise HTTPException(404, "Événement introuvable dans le dernier import.")
    link = f"{PUBLIC_BASE_URL}/calendar" if PUBLIC_BASE_URL else ""
    emb = compo_embed(ev, data.get("class_keys") or {}, _brand_identity()["guild_name"], link,
                      discord_bot.COLOR_GOLD)
    try:
        discord_bot.send(token, channel, embeds=[emb])
    except discord_bot.DiscordError as exc:
        raise HTTPException(400, f"Discord — {exc}")
    return {"ok": True}

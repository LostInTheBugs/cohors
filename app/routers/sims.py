"""Cohors — simulations: submit (DPS / weights / Top Stuff), "Stuff conseillé", list, detail, reports."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from app import bnet
from app.core.auth import _client_ip, _require_user, _user_locale
from app.core.config import (
    DEFAULT_ITERATIONS, ITER_CHOICES, MAX_INPUT_CHARS, PER_IP_ACTIVE, PER_IP_COOLDOWN_S, PER_USER_ACTIVE,
    QUEUE_MAX, REPORTS_DIR, VERSION,
)
from app.core.db import _db, _db_lock
from app.core.util import ITEM_REF_RE, _reject_blocked_profile
from app.services.stuff import (
    BIS_CONTENTS, HEAL_SPECS, STUFF_CONTENTS, STUFF_HEAL_PRIO, STUFF_ITERATIONS, STUFF_SLOTS,
    _stuff_bis_list, _stuff_parse_export,
)

router = APIRouter()


GEAR_MAX_ITEMS = 15


def _build_gear_input(profile_text: str, items_text: str, locale: str | None = None) -> tuple[str, list[str]]:
    """Ajoute les profilesets « Top Stuff » au profil — renvoie (input, avertissements)."""
    refs: list[int] = []
    seen: set[int] = set()
    for m in ITEM_REF_RE.finditer(items_text or ""):
        iid = int(m.group(1))
        if iid not in seen:
            seen.add(iid)
            refs.append(iid)
    if not refs:
        raise HTTPException(400, "Indique au moins une pièce (lien Wowhead ou identifiant).")
    warnings: list[str] = []
    if len(refs) > GEAR_MAX_ITEMS:
        warnings.append(f"{len(refs) - GEAR_MAX_ITEMS} pièce(s) ignorée(s) — maximum {GEAR_MAX_ITEMS} par comparaison.")
        refs = refs[:GEAR_MAX_ITEMS]
    lines: list[str] = []
    for iid in refs:
        try:
            it = bnet.item(iid, locale=locale)
        except bnet.BnetError as exc:
            warnings.append(f"{iid} : pièce ignorée ({exc}).")
            continue
        slots = bnet.INV_TO_SLOTS.get(it["inv_type"])
        if not slots:
            warnings.append(f"{it['name']} : emplacement non géré ({it['inv_type_fr'] or it['inv_type'] or '?'}).")
            continue
        clean = it["name"].replace('"', "'")[:48]
        for slot in slots:
            lines.append(f'profileset."{clean} · {bnet.slot_label(slot, locale)} [{slot}:{iid}]"={slot}=,id={iid}')
    if not lines:
        raise HTTPException(400, "Aucune pièce exploitable parmi celles fournies.")
    return profile_text + "\n\n" + "\n".join(lines) + "\n", warnings


class SimRequest(BaseModel):
    input: str = Field(..., min_length=30, max_length=MAX_INPUT_CHARS)
    iterations: int = DEFAULT_ITERATIONS
    label: str = ""
    kind: str = "dps"
    items: str = Field("", max_length=2000)


@router.post("/api/sim")
def submit_sim(payload: SimRequest, request: Request):
    user = _require_user(request)
    if payload.iterations not in ITER_CHOICES:
        raise HTTPException(400, f"Valeurs d'itérations acceptées : {', '.join(map(str, ITER_CHOICES))}")
    kind = payload.kind if payload.kind in ("dps", "weights", "gear") else None
    if kind is None:
        raise HTTPException(400, "Type de simulation invalide.")
    text = payload.input.replace("\r\n", "\n").strip()
    label = payload.label.strip()[:60]
    warnings: list[str] = []
    if kind == "gear":
        text, warnings = _build_gear_input(text, payload.items, locale=_user_locale(request))
    _reject_blocked_profile(text)
    ip = _client_ip(request)
    now = time.time()

    with _db_lock, _db() as conn:
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM sims WHERE ip=? AND status IN ('queued','running')", (ip,)
        ).fetchone()["c"]
        if active >= PER_IP_ACTIVE:
            raise HTTPException(429, f"Tu as déjà {active} simulation(s) en attente — patiente un peu.")
        user_active = conn.execute(
            "SELECT COUNT(*) AS c FROM sims WHERE user_email=? AND status IN ('queued','running')", (user["email"],)
        ).fetchone()["c"]
        if user_active >= PER_USER_ACTIVE:
            raise HTTPException(429, f"Tu as déjà {user_active} simulation(s) en attente — patiente un peu.")
        last_ts = conn.execute("SELECT MAX(created) AS m FROM sims WHERE ip=?", (ip,)).fetchone()["m"]
        if last_ts and now - last_ts < PER_IP_COOLDOWN_S:
            wait = int(PER_IP_COOLDOWN_S - (now - last_ts)) + 1
            raise HTTPException(429, f"Doucement ! Réessaie dans {wait} s.")
        queue_len = conn.execute("SELECT COUNT(*) AS c FROM sims WHERE status IN ('queued','running')").fetchone()["c"]
        if queue_len >= QUEUE_MAX:
            raise HTTPException(503, "La file est pleine, réessaie dans quelques minutes.")

        input_hash = hashlib.sha256(f"{kind}\n{payload.iterations}\n{text}".encode()).hexdigest()
        cached = conn.execute(
            "SELECT * FROM sims WHERE input_hash=? AND status='done' ORDER BY finished DESC LIMIT 1", (input_hash,)
        ).fetchone()

        sim_id = uuid.uuid4().hex[:20]
        sim_dir = REPORTS_DIR / sim_id
        sim_dir.mkdir(parents=True, exist_ok=True)
        input_file = sim_dir / "input.simc"
        input_file.write_text(text)

        if cached:
            conn.execute(
                """INSERT INTO sims (id, created, ip, label, iterations, status, input_hash, input_file, cached_from,
                                     dps, dps_error_pct, wall_s, report_html, report_json, started, finished,
                                     user_email, user_name, kind, weights)
                   VALUES (?,?,?,?,?, 'done', ?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sim_id, now, ip, label, payload.iterations, input_hash, str(input_file), cached["id"],
                 cached["dps"], cached["dps_error_pct"], cached["wall_s"], cached["report_html"], cached["report_json"],
                 now, now, user["email"], user["name"], kind, cached["weights"]),
            )
            return {"id": sim_id, "status": "done", "cached": True, "warnings": warnings}

        conn.execute(
            """INSERT INTO sims (id, created, ip, label, iterations, status, input_hash, input_file, user_email, user_name, kind)
               VALUES (?,?,?,?,?, 'queued', ?, ?, ?, ?, ?)""",
            (sim_id, now, ip, label, payload.iterations, input_hash, str(input_file), user["email"], user["name"], kind),
        )
        return {"id": sim_id, "status": "queued", "cached": False, "position": queue_len + 1, "warnings": warnings}


BLIZZ_LOADOUT = "Talents actuels (Blizzard)"


def _spec_token(name: str) -> str:
    """« Beast Mastery » -> « beast_mastery » (nom de spé anglais -> jeton SimC)."""
    return re.sub(r"[^a-z]+", "_", (name or "").lower()).strip("_")


def _blizz_talents_for(parsed: dict) -> str | None:
    """Code de talents en jeu (API Blizzard) pour la spé de l'export, ou None."""
    name = (parsed.get("name") or "").strip()
    realm = (parsed.get("server") or bnet.GUILD_REALM or "").strip().lower().replace("_", "-")
    if not name or not realm or not parsed.get("spec"):
        return None
    try:
        t, _ts = bnet.talents(realm, name, locale="en_US")   # noms de spé anglais = jetons SimC
    except bnet.BnetError:
        return None
    for lo in t.get("loadouts") or []:
        if _spec_token(lo.get("spec") or "") == parsed["spec"] and lo.get("code"):
            return lo["code"]
    return None


@router.get("/api/stuff/profile/{pid}")
def stuff_profile(pid: int, request: Request):
    """Résumé d'un export : classe/spé, builds détectés, nb de pièces."""
    _require_user(request)
    with _db_lock, _db() as conn:
        prof = conn.execute("SELECT id, name, input FROM profiles WHERE id=?", (pid,)).fetchone()
    if prof is None:
        raise HTTPException(404, "Profil introuvable.")
    parsed = _stuff_parse_export(prof["input"])
    blizz = _blizz_talents_for(parsed)
    loadouts = [l["name"] for l in parsed["loadouts"]] + ([BLIZZ_LOADOUT] if blizz else [])
    return {"id": prof["id"], "name": prof["name"], "cls": parsed["cls"], "spec": parsed["spec"],
            "level": parsed["level"], "loadouts": loadouts,
            "talents_changed": bool(blizz and parsed["talents"] and blizz != parsed["talents"]),
            "equipped": len(parsed["equipped"]), "bags": len(parsed["bags"]),
            "heal": parsed["spec"] in HEAL_SPECS,
            "prio_known": f'{parsed["cls"]}/{parsed["spec"]}' in STUFF_HEAL_PRIO,
            "prio_note": (STUFF_HEAL_PRIO.get(f'{parsed["cls"]}/{parsed["spec"]}') or {}).get("note_fr", "")}


class StuffRequest(BaseModel):
    profile_id: int
    loadout: str = ""
    content: str = "raid"                # raid | mplus | delves
    mode: str = "cur"                    # cur | max | bis
    bis: bool = False                    # si vrai, lance le guide BIS
    bis_content: list[str] = Field(default_factory=list, max_length=10)  # contenus filtrés quand mode=bis
    max_rank: bool = False               # compat : équivaut à mode="max" (obsolète)

    @field_validator("bis_content")
    @classmethod
    def _validate_bis_content(cls, v: list[str]) -> list[str]:
        """Valider les valeurs de bis_content : inconnu → 400, dédoublonner, max 5."""
        if not v:
            return v
        # Dédoublonner tout en préservant l'ordre
        seen: set[str] = set()
        unique: list[str] = []
        for item in v:
            if item not in seen:
                seen.add(item)
                unique.append(item)
        # Limite de taille
        if len(unique) > 5:
            raise ValueError("bis_content ne doit pas dépasser 5 valeurs.")
        # Chaque valeur doit être dans BIS_CONTENTS
        invalid = [c for c in unique if c not in BIS_CONTENTS]
        if invalid:
            raise ValueError(f"Valeur(s) inconnue(s) dans bis_content : {invalid}")
        return unique

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, v: dict) -> dict:
        """Normaliser mode/max_rank et valider mode connu."""
        if not isinstance(v, dict):
            return v
        # max_rank=True sans mode → mode="max" (avant de vérifier le mode)
        if "mode" not in v and v.get("max_rank"):
            v["mode"] = "max"
            v["max_rank"] = True
        else:
            mode = v.get("mode", "cur")
            if mode in ("cur", "max"):
                v["mode"] = mode
                v["max_rank"] = mode == "max"
            elif mode == "bis":
                v["mode"] = "bis"
                v["bis"] = True
            else:
                v["mode"] = "cur"
                v["max_rank"] = False
        return v


@router.post("/api/stuff")
def submit_stuff(payload: StuffRequest, request: Request):
    """« Stuff conseillé » : que porter, avec ce qu'on possède, pour un ou plusieurs contenus."""
    user = _require_user(request)
    with _db_lock, _db() as conn:
        prof = conn.execute("SELECT id, name, input FROM profiles WHERE id=?", (payload.profile_id,)).fetchone()
    if prof is None:
        raise HTTPException(404, "Profil introuvable.")
    parsed = _stuff_parse_export(prof["input"])
    if not parsed["bags"]:
        raise HTTPException(400, "Cet export ne contient pas les pièces des sacs — réexporte ton personnage avec l'addon (les sacs sont inclus automatiquement).")
    loadout = next((l for l in parsed["loadouts"] if l["name"] == payload.loadout), None)
    if payload.loadout == BLIZZ_LOADOUT and loadout is None:
        code = _blizz_talents_for(parsed)
        if not code:
            raise HTTPException(400, "Talents Blizzard indisponibles pour ce personnage et cette spé.")
        loadout = {"name": BLIZZ_LOADOUT, "talents": code}
    if payload.loadout and loadout is None:
        raise HTTPException(400, "Build introuvable dans cet export.")
    ip = _client_ip(request)
    now = time.time()

    # Valider les contenus
    if payload.bis:
        # BIS : validation de bis_content (liste) — réutilisée plus bas
        sim_contents = []
    else:
        # Non-BIS : payload.content est une chaîne unique
        if payload.content not in STUFF_CONTENTS:
            raise HTTPException(400, f"Contenu invalide : {payload.content}")
        sim_contents = [payload.content]

    with _db_lock, _db() as conn:
        active = conn.execute("SELECT COUNT(*) AS c FROM sims WHERE user_email=? AND status IN ('queued','running')",
                              (user["email"],)).fetchone()["c"]

    # BIS : une seule simulation combinée (tous les contenus cochés fusionnés)
    if payload.bis:
        if active >= PER_USER_ACTIVE:
            raise HTTPException(429, f"Tu as déjà {active} calcul(s) en attente — patiente un peu.")
        if _stuff_bis_list(parsed["cls"], parsed["spec"]) is None:
            raise HTTPException(400, "Liste BIS pas encore disponible pour cette spécialisation.")
        bis_contents = payload.bis_content if payload.bis_content else ["raid"]
        valid_bis = [c for c in bis_contents if c in BIS_CONTENTS]
        sim_id = uuid.uuid4().hex[:20]
        sim_dir = REPORTS_DIR / sim_id
        sim_dir.mkdir(parents=True, exist_ok=True)
        input_file = sim_dir / "input.simc"
        input_file.write_text("")
        label_parts = [f'{prof["name"]} · BIS']
        for c in valid_bis:
            label_parts.append(BIS_CONTENTS[c]["label_fr"])
        label = " / ".join(label_parts) + \
                (f' · {loadout["name"]}' if loadout else "")
        plan = {"profile_id": prof["id"], "profile_name": prof["name"], "cls": parsed["cls"],
                "spec": parsed["spec"], "loadout": loadout or {}, "content": valid_bis,
                "mode": "bis", "max_rank": False, "bis": True}
        with _db_lock, _db() as conn:
            conn.execute(
                """INSERT INTO sims (id, created, ip, label, iterations, status, input_hash, input_file,
                                     user_email, user_name, kind, plan)
                   VALUES (?,?,?,?,?, 'queued', ?, ?, ?, ?, 'stuff', ?)""",
                (sim_id, now, ip, label[:60], STUFF_ITERATIONS, f"stuff:{sim_id}", str(input_file),
                 user["email"], user["name"], json.dumps(plan)))
        return {"id": sim_id, "status": "queued", "sim_ids": [sim_id],
                "heal": parsed["spec"] in HEAL_SPECS,
                "loadouts": [l["name"] for l in parsed["loadouts"]]}

    # Non-BIS (mode cur ou max) : simuler le contenu sélectionné
    if not sim_contents:
        raise HTTPException(400, "Aucun contenu valide sélectionné.")
    sim_ids = []
    for c in sim_contents:
        with _db_lock, _db() as conn:
            active = conn.execute("SELECT COUNT(*) AS c FROM sims WHERE user_email=? AND status IN ('queued','running')",
                                  (user["email"],)).fetchone()["c"]
            if active >= PER_USER_ACTIVE:
                raise HTTPException(429, f"Tu as déjà {active} calcul(s) en attente — patiente un peu.")
            sim_id = uuid.uuid4().hex[:20]
            sim_dir = REPORTS_DIR / sim_id
            sim_dir.mkdir(parents=True, exist_ok=True)
            input_file = sim_dir / "input.simc"
            input_file.write_text("")
            plan = {"profile_id": prof["id"], "profile_name": prof["name"], "cls": parsed["cls"],
                    "spec": parsed["spec"], "loadout": loadout or {}, "content": c,
                    "mode": payload.mode, "max_rank": payload.max_rank}
            label = f'{prof["name"]} · {STUFF_CONTENTS[c]["label_fr"]}' + \
                    ("" if payload.mode != "max" else " · rang max") + \
                    (f' · {loadout["name"]}' if loadout else "")
            conn.execute(
                """INSERT INTO sims (id, created, ip, label, iterations, status, input_hash, input_file,
                                     user_email, user_name, kind, plan)
                   VALUES (?,?,?,?,?, 'queued', ?, ?, ?, ?, 'stuff', ?)""",
                (sim_id, now, ip, label[:60], STUFF_ITERATIONS, f"stuff:{sim_id}", str(input_file),
                 user["email"], user["name"], json.dumps(plan)))
            sim_ids.append(sim_id)

    return {"id": sim_ids[0] if sim_ids else None, "status": "queued", "sim_ids": sim_ids,
            "heal": parsed["spec"] in HEAL_SPECS,
            "loadouts": [l["name"] for l in parsed["loadouts"]]}


def _parse_weights_json(raw: str | None) -> list | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def _public_row(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "created": r["created"],
        "label": r["label"],
        "user_name": r["user_name"],
        "iterations": r["iterations"],
        "status": r["status"],
        "cached": bool(r["cached_from"]),
        "dps": r["dps"],
        "dps_error_pct": r["dps_error_pct"],
        "wall_s": r["wall_s"],
        "has_report": r["status"] == "done" and bool(r["report_html"]),
        "error": (r["error"] or "")[:300] if r["status"] == "failed" else None,
        "note": ((r["error"] or "")[:300] or None) if r["status"] == "done" else None,
        "kind": (r["kind"] or "dps"),
        "weights": _parse_weights_json(r["weights"]) if r["kind"] == "weights" else None,
        "gear": _parse_weights_json(r["gear"]) if r["kind"] == "gear" else None,
        "group": _parse_weights_json(r["gear"]) if r["kind"] == "group" else None,
    }


@router.get("/api/sims")
def list_sims(request: Request):
    _require_user(request)
    with _db_lock, _db() as conn:
        rows = conn.execute("SELECT * FROM sims ORDER BY created DESC LIMIT 50").fetchall()
    return {"sims": [_public_row(r) for r in rows], "version": VERSION}


@router.get("/api/sims/{sim_id}")
def get_sim(sim_id: str, request: Request):
    _require_user(request)
    loc = _user_locale(request)
    with _db_lock, _db() as conn:
        r = conn.execute("SELECT * FROM sims WHERE id=?", (sim_id,)).fetchone()
    if r is None:
        raise HTTPException(404, "Simulation inconnue")
    d = _public_row(r)
    d["cached_from"] = r["cached_from"]
    if r["kind"] == "gear" and d.get("gear"):
        base = r["dps"] or 0.0
        enriched = []
        for g in d["gear"]:
            m = re.search(r"\[(\w+):(\d+)\]$", g.get("name") or "")
            slot, item_id = (m.group(1), int(m.group(2))) if m else (None, None)
            label = re.sub(r"\s*\[[^\]]*\]\s*$", "", g.get("name") or "")
            info = {}
            if item_id:
                try:
                    info = bnet.item(item_id, locale=loc)
                except bnet.BnetError:
                    info = {}
            dps = float(g.get("dps") or 0.0)
            enriched.append({
                "label": label,
                "slot": slot,
                "slot_fr": bnet.slot_label(slot, loc),
                "item_id": item_id,
                "name": info.get("name") or label,
                "icon": info.get("icon"),
                "quality": info.get("quality") or "COMMON",
                "dps": dps,
                "err_pct": round(100 * (float(g.get("err") or 0.0)) / dps, 2) if dps else None,
                "delta": (dps - base) if base else None,
                "delta_pct": round(100 * (dps - base) / base, 2) if base else None,
            })
        enriched.sort(key=lambda e: e["dps"], reverse=True)
        d["gear"] = enriched
        d["gear_base_dps"] = base
    if r["kind"] == "stuff":
        st = _parse_weights_json(r["gear"])
        if st:
            for e in st.get("slots") or []:
                e["slot_fr"] = bnet.slot_label(e.get("slot"), loc)
            for e in st.get("items") or []:
                e["slot_fr"] = bnet.slot_label(e.get("slot"), loc)
        d["stuff"] = st
        if st and st.get("mode") == "bis":
            d["stuff"]["slot_fr"] = {k: bnet.slot_label(k, loc) for k in STUFF_SLOTS}
    return d


@router.get("/reports/{sim_id}/report.html")
def report_html(sim_id: str):
    with _db_lock, _db() as conn:
        r = conn.execute("SELECT report_html FROM sims WHERE id=?", (sim_id,)).fetchone()
    if r is None or not r["report_html"] or not Path(r["report_html"]).exists():
        raise HTTPException(404, "Rapport introuvable")
    return FileResponse(r["report_html"], media_type="text/html")


@router.get("/reports/{sim_id}/report.json")
def report_json(sim_id: str):
    with _db_lock, _db() as conn:
        r = conn.execute("SELECT report_json FROM sims WHERE id=?", (sim_id,)).fetchone()
    if r is None or not r["report_json"] or not Path(r["report_json"]).exists():
        raise HTTPException(404, "Rapport introuvable")
    return FileResponse(r["report_json"], media_type="application/json")

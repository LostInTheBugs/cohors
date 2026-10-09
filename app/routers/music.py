"""Cohors — music (SinusBot): player, library, uploads, bot settings, shuffle/loop engine, TeamSpeak user count.

Officers and administrators only, except the media URL the bot fetches (token-protected) and the
TeamSpeak user count (any signed-in member). The shuffle/loop watcher thread starts at import time,
as it did in app/main.py.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote, unquote

import httpx
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.core.auth import _get_session_user, _require_officer, _user_role
from app.core.config import DATA_DIR, STATIC_DIR

router = APIRouter()


SINUSBOT_URL = os.environ.get("SINUSBOT_URL", "http://127.0.0.1:8087").rstrip("/")
SINUSBOT_USER = os.environ.get("SINUSBOT_USER", "")
SINUSBOT_PASS = os.environ.get("SINUSBOT_PASS", "")
SINUSBOT_BOTID = os.environ.get("SINUSBOT_BOTID", "")
SINUSBOT_INSTANCE = os.environ.get("SINUSBOT_INSTANCE", "")
MUSIC_MEDIA_TOKEN = os.environ.get("MUSIC_MEDIA_TOKEN", "")
MUSIC_MEDIA_BASE = os.environ.get("MUSIC_MEDIA_BASE", "http://127.0.0.1:8030").rstrip("/")
MUSIC_DIR = DATA_DIR / "music"
_MUSIC_SB = {"token": "", "ts": 0.0}


def _music_login() -> str:
    if _MUSIC_SB["token"] and (time.time() - _MUSIC_SB["ts"]) < 3600:
        return _MUSIC_SB["token"]
    payload = {"username": SINUSBOT_USER, "password": SINUSBOT_PASS, "botId": SINUSBOT_BOTID}
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.post(SINUSBOT_URL + "/api/v1/bot/login", json=payload)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Bot musique injoignable ({exc.__class__.__name__}).")
    j = r.json()
    token = j.get("token", "")
    if not token:
        raise HTTPException(502, "Connexion au bot musique impossible.")
    _MUSIC_SB["token"] = token
    _MUSIC_SB["ts"] = time.time()
    return token


def _sb_call(method: str, path: str, body=None, timeout: float = 20.0):
    token = _music_login()
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.request(method, SINUSBOT_URL + path, json=body,
                               headers={"Authorization": "Bearer " + token})
            if r.status_code == 401:
                _MUSIC_SB["token"] = ""
                token = _music_login()
                r = client.request(method, SINUSBOT_URL + path, json=body,
                                   headers={"Authorization": "Bearer " + token})
        return r
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Bot musique injoignable ({exc.__class__.__name__}).")



def _music_payload(j: dict) -> dict:
    ct = j.get("currentTrack") or {}
    conn = j.get("connStatus") or {}
    return {
        "ok": True,
        "running": bool(j.get("running")),
        "connected": bool(conn.get("status") == 4),
        "playing": bool(j.get("playing")),
        "paused": bool((_music_state_load() or {}).get("paused")),
        "stopped": bool((_music_state_load() or {}).get("stopped")),
        "position": int(j.get("position") or 0),
        "volume": int(j.get("volume") or 0),
        "track": ({"uuid": ct.get("uuid", ""), "title": ct.get("title") or ct.get("filename", "")}
                  if ct.get("uuid") else None),
        "modes": {"shuffle": bool((_music_state_load() or {}).get("shuffle")), "loop": bool((_music_state_load() or {}).get("loop"))},
    }


@router.get("/music")
def music_page(request: Request):
    user = _get_session_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    if _user_role(user) not in ("admin", "officer"):
        return RedirectResponse("/dashboard", status_code=302)
    return FileResponse(STATIC_DIR / "music.html")


@router.get("/api/music/status")
def music_status(request: Request):
    _require_officer(request)
    try:
        r = _sb_call("GET", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/status")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Bot injoignable ({exc.__class__.__name__})")
    if r.status_code != 200:
        raise HTTPException(502, "Bot injoignable.")
    return _music_payload(r.json())


@router.get("/api/music/tracks")
def music_tracks(request: Request):
    _require_officer(request)
    r = _sb_call("GET", "/api/v1/bot/files")
    try:
        files = r.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(502, "Bibliothèque illisible.")
    tracks = []
    for f in (files if isinstance(files, list) else []):
        title = (f.get("title") or f.get("filename") or "").strip()
        tracks.append({"uuid": f.get("uuid", ""), "title": title[:120]})
    tracks.sort(key=lambda t: t["title"].lower())
    return {"ok": True, "tracks": tracks}


class MusicPlay(BaseModel):
    uuid: str = Field(..., max_length=80)


@router.post("/api/music/play")
def music_play(payload: MusicPlay, request: Request):
    _require_officer(request)
    if not re.match(r"^[A-Za-z0-9-]{8,80}$", payload.uuid):
        raise HTTPException(400, "Identifiant invalide.")
    r = _sb_call("POST", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/play/byId/{payload.uuid}")
    if r.status_code != 200:
        raise HTTPException(502, "Le bot a refusé la lecture.")
    # Vérifier que la lecture démarre vraiment (piste supprimée du serveur => silence trompeur).
    time.sleep(1.6)
    try:
        _j = _sb_call("GET", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/status").json()
        if not _j.get("playing") and int(_j.get("position") or 0) <= 0:
            raise HTTPException(409, "Impossible de lire ce morceau : le fichier n'existe plus sur le serveur. Choisis une piste dans la bibliothèque.")
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        pass  # statut indisponible : on ne bloque pas la lecture
    st = _music_state_load()
    if st.get("paused") or st.get("stopped"):
        st.pop("paused", None)
        st.pop("stopped", None)
        _music_state_save(st)
    return {"ok": r.status_code == 200}


@router.post("/api/music/pause")
def music_pause(request: Request):
    _require_officer(request)
    r = _sb_call("POST", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/pause")
    st = _music_state_load()
    st["paused"] = True
    _music_state_save(st)
    return {"ok": r.status_code == 200}


@router.post("/api/music/stop")
def music_stop(request: Request):
    _require_officer(request)
    r = _sb_call("POST", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/stop")
    st = _music_state_load()
    st["stopped"] = True  # Stop volontaire ≠ fin de piste : le moteur d'enchaînement doit l'ignorer
    st.pop("paused", None)
    _music_state_save(st)
    return {"ok": r.status_code == 200}


class MusicVolume(BaseModel):
    volume: int = Field(..., ge=0, le=100)


@router.post("/api/music/volume")
def music_volume(payload: MusicVolume, request: Request):
    _require_officer(request)
    r = _sb_call("POST", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/volume/set/{payload.volume}")
    return {"ok": r.status_code == 200}


class MusicUrl(BaseModel):
    url: str = Field(..., max_length=500)


@router.post("/api/music/add-url")
def music_add_url(payload: MusicUrl, request: Request):
    _require_officer(request)
    url = payload.url.strip()
    if not re.match(r"^https?://", url):
        raise HTTPException(400, "Lien invalide (http/https attendu).")
    r = _sb_call("POST", "/api/v1/bot/url", {"url": url, "parent": ""})
    ok = r.status_code == 200 and '"success":true' in r.text
    return {"ok": ok}


@router.post("/api/music/upload")
async def music_upload(request: Request, file: UploadFile = File(...)):
    _require_officer(request)
    raw = await file.read()
    if len(raw) > 60 * 1024 * 1024:
        raise HTTPException(400, "Fichier trop lourd (60 Mo max).")
    name = re.sub(r"[^A-Za-z0-9._ ()-]", "_", (file.filename or "musique.mp3")).strip()[:100] or "musique.mp3"
    if not re.search(r"\.(mp3|ogg|wav|m4a|flac|opus)$", name, re.I):
        name += ".mp3"
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    (MUSIC_DIR / name).write_bytes(raw)
    media_url = f"{MUSIC_MEDIA_BASE}/musicmedia/{MUSIC_MEDIA_TOKEN}/{quote(name)}"
    r = _sb_call("POST", "/api/v1/bot/url", {"url": media_url, "parent": ""})
    ok = r.status_code == 200 and '"success":true' in r.text
    if not ok:
        raise HTTPException(502, "Fichier enregistré mais refusé par le bot.")
    return {"ok": True, "name": name}


@router.delete("/api/music/track/{uuid}")
def music_delete(uuid: str, request: Request):
    _require_officer(request)
    if not re.match(r"^[A-Za-z0-9-]{8,80}$", uuid):
        raise HTTPException(400, "Identifiant invalide.")
    # Retrouver le fichier local correspondant AVANT de supprimer côté bot (ménage disque)
    local_name = ""
    try:
        rl = _sb_call("GET", "/api/v1/bot/files")
        files = rl.json()
        for f in (files if isinstance(files, list) else []):
            if f.get("uuid") == uuid:
                fname = str(f.get("filename") or f.get("title") or "")
                base = Path(unquote(fname.rsplit("/", 1)[-1])).name if "/" in fname else Path(unquote(fname)).name
                if base.lower().endswith((".mp3", ".ogg", ".wav", ".m4a", ".flac", ".opus")):
                    local_name = base
                break
    except Exception:  # noqa: BLE001
        pass
    r = _sb_call("DELETE", f"/api/v1/bot/files/{uuid}")
    file_deleted = False
    if local_name:
        try:
            p = MUSIC_DIR / local_name
            if p.is_file():
                p.unlink()
                file_deleted = True
        except Exception:  # noqa: BLE001
            pass
    return {"ok": r.status_code == 200, "file_deleted": file_deleted}


@router.get("/musicmedia/{token}/{name}")
def music_media(token: str, name: str):
    if not MUSIC_MEDIA_TOKEN or token != MUSIC_MEDIA_TOKEN:
        raise HTTPException(404)
    if "/" in name or chr(92) in name or ".." in name:
        raise HTTPException(404)
    path = MUSIC_DIR / name
    if not path.exists() or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="audio/mpeg")


def _music_channel_list() -> list[dict]:
    """Salons TeamSpeak vus par le bot : id, nom, chemin « parent/salon » et présence d'un mot de passe."""
    r = _sb_call("GET", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/channels")
    try:
        raw = r.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(502, "Liste des salons illisible.")
    by_id = {c.get("id"): c.get("name", "") for c in raw}
    out = []
    for c in raw:
        parent = c.get("parent") or 0
        path = (by_id.get(parent, "") + "/" + c.get("name", "")) if parent else c.get("name", "")
        out.append({"id": c.get("id"), "name": c.get("name", ""), "path": path, "hasPassword": bool(c.get("pw"))})
    return out


@router.get("/api/music/channels")
def music_channels(request: Request):
    _require_officer(request)
    out = _music_channel_list()
    out.sort(key=lambda x: (x["path"] or "").lower())
    return {"ok": True, "channels": out}


@router.get("/api/music/bot")
def music_bot(request: Request):
    _require_officer(request)
    r = _sb_call("GET", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/settings")
    j = r.json()
    return {"ok": True, "nick": j.get("nick", ""), "channel": j.get("channelName", "")}


class MusicBotConfig(BaseModel):
    nick: str | None = Field(default=None, max_length=40)
    channel: str | None = Field(default=None, max_length=120)


@router.post("/api/music/bot")
def music_bot_set(payload: MusicBotConfig, request: Request):
    _require_officer(request)
    patch = {}
    if payload.nick is not None:
        nick = payload.nick.strip()
        if not (2 <= len(nick) <= 30):
            raise HTTPException(400, "Nom du bot : 2 à 30 caractères.")
        if any(ord(ch) < 32 for ch in nick):
            raise HTTPException(400, "Nom du bot invalide.")
        patch["nick"] = nick
    if payload.channel is not None:
        valid = {c["path"]: c["hasPassword"] for c in _music_channel_list()}
        want = payload.channel.strip()
        if want not in valid:
            raise HTTPException(400, "Salon inconnu.")
        if valid[want]:
            raise HTTPException(400, "Ce salon a un mot de passe — pas encore géré.")
        patch["channelName"] = want
    if not patch:
        raise HTTPException(400, "Rien à modifier.")
    r = _sb_call("POST", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/settings", patch)
    if r.status_code != 200 or '"success":true' not in r.text:
        raise HTTPException(502, "Réglage refusé par le bot.")
    _sb_call("POST", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/kill")
    time.sleep(2)
    _sb_call("POST", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/spawn")
    return {"ok": True}


# --- 🎵 Modes de lecture : aléatoire + boucle (moteur côté app) ---
MUSIC_STATE_FILE = MUSIC_DIR / "state.json"


def _music_state_load() -> dict:
    try:
        j = json.loads(MUSIC_STATE_FILE.read_text())
        if isinstance(j, dict):
            return j
    except Exception:  # noqa: BLE001
        pass
    return {}


def _music_state_save(st: dict) -> None:
    try:
        MUSIC_DIR.mkdir(parents=True, exist_ok=True)
        MUSIC_STATE_FILE.write_text(json.dumps(st))
    except Exception:  # noqa: BLE001
        pass


def _music_pick_random(current_uuid: str = "") -> str:
    import random
    r = _sb_call("GET", "/api/v1/bot/files")
    try:
        files = r.json()
    except Exception:  # noqa: BLE001
        return ""
    uuids = [f.get("uuid") for f in (files if isinstance(files, list) else []) if f.get("uuid")]
    if not uuids:
        return ""
    pool = [u for u in uuids if u != current_uuid] or uuids
    return random.choice(pool)


@router.get("/api/music/modes")
def music_modes(request: Request):
    _require_officer(request)
    st = _music_state_load()
    return {"ok": True, "shuffle": bool(st.get("shuffle")), "loop": bool(st.get("loop"))}


class MusicModes(BaseModel):
    shuffle: bool | None = None
    loop: bool | None = None


@router.post("/api/music/modes")
def music_modes_set(payload: MusicModes, request: Request):
    _require_officer(request)
    st = _music_state_load()
    if payload.shuffle is not None:
        st["shuffle"] = bool(payload.shuffle)
    if payload.loop is not None:
        st["loop"] = bool(payload.loop)
    st.pop("paused", None)
    st.pop("stopped", None)
    _music_state_save(st)
    started = ""
    if payload.shuffle:
        try:
            r = _sb_call("GET", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/status")
            j = r.json()
            if not j.get("playing"):
                uid = _music_pick_random()
                if uid:
                    _sb_call("POST", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/play/byId/{uid}")
                    started = uid
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "shuffle": bool(st.get("shuffle")), "loop": bool(st.get("loop")), "started": started}


_MUSIC_WATCH = {"prev_playing": False, "prev_uuid": "", "prev_pos": 0, "max_pos": 0,
                "ticks": 0, "last_tick": 0.0, "last_ended": "", "last_error": ""}


def _music_watch_tick():
    st = _music_state_load()
    r = _sb_call("GET", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/status")
    j = r.json()
    playing = bool(j.get("playing"))
    pos = int(j.get("position") or 0)
    uuid = ((j.get("currentTrack") or {}).get("uuid") or "")
    P = _MUSIC_WATCH
    if playing and uuid:
        P["max_pos"] = max(P["max_pos"], pos) if P["prev_uuid"] == uuid else pos
    ended = False
    if P["prev_playing"] and not playing and uuid and uuid == P["prev_uuid"] and not st.get("paused") and not st.get("stopped"):
        if pos > 0 and pos >= P["prev_pos"] and pos > 2500:
            dur = (st.get("durations") or {}).get(uuid)
            if dur:
                ended = pos >= int(dur) - 2500
            else:
                ended = pos >= P["max_pos"] - 1000
    if ended:
        if not st.get("durations"):
            st["durations"] = {}
        st["durations"][uuid] = max(pos, P["max_pos"])
        _music_state_save(st)
        mode = "shuffle" if st.get("shuffle") else ("loop" if st.get("loop") else "none")
        P["last_ended"] = f"{uuid[:8]}@{pos}ms ({mode})"
        print(f"[music] fin de piste detectee : {P['last_ended']}", flush=True)
        if st.get("shuffle"):
            nxt = _music_pick_random(uuid)
            if nxt:
                _sb_call("POST", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/play/byId/{nxt}")
        elif st.get("loop"):
            _sb_call("POST", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/play/byId/{uuid}")
    P["prev_playing"] = playing
    P["prev_uuid"] = uuid if uuid else P["prev_uuid"]
    P["prev_pos"] = pos


def _music_watcher_loop():
    time.sleep(20)
    while True:
        try:
            _music_watch_tick()
            _MUSIC_WATCH["ticks"] += 1
            _MUSIC_WATCH["last_tick"] = time.time()
            _MUSIC_WATCH["last_error"] = ""
        except Exception as exc:  # noqa: BLE001
            _MUSIC_WATCH["last_error"] = f"{exc.__class__.__name__}: {exc}"[:200]
        time.sleep(3)


threading.Thread(target=_music_watcher_loop, daemon=True).start()


@router.get("/api/music/watch")
def music_watch_state(request: Request):
    """Diagnostic du moteur d'enchaînement (ticks, dernier événement)."""
    _require_officer(request)
    st = _music_state_load()
    keys = ("ticks", "last_tick", "last_ended", "last_error", "prev_playing", "prev_uuid", "prev_pos", "max_pos")
    return {"ok": True, "watch": {k: _MUSIC_WATCH.get(k) for k in keys},
            "state": {"stopped": bool(st.get("stopped")), "paused": bool(st.get("paused")),
                      "shuffle": bool(st.get("shuffle")), "loop": bool(st.get("loop")),
                      "durations": len(st.get("durations") or {})}}


# --- 🎧 Compteur de connectés TeamSpeak (hors bot) ---
MUSIC_BOT_UID = os.environ.get("SINUSBOT_CLIENT_UID", "qr2Ym8lUukm/XbbkStGGt+o4g9Q=")


@router.get("/api/voice/count")
def voice_count(request: Request):
    if _get_session_user(request) is None:
        raise HTTPException(401, "Connexion requise.")
    try:
        r = _sb_call("GET", f"/api/v1/bot/i/{SINUSBOT_INSTANCE}/channels")
        raw = r.json()
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        raise HTTPException(502, "Vocal indisponible.")
    count = 0
    groups = []
    for ch in (raw if isinstance(raw, list) else []):
        names = []
        for cl in (ch.get("clients") or []):
            if cl.get("uid") == MUSIC_BOT_UID:
                continue
            count += 1
            nm = cl.get("nick") or ""
            if nm:
                names.append(nm)
        if names:
            groups.append({"channel": ch.get("name", ""), "names": names})
    return {"ok": True, "count": count, "groups": groups}

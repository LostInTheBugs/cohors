"""Cohors — voice portal on its own subdomain (members only): gate middleware, session handoff,
HTTP and WebSocket proxy to the internal web client (WebSpeak), portable TeamSpeak client download.

VoiceGateMiddleware is defined here; app/main.py registers it, so the middleware order is unchanged.
"""
from __future__ import annotations

import asyncio
import os
from urllib.parse import quote

import httpx
import websockets
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse

from app.core.auth import _get_session_user, _require_user
from app.core.config import COOKIE_DOMAIN, COOKIE_SECURE, DATA_DIR, PUBLIC_BASE_URL, SESSION_COOKIE, SESSION_DAYS

router = APIRouter()


# Le vhost du sous-domaine vocal pose l'en-tête VOICE_HEADER (ex. X-Cohors-Voice)
# puis proxyfie vers cette app : les requêtes marquées sont réécrites vers
# /__voice* où la session est vérifiée avant tout relais vers le client web
# interne (WebSpeak).
VOICE_BACKEND = os.environ.get("VOICE_BACKEND", "http://127.0.0.1:3040").rstrip("/")
VOICE_BACKEND_WS = VOICE_BACKEND.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
VOICE_PUBLIC_HOST = os.environ.get("VOICE_PUBLIC_HOST", "").strip()
VOICE_HEADER = os.environ.get("VOICE_HEADER", "X-Cohors-Voice").strip()
VOICE_CLIENT_ZIP = DATA_DIR / "voice" / "Cohors-TeamSpeak.zip"
_VOICE_APP_BASE = PUBLIC_BASE_URL or ""

# Script injecté dans le client web : pré-remplit le pseudo (fragment #nickname=...).
_VOICE_EXTRAS_JS = """
(function () {
  try {
    var hash = String(location.hash || "");
    var m = hash.match(/[#&]nickname=([^&]+)/);
    var nick = "";
    if (m) { try { nick = decodeURIComponent(m[1]); } catch (e) { nick = m[1]; } }
    nick = (nick || "").trim();
    var auto = /[#&]autojoin=1/.test(hash);
    if (nick) { try { localStorage.setItem("webspeak:nickname", nick); } catch (e) {} }
    if (!nick && !auto) return;
    var tries = 0;
    var nickDone = false;
    var clicks = 0;
    var lastClick = 0;
    var timer = setInterval(function () {
      tries += 1;
      var input = document.querySelector("#nickname");
      if (nick && input && !nickDone) {
        try {
          var setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
          setter.call(input, nick);
          input.dispatchEvent(new Event("input", { bubbles: true }));
          input.dispatchEvent(new Event("change", { bubbles: true }));
        } catch (e) {}
        nickDone = true;
      }
      if (auto && input && (input.value || "").trim()) {
        var btn = null;
        var buttons = document.querySelectorAll("button");
        for (var i = 0; i < buttons.length; i++) {
          var t = (buttons[i].innerText || "").trim().toLowerCase();
          if (t.indexOf("enter voice space") >= 0) { btn = buttons[i]; break; }
        }
        if (btn && !btn.disabled && clicks < 2 && (Date.now() - lastClick) > 4000) {
          clicks += 1;
          lastClick = Date.now();
          btn.click();
        }
      }
      if (tries > 120) clearInterval(timer);
    }, 300);
  } catch (e) {}
})();
"""


class VoiceGateMiddleware:
    """Réécrit les requêtes du vhost vocal (en-tête VOICE_HEADER) vers /__voice*."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") in ("http", "websocket"):
            headers = dict(scope.get("headers") or [])
            if headers.get(VOICE_HEADER.lower().encode()):
                scope = dict(scope)
                scope["voice_gate"] = True
                path = scope.get("path") or "/"
                scope["voice_orig_path"] = path
                scope["path"] = "/__voice" + path
                scope["raw_path"] = scope["path"].encode()
        await self.app(scope, receive, send)


_VOICE_HOP_REQ = {"host", "cookie", "connection", "keep-alive", "transfer-encoding", "upgrade",
                  "proxy-connection", "te", "trailer", "expect", VOICE_HEADER.lower(), "content-length"}
_VOICE_HOP_RESP = {"connection", "keep-alive", "transfer-encoding", "upgrade",
                   "content-encoding", "content-length"}


@router.get("/api/voice/handoff")
def voice_handoff(request: Request, next: str = ""):
    """Répare la session pour le sous-domaine vocal puis renvoie vers le client.

    Le portail vocal vit sur un autre sous-domaine : les cookies host-only du site
    ne l'atteignent pas. Ici on réémet le cookie avec le domaine parent
    (COOKIE_DOMAIN) puis on renvoie vers le client — sans passage par la connexion.
    """
    user = _get_session_user(request)
    target = next if next.startswith("https://" + VOICE_PUBLIC_HOST + "/") or next == "https://" + VOICE_PUBLIC_HOST else "https://" + VOICE_PUBLIC_HOST + "/"
    if user is None:
        return RedirectResponse(f"{_VOICE_APP_BASE}/login?next={quote(_VOICE_APP_BASE + '/api/voice/handoff?next=' + quote(target, safe=''), safe='')}", status_code=302)
    if request.query_params.get("json"):
        response = Response(content='{"ok": true}', media_type="application/json")
    else:
        response = RedirectResponse(target, status_code=302)
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True,
                            samesite="lax", secure=COOKIE_SECURE, path="/", domain=COOKIE_DOMAIN)
    return response


@router.get("/api/voice/config")
def voice_config():
    """Configuration publique du portail vocal (hôte web) pour la page 🎧 Vocal."""
    return {"host": ("https://" + VOICE_PUBLIC_HOST) if VOICE_PUBLIC_HOST else ""}


@router.api_route("/__voice{rest:path}",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def voice_portal(request: Request, rest: str):
    if not request.scope.get("voice_gate"):
        raise HTTPException(404)
    if _get_session_user(request) is None:
        nxt = "https://" + VOICE_PUBLIC_HOST + request.scope.get("voice_orig_path", request.url.path)
        if request.url.query:
            nxt += "?" + request.url.query
        return RedirectResponse(f"{_VOICE_APP_BASE}/api/voice/handoff?next={quote(nxt, safe='')}", status_code=302)
    if rest == "/__cohors_extras.js":
        return Response(content=_VOICE_EXTRAS_JS, media_type="application/javascript; charset=utf-8",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    url = VOICE_BACKEND + rest
    if request.url.query:
        url += "?" + request.url.query
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _VOICE_HOP_REQ}
    headers["host"] = request.headers.get("host", VOICE_PUBLIC_HOST)
    headers["accept-encoding"] = "identity"
    try:
        body = await request.body()
        async with httpx.AsyncClient(timeout=60.0) as client:
            upstream = await client.request(request.method, url, headers=headers, content=body)
    except httpx.HTTPError as exc:
        return PlainTextResponse(f"Vocal indisponible ({exc.__class__.__name__})", status_code=502)
    content = upstream.content
    if "text/html" in (upstream.headers.get("content-type") or ""):
        html = content.decode("utf-8", "replace")
        if "__cohors_extras.js" not in html:
            tag = '<script src="/__cohors_extras.js"></script>'
            if "</head>" in html:
                html = html.replace("</head>", tag + "</head>", 1)
            elif "</body>" in html:
                html = html.replace("</body>", tag + "</body>", 1)
            else:
                html = html + tag
        content = html.encode("utf-8")
    out = Response(content=content, status_code=upstream.status_code)
    for key, value in upstream.headers.multi_items():
        lk = key.lower()
        if lk in _VOICE_HOP_RESP:
            continue
        if lk == "set-cookie":
            out.headers.append(key, value)
        else:
            out.headers[key] = value
    return out


@router.websocket("/__voice{rest:path}")
async def voice_portal_ws(websocket: WebSocket, rest: str):
    if not websocket.scope.get("voice_gate"):
        await websocket.close(code=4404)
        return
    if _get_session_user(websocket) is None:
        await websocket.close(code=4401)
        return
    subprotocols = websocket.scope.get("subprotocols") or []
    qs = websocket.scope.get("query_string", b"").decode("utf-8", "replace")
    url = VOICE_BACKEND_WS + rest + (("?" + qs) if qs else "")
    up_headers = {}
    origin = websocket.headers.get("origin")
    if origin:
        up_headers["Origin"] = origin
    user_agent = websocket.headers.get("user-agent")
    if user_agent:
        up_headers["User-Agent"] = user_agent
    host_header = websocket.headers.get("host")
    if host_header:
        up_headers["Host"] = host_header
    forwarded_for = websocket.headers.get("x-forwarded-for")
    if forwarded_for:
        up_headers["X-Forwarded-For"] = forwarded_for
    try:
        try:
            upstream_cm = websockets.connect(url, subprotocols=subprotocols or None, max_size=None,
                                             open_timeout=15, ping_interval=None, close_timeout=5,
                                             additional_headers=up_headers or None)
        except TypeError:  # websockets < 14 : autre nom du paramètre
            upstream_cm = websockets.connect(url, subprotocols=subprotocols or None, max_size=None,
                                             open_timeout=15, ping_interval=None, close_timeout=5,
                                             extra_headers=up_headers or None)
        async with upstream_cm as upstream:
            chosen = upstream.subprotocol
            await websocket.accept(subprotocol=chosen if chosen in subprotocols else None)

            async def client_to_upstream():
                while True:
                    msg = await websocket.receive()
                    if msg.get("type") == "websocket.disconnect":
                        raise WebSocketDisconnect(msg.get("code", 1000))
                    if msg.get("text") is not None:
                        await upstream.send(msg["text"])
                    elif msg.get("bytes") is not None:
                        await upstream.send(msg["bytes"])

            async def upstream_to_client():
                async for message in upstream:
                    if isinstance(message, (bytes, bytearray)):
                        await websocket.send_bytes(bytes(message))
                    else:
                        await websocket.send_text(message)

            tasks = [asyncio.create_task(client_to_upstream()),
                     asyncio.create_task(upstream_to_client())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
    except Exception as exc:  # noqa: BLE001
        print(f"[voice] session web fermée : {exc!r}")
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


@router.get("/api/voice/client")
def voice_client_download(request: Request):
    """Client TeamSpeak portable préconfiguré (réservé aux membres connectés)."""
    _require_user(request)
    if not VOICE_CLIENT_ZIP.exists():
        raise HTTPException(404, "Client portable pas encore disponible.")
    return FileResponse(VOICE_CLIENT_ZIP, filename="Cohors-TeamSpeak.zip",
                        media_type="application/zip")

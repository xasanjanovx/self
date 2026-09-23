"""HTTP API для приложения «Джарвис» на телефоне (jarvis-android).

Поднимается внутри процесса бота, если задан JARVIS_TOKEN (порт JARVIS_PORT, по умолчанию 8097).
Снаружи — nginx с HTTPS. Все запросы — с заголовком `Authorization: Bearer <JARVIS_TOKEN>`;
действует только для владельца (JARVIS_OWNER_ID или первый из ALLOWED_TELEGRAM_IDS).

  GET  /jarvis/v1/ping          — жив ли сервер, подключён ли Telegram, сколько контактов
  POST /jarvis/v1/voice         — {"audio": base64 WAV | "text": str, "device": {...}} →
                                   {"transcript", "say", "actions": [...], "listen", "need_contacts"}
  POST /jarvis/v1/warm          — услышал «Джарвис»: прогреть кэши, пока человек договаривает
  GET  /jarvis/v1/live          — WebSocket: живой разговор через Gemini Live (протокол — bot/phone_live.py)
  GET  /jarvis/v1/greetings     — короткие отклики («Да?») голосом бота, WAV в base64
  POST /jarvis/v1/contacts      — {"contacts": [{"n": имя, "p": [номера]}]} — телефонная книга
  POST /jarvis/v1/tg/login      — {"phone"} → код; {"code"} → вход или password_needed; {"password"}
  POST /jarvis/v1/tg/logout
  GET  /jarvis/v1/tg/status
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import logging
import os
import re
import time
from typing import Any

from aiohttp import web

from . import phone
from . import tg_user
from .context import ai, settings

logger = logging.getLogger(__name__)

MAX_BODY = 8 * 1024 * 1024
_runner: web.AppRunner | None = None
_lock = asyncio.Lock()

_STT_PROMPT = (
    "Это голосовая команда личному ассистенту «Джарвис» на телефоне. Расшифруй речь дословно. "
    "Язык — русский или узбекский (узбекский пиши латиницей), бывает смесь. Имена и названия пиши как слышишь. "
    "Верни только текст. Если речи нет или она неразборчива — верни ровно: <пусто>"
)
_WAKE_PREFIX = re.compile(r"^\s*((эй|хей|hey|ey|ой)[\s,!.]*)?(джарвис|жарвис|джервис|jarvis|djarvis)[\s,!.:—-]*", re.IGNORECASE)


def token() -> str:
    return (os.getenv("JARVIS_TOKEN") or "").strip()


def owner_id() -> int | None:
    raw = (os.getenv("JARVIS_OWNER_ID") or "").strip()
    if raw.isdigit():
        return int(raw)
    ids = sorted(settings.allowed_telegram_ids)
    return ids[0] if ids else None


def clean_transcript(text: str) -> str:
    text = str(text or "").strip()
    if text.strip("<> .").lower() in {"пусто", "empty", ""}:
        return ""
    return _WAKE_PREFIX.sub("", text, count=1).strip()


@web.middleware
async def _auth(request: web.Request, handler):
    expected = token()
    got = request.headers.get("Authorization", "")
    if not expected or not hmac.compare_digest(got.encode(), f"Bearer {expected}".encode()):
        await asyncio.sleep(0.5)
        return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


async def _json(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text='{"error": "json body required"}', content_type="application/json")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text='{"error": "json object required"}', content_type="application/json")
    return data


async def ping(request: web.Request) -> web.Response:
    uid = owner_id()
    return web.json_response({"ok": True, "telegram": await tg_user.status(), "contacts": len(phone.load_contacts(uid)) if uid else 0})


async def voice(request: web.Request) -> web.Response:
    uid = owner_id()
    if uid is None:
        return web.json_response({"error": "owner is not configured"}, status=500)
    data = await _json(request)
    device = data.get("device") if isinstance(data.get("device"), dict) else {}
    started = time.monotonic()
    transcript = str(data.get("text") or "").strip()
    stt = 0.0
    if not transcript and data.get("audio"):
        try:
            audio = base64.b64decode(str(data["audio"]), validate=False)
        except (binascii.Error, ValueError):
            return web.json_response({"error": "bad audio"}, status=400)
        try:
            transcript = clean_transcript(await ai.transcribe_audio(audio, str(data.get("mime") or "audio/wav"), prompt=_STT_PROMPT))
        except Exception:
            logger.exception("phone stt failed")
            return web.json_response({"transcript": "", "say": "Не расслышал, связь с распознаванием подвела.", "actions": [], "listen": False})
        stt = time.monotonic() - started
    else:
        transcript = clean_transcript(transcript)
    if not transcript:
        return web.json_response({"transcript": "", "say": "", "actions": [], "listen": False})
    async with _lock:
        try:
            out = await phone.handle(uid, transcript, device)
        except Exception:
            logger.exception("phone agent failed")
            out = {"say": "Что-то пошло не так на сервере, повтори, пожалуйста.", "actions": [], "listen": False}
    logger.info("phone voice: stt=%.1fs total=%.1fs «%s» → «%s»", stt, time.monotonic() - started, transcript[:80], str(out.get("say"))[:80])
    return web.json_response({"transcript": transcript, **out})


async def live(request: web.Request) -> web.WebSocketResponse:
    """Живой разговор (bot/phone_live.py): телефон ⇄ Gemini Live."""
    from . import phone_live

    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=16 * 1024 * 1024)
    await ws.prepare(request)
    uid = owner_id()
    try:
        hello = await ws.receive_json(timeout=10)
    except Exception:
        await ws.close()
        return ws
    if uid is None or not isinstance(hello, dict):
        await ws.close()
        return ws
    try:
        await phone_live.run(uid, ws, hello)
    except Exception:
        logger.exception("phone live failed")
        if not ws.closed:
            await ws.send_json({"type": "error", "text": "Сервер споткнулся"})
    if not ws.closed:
        await ws.close()
    return ws


async def greetings(request: web.Request) -> web.Response:
    from . import phone_live

    uid = owner_id()
    return web.json_response(await phone_live.greetings(uid) if uid else {"clips": []})


async def warm(request: web.Request) -> web.Response:
    uid = owner_id()
    if uid is not None:
        phone._later(phone.prefetch(uid))
    return web.json_response({"ok": True})


async def contacts(request: web.Request) -> web.Response:
    uid = owner_id()
    data = await _json(request)
    raw = data.get("contacts")
    if uid is None or not isinstance(raw, list):
        return web.json_response({"error": "contacts list required"}, status=400)
    return web.json_response({"ok": True, "count": phone.save_contacts(uid, raw)})


async def tg_login(request: web.Request) -> web.Response:
    data = await _json(request)
    try:
        if data.get("cancel"):
            await tg_user.login_cancel()
            return web.json_response({"status": "cancelled"})
        if data.get("phone"):
            return web.json_response(await tg_user.login_start(str(data["phone"])))
        if data.get("password"):
            return web.json_response(await tg_user.login_finish(password=str(data["password"])))
        if data.get("code"):
            return web.json_response(await tg_user.login_finish(code=str(data["code"])))
    except Exception as exc:
        logger.exception("tg login failed")
        return web.json_response({"error": f"{type(exc).__name__}: {str(exc)[:200]}"})
    return web.json_response({"error": "phone, code or password required"}, status=400)


async def tg_logout(request: web.Request) -> web.Response:
    return web.json_response(await tg_user.logout())


async def tg_status(request: web.Request) -> web.Response:
    return web.json_response(await tg_user.status())


def build_app() -> web.Application:
    app = web.Application(middlewares=[_auth], client_max_size=MAX_BODY)
    app.router.add_get("/jarvis/v1/ping", ping)
    app.router.add_post("/jarvis/v1/voice", voice)
    app.router.add_post("/jarvis/v1/warm", warm)
    app.router.add_get("/jarvis/v1/live", live)
    app.router.add_get("/jarvis/v1/greetings", greetings)
    app.router.add_post("/jarvis/v1/contacts", contacts)
    app.router.add_post("/jarvis/v1/tg/login", tg_login)
    app.router.add_post("/jarvis/v1/tg/logout", tg_logout)
    app.router.add_get("/jarvis/v1/tg/status", tg_status)
    return app


async def start() -> bool:
    global _runner
    if not token():
        logger.info("phone api disabled: JARVIS_TOKEN is not set")
        return False
    if _runner is not None:
        return True
    port = int(os.getenv("JARVIS_PORT") or 8097)
    _runner = web.AppRunner(build_app(), access_log=None)
    await _runner.setup()
    await web.TCPSite(_runner, "0.0.0.0", port).start()
    logger.info("phone api listening on :%s (owner %s)", port, owner_id())
    return True


async def stop() -> None:
    global _runner
    runner, _runner = _runner, None
    if runner is not None:
        await runner.cleanup()
    await tg_user.stop()


__all__ = ["start", "stop", "build_app", "clean_transcript"]

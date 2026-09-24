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
  POST /jarvis/v1/wake_check    — {"audio": WAV, "confident"} → его ли голос и прозвучало ли «Джарвис» (защита от чужих и ТВ)
  POST /jarvis/v1/announce      — {"name": контакт, "app": Telegram…} → «Звонит мама» + WAV голосом бота
  POST /jarvis/v1/call_command  — {"audio": WAV, "caller"} → «ответь» / «сбрось» / «скажи, что перезвоню» во время звонка
  POST /jarvis/v1/contacts      — {"contacts": [{"n": имя, "p": [номера]}]} — телефонная книга
  POST /jarvis/v1/tg/login      — {"phone"} → код; {"code"} → вход или password_needed; {"password"}
  POST /jarvis/v1/tg/logout
  GET  /jarvis/v1/tg/status
  POST /jarvis/v1/tg/quick_send — {"name", "text"}: сбросил звонок в Telegram — «перезвоню» звонившему от его имени
  POST /jarvis/v1/voice/enroll  — {"wake": [WAV…], "reading": WAV} → отпечаток голоса («только мой голос»)
  GET  /jarvis/v1/voice/status · POST /jarvis/v1/voice/forget
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
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
_warm: asyncio.Task | None = None
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


_WAKE_PROMPT = (
    "На записи человек, возможно, зовёт голосового ассистента по имени «Джарвис» (Jarvis; бывает «Эй, Джарвис»). "
    "Язык — русский или узбекский. Расшифруй запись дословно и реши, прозвучало ли имя «Джарвис» как обращение. "
    "Похожие слова (жара, Джордж, Дарвин, сервис, «жарить») — это НЕ имя. "
    'Верни JSON: {"name": true/false, "text": "дословно", "after": "слова после имени (пусто, если ничего)"}'
)


async def wake_check(request: web.Request) -> web.Response:
    """Телефон решил, что услышал «Джарвис», — проверяем по записи, чтобы он не откликался на телевизор и похожие слова.

    Голос (свой отпечаток, ~0.05 с) и слово (локальный распознаватель, ~0.05–0.1 с) — параллельно; Gemini — только
    если распознаватель недоступен или ничего не расслышал. Пока проверяем, сервер уже готовит разговор с Gemini
    (phone_live.prewarm): подтвердили — телефон подключается к готовой сессии.
    """
    data = await _json(request)
    try:
        audio = base64.b64decode(str(data.get("audio") or ""), validate=False)
    except (binascii.Error, ValueError):
        return web.json_response({"error": "bad audio"}, status=400)
    if not audio:
        return web.json_response({"error": "audio required"}, status=400)
    from . import phone_live, voiceprint, wakeword

    started = time.monotonic()
    uid = owner_id()
    if uid is not None:
        phone_live.prewarm(uid)
    voice, heard = await asyncio.gather(voiceprint.verify(uid, audio) if uid is not None else _no_voice(), wakeword.check(audio))
    took = time.monotonic() - started
    if not voice.get("ok"):
        # чужой голос и телевизор отсекаем сразу
        logger.info("wake check: чужой голос (сходство %s, z %s, порог %s) за %.2f с «%s»", voice.get("score"), voice.get("z"),
                    voice.get("threshold"), took, (heard or {}).get("text", ""))
        _reject(uid)
        return web.json_response({"ok": False, "reason": "voice", "score": voice.get("score")})
    if heard is not None and (heard["text"] or data.get("confident")):
        ok = heard["name"] or (not heard["text"] and bool(data.get("confident")))
        logger.info("wake check: %s за %.2f с «%s» (голос %s, z %s, слово %s мс)", "да" if ok else "нет", took, heard["text"][:60],
                    voice.get("score"), voice.get("z"), heard["ms"])
        if not ok:
            _reject(uid)
        return web.json_response({"ok": ok, "text": heard["text"], "command": bool(heard["after"]), "voice": voice.get("score"), "fast": True})
    try:
        raw = await ai.generate([{"text": _WAKE_PROMPT}, {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(audio).decode()}}],
                                model=ai.transcribe_model, temperature=0.0, json_mode=True, max_tokens=200)
        verdict = json.loads(raw) if raw.strip().startswith("{") else {}
    except Exception:
        logger.warning("wake check failed", exc_info=True)
        # проверка не удалась (сеть/лимит) — не мешаем: пусть решает телефон
        return web.json_response({"ok": True, "unchecked": True})
    ok = bool(verdict.get("name"))
    after = str(verdict.get("after") or "").strip()
    logger.info("wake check: %s за %.1f с «%s» (Gemini)", "да" if ok else "нет", time.monotonic() - started, str(verdict.get("text") or "")[:60])
    if not ok:
        _reject(uid)
    return web.json_response({"ok": ok, "text": str(verdict.get("text") or ""), "command": bool(after)})


async def _no_voice() -> dict[str, Any]:
    return {"ok": True}


def _reject(uid: int | None) -> None:
    """Не «Джарвис» — заготовленный разговор с Gemini не нужен."""
    if uid is not None:
        from . import phone_live

        phone_live.discard(uid)


_CALL_COMMAND_PROMPT = (
    "Телефон владельца звонит (звонит: «{caller}»). Помощник сказал, кто звонит, и 5 секунд слушает команду. "
    "На записи — его голос (может быть и мелодия звонка). Язык — русский или узбекский. Реши, что он велел:\n"
    "answer — ответить/взять трубку («ответь», «возьми», «алло», «javob ber», «ol»);\n"
    "decline — сбросить/отклонить («сбрось», «не бери», «отклони», «o'chir», «olma»);\n"
    "decline_message — сбросить и написать («скажи, что перезвоню», «напиши, что я занят», «keyin qo'ng'iroq qilaman de») — "
    "в message готовый короткий текст от первого лица на языке его речи, со ВСЕМИ его подробностями (время, причина: «через час», «на совещании»);\n"
    "none — ничего из этого (тишина, мелодия, посторонний разговор).\n"
    'Верни JSON: {{"intent": "answer|decline|decline_message|none", "message": "", "heard": "дословно"}}'
)


async def call_command(request: web.Request) -> web.Response:
    """Входящий звонок: после «Звонит мама» телефон 5 с слушает — «ответь», «сбрось», «скажи, что перезвоню»."""
    data = await _json(request)
    try:
        audio = base64.b64decode(str(data.get("audio") or ""), validate=False)
    except (binascii.Error, ValueError):
        return web.json_response({"error": "bad audio"}, status=400)
    if not audio:
        return web.json_response({"intent": "none"})
    started = time.monotonic()
    prompt = _CALL_COMMAND_PROMPT.format(caller=str(data.get("caller") or "неизвестно")[:60])
    try:
        raw = await ai.generate([{"text": prompt}, {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(audio).decode()}}],
                                model=ai.transcribe_model, temperature=0.0, json_mode=True, max_tokens=200)
        verdict = json.loads(raw) if raw.strip().startswith("{") else {}
    except Exception:
        logger.warning("call command failed", exc_info=True)
        return web.json_response({"intent": "none"})
    intent = str(verdict.get("intent") or "none")
    if intent not in {"answer", "decline", "decline_message"}:
        intent = "none"
    message = str(verdict.get("message") or "").strip()[:300]
    if intent == "decline_message" and not message:
        message = "Сейчас не могу говорить, перезвоню позже."
    logger.info("call command: %s за %.1f с «%s»", intent, time.monotonic() - started, str(verdict.get("heard") or "")[:60])
    return web.json_response({"intent": intent, "message": message, "heard": str(verdict.get("heard") or "")})


async def announce(request: web.Request) -> web.Response:
    """Входящий звонок: «Звонит мама» голосом бота (контакт «Onajonim» → «мама»)."""
    from . import phone_live

    uid = owner_id()
    data = await _json(request)
    if uid is None:
        return web.json_response({"error": "owner is not configured"}, status=500)
    return web.json_response(await phone_live.announce(uid, str(data.get("name") or ""), str(data.get("app") or "")))


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


async def voice_enroll(request: web.Request) -> web.Response:
    """Запись голоса из приложения: 10 раз «Джарвис» + ~40 с чтения → отпечаток и порог «только мой голос»."""
    from . import voiceprint

    uid = owner_id()
    data = await _json(request)
    if uid is None:
        return web.json_response({"error": "owner is not configured"}, status=500)
    try:
        wake = [base64.b64decode(str(w), validate=False) for w in data.get("wake") or []]
        reading = base64.b64decode(str(data.get("reading") or ""), validate=False) or None
    except (binascii.Error, ValueError):
        return web.json_response({"error": "bad audio"}, status=400)
    return web.json_response(await voiceprint.enroll(uid, wake, reading))


async def voice_status(request: web.Request) -> web.Response:
    from . import voiceprint

    uid = owner_id()
    return web.json_response(voiceprint.status(uid) if uid else {"enrolled": False})


async def voice_forget(request: web.Request) -> web.Response:
    from . import voiceprint

    uid = owner_id()
    if uid:
        voiceprint.forget(uid)
    return web.json_response({"ok": True})


async def tg_quick_send(request: web.Request) -> web.Response:
    """Он сбросил звонок в Telegram словами «скажи, что перезвоню» — пишем звонившему от его имени.
    Имя — ровно как в уведомлении о звонке; если чат не найден однозначно — не пишем никому."""
    data = await _json(request)
    name, text = str(data.get("name") or "").strip(), str(data.get("text") or "").strip()
    if not name or not text:
        return web.json_response({"error": "name and text required"}, status=400)
    if not tg_user.configured():
        return web.json_response({"error": "telegram not connected"})
    found = await tg_user.find_chat([name])
    if "match" not in found:
        logger.info("tg quick send: чат «%s» не найден однозначно", name)
        return web.json_response({"error": "chat not found", "candidates": [c["name"] for c in found.get("candidates") or []]})
    ok = await tg_user.send(found["match"], text[:500])
    logger.info("tg quick send → %s: %s", found["match"].get("name"), "ok" if ok else "fail")
    return web.json_response({"ok": ok, "to": found["match"].get("name")})


def build_app() -> web.Application:
    app = web.Application(middlewares=[_auth], client_max_size=MAX_BODY)
    app.router.add_get("/jarvis/v1/ping", ping)
    app.router.add_post("/jarvis/v1/voice", voice)
    app.router.add_post("/jarvis/v1/warm", warm)
    app.router.add_get("/jarvis/v1/live", live)
    app.router.add_get("/jarvis/v1/greetings", greetings)
    app.router.add_post("/jarvis/v1/wake_check", wake_check)
    app.router.add_post("/jarvis/v1/announce", announce)
    app.router.add_post("/jarvis/v1/call_command", call_command)
    app.router.add_post("/jarvis/v1/contacts", contacts)
    app.router.add_post("/jarvis/v1/tg/login", tg_login)
    app.router.add_post("/jarvis/v1/tg/logout", tg_logout)
    app.router.add_get("/jarvis/v1/tg/status", tg_status)
    app.router.add_post("/jarvis/v1/tg/quick_send", tg_quick_send)
    app.router.add_post("/jarvis/v1/voice/enroll", voice_enroll)
    app.router.add_get("/jarvis/v1/voice/status", voice_status)
    app.router.add_post("/jarvis/v1/voice/forget", voice_forget)
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
    global _warm
    if owner_id() is not None:
        _warm = asyncio.create_task(phone.keep_warm(owner_id()), name="jarvis-warm")
        from . import voiceprint

        asyncio.create_task(voiceprint.warm(), name="voiceprint-warm")
        from . import wakeword

        asyncio.create_task(wakeword.warm(), name="wakeword-warm")
    return True


async def stop() -> None:
    global _runner, _warm
    runner, _runner = _runner, None
    if _warm is not None:
        _warm.cancel()
        _warm = None
    if runner is not None:
        await runner.cleanup()
    await tg_user.stop()


__all__ = ["start", "stop", "build_app", "clean_transcript"]

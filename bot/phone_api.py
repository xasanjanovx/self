"""HTTP API для приложения «JES» на телефоне (jarvis-android).

Поднимается внутри процесса бота, если задан JARVIS_TOKEN (порт JARVIS_PORT, по умолчанию 8097).
Снаружи — nginx с HTTPS. Все запросы — с заголовком `Authorization: Bearer <JARVIS_TOKEN>`;
действует только для владельца (JARVIS_OWNER_ID или первый из ALLOWED_TELEGRAM_IDS).

  GET  /jarvis/v1/ping          — жив ли сервер, подключён ли Telegram, сколько контактов
  POST /jarvis/v1/voice         — {"audio": base64 WAV | "text": str, "device": {...}} →
                                   {"transcript", "say", "actions": [...], "listen", "need_contacts"}
  POST /jarvis/v1/warm          — услышал «JES»: прогреть кэши, пока человек договаривает
  GET  /jarvis/v1/live          — WebSocket: живой разговор через Gemini Live (протокол — bot/phone_live.py)
  GET  /jarvis/v1/greetings     — короткие отклики («Да?») голосом бота, WAV в base64
  POST /jarvis/v1/wake_check    — {"audio": WAV, "confident"} → его ли голос и прозвучало ли «JES» (защита от чужих и ТВ)
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
    "Это голосовая команда личному ассистенту «JES» на телефоне. Расшифруй речь дословно. "
    "Язык — русский или узбекский (узбекский пиши латиницей), бывает смесь. Имена и названия пиши как слышишь. "
    "Верни только текст. Если речи нет или она неразборчива — верни ровно: <пусто>"
)
# имя ассистента — JES (читается «Джес», с 26.09.2026, 2.1); прежние имена не будят и не срезаются
_WAKE_PREFIX = re.compile(r"^\s*((эй|хей|hey|ey|ой)[\s,!.]*)?(джесс?|джейс|джез|жес|jess?|jez)"
                          r"[\s,!.:—-]*", re.IGNORECASE)


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
    _note_version(request.headers.get("X-JES-Version", ""))
    _note_net(request.headers.get("X-JES-Net", ""))
    return await handler(request)


_app_version = ""
_net: tuple[str, float] = ("", 0.0)


def _note_net(kind: str) -> None:
    """28.09: звонки Telegram рвались «на Wi-Fi» — по какой сети телефон (приложение 2.11+), для журнала звонка."""
    global _net
    if kind in {"wifi", "cell", "other", "none"}:
        _net = (kind, time.time())


def phone_net(max_age_s: float = 900) -> str:
    """wifi | cell | other | "" (не знаем: приложение молчало дольше 15 минут)."""
    kind, at = _net
    return kind if kind and time.time() - at <= max_age_s else ""


def _note_version(version: str) -> None:
    """28.09: какая версия приложения стоит у него (раньше не знали — журнал 2.5/2.6 так и не пришёл)."""
    global _app_version
    version = (version or "")[:40]
    if not version or version == _app_version:  # без заголовка (разговор по websocket) — не значит «старая версия»
        return
    _app_version = version
    logger.info("phone app: версия %s", version)
    from . import journal

    journal.miss(owner_id() or 0, "app_info", f"версия приложения {version}")


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
    "На записи человек, возможно, зовёт голосового ассистента по имени «JES» (читается «Джес»). "
    "Язык — русский или узбекский. Расшифруй запись дословно и реши, прозвучало ли имя как обращение. "
    "Похожие слова (жест, жесть, есть, здесь, джаз, Джек, джинсы) и прежние имена «Джарвис», «Нурай», «Зеки» — это НЕ имя. "
    'Верни JSON: {"name": true/false, "text": "дословно", "after": "слова после имени (пусто, если ничего)"}'
)


MEDIA_MIN_VOICE = 0.5  # пока играет видео (без эхоподавления) — сходство с его голосом не ниже этого


async def wake_check(request: web.Request) -> web.Response:
    """Телефон решил, что услышал «JES», — проверяем по записи, чтобы он не откликался на телевизор и похожие слова.

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
        logger.info("wake check: чужой голос (сходство %s, z %s, порог %s, банк %s) за %.2f с «%s»", voice.get("score"), voice.get("z"),
                    voice.get("threshold"), voice.get("bank"), took, (heard or {}).get("text", ""))
        _reject(uid, f"чужой голос: «{(heard or {}).get('text', '')[:60]}»")
        return web.json_response({"ok": False, "reason": "voice", "score": voice.get("score")})
    if heard is not None:
        # 26.09: пустая запись (кашель, стук) при «уверенном» телефоне тоже пропускалась — JES открывался сам по себе.
        # Теперь — только если распознаватель услышал имя
        ok, after, why = bool(heard["name"]), heard["after"], ""
        # 27.09: голос точно его (банк его записей ≥ 0.8 или уверенный отпечаток) — имя принимаем и в кривом прочтении
        # распознавателя («джой», «джесси», «дж», «с», обрезанное «позвони маме»): 236 отказов за сутки были им самим
        strong = wakeword.strong_voice(heard["text"], voice)
        # media — на телефоне играет видео/музыка (2.11+); aec — телефон слушает с эхоподавлением и сам вычитает этот звук (2.12+)
        media, aec = bool(data.get("media")), bool(data.get("aec"))
        if not ok:
            # во время видео — только чёткое имя, без поблажек
            ok, lenient_after, why = wakeword.lenient(heard["text"], strong=strong, confident=bool(data.get("confident")), uid=uid,
                                                      media=media)
            if ok:
                after = lenient_after
            else:
                wakeword.note_reject(uid, heard["text"], strong)
        else:
            wakeword.note_accept(uid, after)
        if ok and media and (heard.get("pos", 0) > (1 if aec else 0) or (not aec and float(voice.get("score") or 0) < MEDIA_MIN_VOICE)):
            # 28.09: «не реагировать на видео, даже если там я сам говорю «Джес»»: пока телефон играет видео, «Джес» — только
            # первым словом и голосом, точно похожим на его живой (без эхоподавления звук видео доходит до микрофона целиком)
            ok, why = False, "играет видео: имя не первым словом или голос не точно его"
        if ok and uid is not None:
            voiceprint.remember(uid, voice.get("_emb"))  # его «JES» — образец голоса для фраз этого разговора
            if voice.get("sure") and heard["name"] and not media:
                # в банк его настоящих записей — только чётко расслышанное имя и без видео на фоне (28.09: в банк попали
                # записи из видео, и похожие голоса проходили по нему)
                voiceprint.bank_add(uid, voice.get("_emb"))
        logger.info("wake check: %s за %.2f с «%s» (голос %s, z %s, банк %s, слово %s мс)%s%s", "да" if ok else "нет", took, heard["text"][:60],
                    voice.get("score"), voice.get("z"), voice.get("bank"), heard["ms"],
                    (" [видео" + (", эхоподавление]" if aec else "]")) if media else "", f" — {why}" if why else "")
        if not ok:
            _reject(uid, f"не «Джес»: «{heard['text'][:60]}»")
            return web.json_response({"ok": False, "text": heard["text"], "fast": True})
        if data.get("act") and after and uid is not None:
            # 27.09 «моментально и бесплатно»: телефон дождался конца фразы — команду делаем сразу, без разговора с Gemini
            done = await _instant_command(uid, heard["text"], after, data.get("device") if isinstance(data.get("device"), dict) else {})
            if done is not None:
                logger.info("wake check: сразу команда «%s» → %s за %.2f с (без Live)", heard["text"][:60], done["tool"], time.monotonic() - started)
                return web.json_response({"ok": True, "text": heard["text"], "fast": True, **done})
        return web.json_response({"ok": True, "text": heard["text"], "command": bool(after), "voice": voice.get("score"), "fast": True})
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
        _reject(uid, "не «Джес» (Gemini)")
    return web.json_response({"ok": ok, "text": str(verdict.get("text") or ""), "command": bool(after)})


async def wake_plan(request: web.Request) -> web.Response:
    """Ближайший подъём на фаджр — приложение ставит по нему будильник Android (bot/app_alarm.py)."""
    from . import app_alarm
    from .handlers.common import profile_by_id

    uid = owner_id()
    if uid is None:
        return web.json_response({"enabled": False})
    return web.json_response(await app_alarm.next_plan(await profile_by_id(uid)))


async def wake_settings(request: web.Request) -> web.Response:
    """Будильник из приложения (26.09 «чтобы и в боте менялся»): {"enabled": bool} | {"offset_delta": ±5} — те же настройки,
    что в боте (одна таблица), с той же проверкой окна фаджра. Ответ — как wake_plan."""
    from . import app_alarm, places, services, wake_runner
    from . import wake as wake_mod
    from .handlers.common import profile_by_id

    data = await _json(request)
    uid = owner_id()
    if uid is None:
        return web.json_response({"error": "no owner"}, status=400)
    profile = await profile_by_id(uid)
    s = wake_mod.WakeSettings.from_row(await services.wake_settings(uid))
    fields: dict[str, Any] = {}
    if "enabled" in data:
        if data["enabled"] and not places.has_place(uid):
            return web.json_response({"error": "Сначала место: в боте Будильник → 📍 Место"}, status=400)
        fields["enabled"] = bool(data["enabled"])
    if data.get("offset_delta"):
        offset = max(0, min(180, s.offset_min + int(data["offset_delta"])))
        if (err := await wake_runner.window_error(profile, s, offset=offset)):
            return web.json_response({"error": err}, status=400)
        fields["offset_min"] = offset
    if fields:
        await services.save_wake_settings(uid, fields)
    return web.json_response(await app_alarm.next_plan(profile))


async def wake_event(request: web.Request) -> web.Response:
    """Будильник в приложении: {"event": "scheduled", "day", "at_ms"} | {"event": "awake"} | {"event": "snooze", "minutes"}."""
    from . import app_alarm, wake_runner
    from .context import bot_instance
    from .handlers.common import profile_by_id

    data = await _json(request)
    uid = owner_id()
    if uid is None:
        return web.json_response({"error": "no owner"}, status=400)
    profile = await profile_by_id(uid)
    event = str(data.get("event") or "")
    if event == "scheduled" and data.get("day") and data.get("at_ms"):
        app_alarm.scheduled(uid, str(data["day"])[:10], int(data["at_ms"]))
        logger.info("app alarm %s: поставлен в телефоне на %s", uid, str(data["day"])[:10])
        return web.json_response({"ok": True})
    if event == "awake":
        await wake_runner.mark_awake(bot_instance(), profile, source="app")
        logger.info("app alarm %s: проснулся (кнопка в приложении)", uid)
        return web.json_response({"ok": True})
    if event == "snooze":
        until = await wake_runner.snooze(profile, int(data.get("minutes") or 5))
        return web.json_response({"ok": True, "until": until.astimezone(profile.tz).strftime("%H:%M")})
    return web.json_response({"error": "bad event"}, status=400)


async def _no_voice() -> dict[str, Any]:
    return {"ok": True}


async def _instant_command(uid: int, said: str, after: str, device: dict[str, Any]) -> dict[str, Any] | None:
    """«Джес, позвони маме» одной фразой: разобрать без ИИ и выполнить сразу — телефон получает готовые действия в ответе
    на проверку имени, разговор с Gemini не открывается (быстрее на ~1 с и $0). Не команда / не вышло — None (как раньше)."""
    from . import agent_tools, instant, phone_live, services
    from . import agent_tools_extra as extra
    from .handlers.common import profile_by_id

    cmd = instant.parse(after)
    first, _, rest = after.partition(" ")
    if cmd is None and rest and first[:1] in {"д", "ж", "ч"}:
        cmd = instant.parse(rest)  # имя расслышано как «джаз»/«жест» — команда после него
    if cmd is not None and cmd.tool == "live_mode":
        from . import billing

        billing.force_live(bool(cmd.args.get("on")))
        return None  # разговор откроется уже в нужном режиме — там JES и подтвердит голосом
    if cmd is None or (device.get("locked") and cmd.tool in phone_live.NEED_UNLOCK):
        return None  # заблокирован — после разблокировки доделает Gemini (ему нужна фраза)
    profile = await profile_by_id(uid)
    turn = phone.PhoneTurn(uid=uid, device=device)
    ctx = agent_tools.ToolContext(profile=profile, text=said)
    try:
        result = await phone.make_runner(turn)(cmd.tool, cmd.args, ctx)
    except Exception:
        logger.warning("instant: команда не вышла", exc_info=True)
        return None
    if not instant.succeeded(result) or not (turn.actions or (isinstance(result, dict) and result.get("calling_via_telegram"))):
        return None
    phone_live.discard(uid)  # заготовленный разговор с Gemini не нужен
    phone._later(extra.remember_exchange(uid, "📱 " + said, f"(сделано: {cmd.tool})", when=profile.now.strftime("%d.%m %H:%M")))
    phone._later(services.log_agent(uid, text=said, kind="phone_instant", tools=cmd.tool, reply="", ok=True))
    return {"done": True, "tool": cmd.tool, "actions": turn.actions, "need_contacts": turn.need_contacts}


def _reject(uid: int | None, why: str = "") -> None:
    """Не «JES» — заготовленный разговор с Gemini не нужен; в журнал — для ночного отчёта (сколько ложных «Джес»)."""
    if uid is not None:
        from . import journal, phone_live

        phone_live.discard(uid)
        journal.miss(uid, "false_wake", why)


async def geo_zones(request: web.Request) -> web.Response:
    """Напоминания по месту для телефона (2.13): {"version", "zones": [{"id", "lat", "lon", "radius", "when", "text"}]}."""
    from . import geo

    uid = owner_id()
    return web.json_response(geo.for_phone(uid) if uid is not None else {"version": 0, "zones": []})


async def geo_place(request: web.Request) -> web.Response:
    """«Джес, запомни, здесь мой дом»: телефон прислал, где он сейчас — {"name", "lat", "lon", "accuracy"}."""
    from . import geo

    uid = owner_id()
    data = await _json(request)
    try:
        lat, lon = float(data["lat"]), float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return web.json_response({"error": "lat/lon required"}, status=400)
    if uid is None:
        return web.json_response({"error": "no owner"}, status=400)
    p = geo.save_place(uid, str(data.get("name") or "дом"), lat, lon)
    return web.json_response({"ok": True, "place": p["name"]})


async def geo_fired(request: web.Request) -> web.Response:
    """Телефон: он пришёл в место / ушёл (зона Android). Совпало с напоминанием — бот пишет в чат."""
    from . import geo

    uid = owner_id()
    data = await _json(request)
    hit = geo.fired(uid, str(data.get("id") or ""), bool(data.get("entering"))) if uid is not None else None
    if hit is None:
        return web.json_response({"ok": False})

    async def tell() -> None:
        try:
            from .context import bot_instance
            from .profile import h

            where = ("🏠 " if hit["place"] == "дом" else "📍 ") + hit["place"].capitalize()
            await bot_instance().send_message(uid, f"{where}: <b>{h(hit['text'])}</b>")
        except Exception:
            logger.warning("geo: сообщение в чат не ушло", exc_info=True)

    phone._later(tell())
    return web.json_response({"ok": True, "text": hit["text"], "place": hit["place"]})


async def media_progress(request: web.Request) -> web.Response:
    """Где он остановился в YouTube (2.13, MediaWatcher): {"title", "channel", "position_s", "duration_s", "state"}."""
    from . import lessons

    uid = owner_id()
    if uid is None:
        return web.json_response({"error": "no owner"}, status=400)
    item = lessons.note(uid, await _json(request))
    return web.json_response({"ok": item is not None})


async def bank_notification(request: web.Request) -> web.Response:
    """Уведомление банка/SMS об операции (2.9): {"app", "package", "title", "text", "t"} → вопрос в боте «Записать?».
    Отвечаем сразу, разбор — фоном (телефону ждать нечего)."""
    from . import bank_events

    data = await _json(request)
    uid = owner_id()
    if uid is None:
        return web.json_response({"error": "no owner"}, status=400)

    async def run() -> None:
        try:
            res = await bank_events.receive(uid, data)
            if res.get("skipped"):
                logger.info("bank: пропустил (%s)", res["skipped"])
        except Exception:
            logger.exception("bank: уведомление не разобрано")

    phone._later(run())
    return web.json_response({"ok": True})


async def calendar_sync(request: web.Request) -> web.Response:
    """Календарь телефона (2.9): {"events": [...], "done": [ids выполненных просьб]} — ближайшие события для «что у меня
    завтра?» и утренней сводки; в ответ — просьбы из чата («добавь встречу…»), которые телефон ещё не сделал."""
    from . import calendar_sync as cal

    data = await _json(request)
    uid = owner_id()
    if uid is None:
        return web.json_response({"error": "no owner"}, status=400)
    events = data.get("events") if isinstance(data.get("events"), list) else []
    cal.save_events(uid, events)
    cal.mark_done(uid, [str(x) for x in data.get("done") or []])
    return web.json_response({"ok": True, "pending": cal.pending(uid)})


async def calendar_pending(request: web.Request) -> web.Response:
    """Есть ли просьбы из чата для календаря телефона (телефон спрашивает при включении экрана)."""
    from . import calendar_sync as cal

    uid = owner_id()
    return web.json_response({"pending": cal.pending(uid) if uid else []})


async def app_log(request: web.Request) -> web.Response:
    """Журнал с телефона (2.5): {"events": [{"t": мс, "level": "error"|"crash"|"info", "tag", "msg"}]} — ошибки и сбои
    приложения попадают в ночной отчёт, чинить можно без скриншотов."""
    from . import journal

    data = await _json(request)
    uid = owner_id()
    events = data.get("events") if isinstance(data.get("events"), list) else []
    n = journal.app_events(uid or 0, events)
    crashes = [e for e in events if isinstance(e, dict) and e.get("level") == "crash"]
    for e in crashes[:3]:
        logger.warning("app crash: %s %s", e.get("tag"), str(e.get("msg"))[:500])
    return web.json_response({"ok": True, "saved": n})


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
    count = phone.save_contacts(uid, raw)
    from . import phone_live

    phone._later(phone_live.prefetch_announcements(uid))
    return web.json_response({"ok": True, "count": count})


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
    """Запись голоса из приложения: 10 раз «JES» + ~40 с чтения → отпечаток и порог «только мой голос»."""
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
    app.router.add_get("/jarvis/v1/wake_plan", wake_plan)
    app.router.add_post("/jarvis/v1/wake_event", wake_event)
    app.router.add_post("/jarvis/v1/log", app_log)
    app.router.add_post("/jarvis/v1/bank", bank_notification)
    app.router.add_post("/jarvis/v1/media", media_progress)
    app.router.add_get("/jarvis/v1/geo", geo_zones)
    app.router.add_post("/jarvis/v1/geo/place", geo_place)
    app.router.add_post("/jarvis/v1/geo/fired", geo_fired)
    app.router.add_post("/jarvis/v1/calendar", calendar_sync)
    app.router.add_get("/jarvis/v1/calendar/pending", calendar_pending)
    app.router.add_post("/jarvis/v1/wake_settings", wake_settings)
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
    # 28.09: контейнер — в сети хоста (голос звонков Telegram напрямую по UDP), поэтому слушаем только 127.0.0.1 (JARVIS_HOST):
    # снаружи — через nginx, как и раньше
    host = os.getenv("JARVIS_HOST") or "0.0.0.0"
    _runner = web.AppRunner(build_app(), access_log=None)
    await _runner.setup()
    await web.TCPSite(_runner, host, port).start()
    logger.info("phone api listening on %s:%s (owner %s)", host, port, owner_id())
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

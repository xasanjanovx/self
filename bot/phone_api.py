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
  GET  /jarvis/v1/alarm_voice   — фразы будильника («Доброе утро, шеф! Пора вставать на фаджр») голосом бота, WAV в base64
  GET  /jarvis/v1/wake_state    — {"awake": bool}: встал ли уже (звонящий будильник замолкает сам)
  POST /jarvis/v1/test_wake     — проверить будильник сейчас: звонок в Telegram как утром (в журнал подъёмов не пишется)
  GET  /jarvis/v1/screen_rules  — экранное время: лимиты, «подряд», ночь, «занят» (bot/screentime.py)
  POST /jarvis/v1/screen_usage  — минуты по приложениям за сегодня (+ 7 прошлых дней) → вечерняя сводка, предложение лимитов
  POST /jarvis/v1/screen_alert  — порог сработал: говорить ли вслух; подробности — в чат бота
  GET  /jarvis/v1/nudge_voice   — нейтральные фразы («Сэр, у вас есть дела поважнее») голосом JES, WAV в base64
  POST /jarvis/v1/wake_check    — {"audio": WAV, "confident"} → его ли голос и прозвучало ли «JES» (защита от чужих и ТВ)
  POST /jarvis/v1/announce      — {"name": контакт, "app": Telegram…} → «Звонит мама» + WAV голосом бота
  POST /jarvis/v1/call_command  — {"audio": WAV, "caller"} → «ответь» / «сбрось» / «скажи, что перезвоню» во время звонка
  POST /jarvis/v1/contacts      — {"contacts": [{"n": имя, "p": [номера]}]} — телефонная книга
  POST /jarvis/v1/tg/login      — {"phone"} → код; {"code"} → вход или password_needed; {"password"}
  POST /jarvis/v1/tg/logout
  GET  /jarvis/v1/tg/status
  POST /jarvis/v1/tg/quick_send — {"name", "text"}: сбросил звонок в Telegram — «перезвоню» звонившему от его имени
  POST /jarvis/v1/watch/hello · audio · event · health · log, GET /jarvis/v1/watch/poll — часы Amazfit (bot/watch.py)
  GET  /jarvis/v1/phone/pull    — телефон ждёт действий с часов и сообщает, в руках ли он (bot/phone_link.py); POST …/phone/state
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
# 2.16 (29.09): за сутки телефон присылал 3331 ложную проверку «Джес» (шум «с», его разговор, чужие голоса) — ~330 МБ интернета
# и батарея. Серия отказов — телефон 20 с шлёт только уверенное «Джес»
COOLDOWN_AFTER = 3
COOLDOWN_WINDOW_S = 60.0
COOLDOWN_S = 20
_rejects: dict[int, list[float]] = {}

# 09.10 «иногда вообще не отвечает, даже когда чётко говорю Джес»: за вечер телефон слал ~270 проверок в час, распознаватель слышал в них
# «с» (голос его, z 4–5, а слово из-за короткого куска потеряно), и пауза 20 с после серии отказов закрывала телефон на пятую часть
# суток — его настоящее «Джес» в паузе вообще не проверялось. Теперь:
#   • отказ, где голос его (HIS_*), в серию не считается — пауза только от чужих звуков и шума;
#   • голос его, а слово не расслышано (≤ 2 слов) — второе мнение: Gemini слушает запись целиком (~1 с, доли цента).
HIS_SCORE = 0.60          # сходство голоса — это точно он…
HIS_BANK = 0.90           # …или запись почти как его подтверждённые «Джес»…
HIS_Z = 3.0               # …или нормированная оценка высокая при сходстве не ниже HIS_Z_SCORE
HIS_Z_SCORE = 0.40
SECOND_MAX_WORDS = 2      # короче этого — «слово потеряно»; длиннее — обычная речь, а не обращение
SECOND_STRONG_WORDS = 5   # …но при самом уверенном голосе слушаем и фразу до 5 слов, если она начинается похоже на имя («жэс открой ютуб»)
SECOND_GAP_S = 3.0        # не чаще раза в 3 с
SECOND_PER_DAY = 600
SECOND_TIMEOUT_S = 2.6    # телефон ждёт ответа 4 с
_second_state: dict[str, Any] = {"at": 0.0, "day": "", "n": 0}
MISS_KEEP = 40            # записей «его голос, а имя не принято» — DATA_DIR/wake_miss (послушать и понять, что не так)


def his_voice(voice: dict[str, Any]) -> bool:
    """Голос на записи — его (достаточно уверенно, чтобы не считать отказ шумом)."""
    def num(key: str) -> float:
        try:
            return float(voice.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    score, bank, z = num("score"), num("bank"), num("z")
    return score >= HIS_SCORE or bank >= HIS_BANK or (score >= HIS_Z_SCORE and z >= HIS_Z)


def _cooldown(uid: int | None, confident: bool, counts: bool = True) -> int:
    """Ещё один отказ: сколько секунд телефону не присылать неуверенные срабатывания (0 — можно).
    counts=False — отказ с его голосом (слово не расслышано): в серию не идёт."""
    if uid is None or confident or not counts:
        return 0
    now = time.monotonic()
    recent = [t for t in _rejects.get(uid, []) if now - t <= COOLDOWN_WINDOW_S] + [now]
    _rejects[uid] = recent[-10:]
    return COOLDOWN_S if len(recent) >= COOLDOWN_AFTER else 0


def ulaw_to_pcm16(data: bytes) -> bytes:
    """G.711 μ-law → PCM 16 бит (2.16: телефон шлёт запись «Джес» вдвое меньше)."""
    import numpy as np

    u = ~np.frombuffer(data, dtype=np.uint8)
    sign = u & 0x80
    exponent = ((u >> 4) & 0x07).astype(np.int32)
    mantissa = (u & 0x0F).astype(np.int32)
    sample = (((mantissa << 3) + 0x84) << exponent) - 0x84
    return np.where(sign != 0, -sample, sample).astype(np.int16).tobytes()


async def wake_check(request: web.Request) -> web.Response:
    """Телефон решил, что услышал «JES», — проверяем по записи, чтобы он не откликался на телевизор и похожие слова.

    Голос (свой отпечаток, ~0.05 с) и слово (локальный распознаватель, ~0.05–0.1 с) — параллельно; Gemini — только
    если распознаватель недоступен или ничего не расслышал. Пока проверяем, сервер уже готовит разговор с Gemini
    (phone_live.prewarm): подтвердили — телефон подключается к готовой сессии.
    """
    data = await _json(request)
    from . import phone_live

    try:
        if data.get("audio_ulaw"):  # 2.16: μ-law 8 бит, 16 кГц, без тишины перед словом
            pcm = ulaw_to_pcm16(base64.b64decode(str(data["audio_ulaw"]), validate=False))
            audio = phone_live.pcm_to_wav(pcm, int(data.get("rate") or 16000)) if pcm else b""
        else:
            audio = base64.b64decode(str(data.get("audio") or ""), validate=False)
    except (binascii.Error, ValueError):
        return web.json_response({"error": "bad audio"}, status=400)
    if not audio:
        return web.json_response({"error": "audio required"}, status=400)

    started = time.monotonic()
    uid = owner_id()
    if uid is not None:
        phone_live.prewarm(uid)
    device = data.get("device") if isinstance(data.get("device"), dict) else {}
    verdict = await judge_wake(uid, audio, confident=bool(data.get("confident")), media=bool(data.get("media")), aec=bool(data.get("aec")))
    if verdict.get("unchecked"):
        # проверка не удалась (сеть/лимит) — не мешаем: пусть решает телефон
        return web.json_response({"ok": True, "unchecked": True})
    if not verdict["ok"]:
        if verdict.get("reason") == "voice":
            return web.json_response({"ok": False, "reason": "voice", "score": verdict.get("score"),
                                      "cooldown": _cooldown(uid, bool(data.get("confident")))})
        if verdict.get("fast"):
            return web.json_response({"ok": False, "text": verdict["text"], "fast": True,
                                      "cooldown": _cooldown(uid, bool(data.get("confident")), counts=not verdict.get("his"))})
        return web.json_response({"ok": False, "text": verdict["text"], "command": False})
    if uid is not None:
        from . import watch

        # 04.10 его выбор «Часы, если надеты»: часы на руке слушают, а телефон заблокирован (в кармане) — отвечают часы
        if watch.should_answer(uid, phone_locked=bool(device.get("locked"))):
            watch.take_over(uid, verdict["text"], audio if verdict.get("after") else None)
            logger.info("wake check: «%s» — отвечают часы (они на руке, телефон заблокирован)", verdict["text"][:60])
            return web.json_response({"ok": False, "reason": "watch", "text": verdict["text"]})
    after = verdict.get("after") or ""
    if not verdict.get("fast"):
        return web.json_response({"ok": True, "text": verdict["text"], "command": bool(after)})
    if data.get("act") and after and uid is not None:
        # 27.09 «моментально и бесплатно»: телефон дождался конца фразы — команду делаем сразу, без разговора с Gemini
        done = await _instant_command(uid, verdict["text"], after, device)
        if done is not None:
            logger.info("wake check: сразу команда «%s» → %s за %.2f с (без Live)", verdict["text"][:60], done["tool"], time.monotonic() - started)
            return web.json_response({"ok": True, "text": verdict["text"], "fast": True, **done})
    return web.json_response({"ok": True, "text": verdict["text"], "command": bool(after), "voice": verdict.get("voice"), "fast": True})


async def judge_wake(uid: int | None, audio: bytes, *, confident: bool = False, media: bool = False, aec: bool = False,
                     source: str = "телефон") -> dict[str, Any]:
    """Прозвучало ли «JES» его голосом (WAV 16 кГц). Общее для телефона (wake_check) и часов (bot/watch.py).

    → {"ok", "text", "after", "voice", "fast"} | {"ok": False, "reason": "voice", "score", "text"} | {"ok": True, "unchecked": True}.
    Голос (свой отпечаток, ~0.05 с) и слово (локальный распознаватель, ~0.05–0.1 с) — параллельно; Gemini — только
    если распознаватель недоступен."""
    from . import voiceprint, wakeword

    started = time.monotonic()
    where = "" if source == "телефон" else f" [{source}]"
    tag = "" if source == "телефон" else f"{source}: "
    voice, heard = await asyncio.gather(voiceprint.verify(uid, audio) if uid is not None else _no_voice(), wakeword.check(audio))
    took = time.monotonic() - started
    if not voice.get("ok"):
        # чужой голос и телевизор отсекаем сразу
        logger.info("wake check%s: чужой голос (сходство %s, z %s, порог %s, банк %s) за %.2f с «%s»", where, voice.get("score"), voice.get("z"),
                    voice.get("threshold"), voice.get("bank"), took, (heard or {}).get("text", ""))
        _reject(uid, f"{tag}чужой голос: «{(heard or {}).get('text', '')[:60]}»", source=source)
        return {"ok": False, "reason": "voice", "score": voice.get("score"), "text": (heard or {}).get("text", "")}
    if heard is not None:
        # 26.09: пустая запись (кашель, стук) при «уверенном» телефоне тоже пропускалась — JES открывался сам по себе.
        # Теперь — только если распознаватель услышал имя
        ok, after, why = bool(heard["name"]), heard["after"], ""
        # 27.09: голос точно его (банк его записей ≥ 0.8 или уверенный отпечаток) — имя принимаем и в кривом прочтении
        # распознавателя («джой», «джесси», «дж», «с», обрезанное «позвони маме»): 236 отказов за сутки были им самим
        strong = wakeword.strong_voice(heard["text"], voice)
        # media — на телефоне играет видео/музыка (2.11+); aec — телефон слушает с эхоподавлением и сам вычитает этот звук (2.12+)
        if not ok:
            # во время видео — только чёткое имя, без поблажек
            ok, lenient_after, why = wakeword.lenient(heard["text"], strong=strong, confident=confident, uid=uid, media=media)
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
        said = heard["text"]
        his = his_voice(voice)
        if not ok and his and not media and source == "телефон":
            # 09.10: голос его, а слово потеряно («с», «в», пусто, «же стоит») — пусть послушает Gemini целиком
            words = wakeword.words(heard["text"])
            if len(words) <= SECOND_MAX_WORDS or (len(words) <= SECOND_STRONG_WORDS and strong and wakeword.name_like(words[0])):
                opinion = await second_opinion(audio)
                if opinion is not None and opinion["ok"]:
                    ok, after, why, said = True, opinion["after"], f"второе мнение (Gemini): «{opinion['text'][:40]}»", opinion["text"] or said
                elif opinion is not None:
                    why = "второе мнение (Gemini): имени нет"
            if not ok:
                _keep_miss(audio, heard["text"], voice)
        if ok and uid is not None:
            voiceprint.remember(uid, voice.get("_emb"))  # его «JES» — образец голоса для фраз этого разговора
            if voice.get("sure") and heard["name"] and not media and source == "телефон":
                # в банк его настоящих записей — только чётко расслышанное имя и без видео на фоне (28.09: в банк попали
                # записи из видео, и похожие голоса проходили по нему); микрофон часов звучит иначе — в банк не берём
                voiceprint.bank_add(uid, voice.get("_emb"))
        logger.info("wake check%s: %s за %.2f с «%s» (голос %s, z %s, банк %s, слово %s мс)%s%s", where, "да" if ok else "нет", took,
                    heard["text"][:60], voice.get("score"), voice.get("z"), voice.get("bank"), heard["ms"],
                    (" [видео" + (", эхоподавление]" if aec else "]")) if media else "", f" — {why}" if why else "")
        if not ok:
            _reject(uid, f"{tag}не «Джес»: «{heard['text'][:60]}»", source=source)
            return {"ok": False, "text": heard["text"], "fast": True, "his": his}
        return {"ok": True, "text": said, "after": after, "voice": voice.get("score"), "fast": True}
    try:
        raw = await ai.generate([{"text": _WAKE_PROMPT}, {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(audio).decode()}}],
                                model=ai.transcribe_model, temperature=0.0, json_mode=True, max_tokens=200)
        verdict = json.loads(raw) if raw.strip().startswith("{") else {}
    except Exception:
        logger.warning("wake check%s failed", where, exc_info=True)
        return {"ok": True, "unchecked": True, "text": "", "after": ""}
    ok = bool(verdict.get("name"))
    after = str(verdict.get("after") or "").strip()
    logger.info("wake check%s: %s за %.1f с «%s» (Gemini)", where, "да" if ok else "нет", time.monotonic() - started, str(verdict.get("text") or "")[:60])
    if not ok:
        _reject(uid, f"{tag}не «Джес» (Gemini)", source=source)
    return {"ok": ok, "text": str(verdict.get("text") or ""), "after": after}


async def second_opinion(audio: bytes) -> dict[str, Any] | None:
    """Голос его, а локальный распознаватель имени не услышал: Gemini слушает запись целиком.
    → {"ok", "text", "after"}; None — не спрашивали (лимит) или Gemini не успел/не ответил (тогда как раньше — отказ)."""
    from . import wakeword

    now = time.monotonic()
    day = time.strftime("%Y-%m-%d")
    state = _second_state
    if state["day"] != day:
        state["day"], state["n"] = day, 0
    if state["n"] >= SECOND_PER_DAY or now - state["at"] < SECOND_GAP_S:
        return None
    state["at"], state["n"] = now, state["n"] + 1
    try:
        raw = await asyncio.wait_for(
            ai.generate([{"text": _WAKE_PROMPT}, {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(audio).decode()}}],
                        model=ai.transcribe_model, temperature=0.0, json_mode=True, max_tokens=200), timeout=SECOND_TIMEOUT_S)
        verdict = json.loads(raw) if str(raw).strip().startswith("{") else {}
    except Exception as exc:
        logger.info("wake check: второе мнение не получено (%s)", type(exc).__name__)
        return None
    text = str(verdict.get("text") or "").strip()
    after = str(verdict.get("after") or "").strip()
    words = wakeword.words(text)
    # модель могла выдумать имя из шума: в её же расшифровке должно быть слово, похожее на «Джес», в начале фразы
    named = bool(verdict.get("name")) and bool(words) and (wakeword.match(text)[0] or any(wakeword.name_like(w) for w in words[:3]))
    logger.info("wake check: второе мнение (Gemini) — %s «%s»", "имя есть" if named else "имени нет", text[:60])
    return {"ok": named, "text": text, "after": after}


def _keep_miss(audio: bytes, text: str, voice: dict[str, Any]) -> None:
    """Записи, где голос его, а имя не принято, — DATA_DIR/wake_miss (последние MISS_KEEP): послушать и понять, что не так."""
    try:
        from .tg_user import data_dir

        folder = data_dir() / "wake_miss"
        folder.mkdir(parents=True, exist_ok=True)
        label = re.sub(r"\W+", "_", text.lower()).strip("_")[:20] or "empty"
        score = int(float(voice.get("score") or 0) * 100)
        (folder / f"{int(time.time() * 1000)}_{score}_{label}.wav").write_bytes(audio)
        for old in sorted(folder.glob("*.wav"))[:-MISS_KEEP]:
            old.unlink(missing_ok=True)
    except OSError:
        logger.debug("wake miss: не записала", exc_info=True)


async def wake_plan(request: web.Request) -> web.Response:
    """Ближайший подъём на фаджр — приложение ставит по нему будильник Android (bot/app_alarm.py)."""
    from . import app_alarm
    from .handlers.common import profile_by_id

    uid = owner_id()
    if uid is None:
        return web.json_response({"enabled": False})
    return web.json_response(await app_alarm.next_plan(await profile_by_id(uid)))


async def wake_settings(request: web.Request) -> web.Response:
    """Будильник из приложения (26.09 «чтобы и в боте менялся»): {"enabled": bool} | {"offset_delta": ±5} | {"app_alarm": bool} —
    те же настройки, что в боте (одна таблица), с той же проверкой окна фаджра. Ответ — как wake_plan."""
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
    if "app_alarm" in data:
        app_alarm.set_app(uid, bool(data["app_alarm"]))   # дополнительный будильник в приложении (звонок Telegram идёт в любом случае)
    if fields:
        await services.save_wake_settings(uid, fields)
    return web.json_response(await app_alarm.next_plan(profile))


async def wake_event(request: web.Request) -> web.Response:
    """Будильник в приложении: {"event": "scheduled", "day", "at_ms"} | {"event": "ring"}; «awake» и «snooze» приходят, но подъём
    и звонки не трогают (подъём засчитывает только голос в звонке Telegram)."""
    from . import app_alarm
    from .handlers.common import profile_by_id

    data = await _json(request)
    uid = owner_id()
    if uid is None:
        return web.json_response({"error": "no owner"}, status=400)
    profile = await profile_by_id(uid)
    event = str(data.get("event") or "")
    if event in {"scheduled", "ring"} and not app_alarm.app_enabled(uid):
        # будильник в приложении выключен (по умолчанию): будит звонок Telegram; если на телефоне остался старый будильник — не мешаем
        logger.info("app alarm %s: событие %s проигнорировано (будильник приложения выключен)", uid, event)
        return web.json_response({"ok": True, "ignored": True})
    if event == "scheduled" and data.get("day") and data.get("at_ms"):
        app_alarm.scheduled(uid, str(data["day"])[:10], int(data["at_ms"]))
        logger.info("app alarm %s: поставлен в телефоне на %s", uid, str(data["day"])[:10])
        return web.json_response({"ok": True})
    if event == "ring":
        app_alarm.rang(uid, profile.today.isoformat())
        logger.info("app alarm %s: звонит в телефоне (звонок Telegram идёт в то же время)", uid)
        return web.json_response({"ok": True})
    if event in {"awake", "snooze"}:
        # 04.10: он нажал «Проснулся» в приложении через 8 секунд после звонка, сервер засчитал подъём — и Telegram не позвонил.
        # Подъём засчитывает только голос в звонке (или JES на телефоне, когда он ответил ей голосом); «ещё 5 минут» звонок не откладывает
        logger.info("app alarm %s: %s в приложении — звонок Telegram это не отменяет", uid, event)
        return web.json_response({"ok": True, "ignored": True})
    return web.json_response({"error": "bad event"}, status=400)


async def wake_state(request: web.Request) -> web.Response:
    """Звонящий будильник спрашивает: уже встал (ответил в звонке Telegram, нажал в боте)? Взял трубку звонка Telegram? Тогда он
    замолкает сам (оборвётся звонок и он не встал — Telegram перезвонит, а будильник в приложении уже не нужен)."""
    from . import app_alarm, services
    from .handlers.common import profile_by_id

    uid = owner_id()
    if uid is None:
        return web.json_response({"awake": False})
    profile = await profile_by_id(uid)
    log = await services.wake_log(uid, profile.today) or {}
    return web.json_response({"awake": bool(log.get("woke_at")) or app_alarm.in_call(uid), "day": profile.today.isoformat()})


async def test_wake(request: web.Request) -> web.Response:
    """Проверить будильник сейчас: звонок в Telegram ровно как утром (в журнал подъёмов не пишется)."""
    from . import call_assistant, caller
    from .handlers.common import profile_by_id

    uid = owner_id()
    if uid is None:
        return web.json_response({"error": "no owner"}, status=400)
    if not caller.available():
        return web.json_response({"error": "звонки не настроены"}, status=503)
    task = call_assistant.wake_test_in_background(await profile_by_id(uid))
    return web.json_response({"calling": task is not None, "busy": task is None})


async def screen_rules(request: web.Request) -> web.Response:
    """Экранное время: что считать и когда говорить (bot/screentime.py). Телефон берёт раз в 30 минут, только при включённом экране."""
    from . import screentime
    from .handlers.common import profile_by_id

    uid = owner_id()
    if uid is None:
        return web.json_response({"enabled": False})
    return web.json_response(await screentime.rules(await profile_by_id(uid)))


async def screen_usage(request: web.Request) -> web.Response:
    """Минуты по приложениям за сегодня (и раз в день — 7 прошлых дней). Первая неделя пришла — предлагаем лимиты в чате."""
    from . import screentime
    from .context import bot_instance
    from .handlers.common import profile_by_id

    data = await _json(request)
    uid = owner_id()
    if uid is None:
        return web.json_response({"error": "no owner"}, status=400)
    proposal = screentime.record_usage(uid, data)
    if proposal:
        profile = await profile_by_id(uid)
        try:
            st = screentime.load(uid)
            await bot_instance().send_message(uid, screentime.proposal_text(profile, proposal),
                                              reply_markup=screentime.settings_keyboard(profile, st))
            logger.info("screentime %s: предложил лимиты %s", uid, proposal["limits"])
        except Exception:
            logger.warning("screentime: предложение не отправилось", exc_info=True)
    return web.json_response({"ok": True, "proposed": bool(proposal)})


async def screen_alert(request: web.Request) -> web.Response:
    """Телефон: «порог — пора сказать». Ответ: говорить ли (занят / лимит в час); подробности сервер шлёт в чат сам."""
    from . import screentime
    from .context import bot_instance
    from .handlers.common import profile_by_id

    data = await _json(request)
    uid = owner_id()
    if uid is None:
        return web.json_response({"speak": False})
    return web.json_response(await screentime.alert(bot_instance(), await profile_by_id(uid), data))


async def nudge_voice(request: web.Request) -> web.Response:
    """Нейтральные фразы экранного времени («Сэр, у вас есть дела поважнее») голосом JES, WAV в base64 — телефон хранит их у себя."""
    from . import nudge_voice as nv

    uid = owner_id()
    return web.json_response(await nv.clips(uid) if uid else {"clips": []})


async def alarm_voice(request: web.Request) -> web.Response:
    """Голосовые фразы будильника («Доброе утро, шеф! Пора вставать на фаджр»): телефон хранит их и произносит между звонками мелодии."""
    from . import alarm_voice as av

    uid = owner_id()
    return web.json_response(await av.clips(uid) if uid else {"clips": []})


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
    if not instant.succeeded(result) or not (turn.actions or (isinstance(result, dict) and (result.get("calling_via_telegram") or result.get("server_done")))):
        return None  # server_done — умный дом: команда ушла на сервере, телефону нечего делать (он просто вибрирует)
    phone_live.discard(uid)  # заготовленный разговор с Gemini не нужен
    phone._later(extra.remember_exchange(uid, "📱 " + said, f"(сделано: {cmd.tool})", when=profile.now.strftime("%d.%m %H:%M")))
    phone._later(services.log_agent(uid, text=said, kind="phone_instant", tools=cmd.tool, reply="", ok=True))
    return {"done": True, "tool": cmd.tool, "actions": turn.actions, "need_contacts": turn.need_contacts}


def _reject(uid: int | None, why: str = "", source: str = "телефон") -> None:
    """Не «JES» — заготовленный разговор с Gemini не нужен; в журнал — для ночного отчёта (сколько ложных «Джес»)."""
    if uid is not None:
        from . import journal, phone_live

        if source == "телефон":
            phone_live.discard(uid)   # заготовка — телефонная; часы её не трогают
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
    zone = str(data.get("id") or "")
    if uid is not None and zone.startswith(geo.TRACK_PREFIX):
        # 29.09: зона сохранённого места — пришёл/ушёл: в память дел и «где он», без уведомления
        from . import where

        where.arrived(uid, zone[len(geo.TRACK_PREFIX):], bool(data.get("entering")))
        return web.json_response({"ok": False, "tracked": True})
    hit = geo.fired(uid, zone, bool(data.get("entering"))) if uid is not None else None
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


async def where_update(request: web.Request) -> web.Response:
    """29.09: где он — телефон присылает, когда он зовёт JES: {"lat", "lon", "acc", "age_s"} (приложение 2.17)."""
    from . import where

    uid = owner_id()
    data = await _json(request)
    row = where.update(uid, data.get("lat"), data.get("lon"), accuracy=data.get("acc"), age_s=data.get("age_s"))
    return web.json_response({"ok": row is not None, "place": (row or {}).get("place")})


async def media_progress(request: web.Request) -> web.Response:
    """Где он остановился в YouTube (2.13, MediaWatcher): {"title", "channel", "position_s", "duration_s", "state"}."""
    from . import lessons

    uid = owner_id()
    if uid is None:
        return web.json_response({"error": "no owner"}, status=400)
    data = await _json(request)
    await lessons.adopt_daily_links(uid)  # плейлисты его ежедневных дел — тоже «присланные им»
    item = lessons.note(uid, data)  # 30.09: чужие ролики (не присланные ссылкой) не запоминаются
    if item is not None:
        from . import deeds

        deeds.note(uid, "lesson", src="телефон", text=f"смотрели: {item['title'][:80]}", dedupe_s=6 * 3600)
    return web.json_response({"ok": item is not None})


async def tv_event(request: web.Request) -> web.Response:
    """02.10: приложение сопряглось с ТВ на Android TV / забыло его: {"event": "paired", "name": "Artel …"} | {"event": "forget"} —
    после этого «Джес, включи ТВ» превращается в действие для телефона (bot/smarthome.py: tv_action)."""
    from . import smarthome

    uid = owner_id()
    if uid is None:
        return web.json_response({"error": "no owner"}, status=400)
    data = await _json(request)
    event = str(data.get("event") or "")
    if event == "paired":
        row = smarthome.register_phone_tv(uid, str(data.get("name") or "ТВ"))
        logger.info("tv %s: сопряжён (%s)", uid, row.get("name"))
        return web.json_response({"ok": True, "name": row.get("name")})
    if event == "forget":
        return web.json_response({"ok": smarthome.forget_phone_tv(uid)})
    return web.json_response({"error": "bad event"}, status=400)


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
    name, app = str(data.get("name") or ""), str(data.get("app") or "")
    from . import deeds

    deeds.note(uid, "incoming_call", {"who": name, "app": app}, src="телефон", dedupe_s=180)
    return web.json_response(await phone_live.announce(uid, name, app))


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


# ------------------------------------------------------------------ часы Amazfit (bot/watch.py) и канал на телефон (bot/phone_link.py)
def _owner_or_500() -> int:
    uid = owner_id()
    if uid is None:
        raise web.HTTPInternalServerError(text='{"error": "owner is not configured"}', content_type="application/json")
    return uid


async def watch_hello(request: web.Request) -> web.Response:
    from . import watch

    return web.json_response(await watch.hello(_owner_or_500(), await _json(request)))


async def watch_audio(request: web.Request) -> web.Response:
    from . import watch

    return web.json_response(await watch.audio(_owner_or_500(), await _json(request)))


async def watch_poll(request: web.Request) -> web.Response:
    from . import watch

    return web.json_response(await watch.poll(_owner_or_500(), request.query.get("sid", "")))


async def watch_event(request: web.Request) -> web.Response:
    from . import watch

    return web.json_response(await watch.event(_owner_or_500(), await _json(request)))


async def watch_health(request: web.Request) -> web.Response:
    from . import watch

    return web.json_response(await watch.health(_owner_or_500(), await _json(request)))


async def watch_log(request: web.Request) -> web.Response:
    from . import watch

    return web.json_response(watch.note_log(_owner_or_500(), await _json(request)))


def _flag(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    return value.lower() in {"1", "true", "yes", "on"}


async def phone_pull(request: web.Request) -> web.Response:
    """Телефон ждёт действий для себя (с часов: позвонить, включить музыку, «где телефон?») и сообщает, в руках ли он."""
    from . import phone_link

    uid = _owner_or_500()
    phone_link.note_state(uid, screen=_flag(request.query.get("screen")), locked=_flag(request.query.get("locked")))
    return web.json_response(await phone_link.pull(uid))


async def phone_state(request: web.Request) -> web.Response:
    """Телефон сразу сообщает: экран включён/выключен, разблокирован — без ожидания."""
    from . import phone_link

    uid = _owner_or_500()
    data = await _json(request)
    screen = data.get("screen")
    locked = data.get("locked")
    phone_link.note_state(uid, screen=bool(screen) if screen is not None else None, locked=bool(locked) if locked is not None else None)
    return web.json_response({"ok": True, "asleep": phone_link.asleep(uid)})


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
    app.router.add_get("/jarvis/v1/wake_state", wake_state)
    app.router.add_get("/jarvis/v1/alarm_voice", alarm_voice)
    app.router.add_post("/jarvis/v1/test_wake", test_wake)
    app.router.add_get("/jarvis/v1/screen_rules", screen_rules)
    app.router.add_post("/jarvis/v1/screen_usage", screen_usage)
    app.router.add_post("/jarvis/v1/screen_alert", screen_alert)
    app.router.add_get("/jarvis/v1/nudge_voice", nudge_voice)
    app.router.add_post("/jarvis/v1/log", app_log)
    app.router.add_post("/jarvis/v1/bank", bank_notification)
    app.router.add_post("/jarvis/v1/media", media_progress)
    app.router.add_post("/jarvis/v1/tv", tv_event)
    app.router.add_post("/jarvis/v1/where", where_update)
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
    app.router.add_post("/jarvis/v1/watch/hello", watch_hello)
    app.router.add_post("/jarvis/v1/watch/audio", watch_audio)
    app.router.add_get("/jarvis/v1/watch/poll", watch_poll)
    app.router.add_post("/jarvis/v1/watch/event", watch_event)
    app.router.add_post("/jarvis/v1/watch/health", watch_health)
    app.router.add_post("/jarvis/v1/watch/log", watch_log)
    app.router.add_get("/jarvis/v1/phone/pull", phone_pull)
    app.router.add_post("/jarvis/v1/phone/state", phone_state)
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

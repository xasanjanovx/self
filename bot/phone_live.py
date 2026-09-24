"""Живой разговор с Джарвисом на телефоне (как Gemini Live): телефон ⇄ WebSocket ⇄ Gemini Live.

Тот же голос, характер и инструменты, что в звонках бота (bot/live_call.py, режим "phone"), плюс
телефонные действия (bot/phone.py): звонок, SMS, Telegram от имени владельца, будильник, таймер,
приложения, фонарик, громкость, музыка, маршрут, системные кнопки. Действия выполняет само приложение.

Протокол /jarvis/v1/live (заголовок Authorization: Bearer <JARVIS_TOKEN>):
  телефон → сервер
    {"type":"hello","device":{…},"greet":bool,"text":str?} — первым сообщением
    бинарные кадры — микрофон, PCM s16le 16 кГц моно
    {"type":"text","text":…} — написанная реплика; {"type":"greet"} — позвали только по имени
    {"type":"action_error","text":…} — действие на телефоне не удалось; {"type":"bye"}
  сервер → телефон
    бинарные кадры — голос Джарвиса, PCM s16le 24 кГц моно
    {"type":"ready"} {"type":"user","text":…} {"type":"jarvis","text":…} {"type":"turn_complete"} {"type":"interrupted"}
    {"type":"action","action":{…}} {"type":"status","text":…} {"type":"need_contacts"} {"type":"end"} {"type":"error","text":…}
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import time
import wave
from typing import Any

from . import billing
from . import live_call
from . import phone
from . import services
from . import undo
from .context import ai
from .live_call import MAX_SECONDS, _decode, _jsonable, _send_to_chat, _Session

logger = logging.getLogger(__name__)

INPUT_RATE = 16000
VAD_SILENCE_MS = int(os.getenv("PHONE_VAD_SILENCE_MS") or 700)
GREET = "[Он позвал тебя по имени и ждёт. Откликнись одним-двумя словами («Да?», «Слушаю»), без приветствий.]"

_TOOL_STATUS = {
    "web_search": "Ищу в интернете…", "weather": "Смотрю погоду…", "currency_rates": "Смотрю курс…",
    "telegram_read": "Читаю Telegram…", "telegram_send": "Готовлю сообщение…", "deep_analysis": "Анализирую…",
    "list_finance_entries": "Смотрю операции…", "get_finance_stats": "Считаю…",
}


def phone_declarations() -> list[dict[str, Any]]:
    return [t.declaration() for t in phone.PHONE_TOOLS.values()]


# Он выбрал «всё без разблокировки»: звонки, SMS, Telegram, журнал — сразу. Разблокировка нужна только там, где
# открывается другое приложение или экран (Android сам не покажет его поверх блокировки).
NEED_UNLOCK = {"open_app", "open_link", "navigate", "settings_panel", "look", "screen_look", "whatsapp_send", "play_media", "taxi",
               "call_forwarding", "gallery"}

_SKY_ICON = (("гроз", "⛈"), ("снег", "🌨"), ("дожд", "🌧"), ("ливн", "🌧"), ("морос", "🌦"), ("туман", "🌫"), ("пасмур", "☁️"),
             ("облач", "⛅"), ("ясн", "☀️"))


def result_card(name: str, args: dict[str, Any], result: dict[str, Any]) -> dict[str, Any] | None:
    """Карточка результата для панели на телефоне (над субтитрами). Звонки/таймеры телефон рисует сам по действиям."""
    if not isinstance(result, dict):
        return None
    if name == "weather" and isinstance(result.get("now"), dict):
        now = result["now"]
        sky = str(now.get("sky") or "")
        icon = next((i for k, i in _SKY_ICON if k in sky.lower()), "🌤")
        temp = now.get("temp")
        feels = now.get("feels")
        return {"icon": icon, "title": f"{round(temp)}° · {sky}" if isinstance(temp, (int, float)) else sky,
                "subtitle": str(result.get("place") or "") + (f" · ощущается {round(feels)}°" if isinstance(feels, (int, float)) else "")}
    if name in {"telegram_send", "send_sms"} and result.get("status") == "awaiting_confirmation":
        return {"icon": "✉️", "title": "Отправить?", "subtitle": str(result.get("ask_exactly") or "")[:160], "accent": "confirm"}
    if name == "confirm_send" and result.get("ok"):
        return {"icon": "✅", "title": "Отправлено", "subtitle": str(result.get("to") or "")}
    if name == "add_reminder" and isinstance(result.get("added"), dict):
        r = result["added"]
        return {"icon": "🔔", "title": "Напомню", "subtitle": " ".join(str(r.get(k) or "") for k in ("time", "text") if r.get(k)).strip()[:120]}
    if name == "add_task" and args.get("text"):
        return {"icon": "📝", "title": "Задача", "subtitle": str(args.get("text"))[:120]}
    return None


class PhoneLive(_Session):
    def __init__(self, profile, persona, *, system: str, phone_ws, device: dict[str, Any]) -> None:  # noqa: ANN001
        super().__init__(profile, persona, mode="phone", system=system)
        self.phone_ws = phone_ws
        self.turn = phone.PhoneTurn(uid=self.uid, device=device)
        self.runner = phone.make_runner(self.turn)
        self._phone_lock = asyncio.Lock()
        self._gem_lock = self._send_lock
        self.user_lines: list[str] = []
        self.jarvis_lines: list[str] = []

    # --- Gemini: быстрее понимать, что он договорил (по умолчанию модель ждёт паузу ~2–3 с)
    vad_tuned = True

    def setup_payload(self, model: str, *, rich: bool) -> dict[str, Any]:
        payload = super().setup_payload(model, rich=rich)
        if self.vad_tuned:
            vad: dict[str, Any] = {"endOfSpeechSensitivity": "END_SENSITIVITY_HIGH", "silenceDurationMs": VAD_SILENCE_MS, "prefixPaddingMs": 200}
            if self.turn.device.get("duplex"):
                # телефон шлёт микрофон и пока Джарвис говорит (эхоподавление): остаток эха не должен его перебивать
                vad["startOfSpeechSensitivity"] = "START_SENSITIVITY_LOW"
            payload["setup"]["realtimeInputConfig"] = {"automaticActivityDetection": vad}
        return payload

    async def connect(self, session):  # noqa: ANN001
        try:
            return await super().connect(session)
        except Exception as exc:
            if not self.vad_tuned:
                raise
            logger.warning("phone live: настройка VAD не принята (%s) — без неё", str(exc)[:160])
            self.vad_tuned = False
            return await super().connect(session)

    # --- связь с телефоном и с Gemini
    async def to_phone(self, payload: dict[str, Any] | bytes) -> None:
        if self.phone_ws.closed:
            self.stop.set()
            return
        try:
            async with self._phone_lock:
                if isinstance(payload, (bytes, bytearray)):
                    await self.phone_ws.send_bytes(bytes(payload))
                else:
                    await self.phone_ws.send_str(json.dumps(payload, ensure_ascii=False, default=str))
        except Exception:
            self.stop.set()

    async def to_gemini(self, gem, payload: dict[str, Any]) -> None:  # noqa: ANN001
        try:
            async with self._gem_lock:
                await gem.send_str(json.dumps(payload, ensure_ascii=False, default=str))
        except Exception:
            self.stop.set()

    async def say_text(self, gem, text: str) -> None:  # noqa: ANN001
        await self.to_gemini(gem, {"clientContent": {"turns": [{"role": "user", "parts": [{"text": text}]}], "turnComplete": True}})

    async def pump_phone(self, gem) -> None:  # noqa: ANN001
        """Телефон → Gemini: микрофон и служебные сообщения."""
        import aiohttp

        async for msg in self.phone_ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                await self.to_gemini(gem, {"realtimeInput": {"audio": {"data": base64.b64encode(msg.data).decode(),
                                                                       "mimeType": f"audio/pcm;rate={INPUT_RATE}"}}})
            elif msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                kind = data.get("type")
                if kind == "text" and str(data.get("text") or "").strip():
                    await self.to_phone({"type": "user", "text": str(data["text"]), "final": True})
                    self.user_lines.append(str(data["text"]))
                    await self.say_text(gem, str(data["text"]))
                elif kind == "device" and isinstance(data.get("device"), dict):
                    self.turn.device.update(data["device"])
                elif kind == "unlocked":
                    self.turn.device["locked"] = False
                    await self.say_text(gem, "[Телефон разблокирован — сразу сделай то, что он просил.]")
                elif kind == "greet":
                    await self.say_text(gem, GREET)
                elif kind == "image" and data.get("data"):
                    # кадр живой камеры (JPEG ~1 в секунду) — модель «видит», что он показывает
                    await self.to_gemini(gem, {"realtimeInput": {"video": {"data": str(data["data"]), "mimeType": str(data.get("mime") or "image/jpeg")}}})
                elif kind in {"camera", "screen"}:
                    what = "Камера" if kind == "camera" else "Показ экрана"
                    seen = "то, что он показывает камерой" if kind == "camera" else "экран его телефона (переведи, объясни, прочитай — что попросит)"
                    note = (f"[{what} включена — ты видишь {seen}. Кадры идут потоком.]" if data.get("on")
                            else f"[{what} выключена — ты больше ничего не видишь.]")
                    await self.to_gemini(gem, {"clientContent": {"turns": [{"role": "user", "parts": [{"text": note}]}], "turnComplete": False}})
                elif kind == "note" and str(data.get("text") or "").strip():
                    # служебная пометка от телефона («вот последние 2 фото из галереи») — модель отвечает сама
                    await self.say_text(gem, f"[{str(data['text'])[:300]}]")
                elif kind == "action_error":
                    await self.say_text(gem, f"[Действие на телефоне не удалось: {str(data.get('text') or '')[:200]}. Коротко скажи ему об этом.]")
                elif kind == "bye":
                    break
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
        self.stop.set()

    async def downlink(self, ws) -> None:  # noqa: ANN001
        """Gemini → телефон: голос, субтитры, перебивания, инструменты."""
        while not self.stop.is_set():
            msg = await ws.receive()
            data = _decode(msg)
            if data is None:
                if msg.type.name in {"CLOSE", "CLOSED", "CLOSING", "ERROR"}:
                    extra = str(getattr(msg, "extra", "") or "")
                    logger.info("phone live: Gemini закрыл соединение (%s)", extra)
                    if billing.is_billing_error(None, extra):
                        billing.exhausted(extra)
                        await self.to_phone({"type": "error", "text": "Баланс Gemini закончился — пополните в AI Studio"})
                    self.stop.set()
                    return
                continue
            if "usageMetadata" in data:
                billing.record(self.result.model or live_call.MODELS[0], data["usageMetadata"], kind="live")
            sc = data.get("serverContent") or {}
            if sc.get("interrupted"):
                await self.to_phone({"type": "interrupted"})
            for part in ((sc.get("modelTurn") or {}).get("parts") or []):
                blob = part.get("inlineData") or {}
                if blob.get("data"):
                    await self.to_phone(base64.b64decode(blob["data"]))
            if (t := (sc.get("inputTranscription") or {}).get("text")):
                self._in_text.append(t)
                await self.to_phone({"type": "user", "text": t})
            if (t := (sc.get("outputTranscription") or {}).get("text")):
                self._out_text.append(t)
                await self.to_phone({"type": "jarvis", "text": t})
            if sc.get("turnComplete"):
                self._flush_transcript()
                await self.to_phone({"type": "turn_complete"})
            if "toolCall" in data:
                task = asyncio.create_task(self._run_tools(ws, data["toolCall"].get("functionCalls") or []))
                self._tool_tasks.add(task)
                task.add_done_callback(self._tool_tasks.discard)
            if "goAway" in data:
                logger.info("phone live: сервер Gemini просит завершить сессию")
                await self.to_phone({"type": "end"})

    def _flush_transcript(self) -> None:
        if self._in_text:
            self.user_lines.append("".join(self._in_text).strip())
        if self._out_text:
            self.jarvis_lines.append("".join(self._out_text).strip())
        super()._flush_transcript()

    async def _run_tools(self, ws, calls: list[dict[str, Any]]) -> None:  # noqa: ANN001
        responses = []
        for call in calls:
            name, args, cid = str(call.get("name")), call.get("args") or {}, call.get("id")
            logger.info("phone live tool %s %s", name, json.dumps(args, ensure_ascii=False)[:200])
            if name in _TOOL_STATUS:
                await self.to_phone({"type": "status", "text": _TOOL_STATUS[name]})
            if self.turn.device.get("locked") and name in NEED_UNLOCK:
                # заблокированный телефон: звонки, сообщения и чужая переписка — только после разблокировки
                await self.to_phone({"type": "unlock"})
                result: dict[str, Any] = {"error": "телефон заблокирован", "need_unlock": True}
            elif name == "end_call":
                await self.to_phone({"type": "end"})
                result = {"ok": True}
            elif name == "send_to_chat":
                result = await _send_to_chat(self.uid, str(args.get("text") or ""))
            elif name == "bot_task":
                await self.to_phone({"type": "status", "text": "Делаю…"})
                result = await live_call.delegate(self.profile, str(args.get("request") or ""))
            else:
                before = len(self.turn.actions)
                result = await self.runner(name, args, self.ctx)
                for action in self.turn.actions[before:]:
                    await self.to_phone({"type": "action", "action": action})
                if self.turn.need_contacts:
                    self.turn.need_contacts = False
                    await self.to_phone({"type": "need_contacts"})
            if not (isinstance(result, dict) and result.get("error")):
                self.result.actions.append(name)
                if (card := result_card(name, args, result)) is not None:
                    await self.to_phone({"type": "card", "card": card})
            responses.append({"id": cid, "name": name, "response": _jsonable(result)})
        await self.to_gemini(ws, {"toolResponse": {"functionResponses": responses}})


# ------------------------------------------------------------------ заготовка разговора
# Пока сервер проверяет «Джарвис» (голос и слово, ~0.1–0.2 с), он уже собирает промпт и подключается к Gemini:
# подтвердили — телефон получает готовую сессию, и команда, сказанная на одном дыхании с именем, уходит сразу.
PREWARM_TTL = 15.0
_last_device: dict[int, dict[str, Any]] = {}
_warm: dict[int, tuple[float, asyncio.Task]] = {}


async def _build(uid: int, device: dict[str, Any]) -> tuple[PhoneLive, Any, Any, float]:
    """(сессия, aiohttp-клиент, соединение с Gemini, сколько собирали данные)."""
    import aiohttp

    from . import agent_tools_extra as extra
    from .handlers.common import profile_by_id

    started = time.monotonic()
    profile, persona, memory = await asyncio.gather(profile_by_id(uid), services.persona(uid), extra.memory_prompt(uid))
    snapshot = await phone._snapshot(profile)
    prepared = time.monotonic() - started
    system = live_call.system_instruction(profile, persona, mode="phone", snapshot=snapshot, memory=memory)
    system += phone.device_prompt(device) + billing.voice_note()
    sess = PhoneLive(profile, persona, system=system, phone_ws=None, device=dict(device))
    http = aiohttp.ClientSession()
    try:
        gem = await sess.connect(http)
    except BaseException:
        await http.close()
        raise
    return sess, http, gem, prepared


async def _close_built(task: asyncio.Task) -> None:
    try:
        _sess, http, gem, _ = await task
    except BaseException:
        return
    try:
        await gem.close()
    finally:
        await http.close()


def prewarm(uid: int) -> None:
    """Начать собирать разговор, пока проверяется «Джарвис». Телефон ещё ни разу не подключался — не с чем."""
    device = _last_device.get(uid)
    if device is None:
        return
    discard(uid)
    task = asyncio.create_task(_build(uid, device), name="phone-prewarm")
    _warm[uid] = (time.monotonic(), task)

    def expire() -> None:
        if _warm.get(uid, (0, None))[1] is task:
            discard(uid)

    asyncio.get_running_loop().call_later(PREWARM_TTL, expire)


def discard(uid: int) -> None:
    """Заготовка не пригодилась (не «Джарвис» или телефон так и не подключился)."""
    item = _warm.pop(uid, None)
    if item is None:
        return
    task = item[1]
    if task.done():
        asyncio.create_task(_close_built(task))
    else:
        task.cancel()


async def _take(uid: int, device: dict[str, Any]) -> tuple[PhoneLive, Any, Any, float] | None:
    item = _warm.pop(uid, None)
    if item is None or time.monotonic() - item[0] > PREWARM_TTL:
        if item is not None:
            asyncio.create_task(_close_built(item[1]))
        return None
    try:
        sess, http, gem, prepared = await asyncio.wait_for(asyncio.shield(item[1]), timeout=8)
    except BaseException:
        if not item[1].done():
            item[1].cancel()
        return None
    if bool(sess.turn.device.get("duplex")) != bool(device.get("duplex")) or gem.closed:
        # настройка «перебивать голосом» поменялась — VAD в заготовке другой
        await _close_built(item[1])
        return None
    return sess, http, gem, prepared


# ------------------------------------------------------------------ точка входа
async def run(uid: int, phone_ws, hello: dict[str, Any]) -> None:  # noqa: ANN001
    from . import agent_tools_extra as extra

    started = time.monotonic()
    device = hello.get("device") if isinstance(hello.get("device"), dict) else {}
    _last_device[uid] = dict(device)
    built = await _take(uid, device)
    warm = built is not None
    if built is None:
        try:
            built = await _build(uid, device)
        except Exception as exc:
            logger.warning("phone live: Gemini недоступен: %s", exc)
            await phone_ws.send_str(json.dumps({"type": "error", "text": "Не удалось подключиться к Gemini"}, ensure_ascii=False))
            return
    sess, http, gem, prepared = built
    profile = sess.profile
    sess.phone_ws = phone_ws
    if warm:
        # заготовка собрана с прошлыми данными телефона — свежие (заряд, пропущенные, блокировка) пометкой
        stale = phone.device_prompt(sess.turn.device)
        sess.turn.device.update(device)
        fresh = phone.device_prompt(device)
        if fresh.strip() and fresh != stale:
            await sess.to_gemini(gem, {"clientContent": {"turns": [{"role": "user", "parts": [{"text": f"[Сейчас.{fresh}]"}]}],
                                                         "turnComplete": False}})
    try:
        await sess.to_phone({"type": "ready", "model": sess.result.model})
        logger.info("phone live %s: готов за %.1f с (данные %.1f с%s)", uid, time.monotonic() - started, prepared, ", заготовка" if warm else "")
        if str(hello.get("text") or "").strip():
            await sess.to_phone({"type": "user", "text": str(hello["text"]), "final": True})
            sess.user_lines.append(str(hello["text"]))
            await sess.say_text(gem, str(hello["text"]))
        elif hello.get("greet"):
            await sess.say_text(gem, GREET)
        undo.begin_turn(uid)
        tasks = [asyncio.create_task(sess.pump_phone(gem), name="phone-up"), asyncio.create_task(sess.downlink(gem), name="phone-down")]
        stopper = asyncio.create_task(sess.stop.wait(), name="phone-stop")
        try:
            await asyncio.wait({*tasks, stopper}, timeout=MAX_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sess.stop.set()
            for task in (*tasks, stopper, *sess._tool_tasks):
                task.cancel()
            await asyncio.gather(*tasks, stopper, *sess._tool_tasks, return_exceptions=True)
            try:
                await gem.close()
            except Exception:
                pass
            sess._flush_transcript()
            undo.end_turn(uid)
    finally:
        await http.close()
    said = " / ".join(x for x in sess.user_lines if x)[:400]
    answered = " / ".join(x for x in sess.jarvis_lines if x)[:400]
    logger.info("phone live %s: %.0f с, действия %s, «%s» → «%s»", uid, time.monotonic() - started, sess.result.actions, said[:80], answered[:80])
    if said:
        phone._later(services.log_agent(uid, text=said, kind="phone_live", tools=",".join(sess.result.actions), reply=answered, ok=True))
        phone._later(extra.remember_exchange(uid, said, answered, when=profile.now.strftime("%d.%m %H:%M")))


# ------------------------------------------------------------------ «Да, слушаю» голосом бота — мгновенно, без ожидания Gemini
# совсем короткое («Да?») TTS Gemini часто не озвучивает — фразы чуть длиннее
_GREETINGS = {
    "ru": ["Да, слушаю.", "Слушаю вас.", "Да, {hon}, я здесь."],
    "uz": ["Ha, eshitaman.", "Labbay, eshitaman.", "Ha, {hon}, shu yerdaman."],
    "en": ["Yes, I'm listening.", "I'm here.", "Yes, {hon}?"],
}
GREETINGS_VERSION = 2
_HON = {"shef": ("Шеф", "Shef"), "ser": ("Сэр", "Ser"), "boss": ("Босс", "Boss"), "mix": ("Шеф", "Shef")}


def pcm_to_wav(pcm: bytes, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


async def greetings(uid: int) -> dict[str, Any]:
    from .tg_user import data_dir

    persona = await services.persona(uid)
    hon = _HON.get(persona.honorific)
    texts = []
    for t in _GREETINGS.get(persona.lang, _GREETINGS["ru"]):
        if "{hon}" in t:
            if not hon:
                continue
            t = t.format(hon=hon[0] if persona.lang == "ru" else hon[1])
        texts.append(t)
    key = f"v{GREETINGS_VERSION}_{persona.voice}_{persona.lang}_{persona.honorific}"
    cache_file = data_dir() / f"greetings_{key}.json"
    try:
        return json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    pcms = await asyncio.gather(*(ai.synthesize(t, voice=persona.voice) for t in texts), return_exceptions=True)
    clips = [{"text": t, "wav": base64.b64encode(pcm_to_wav(pcm)).decode()} for t, pcm in zip(texts, pcms) if isinstance(pcm, bytes) and pcm]
    out = {"key": key, "clips": clips}
    if len(clips) == len(texts):
        try:
            cache_file.write_text(json.dumps(out), encoding="utf-8")
        except OSError:
            logger.warning("greetings not cached", exc_info=True)
    return out


# ------------------------------------------------------------------ «Звонит мама» — кто звонит, голосом бота
_LANG_NAMES = {"ru": "русском", "uz": "узбекском (латиница)", "en": "английском"}
_ANNOUNCE_FALLBACK = {"ru": ("Звонит {name}", "Вам звонят"), "uz": ("{name} qo'ng'iroq qilyapti", "Sizga qo'ng'iroq"),
                      "en": ("{name} is calling", "Incoming call")}


def announce_prompt(name: str, app: str, lang: str, memory: str = "") -> str:
    where = f" Звонок через {app}." if app and app.lower() not in {"phone", "телефон", ""} else ""
    return (
        f"На телефон владельца звонят. Контакт записан у него так: «{name or 'без имени'}».{where}\n"
        f"Скажи ОДНОЙ короткой фразой на {_LANG_NAMES.get(lang, 'русском')} языке, кто звонит, — как живой помощник, который "
        "понимает, кто это. Родственные и ласковые слова переводи и говори просто: onajonim, oyijon, ойи, онам → мама; "
        "dadajon, otam, дада → папа; akam, ukam → брат; opam, singlim → сестра; buvijon → бабушка; bobojon → дедушка. "
        "Лишнее из записи убирай (оператор, эмодзи, номера, «new», «work», «2»): «Мама Beeline» → мама. "
        "Имя человека называй по-человечески: «Alisher aka» → Алишер. Номер без имени — «незнакомый номер». "
        "Если звонок в Telegram или WhatsApp — добавь это. Верни ТОЛЬКО фразу, например: «Звонит мама», "
        "«Алишер звонит в Telegram», «Звонит незнакомый номер»."
        + (f"\nЧто владелец рассказывал о своих людях:\n{memory[:1500]}" if memory else "")
    )


def clean_announcement(text: str) -> str:
    return str(text or "").strip().strip("«»\"'").splitlines()[0].strip() if str(text or "").strip() else ""


async def announce(uid: int, name: str, app: str = "") -> dict[str, Any]:
    """Фраза «Звонит мама» и её голос (WAV в base64). Кэш — по имени, приложению и голосу."""
    import hashlib

    from . import agent_tools_extra as extra
    from .tg_user import data_dir

    persona = await services.persona(uid)
    name = str(name or "").strip()[:80]
    app = str(app or "").strip()[:30]
    key = hashlib.sha1(f"{persona.voice}|{persona.lang}|{name}|{app}".encode()).hexdigest()[:16]
    folder = data_dir() / "announce"
    folder.mkdir(exist_ok=True)
    cache_file = folder / f"{key}.json"
    try:
        return json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    one, anon = _ANNOUNCE_FALLBACK.get(persona.lang, _ANNOUNCE_FALLBACK["ru"])
    text = ""
    try:
        memory = await extra.memory_prompt(uid)
        text = clean_announcement(await ai.generate([{"text": announce_prompt(name, app, persona.lang, memory)}],
                                                    temperature=0.2, json_mode=False, max_tokens=60))
    except Exception:
        logger.warning("announce text failed", exc_info=True)
    text = text or (one.format(name=name) if name else anon)
    pcm = await ai.synthesize(text, voice=persona.voice)
    out = {"text": text, "wav": base64.b64encode(pcm_to_wav(pcm)).decode() if pcm else ""}
    if pcm:
        try:
            cache_file.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        except OSError:
            logger.warning("announce not cached", exc_info=True)
    logger.info("announce «%s» (%s) → «%s»%s", name, app or "phone", text, "" if pcm else " (без голоса)")
    return out


__all__ = ["run", "greetings", "announce", "phone_declarations", "PhoneLive", "pcm_to_wav"]

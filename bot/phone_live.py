"""Живой разговор с Джарвисом на телефоне (как Gemini Live): телефон ⇄ WebSocket ⇄ Gemini Live.

Тот же голос, характер и инструменты, что в звонках бота (bot/live_call.py, режим "phone"), плюс
телефонные действия (bot/phone.py): звонок, SMS, Telegram от имени владельца, будильник, таймер,
приложения, фонарик, громкость, музыка, маршрут, системные кнопки. Действия выполняет само приложение.

По умолчанию разговор начинается в экономном режиме (bot/phone_cheap.py: команды и короткие ответы без Live);
камера, экран, галерея и «давай поговорим» — и он продолжается здесь, в Gemini Live, по тому же соединению.

Протокол /jarvis/v1/live (заголовок Authorization: Bearer <JARVIS_TOKEN>):
  телефон → сервер
    {"type":"hello","device":{…},"greet":bool,"text":str?} — первым сообщением
    бинарные кадры — микрофон, PCM s16le 16 кГц моно
    {"type":"text","text":…} — написанная реплика; {"type":"greet"} — позвали только по имени
    {"type":"action_error","text":…} — действие на телефоне не удалось; {"type":"bye"}
    {"type":"image",…} — кадр камеры/экрана (device.frames_on_request — только в ответ на frame_request)
  сервер → телефон
    бинарные кадры — голос Джарвиса, PCM s16le 24 кГц моно
    {"type":"ready"} {"type":"user","text":…} {"type":"jarvis","text":…} {"type":"turn_complete"} {"type":"interrupted"}
    {"type":"action","action":{…}} {"type":"status","text":…} {"type":"need_contacts"} {"type":"end"} {"type":"error","text":…}
    {"type":"frame_request"} — пришли один свежий кадр камеры/экрана
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
# он выбрал: ждать 1 секунду тишины — не обрывать на полуслове, если задумался посреди фразы
VAD_SILENCE_MS = int(os.getenv("PHONE_VAD_SILENCE_MS") or 1000)
GREET = "[Он позвал тебя по имени и ждёт. Откликнись одним-двумя словами («Да?», «Слушаю»), без приветствий.]"
IDLE_END_S = 15.0        # он выбрал: 15 с тишины — разговор закрывается
IDLE_END_ECONOMY = 8.0   # после дневного лимита — быстрее
FRAME_EVERY_S = 3.0      # камера/экран: пока он говорит — не чаще кадра в 3 с


class SpeechGate:
    """Микрофон → Gemini только когда он говорит: с 0.5 с до начала речи и до 1.5 с тишины после (Gemini успевает
    понять, что фраза кончилась). Тишина и фоновый шум не уходят — не оплачиваются и не копятся в разговоре."""

    PREROLL_S = 0.5

    def __init__(self) -> None:
        from collections import deque

        self.active = False
        self.floor = 300.0
        self.quiet = 0.0
        self.hangover = (VAD_SILENCE_MS + 500) / 1000
        self.last_voice = time.monotonic()
        self._pre: Any = deque()
        self._pre_s = 0.0

    def feed(self, pcm: bytes) -> list[bytes]:
        import numpy as np

        x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32)
        if not len(x):
            return []
        dur = len(x) / INPUT_RATE
        rms = float(np.sqrt(np.mean(x * x)))
        loud = rms > max(self.floor * 2.5, 350.0)
        if not loud and rms > 0:
            self.floor = min(3000.0, max(50.0, self.floor * 0.95 + rms * 0.05))
        if loud:
            self.last_voice = time.monotonic()
        if self.active:
            self.quiet = 0.0 if loud else self.quiet + dur
            if self.quiet > self.hangover:
                self.active = False
            return [pcm]
        if loud:
            self.active = True
            self.quiet = 0.0
            out = [*self._pre, pcm]
            self._pre.clear()
            self._pre_s = 0.0
            return out
        self._pre.append(pcm)
        self._pre_s += dur
        while self._pre_s > self.PREROLL_S and self._pre:
            self._pre_s -= len(self._pre.popleft()) / 2 / INPUT_RATE
        return []


class OwnerGate:
    """«Только мой голос» посреди разговора: начало каждой фразы (~1.2 с) сверяем с его отпечатком; явно чужой голос
    (телевизор, кто-то рядом, эхо самого Джарвиса) в Gemini не уходит — не оплачивается и не вызывает ответ.
    Его фразы уходят целиком: задержка — только в начале (конец фразы Gemini слышит вовремя)."""

    CHECK_S = 1.2

    def __init__(self, uid: int, enabled: bool) -> None:
        self.uid = uid
        self.enabled = enabled
        self.state = "idle"            # idle | buffer | pass | drop
        self._buf: list[bytes] = []
        self.dropped = 0

    async def filter(self, chunks: list[bytes], active: bool) -> list[bytes]:
        """chunks — то, что пропустил SpeechGate; active — идёт ли ещё фраза."""
        if not self.enabled:
            return chunks
        if not chunks:
            if not active:
                self.state = "idle"
            return []
        if self.state == "idle":
            self.state, self._buf = "buffer", []
        out: list[bytes] = []
        if self.state == "buffer":
            self._buf.extend(chunks)
            if sum(len(c) for c in self._buf) / 2 / INPUT_RATE >= self.CHECK_S or not active:
                from . import voiceprint

                audio, self._buf = b"".join(self._buf), []
                verdict = await voiceprint.is_other(self.uid, pcm_to_wav(audio, INPUT_RATE))
                if verdict.get("other"):
                    self.state = "drop"
                    self.dropped += 1
                    logger.info("phone live: чужой голос — в Gemini не отправляю (сходство %s, z %s)", verdict.get("score"), verdict.get("z"))
                else:
                    self.state = "pass"
                    out = [audio]
        elif self.state == "pass":
            out = chunks
        if not active:
            self.state = "idle"
        return out


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
        self.gate = SpeechGate()
        self.owner = OwnerGate(self.uid, enabled=False)   # включает run(), если есть отпечаток голоса
        self.idle_limit = IDLE_END_ECONOMY if billing.over_limit() else IDLE_END_S
        self._last_model_audio = time.monotonic()
        self._ended = False
        self._streams: set[str] = set()        # идёт камера / показ экрана
        self._stream_on_at = 0.0
        self._frame_rx = 0.0                   # когда пришёл последний кадр с телефона
        self._frame_tx = 0.0                   # когда последний кадр ушёл в Gemini
        self._frame_req = 0.0                  # когда последний раз просили кадр у телефона
        self._pending_frame: dict[str, Any] | None = None
        # новое приложение шлёт кадр камеры/экрана только по просьбе — не гоняет JPEG каждую секунду впустую
        self.frames_on_request = bool(device.get("frames_on_request"))

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
        except live_call.BillingExhausted:
            raise
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
                # в Gemini — только речь (с полсекунды до и 1.5 с после): тишина тоже оплачивается и копится в разговоре;
                # и только его речь (OwnerGate): телевизор и чужие голоса — нет
                was_active = self.gate.active
                chunks = await self.owner.filter(self.gate.feed(bytes(msg.data)), self.gate.active)
                for chunk in chunks:
                    await self.to_gemini(gem, {"realtimeInput": {"audio": {"data": base64.b64encode(chunk).decode(),
                                                                           "mimeType": f"audio/pcm;rate={INPUT_RATE}"}}})
                if self.gate.active and not was_active:
                    # заговорил — модель видит свежий кадр экрана/камеры
                    if self.frames_on_request:
                        await self._request_frame(min_gap=1.0)
                    else:
                        await self._send_pending_frame(gem, min_gap=1.0)
                elif self.frames_on_request and self.gate.active and not billing.over_limit():
                    await self._request_frame(min_gap=FRAME_EVERY_S)  # говорит долго — кадр раз в 3 с
                await self._maybe_end_idle()
            elif msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                kind = data.get("type")
                if kind == "text" and str(data.get("text") or "").strip():
                    self.gate.last_voice = time.monotonic()
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
                    await self._on_frame(gem, {"data": str(data["data"]), "mimeType": str(data.get("mime") or "image/jpeg")})
                elif kind in {"camera", "screen"}:
                    if data.get("on"):
                        self._streams.add(kind)
                        self._stream_on_at = time.monotonic()
                        if self.frames_on_request:
                            await self._request_frame(min_gap=0.0)  # первый кадр — сразу
                    else:
                        self._streams.discard(kind)
                    what = "Камера" if kind == "camera" else "Показ экрана"
                    seen = "то, что он показывает камерой" if kind == "camera" else "экран его телефона (переведи, объясни, прочитай — что попросит)"
                    note = (f"[{what} включена — ты видишь {seen}. Свежий кадр приходит, когда он говорит.]" if data.get("on")
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

    # --- кадры камеры и экрана: телефон шлёт раз в 1–1.5 с, в Gemini — по одному, когда нужно (каждый кадр
    # оплачивается и остаётся в разговоре; раньше 2 минуты экрана = ~80 кадров в каждом следующем ответе)
    async def _on_frame(self, gem, frame: dict[str, Any]) -> None:  # noqa: ANN001
        now = time.monotonic()
        if self.frames_on_request:
            # кадр пришёл по просьбе (или это фото из галереи) — сразу в разговор
            self._frame_rx = self._frame_tx = now
            await self.to_gemini(gem, {"realtimeInput": {"video": frame}})
            return
        burst = now - self._frame_rx < 0.4  # галерея шлёт фото пачкой — их все
        self._frame_rx = now
        self._pending_frame = frame
        first = self._frame_tx < self._stream_on_at
        periodic = self.gate.active and not billing.over_limit() and now - self._frame_tx >= FRAME_EVERY_S
        if not self._streams or burst or first or periodic:
            await self._send_pending_frame(gem, min_gap=0.0)

    async def _request_frame(self, *, min_gap: float) -> None:
        if not self._streams or time.monotonic() - self._frame_req < min_gap:
            return
        self._frame_req = time.monotonic()
        await self.to_phone({"type": "frame_request"})

    async def _send_pending_frame(self, gem, *, min_gap: float) -> None:  # noqa: ANN001
        frame = self._pending_frame
        if frame is None or time.monotonic() - self._frame_tx < min_gap:
            return
        self._pending_frame = None
        self._frame_tx = time.monotonic()
        await self.to_gemini(gem, {"realtimeInput": {"video": frame}})

    async def _maybe_end_idle(self) -> None:
        """Как он выбрал: 15 с тишины (экономный режим — 8 с) — разговор закрывается, чужую речь дальше не слушаем."""
        if self._ended or self._tool_tasks or self.gate.active:
            return
        if time.monotonic() - max(self.gate.last_voice, self._last_model_audio) > self.idle_limit:
            self._ended = True
            logger.info("phone live: %.0f с тишины — закрываю разговор", self.idle_limit)
            await self.to_phone({"type": "end"})

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
                    self._last_model_audio = time.monotonic()
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
            result = await exec_tool(self, name, args)
            responses.append({"id": cid, "name": name, "response": _jsonable(result)})
        await self.to_gemini(ws, {"toolResponse": {"functionResponses": responses}})


async def exec_tool(sess, name: str, args: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN001
    """Один инструмент телефона — и в Live (PhoneLive), и в экономном режиме (phone_cheap): действия уходят на телефон,
    карточки — на панель. sess: uid, profile, turn, runner, ctx, result, to_phone()."""
    logger.info("phone tool %s %s", name, json.dumps(args, ensure_ascii=False)[:200])
    if name in _TOOL_STATUS:
        await sess.to_phone({"type": "status", "text": _TOOL_STATUS[name]})
    if sess.turn.device.get("locked") and name in NEED_UNLOCK:
        # заблокированный телефон: открыть приложение или экран — только после разблокировки
        await sess.to_phone({"type": "unlock"})
        result: dict[str, Any] = {"error": "телефон заблокирован", "need_unlock": True}
    elif name == "end_call":
        await sess.to_phone({"type": "end"})
        result = {"ok": True}
    elif name == "send_to_chat":
        result = await _send_to_chat(sess.uid, str(args.get("text") or ""))
    elif name == "bot_task":
        await sess.to_phone({"type": "status", "text": "Делаю…"})
        result = await live_call.delegate(sess.profile, str(args.get("request") or ""))
    else:
        before = len(sess.turn.actions)
        result = await sess.runner(name, args, sess.ctx)
        for action in sess.turn.actions[before:]:
            await sess.to_phone({"type": "action", "action": action})
        if sess.turn.need_contacts:
            sess.turn.need_contacts = False
            await sess.to_phone({"type": "need_contacts"})
    if not (isinstance(result, dict) and result.get("error")):
        sess.result.actions.append(name)
        if (card := result_card(name, args, result)) is not None:
            await sess.to_phone({"type": "card", "card": card})
    return result


# ------------------------------------------------------------------ заготовка разговора
# Пока сервер проверяет «Джарвис» (голос и слово, ~0.1–0.2 с), он уже собирает промпт и подключается к Gemini:
# подтвердили — телефон получает готовую сессию, и команда, сказанная на одном дыхании с именем, уходит сразу.
PREWARM_TTL = 15.0
_last_device: dict[int, dict[str, Any]] = {}
_warm: dict[int, tuple[float, asyncio.Task]] = {}
_modes: dict[int, str] = {}   # режим телефона по настройкам (economy | live) — с прошлого разговора


async def _build(uid: int, device: dict[str, Any]) -> tuple[PhoneLive, Any, Any, float]:
    """(сессия, aiohttp-клиент, соединение с Gemini, сколько собирали данные)."""
    import aiohttp

    from . import agent_tools_extra as extra
    from .handlers.common import profile_by_id

    started = time.monotonic()
    profile, persona, memory = await asyncio.gather(profile_by_id(uid), services.persona(uid), extra.memory_prompt(uid))
    prepared = time.monotonic() - started
    # данные бота (траты, задачи) в промпт не кладём — они оплачивались бы в каждом ответе; нужны — инструменты и bot_task
    system = live_call.system_instruction(profile, persona, mode="phone", memory=memory)
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
    """Начать собирать разговор, пока проверяется «Джарвис». Телефон ещё ни разу не подключался — не с чем.
    Экономный режим начинается без Gemini Live — заготовка не нужна."""
    device = _last_device.get(uid)
    if device is None or _modes.get(uid, "economy") != "live" or not billing.live_allowed("phone"):
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
    """Экономный режим (по умолчанию): команды и короткие ответы — Flash-Lite (bot/phone_cheap.py); попросил камеру,
    экран, галерею или поговорить — разговор продолжается в Gemini Live по тому же соединению. «Всегда Live» в
    настройках — сразу Live. После дневного лимита — только экономный режим."""
    from . import phone_cheap

    device = hello.get("device") if isinstance(hello.get("device"), dict) else {}
    _last_device[uid] = dict(device)
    persona = await services.persona(uid)
    _modes[uid] = persona.voice_mode
    economy = persona.voice_mode != "live" or not billing.live_allowed("phone")
    meter = billing.start_session("phone", "economy" if economy else "live")
    info: dict[str, Any] = {}
    try:
        if economy:
            discard(uid)  # настройку могли поменять после «Джарвис» — заготовка Live не нужна
            upgrade = await phone_cheap.run(uid, phone_ws, hello, info)
            if upgrade is None:
                return
            meter.mode = "economy+live"
            hello = {"text": upgrade.request, "context": upgrade.context, "device": device}
        await _run_live(uid, phone_ws, hello, info)
    finally:
        billing.end_session(meter, said=str(info.get("said") or "")[:80])


async def _run_live(uid: int, phone_ws, hello: dict[str, Any], info: dict[str, Any]) -> None:  # noqa: ANN001
    from . import agent_tools_extra as extra
    from . import voiceprint

    started = time.monotonic()
    device = hello.get("device") if isinstance(hello.get("device"), dict) else {}
    built = await _take(uid, device)
    warm = built is not None
    if built is None:
        try:
            built = await _build(uid, device)
        except Exception as exc:
            logger.warning("phone live: Gemini недоступен: %s", exc)
            text = ("Баланс Gemini закончился — пополните в AI Studio" if isinstance(exc, live_call.BillingExhausted)
                    else "Не удалось подключиться к Gemini")
            await phone_ws.send_str(json.dumps({"type": "error", "text": text}, ensure_ascii=False))
            return
    sess, http, gem, prepared = built
    profile = sess.profile
    sess.phone_ws = phone_ws
    sess.frames_on_request = bool(device.get("frames_on_request"))
    sess.owner.enabled = voiceprint.enrolled(uid)
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
        if hello.get("context"):
            # продолжение экономного разговора: что уже было сказано — пометкой, просьба — репликой (субтитр уже на экране)
            await sess.to_gemini(gem, {"clientContent": {"turns": [{"role": "user", "parts": [{"text": str(hello["context"])}]}],
                                                         "turnComplete": False}})
        if str(hello.get("text") or "").strip():
            if not hello.get("context"):
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
    info["said"] = " / ".join(x for x in (info.get("said"), said) if x)
    logger.info("phone live %s: %.0f с, действия %s, «%s» → «%s»%s", uid, time.monotonic() - started, sess.result.actions, said[:80], answered[:80],
                f", чужой голос не пропущен {sess.owner.dropped} раз" if sess.owner.dropped else "")
    if said:
        phone._later(services.log_agent(uid, text=said, kind="phone_live", tools=",".join(sess.result.actions), reply=answered, ok=True))
        phone._later(extra.remember_exchange(uid, said, answered, when=profile.now.strftime("%d.%m %H:%M")))


# ------------------------------------------------------------------ «Да, слушаю» голосом бота — мгновенно, без ожидания Gemini
# совсем короткое («Да?») TTS Gemini часто не озвучивает — фразы чуть длиннее
# Он выбрал сам: на вызов — только коротко: «Да, сэр», «Да, шеф», «Да, босс» (длинное «Да, слышу вас…» — нет).
_GREETINGS = {
    "ru": ["Да, {hon}."],
    "uz": ["Labbay, {hon}."],
    "en": ["Yes, {hon}."],
}
_GREETINGS_PLAIN = {"ru": ["Да?", "Слушаю."], "uz": ["Labbay?"], "en": ["Yes?"]}
GREETINGS_VERSION = 4
_HON = {"shef": [("шеф", "shef", "boss")], "ser": [("сэр", "ser", "sir")], "boss": [("босс", "boss", "boss")],
        "mix": [("сэр", "ser", "sir"), ("шеф", "shef", "boss"), ("босс", "boss", "boss")]}


def greeting_texts(lang: str, honorific: str) -> list[str]:
    col = {"ru": 0, "uz": 1, "en": 2}.get(lang, 0)
    hons = _HON.get(honorific)
    if not hons:
        return list(_GREETINGS_PLAIN.get(lang, _GREETINGS_PLAIN["ru"]))
    texts: list[str] = []
    for t in _GREETINGS.get(lang, _GREETINGS["ru"]):
        for h in hons:
            text = t.format(hon=h[col])
            texts.append(text[0].upper() + text[1:])
    return list(dict.fromkeys(texts))


def trim_clip(pcm: bytes, rate: int = 24000) -> bytes:
    """Срезать тишину и шорох по краям, мягко (20 мс) начать и закончить — без щелчка и «хвоста» в конце."""
    import numpy as np

    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    frame = rate // 100  # 10 мс
    n = len(x) // frame
    if n < 5:
        return pcm
    energy = np.sqrt((x[: n * frame].reshape(n, frame) ** 2).mean(axis=1))
    voiced = np.where(energy > max(300.0, float(np.percentile(energy, 90)) * 0.06))[0]
    if not len(voiced):
        return pcm
    a = max(0, voiced[0] - 3) * frame
    b = min(n, voiced[-1] + 5) * frame
    y = x[a:b].copy()
    fade = min(len(y) // 4, rate // 50)
    if fade > 0:
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        y[:fade] *= ramp
        y[-fade:] *= ramp[::-1]
    return np.clip(y, -32768, 32767).astype(np.int16).tobytes()


def _same_words(heard: str, text: str) -> bool:
    import re

    norm = lambda v: re.sub(r"[^\w]+", " ", v.lower().replace("ё", "е")).split()  # noqa: E731
    return norm(heard) == norm(text)


async def _live_say(uid: int, persona, text: str) -> bytes | None:  # noqa: ANN001
    """Фраза тем же голосом, что и в разговоре (Gemini Live), слово в слово — проверяем по расшифровке."""
    import aiohttp

    from .handlers.common import profile_by_id

    profile = await profile_by_id(uid)
    for _ in range(3):
        sess = _Session(profile, persona, mode="wake", system="Ты диктор. Произнеси ровно тот текст, что тебе дали, — "
                        "ни слова больше и ни слова меньше, спокойно и тепло, без пауз в начале.")
        out, said = bytearray(), []
        try:
            async with aiohttp.ClientSession() as http:
                ws = await sess.connect(http)
                await ws.send_str(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{"text": f"Произнеси: {text}"}]}],
                                                                "turnComplete": True}}))
                while True:
                    data = _decode(await asyncio.wait_for(ws.receive(), timeout=30))
                    if data is None:
                        break
                    sc = data.get("serverContent") or {}
                    for part in ((sc.get("modelTurn") or {}).get("parts") or []):
                        if (part.get("inlineData") or {}).get("data"):
                            out.extend(base64.b64decode(part["inlineData"]["data"]))
                    if (t := (sc.get("outputTranscription") or {}).get("text")):
                        said.append(t)
                    if sc.get("turnComplete"):
                        break
                await ws.close()
        except Exception:
            logger.warning("greeting via live failed: %s", text, exc_info=True)
            continue
        if out and (not said or _same_words("".join(said), text)):
            clip = trim_clip(bytes(out))
            if await _clear(clip, text, persona.lang):
                return clip
            said = ["(нечётко после обрезки)"]
        logger.info("greeting: сказала «%s» вместо «%s» — ещё раз", "".join(said), text)
    return None


async def _qwen_say(persona, text: str) -> bytes | None:  # noqa: ANN001
    """Отклик голосом Qwen — слово в слово (по его расшифровке и нашему распознавателю)."""
    from . import qwen_live

    for _ in range(3):
        try:
            pcm, said = await qwen_live.say(text, voice=persona.qwen_voice)
        except Exception:
            logger.warning("greeting via qwen failed: %s", text, exc_info=True)
            return None
        if pcm and (not said or _same_words(said, text)):
            clip = trim_clip(pcm)
            if await _clear(clip, text, persona.lang):
                return clip
        logger.info("greeting (qwen): сказала «%s» вместо «%s» — ещё раз", said, text)
    return None


async def _clear(clip: bytes, text: str, lang: str) -> bool:
    """Русский отклик ещё раз слушаем своим распознавателем: «Да, босс» не должно звучать как «даблас»."""
    if lang != "ru":
        return True
    from . import wakeword

    heard = await wakeword.check(pcm_to_wav(clip))
    return heard is None or _same_words(heard["text"], text)


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

    from . import qwen_live

    persona = await services.persona(uid)
    texts = greeting_texts(persona.lang, persona.honorific)
    # разговор идёт голосом Qwen — и «Да, сэр» его голосом, чтобы голос не менялся посреди разговора
    use_qwen = persona.voice_model == "qwen" and qwen_live.available()
    voice_key = f"qwen-{persona.qwen_voice}" if use_qwen else persona.voice
    key = f"v{GREETINGS_VERSION}_{voice_key}_{persona.lang}_{persona.honorific}"
    cache_file = data_dir() / f"greetings_{key}.json"
    try:
        return json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    # уже записанные раньше фразы (прошлые версии) берём как есть — без Gemini
    ready: dict[str, str] = {}
    for old in data_dir().glob(f"greetings_v*_{voice_key}_{persona.lang}_{persona.honorific}.json"):
        try:
            ready.update({c["text"]: c["wav"] for c in json.loads(old.read_text(encoding="utf-8")).get("clips", [])})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if all(t in ready for t in texts):
        out = {"key": key, "clips": [{"text": t, "wav": ready[t]} for t in texts]}
        try:
            cache_file.write_text(json.dumps(out), encoding="utf-8")
        except OSError:
            logger.warning("greetings not cached", exc_info=True)
        return out
    say = (lambda t: _qwen_say(persona, t)) if use_qwen else (lambda t: _live_say(uid, persona, t))
    pcms = await asyncio.gather(*(say(t) for t in texts), return_exceptions=True)
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

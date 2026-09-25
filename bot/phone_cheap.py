"""Экономный режим Джарвиса на телефоне: команды и короткие ответы — без Gemini Live.

Почему дорого было: Live в КАЖДОМ ответе заново оплачивает инструкцию, описания 46 инструментов (~7 тыс. токенов)
и весь разговор, а звук у него — $3 на входе и $12 на выходе за 1M токенов. 24.09 голос съел 99% расхода.

Здесь та же связь с телефоном (протокол /jarvis/v1/live — приложение работает как с Live), но:
  • сервер сам режет речь на фразы (пауза VAD_SILENCE_MS — он выбрал 1 с), тишина никуда не уходит;
    явно чужой голос (телевизор, кто-то рядом) отсекает отпечаток голоса;
  • фраза (звук) → Flash-Lite с теми же инструментами: звук на входе в 10 раз дешевле, а инструкция и инструменты
    не меняются между запросами (время — в реплике), поэтому Gemini берёт их из кэша за 10% цены;
    параллельно — расшифровка для субтитров и истории (в историю кладём текст, не звук);
  • команды — молча (карточка на телефоне), ответ — голосом: короткая сессия Live «диктор» без инструментов
    (тот же голос, звук идёт сразу, платим только за сам ответ); не вышло — TTS; и он не смог — только субтитры;
  • камера, экран, галерея и «давай поговорим» → live_mode: разговор продолжается в Gemini Live (bot/phone_live.py)
    по тому же соединению — с пометкой, о чём уже говорили.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from . import billing
from . import live_call
from . import phone
from . import services
from . import undo
from .context import ai
from .live_call import _decode, _jsonable, _Session
from .phone_live import INPUT_RATE, VAD_SILENCE_MS, exec_tool, pcm_to_wav

logger = logging.getLogger(__name__)

LIVE_TOOLS = {"look", "screen_look", "gallery"}   # им нужны кадры в разговоре — это только Live
MAX_STEPS = 4
PREROLL_S = 0.3           # до начала речи — чтобы не съесть первый слог
MIN_SPEECH_S = 0.25       # короче — щелчок, кашель: не фраза
MAX_UTTERANCE_S = 30.0
TAIL_S = 0.3              # тишины после речи оставляем чуть-чуть — остальное не отправляем
HISTORY_MESSAGES = 16
HISTORY_CHARS = 12000
SPEAK_CHUNK = 9600        # TTS: кусками по 0.2 с (24 кГц), телефон начинает играть сразу
STT_WAIT_S = 5.0          # расшифровка (субтитр и история) обычно готова раньше ответа; дольше не ждём


@dataclass
class Upgrade:
    """Нужен живой разговор: request — его просьба (продолжить в Live), context — что было до неё."""

    request: str
    context: str


_LIVE_MODE = {
    "name": "live_mode",
    "description": "Перейти в живой голосовой разговор (Gemini Live) и продолжить там: посмотреть камерой («посмотри», «что это?», "
                   "«прочитай, что написано»), экран («что на экране», «переведи это»), фото из галереи, «давай поговорим», "
                   "долгая беседа, урок, игра. Живой режим дороже — только когда без него никак.",
    "parameters": {"type": "OBJECT", "properties": {"request": {"type": "STRING", "description": "что он просит, своими словами"}},
                   "required": ["request"]},
}

CHEAP_RULES = (
    "\nЭКОНОМНЫЙ РЕЖИМ. Его реплика приходит записью голоса (в начале может прозвучать «Джарвис» — это обращение, не просьба) "
    "с пометкой времени в квадратных скобках. Твой текстовый ответ телефон произнесёт твоим голосом.\n"
    "• Команда — вызови инструмент и НЕ пиши текст: телефон сам покажет карточку. Текст — только если он спросил то, на что нужен "
    "ответ, инструмент вернул ошибку или нужно подтверждение (ask_exactly — дословно).\n"
    "• Ответ — одна-две короткие разговорные фразы, без списков, эмодзи, markdown и ссылок; числа и суммы — словами, как говорят.\n"
    "• В записи нет просьбы к тебе (тишина, шум, разговор с кем-то рядом, телевизор, только имя) — ответь ровно «-».\n"
    "• look, screen_look и gallery здесь нет: камера, экран, галерея, «давай поговорим», долгая беседа — live_mode "
    "(дальше разговор идёт вживую).\n"
)


def declarations() -> list[dict[str, Any]]:
    """Те же инструменты, что в Live (короткие описания), без кадров — вместо них live_mode."""
    return [d for d in live_call.tool_declarations("phone") if d["name"] not in LIVE_TOOLS] + [_LIVE_MODE]


def system_prompt(profile, persona, memory: str) -> str:  # noqa: ANN001
    """Без текущего времени — оно в каждой реплике: инструкция не меняется, Gemini берёт её из кэша."""
    return live_call.system_instruction(profile, persona, mode="phone", memory=memory, with_time=False) + CHEAP_RULES


def clean_reply(text: str) -> str:
    """Что произнести: без разметки; «-», пусто или одна пунктуация — молчим."""
    say = phone.speakable(text or "").strip()
    return "" if not re.sub(r"[\W_]+", "", say) else say


# ------------------------------------------------------------------ речь → фразы
class Segmenter:
    """Звук с телефона → фразы: от ~0.3 с до начала речи до паузы VAD_SILENCE_MS. Порог — как у SpeechGate
    (шум подстраивается сам). Тишина, щелчки и кашель никуда не уходят — за них не платим."""

    def __init__(self) -> None:
        self.floor = 300.0
        self.active = False
        self._pre: deque[bytes] = deque()
        self._pre_s = 0.0
        self._buf = bytearray()
        self._speech_s = self._quiet_s = self._len_s = 0.0

    def feed(self, pcm: bytes) -> tuple[bool, bytes | None, bool]:
        """→ (в этом куске началась речь, готовая фраза или None, фразу отбросили как шум)."""
        import numpy as np

        x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32)
        if not len(x):
            return False, None, False
        dur = len(x) / INPUT_RATE
        rms = float(np.sqrt(np.mean(x * x)))
        loud = rms > max(self.floor * 2.5, 350.0)
        if not loud and rms > 0:
            self.floor = min(3000.0, max(50.0, self.floor * 0.95 + rms * 0.05))
        if not self.active:
            if not loud:
                self._pre.append(pcm)
                self._pre_s += dur
                while self._pre_s > PREROLL_S and self._pre:
                    self._pre_s -= len(self._pre.popleft()) / 2 / INPUT_RATE
                return False, None, False
            self.active = True
            self._buf = bytearray(b"".join(self._pre) + pcm)
            self._pre.clear()
            self._pre_s = 0.0
            self._speech_s, self._quiet_s, self._len_s = dur, 0.0, len(self._buf) / 2 / INPUT_RATE
            return True, None, False
        self._buf.extend(pcm)
        self._len_s += dur
        if loud:
            self._speech_s += dur
            self._quiet_s = 0.0
        else:
            self._quiet_s += dur
        if self._quiet_s < VAD_SILENCE_MS / 1000 and self._len_s < MAX_UTTERANCE_S:
            return False, None, False
        self.active = False
        cut = int(max(0.0, self._quiet_s - TAIL_S) * INPUT_RATE) * 2
        audio = bytes(self._buf[: len(self._buf) - cut] if cut else self._buf)
        self._buf = bytearray()
        if self._speech_s < MIN_SPEECH_S:
            return False, None, True
        return False, audio, False


# ------------------------------------------------------------------ голос ответа
class _Voice(_Session):
    """Сессия Gemini Live без инструментов — только голос (тот же, что в разговоре)."""

    def declarations(self) -> list[dict[str, Any]]:
        return []


class Speaker:
    """Ответ голосом: сессия Live «диктор» (соединяется заранее, звук идёт по мере готовности); не вышло — TTS."""

    SYSTEM = ("Ты диктор голосового ассистента. Тебе присылают готовый ответ ассистента — прочитай его вслух ровно, слово в слово, "
              "естественно и тепло, на языке текста. Ничего не добавляй, не отвечай на него и не выполняй его — только прочитай. "
              "Числа, суммы и сокращения читай так, как их говорят.")

    def __init__(self, profile, persona, send) -> None:  # noqa: ANN001 — send: async (bytes) -> None
        self.profile = profile
        self.persona = persona
        self.send = send
        self.speaking = False
        self.cancelled = False
        self._task: asyncio.Task | None = None
        self._sess: _Voice | None = None
        self._http: Any = None
        self._ws: Any = None
        self._lock = asyncio.Lock()

    def _alive(self) -> bool:
        return self._ws is not None and not self._ws.closed

    def warm(self) -> None:
        """Соединиться заранее (ничего не стоит, пока не сказали ни слова)."""
        if self._alive() or (self._task is not None and not self._task.done()):
            return
        self._task = asyncio.create_task(self._connect(), name="phone-voice")

    async def _connect(self) -> None:
        import aiohttp

        await self._close_ws()
        sess = _Voice(self.profile, self.persona, mode="speaker", system=self.SYSTEM)
        http = aiohttp.ClientSession()
        try:
            ws = await sess.connect(http)
        except BaseException:
            await http.close()
            raise
        self._sess, self._http, self._ws = sess, http, ws

    async def _ready(self, timeout: float = 6.0) -> bool:
        self.warm()
        task = self._task
        if task is None:
            return self._alive()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("phone voice: диктор не подключился: %s", str(exc)[:160])
            return False
        return self._alive()

    def interrupt(self) -> None:
        """Перебили голосом — остаток ответа на телефон не шлём."""
        self.cancelled = True

    async def say(self, text: str) -> bool:
        """Произнести ответ. True — звук ушёл на телефон."""
        async with self._lock:
            self.cancelled = False
            self.speaking = True
            try:
                if await self._ready():
                    got = await self._say_live(text)
                    if got or self.cancelled:
                        return got
                try:
                    pcm = await ai.synthesize(text, voice=self.persona.voice)
                except Exception:
                    logger.warning("phone voice: TTS не смог", exc_info=True)
                    pcm = None
                if not pcm or self.cancelled:
                    return False
                for i in range(0, len(pcm), SPEAK_CHUNK):
                    if self.cancelled:
                        break
                    await self.send(pcm[i: i + SPEAK_CHUNK])
                return True
            finally:
                self.speaking = False

    async def _say_live(self, text: str) -> bool:
        ws, sess = self._ws, self._sess
        got = False
        said: list[str] = []
        try:
            await ws.send_str(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{"text": f"Прочитай вслух:\n{text}"}]}],
                                                            "turnComplete": True}}, ensure_ascii=False))
            while True:
                msg = await asyncio.wait_for(ws.receive(), timeout=15)
                data = _decode(msg)
                if data is None:
                    if msg.type.name in {"CLOSE", "CLOSED", "CLOSING", "ERROR"}:
                        extra = str(getattr(msg, "extra", "") or "")
                        if billing.is_billing_error(None, extra):
                            billing.exhausted(extra)
                        logger.info("phone voice: диктор закрыл соединение (%s)", extra[:160])
                        await self._close_ws()
                        return got
                    continue
                if "usageMetadata" in data:
                    billing.record(sess.result.model or live_call.MODELS[0], data["usageMetadata"], kind="voice")
                sc = data.get("serverContent") or {}
                for part in ((sc.get("modelTurn") or {}).get("parts") or []):
                    blob = part.get("inlineData") or {}
                    if blob.get("data") and not self.cancelled:
                        got = True
                        await self.send(base64.b64decode(blob["data"]))
                if (t := (sc.get("outputTranscription") or {}).get("text")):
                    said.append(t)
                if sc.get("turnComplete"):
                    heard = "".join(said).strip()
                    if heard and _overlap(heard, text) < 0.5:
                        logger.warning("phone voice: диктор сказал не то: «%s» вместо «%s»", heard[:80], text[:80])
                    return got
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("phone voice: диктор сорвался: %s", str(exc)[:160])
            await self._close_ws()
            return got

    async def _close_ws(self) -> None:
        ws, http = self._ws, self._http
        self._ws = self._http = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if http is not None:
            await http.close()

    async def close(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._close_ws()


def _overlap(heard: str, text: str) -> float:
    words = lambda v: set(re.sub(r"[^\w]+", " ", v.lower().replace("ё", "е")).split())  # noqa: E731
    want = words(text)
    return len(want & words(heard)) / len(want) if want else 1.0


# ------------------------------------------------------------------ разговор
class PhoneCheap:
    def __init__(self, profile, persona, phone_ws, device: dict[str, Any], memory: str) -> None:  # noqa: ANN001
        from . import agent_tools
        from . import voiceprint

        self.profile = profile
        self.persona = persona
        self.phone_ws = phone_ws
        self.uid = profile.telegram_id
        self.turn = phone.PhoneTurn(uid=self.uid, device=dict(device))
        self.runner = phone.make_runner(self.turn)
        self.ctx = agent_tools.ToolContext(profile=profile, text="(телефон)")
        self.result = live_call.LiveResult()
        self.system = system_prompt(profile, persona, memory)
        self.decls = declarations()
        self.contents: list[dict[str, Any]] = []
        self.seg = Segmenter()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.speaker = Speaker(profile, persona, self.to_phone)
        self.upgrade: Upgrade | None = None
        self.owner_check = voiceprint.enrolled(self.uid)
        self.busy = False
        self.ended = False
        self.closed = False
        self.last_activity = time.monotonic()
        self.log: list[tuple[str, str]] = []   # («Он»/«Ты», текст) — для пометки, если перейдём в Live
        self.user_lines: list[str] = []
        self.jarvis_lines: list[str] = []
        self.dropped = 0
        self.turns = 0
        self._first = True
        self._phone_lock = asyncio.Lock()

    # --- телефон
    async def to_phone(self, payload: dict[str, Any] | bytes) -> None:
        if self.phone_ws.closed:
            self.closed = True
            return
        try:
            async with self._phone_lock:
                if isinstance(payload, (bytes, bytearray)):
                    await self.phone_ws.send_bytes(bytes(payload))
                else:
                    await self.phone_ws.send_str(json.dumps(payload, ensure_ascii=False, default=str))
        except Exception:
            self.closed = True

    async def run(self, hello: dict[str, Any]) -> Upgrade | None:
        await self.to_phone({"type": "ready", "model": "economy"})
        self.speaker.warm()
        if str(hello.get("text") or "").strip():
            self.queue.put_nowait(("text", str(hello["text"]), True))
        worker = asyncio.create_task(self._worker(), name="phone-cheap")
        try:
            await self._read_phone()
        finally:
            if self.upgrade is None:
                # телефон закрыл панель — начатое (запись траты, сообщение) доделываем, новых фраз не берём
                self.closed = True
                self.queue.put_nowait(None)
                try:
                    await asyncio.wait_for(asyncio.shield(worker), timeout=30)
                except (asyncio.TimeoutError, Exception):
                    pass
            if not worker.done():
                worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        return self.upgrade

    async def _read_phone(self) -> None:
        import aiohttp

        deadline = time.monotonic() + live_call.MAX_SECONDS
        async for msg in self.phone_ws:
            if self.upgrade is not None:
                return
            if msg.type == aiohttp.WSMsgType.BINARY:
                started, utterance, dropped = self.seg.feed(bytes(msg.data))
                if started:
                    self.last_activity = time.monotonic()
                    if self.speaker.speaking:  # перебил голосом — замолкаем и слушаем
                        self.speaker.interrupt()
                        await self.to_phone({"type": "interrupted"})
                if utterance:
                    self.queue.put_nowait(("audio", utterance))
                elif dropped and not self.busy:
                    await self.to_phone({"type": "turn_complete"})  # кашель/щелчок: телефон не ждёт ответа
                await self._maybe_end_idle()
                if time.monotonic() > deadline and not self.ended:
                    self.ended = True
                    await self.to_phone({"type": "end"})
            elif msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                kind = data.get("type")
                if kind == "text" and str(data.get("text") or "").strip():
                    self.last_activity = time.monotonic()
                    self.queue.put_nowait(("text", str(data["text"]), True))
                elif kind == "device" and isinstance(data.get("device"), dict):
                    self.turn.device.update(data["device"])
                elif kind == "unlocked":
                    self.turn.device["locked"] = False
                    self.queue.put_nowait(("text", "[Телефон разблокирован — сразу сделай то, что он просил.]", False))
                elif kind == "action_error":
                    self.queue.put_nowait(("text", f"[Действие на телефоне не удалось: {str(data.get('text') or '')[:200]}. "
                                                   "Коротко скажи ему об этом.]", False))
                elif kind == "greet":
                    self.queue.put_nowait(("greet",))
                elif kind == "bye":
                    return
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                return

    async def _maybe_end_idle(self) -> None:
        """Как в Live: 15 с тишины — разговор закрывается (он так выбрал)."""
        from .phone_live import IDLE_END_S

        if self.ended or self.busy or self.seg.active or self.speaker.speaking or not self.queue.empty():
            return
        if time.monotonic() - self.last_activity > IDLE_END_S:
            self.ended = True
            logger.info("phone cheap: %.0f с тишины — закрываю разговор", IDLE_END_S)
            await self.to_phone({"type": "end"})

    async def _worker(self) -> None:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            if self.closed and item[0] != "text":
                continue
            self.busy = True
            try:
                if item[0] == "audio":
                    await self._on_audio(item[1])
                elif item[0] == "text":
                    await self._on_text(item[1], visible=item[2])
                elif item[0] == "greet":
                    from .phone_live import greeting_texts

                    await self.speaker.say(greeting_texts(self.persona.lang, self.persona.honorific)[0])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if _billing_failure(exc):
                    await self.to_phone({"type": "error", "text": "Баланс Gemini закончился — пополните в AI Studio"})
                    return
                logger.exception("phone cheap: ход не удался")
                await self.to_phone({"type": "status", "text": "Не получилось — повторите, пожалуйста"})
                await self.to_phone({"type": "turn_complete"})
            finally:
                self.busy = False
                self.last_activity = time.monotonic()
            if self.upgrade is not None:
                return

    # --- ход разговора
    def _stamp(self) -> str:
        """Время (и в первой реплике — сведения о телефоне) — в реплике, а не в инструкции."""
        extra = phone.device_prompt(self.turn.device).strip() if self._first else ""
        self._first = False
        locked = "; телефон заблокирован" if self.turn.device.get("locked") else ""
        return f"[{live_call.now_line(self.profile)}{locked}{'. ' + extra if extra else ''}]"

    async def _on_audio(self, pcm: bytes) -> None:
        from . import voiceprint

        wav = pcm_to_wav(pcm, INPUT_RATE)
        if self.owner_check:
            verdict = await voiceprint.is_other(self.uid, wav)
            if verdict.get("other"):
                self.dropped += 1
                logger.info("phone cheap: чужой голос — не отвечаю (сходство %s, z %s)", verdict.get("score"), verdict.get("z"))
                await self.to_phone({"type": "turn_complete"})
                return
        stt = asyncio.create_task(self._transcribe(wav), name="phone-stt")
        audio = {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(wav).decode()}}
        await self._turn([{"text": self._stamp()}, audio], stt=stt)

    async def _on_text(self, text: str, *, visible: bool) -> None:
        if visible:
            await self.to_phone({"type": "user", "text": text, "final": True})
            self.user_lines.append(text)
            self.log.append(("Он", text))
        await self._turn([{"text": f"{self._stamp()} {text}"}], stt=None, said=text if visible else "")

    async def _transcribe(self, wav: bytes) -> str:
        from .phone_api import _STT_PROMPT, clean_transcript

        try:
            text = clean_transcript(await ai.transcribe_audio(wav, "audio/wav", prompt=_STT_PROMPT))
        except Exception as exc:
            logger.warning("phone cheap: расшифровка не удалась: %s", str(exc)[:160])
            return ""
        if text:
            await self.to_phone({"type": "user", "text": text, "final": True})
            self.user_lines.append(text)
            self.log.append(("Он", text))
        return text

    async def _turn(self, parts: list[dict[str, Any]], *, stt: asyncio.Task | None, said: str = "") -> None:
        """Одна его реплика: parts — пометка времени и звук (или текст); stt — её расшифровка (идёт параллельно);
        said — написанная реплика (у голосовой её роль играет расшифровка)."""
        started = time.monotonic()
        idx = len(self.contents)
        self.contents.append({"role": "user", "parts": parts})
        text = ""
        calls: list[str] = []
        for _ in range(MAX_STEPS):
            step = await ai.agent_step(self.contents, system=self.system, tools=self.decls, thinking_budget=0)
            self.contents.append({"role": "model", "parts": step.parts or [{"text": step.text or "-"}]})
            if not step.calls:
                text = step.text
                break
            responses, results = [], []
            for name, args in step.calls:
                calls.append(name)
                if name == "live_mode" or name in LIVE_TOOLS:
                    result = await self._go_live(args, stt, said)
                else:
                    result = await exec_tool(self, name, args)
                results.append(result if isinstance(result, dict) else {})
                responses.append({"functionResponse": {"name": name, "response": _jsonable(result)}})
            self.contents.append({"role": "user", "parts": responses})
            if self.upgrade is not None:
                self.contents.append({"role": "model", "parts": [{"text": "(дальше — живой разговор)"}]})
                break
            quick = phone.quick_reply(self.turn, self.contents, self.persona.lang)
            if quick is not None:
                # звонок, будильник, «отправлено» — молча (карточка на экране); «Отправить Алишеру: …?» — вопрос, его произносим
                self.contents.append({"role": "model", "parts": [{"text": quick}]})
                text = quick if any(r.get("ask_exactly") for r in results) else ""
                break
        # в историю — расшифровка вместо звука: дешевле и не ломает кэш
        transcript = ""
        if stt is not None:
            try:
                transcript = await asyncio.wait_for(asyncio.shield(stt), timeout=STT_WAIT_S)
            except (asyncio.TimeoutError, Exception):
                transcript = ""
            self.contents[idx] = {"role": "user", "parts": [{"text": f"{parts[0]['text']} {transcript or '(неразборчиво)'}"}]}
        from .handlers.agent import trim_history

        self.contents = trim_history(self.contents, max_messages=HISTORY_MESSAGES, max_chars=HISTORY_CHARS)
        self.turns += 1
        if self.upgrade is not None:
            return  # отвечать будет Live
        say = clean_reply(text)
        logger.info("phone cheap: %.1f с, инструменты %s, «%s» → «%s»", time.monotonic() - started, calls, transcript[:60], say[:60])
        if say:
            self.jarvis_lines.append(say)
            self.log.append(("Ты", say))
            await self.to_phone({"type": "jarvis", "text": say})
            await self.speaker.say(say)
        elif calls:
            self.log.append(("Ты", "(сделано: " + ", ".join(calls) + ")"))
        await self.to_phone({"type": "turn_complete"})

    async def _go_live(self, args: dict[str, Any], stt: asyncio.Task | None, said: str) -> dict[str, Any]:
        if not billing.live_allowed("phone"):
            return {"error": "живой режим (камера, экран, долгий разговор) до полуночи выключен — дневной лимит расходов. "
                             "Скажи ему это одной фразой и предложи, что можно сделать без него."}
        if stt is not None:
            try:
                said = await asyncio.wait_for(asyncio.shield(stt), timeout=STT_WAIT_S)
            except (asyncio.TimeoutError, Exception):
                said = ""
        request = said or str(args.get("request") or "").strip() or "продолжим разговор"
        log = self.log[:-1] if said and self.log and self.log[-1] == ("Он", said) else self.log  # сама просьба уйдёт репликой
        earlier = [f"{who}: «{t}»" for who, t in log][-6:]
        context = ("[До этого в разговоре: " + "; ".join(earlier) + ". Дальше — живой разговор голосом.]") if earlier else ""
        self.upgrade = Upgrade(request=request, context=context)
        logger.info("phone cheap → Live: «%s»", request[:80])
        await self.to_phone({"type": "status", "text": "Подключаю живой режим…"})
        return {"ok": True}


def _billing_failure(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    body = str(getattr(response, "text", "") or exc)
    return isinstance(exc, live_call.BillingExhausted) or billing.is_billing_error(status, body)


# ------------------------------------------------------------------ точка входа
async def run(uid: int, phone_ws, hello: dict[str, Any], info: dict[str, Any]) -> Upgrade | None:  # noqa: ANN001
    """Экономный разговор до конца (None) или до просьбы, которой нужен живой режим (Upgrade — продолжит phone_live)."""
    from . import agent_tools_extra as extra
    from .handlers.common import profile_by_id

    started = time.monotonic()
    device = hello.get("device") if isinstance(hello.get("device"), dict) else {}
    profile, persona, memory = await asyncio.gather(profile_by_id(uid), services.persona(uid), extra.memory_prompt(uid))
    sess = PhoneCheap(profile, persona, phone_ws, device, memory)
    undo.begin_turn(uid)
    try:
        upgrade = await sess.run(hello)
    finally:
        undo.end_turn(uid)
        await sess.speaker.close()
    said = " / ".join(x for x in sess.user_lines if x)[:400]
    answered = " / ".join(x for x in sess.jarvis_lines if x)[:400]
    info["said"] = said
    logger.info("phone cheap %s: %.0f с, ходов %s, действия %s%s, «%s» → «%s»", uid, time.monotonic() - started, sess.turns,
                sess.result.actions, f", чужой голос {sess.dropped} раз" if sess.dropped else "", said[:80], answered[:80])
    if said:
        phone._later(services.log_agent(uid, text=said, kind="phone_cheap", tools=",".join(sess.result.actions), reply=answered, ok=True))
        phone._later(extra.remember_exchange(uid, said, answered, when=profile.now.strftime("%d.%m %H:%M")))
    return upgrade


__all__ = ["run", "Segmenter", "Speaker", "PhoneCheap", "Upgrade", "declarations", "system_prompt", "clean_reply", "LIVE_TOOLS"]

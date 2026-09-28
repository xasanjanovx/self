"""Экономный голос в звонке Telegram — без Gemini Live (28.09, его выбор).

Будильник на фаджр — всегда так; звонок «позвони мне» и его звонок JES — если в настройках «📞 Экономно» (jarvis:cmode).
Live в КАЖДОМ ответе заново оплачивает инструкцию, инструменты и весь разговор, а звук у него — $3/$12 за 1M токенов
(звонок 2 мин 42 с стоил $0.087). Здесь:
  • речь режем на фразы по паузе (phone_cheap.Segmenter) — тишина и шум никуда не уходят;
  • фраза (звук) + разговор текстом → Flash-Lite с инструментами через БЕСПЛАТНЫЙ ключ Google (кончился лимит — платно,
    это копейки); параллельно — расшифровка: в историю кладём текст, а не звук;
  • ответ — озвучка потоком тем же голосом JES (gemini-3.8-flash-lite-tts, тоже бесплатный ключ); не вышло — голос Microsoft;
  • перебил — замолкаем сразу; эхо собственного ответа в трубке — не отвечаем.
Цена — около $0; ответ звучит через ~1.5–3 с после его фразы.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from typing import Any

from . import caller, live_call, undo
from .context import ai
from .live_call import LiveResult, _jsonable, _Session
from .persona import Persona
from .profile import Profile

logger = logging.getLogger(__name__)

RATE = caller.LIVE_RATE        # звук звонка — 24 кГц моно, как и озвучка
MAX_STEPS = 4                  # инструмент → ответ → инструмент…: не больше стольких шагов на одну его фразу
MAX_SECONDS = 420              # звонок — 7 минут максимум
WAKE_MAX_SECONDS = 240         # будильник — 4 минуты
SILENCE_NUDGE_S = 8.0          # будильник: столько тишины в начале — зовём снова (уже разговаривали — втрое дольше)
BARGE_IN_S = 0.6               # столько его речи поверх ответа — перебил: замолкаем (эхо из трубки короче и тише)
STT_WAIT_S = 4.0
HISTORY_MESSAGES = 16
HISTORY_CHARS = 10000
SAY_CHUNK = 9600               # голос Microsoft — кусками по 0.2 с

CHEAP_RULES = (
    "\nЭКОНОМНЫЙ ГОЛОС. Его реплика приходит записью голоса с пометкой времени в квадратных скобках; твой текст телефон "
    "произнесёт твоим голосом. Отвечай на КАЖДУЮ реплику одной-двумя короткими разговорными фразами, без списков, эмодзи и "
    "markdown. Числа, суммы и время — цифрами («25 000 сум», «5:12»). По-узбекски — ТОЛЬКО латиницей. Ровно «-» — только "
    "если в записи нет слов (тишина, шум, щелчок). Действие сделала — одной фразой скажи, что сделано.\n"
)


def wanted(mode: str, persona: Persona) -> bool:
    """Будильник — всегда экономно (его выбор 28.09); звонки — если так выбрано в настройках."""
    if mode == "wake":
        return True
    return mode == "assistant" and getattr(persona, "call_mode", "live") == "economy"


def _stamp(profile: Profile) -> str:
    return f"[{live_call.now_line(profile)}]"


def _words(text: str) -> set[str]:
    return set(re.sub(r"[^\w]+", " ", str(text or "").lower().replace("ё", "е")).split())


def is_echo(heard: str, said: str) -> bool:
    """Расшифровка — это его ответ JES, вернувшийся эхом из трубки (почти те же слова), а не новая реплика."""
    h, s = _words(heard), _words(said)
    return bool(h) and len(h) >= 2 and len(h & s) / len(h) >= 0.7


class CheapSession(_Session):
    """Разговор в звонке без Live: playout, инструменты (_run_one), итог и завершение — как у Live-сессии."""

    def __init__(self, profile: Profile, persona: Persona, *, mode: str, system: str, decls: list[dict[str, Any]]) -> None:
        super().__init__(profile, persona, mode=mode, system=system)
        self.decls = decls
        self.contents: list[dict[str, Any]] = []
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.last_said = ""
        self.speaking = False
        # end_call/snooze: положить трубку, когда договорит прощание. Не hangup_after_speech: playout Live кладёт трубку через
        # 0.6 с тишины, а здесь прощание ещё только пишется и озвучивается
        self.hang_after_turn = False
        self._say_task: asyncio.Task | None = None
        self.result.model = f"{ai.agent_model} (экономно)"

    # --- слух
    async def listen(self, incoming: asyncio.Queue) -> None:
        """Звук из трубки → фразы. Говорит поверх ответа JES — перебил: замолкаем."""
        from .phone_cheap import Segmenter

        seg = Segmenter(RATE)
        overlap_s = 0.0
        while not self.stop.is_set():
            try:
                chunk = await asyncio.wait_for(incoming.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            started, phrase, _noise = seg.feed(chunk)
            if seg.active and (self.out or self.speaking):
                overlap_s += len(chunk) / 2 / RATE
                if overlap_s >= BARGE_IN_S:
                    self.interrupt()
            elif not seg.active:
                overlap_s = 0.0
            if started:
                self.last_activity = asyncio.get_running_loop().time()
            if phrase:
                self.last_activity = asyncio.get_running_loop().time()
                self.heard_user = True
                self.queue.put_nowait(phrase)

    def interrupt(self) -> None:
        if self.out or self.speaking:
            logger.info("cheap voice: перебил — замолкаю")
        self.out.clear()
        if self._say_task is not None and not self._say_task.done():
            self._say_task.cancel()

    # --- голос
    async def say(self, text: str) -> None:
        """Произнести ответ: озвучка потоком в self.out (playout отправляет её в звонок в реальном времени)."""
        text = text.strip()
        if not text:
            return
        self.last_said = text
        self.result.transcript.append("я: " + text)
        self._say_task = asyncio.create_task(self._speak(text), name="cheap-say")
        try:
            await self._say_task
        except asyncio.CancelledError:
            if self.stop.is_set():
                raise
        finally:
            self._say_task = None

    async def _speak(self, text: str) -> None:
        from . import phone

        spoken = phone.speakable(text)
        self.speaking = True
        try:
            got = False
            stream = ai.speak_stream(spoken, voice=self.persona.voice)
            try:
                async for pcm in stream:
                    got = True
                    self.out.extend(pcm)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("cheap voice: озвучка не удалась: %s", str(exc)[:160])
            finally:
                await stream.aclose()
            if not got:
                from . import free_voice

                pcm = await free_voice.synthesize(spoken)
                if pcm:
                    self.out.extend(pcm)
        finally:
            self.speaking = False

    async def wait_quiet(self, limit: float = 30.0) -> None:
        """Дождаться, пока договорит (прощание перед тем, как положить трубку)."""
        loop = asyncio.get_running_loop()
        end = loop.time() + limit
        while (self.out or self.speaking) and loop.time() < end and not self.stop.is_set():
            await asyncio.sleep(0.1)

    # --- ход разговора
    async def worker(self) -> None:
        while not self.stop.is_set():
            pcm = await self.queue.get()
            try:
                await self.on_phrase(pcm)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("cheap voice: ход сорвался")
                await self.say("Секунду, не расслышала — повторите, пожалуйста." if self.persona.lang != "uz" else "Kechirasiz, qaytaring.")
            if self.hang_after_turn:
                await self.wait_quiet()
                await asyncio.sleep(0.5)  # последние кадры прощания — до телефона
                self.stop.set()

    async def on_phrase(self, pcm: bytes) -> None:
        from .phone_live import pcm_to_wav

        from . import ai as ai_mod

        wav = pcm_to_wav(pcm, RATE)
        free = ai_mod.use_free("")  # расшифровка — тоже через бесплатный ключ (задача берёт это с собой)
        try:
            stt = asyncio.create_task(self._transcribe(wav), name="cheap-stt")
        finally:
            ai_mod.reset_free(free)
        audio = {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(wav).decode()}}
        await self.turn([{"text": _stamp(self.profile)}, audio], stt=stt)

    async def on_text(self, text: str) -> None:
        """Реплика «от системы» (тишина — позвать его снова): без звука, текстом."""
        await self.turn([{"text": f"{_stamp(self.profile)} {text}"}], stt=None)

    async def _transcribe(self, wav: bytes) -> str:
        from .phone_api import _STT_PROMPT, clean_transcript

        try:
            return clean_transcript(await ai.transcribe_audio(wav, "audio/wav", prompt=_STT_PROMPT))
        except Exception as exc:
            logger.warning("cheap voice: расшифровка не удалась: %s", str(exc)[:160])
            return ""

    async def turn(self, parts: list[dict[str, Any]], *, stt: asyncio.Task | None) -> None:
        from . import ai as ai_mod
        from .handlers.agent import trim_history

        started = time.monotonic()
        idx = len(self.contents)
        self.contents.append({"role": "user", "parts": parts})
        text, calls = "", []
        free = ai_mod.use_free("")  # той же моделью (Flash-Lite), но через бесплатный ключ Google
        try:
            for _ in range(MAX_STEPS):
                step = await ai.agent_step(self.contents, system=self.system, tools=self.decls, thinking_budget=0, max_tokens=400)
                self.contents.append({"role": "model", "parts": step.parts or [{"text": step.text or "-"}]})
                if not step.calls:
                    text = step.text
                    break
                responses = []
                for name, args in step.calls:
                    calls.append(name)
                    res = await self._run_one({"name": name, "args": args, "id": None})
                    if self.hangup_after_speech:
                        self.hangup_after_speech, self.hang_after_turn = False, True
                    responses.append({"functionResponse": {"name": name, "response": _jsonable(res.get("response") or {})}})
                self.contents.append({"role": "user", "parts": responses})
        finally:
            ai_mod.reset_free(free)
        heard = ""
        if stt is not None:
            try:
                heard = await asyncio.wait_for(asyncio.shield(stt), timeout=STT_WAIT_S)
            except (asyncio.TimeoutError, Exception):
                heard = ""
            self.contents[idx] = {"role": "user", "parts": [{"text": f"{parts[0]['text']} {heard or '(неразборчиво)'}"}]}
            if heard:
                self.result.transcript.append("он: " + heard)
        self.contents = trim_history(self.contents, max_messages=HISTORY_MESSAGES, max_chars=HISTORY_CHARS)
        say = "" if re.fullmatch(r"[\W_]*", text or "") else text
        if stt is not None and not calls and (not heard or is_echo(heard, self.last_said)):
            say = ""  # шум или эхо собственного ответа из трубки — не отвечаем на то, чего он не говорил
        logger.info("cheap voice: %.1f с, инструменты %s, «%s» → «%s»", time.monotonic() - started, calls, heard[:60], say[:60])
        if say:
            await self.say(say)

    async def nudger(self) -> None:
        """Будильник: замолчал (мог снова заснуть) — позвать; уже разговаривали — ждём дольше."""
        loop = asyncio.get_running_loop()
        self.last_activity = loop.time()
        while not self.stop.is_set():
            await asyncio.sleep(1.0)
            if self.out or self.speaking or self.hang_after_turn or self.result.confirmed or not self.queue.empty():
                self.last_activity = loop.time() if (self.out or self.speaking) else self.last_activity
                continue
            limit = SILENCE_NUDGE_S * (3 if self.heard_user else 1)
            if loop.time() - self.last_activity < limit:
                continue
            self.last_activity = loop.time()
            logger.info("cheap voice: тишина %.0f с — зову снова", limit)
            await self.on_text("[Он молчит. Позови его по имени бодро ОДНОЙ короткой фразой и спроси, слышит ли он.]"
                               if not self.heard_user else "[Он давно молчит. Коротко спроси, всё ли в порядке и слышит ли он тебя.]")


async def run_call(profile: Profile, persona: Persona, dial: asyncio.Task, *, mode: str, topic: str = "",
                   wake: dict[str, Any] | None = None) -> LiveResult:
    """Звонок уже набирается (dial): готовим приветствие (запись с диска), взяли трубку — разговор экономным голосом."""
    from . import agent_tools_extra as extra
    from . import billing

    memory = await extra.memory_prompt(profile.telegram_id) if mode == "assistant" else ""
    system = live_call.system_instruction(profile, persona, mode=mode, memory=memory, wake=wake, topic=topic) + CHEAP_RULES
    if mode == "assistant":
        system += billing.voice_note()
    decls = live_call.tool_declarations(mode, full=True)
    sess = CheapSession(profile, persona, mode=mode, system=system, decls=decls)
    if mode == "wake":
        clip = asyncio.create_task(live_call.wake_clip(profile, persona), name="cheap-clip")
    elif not topic:
        clip = asyncio.create_task(live_call.hello_clip(profile, persona), name="cheap-clip")
    else:
        clip = None
    try:
        call = await dial
    except BaseException:
        if clip is not None:
            clip.cancel()
        raise
    sess.result.dialed = True
    if not call.get("answered"):
        sess.result.error = call.get("error")
        if clip is not None:
            clip.cancel()
        return sess.result
    sess.result.answered = True
    greeting = None
    if clip is not None:
        try:
            greeting = await asyncio.wait_for(clip, timeout=3)
        except Exception:
            greeting = None
    await _converse(sess, call, greeting, topic)
    logger.info("call %s: итог (экономно) — реплик %s, действия %s", sess.uid, len(sess.result.transcript), sess.result.actions)
    return sess.result


async def answer_call(profile: Profile, persona: Persona) -> LiveResult:
    """Он сам позвонил JES (экономный голос выбран): берём трубку и сразу «Алло, шеф! Слушаю.»."""
    from . import agent_tools_extra as extra

    memory = await extra.memory_prompt(profile.telegram_id)
    system = live_call.system_instruction(profile, persona, mode="assistant", memory=memory) + CHEAP_RULES
    sess = CheapSession(profile, persona, mode="assistant", system=system, decls=live_call.tool_declarations("assistant", full=True))
    clip = asyncio.create_task(live_call.hello_clip(profile, persona), name="cheap-clip")
    call = await caller.accept_stream_call(profile.telegram_id)
    if not call.get("answered"):
        clip.cancel()
        sess.result.error = call.get("error")
        return sess.result
    sess.result.answered = True
    try:
        greeting = await asyncio.wait_for(clip, timeout=3)
    except Exception:
        greeting = None
    await _converse(sess, call, greeting, "")
    return sess.result


async def _converse(sess: CheapSession, call: dict[str, Any], greeting: tuple[str, bytes] | None, topic: str) -> None:
    uid = sess.uid
    undo.begin_turn(uid)
    tasks: list[asyncio.Task] = []
    try:
        await asyncio.sleep(live_call.ANSWER_PAUSE)
        tasks = [asyncio.create_task(sess.playout(), name="cheap-play"),
                 asyncio.create_task(sess.listen(call["incoming"]), name="cheap-listen"),
                 asyncio.create_task(sess.worker(), name="cheap-worker")]
        if sess.mode == "wake":
            tasks.append(asyncio.create_task(sess.nudger(), name="cheap-nudge"))
        if greeting:
            text, pcm = greeting
            sess.out.extend(pcm)
            sess.last_said = text
            sess.result.transcript.append("я: " + text)
            # модель знает, что уже поздоровалась, — дальше слушает его
            sess.contents += [{"role": "user", "parts": [{"text": f"{_stamp(sess.profile)} [Звонок соединён.]"}]},
                              {"role": "model", "parts": [{"text": text}]}]
        else:
            await sess.on_text("[Звонок соединён. Начинай.]" if not topic else f"[Звонок соединён. Он просил поговорить о: {topic}]")
        ended: asyncio.Event = call["ended"]
        watcher = asyncio.create_task(ended.wait(), name="cheap-ended")
        stopper = asyncio.create_task(sess.stop.wait(), name="cheap-stop")
        tasks += [watcher, stopper]
        limit = WAKE_MAX_SECONDS if sess.mode == "wake" else MAX_SECONDS
        await asyncio.wait({watcher, stopper}, timeout=limit, return_when=asyncio.FIRST_COMPLETED)
        if ended.is_set():
            logger.info("call %s: собеседник положил трубку", uid)
    except Exception as exc:
        sess.result.error = f"cheap: {type(exc).__name__}: {exc}"[:300]
        logger.exception("call %s: экономный разговор сорвался", uid)
    finally:
        sess.stop.set()
        for task in (*tasks, *sess._tool_tasks):
            task.cancel()
        await asyncio.gather(*tasks, *sess._tool_tasks, return_exceptions=True)
        await caller.hang_up(uid)
        sess.result.mutated = bool(undo.end_turn(uid))


__all__ = ["wanted", "run_call", "answer_call", "is_echo", "CheapSession"]

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
import random
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
BARGE_IN_S = 0.8               # столько его речи поверх ответа — перебил: замолкаем (эхо из трубки короче и тише)
ECHO_WINDOW_S = 1.2            # фраза началась во время ответа JES или сразу после — сначала проверяем, не эхо ли это
STT_WAIT_S = 2.5            # расшифровка обычно готова вместе с ответом; дольше не держим ответ
HISTORY_MESSAGES = 16
HISTORY_CHARS = 10000
SAY_CHUNK = 9600               # голос Microsoft — кусками по 0.2 с
CALL_SILENCE_MS = 700          # 29.09: фраза кончилась — 0.7 с тишины (было 1 с, как на телефоне): каждый ответ на 0.3 с раньше
QUICK_MAX_S = 2.2              # короткая фраза («алло», «спасибо») — сначала свой распознаватель (~0.1 с) и записанный ответ
SLOW_TOOLS = {"web_search", "bot_task", "weather", "currency_rates", "send_to_chat"}  # пока ищет — «Секунду, сэр» записанным голосом

# 29.09 его просьба «записать несколько голосов, чтобы не тратить каждый раз токены»: частые реплики — фразами, озвученными
# один раз (лежат на диске, ai.speak_stream), без модели и за ~0.2 с. {t} — «сэр» (чаще) или «шеф», как он просил
_QUICK = (
    ("hear", re.compile(r"(алло|ало|алё|але|алле|allo|alo)( (алло|ало|алё|allo|alo))*|((ты|вы) )?(меня )?слыш(ишь|ите)( меня)?|"
                        r"(ты|вы) (тут|здесь|где)|эй|ау")),
    ("thanks", re.compile(r"(большое )?спасибо( большое)?|благодарю|(katta )?rahmat|(катта )?рахмат|раҳмат")),
    ("bye", re.compile(r"(ну |ладно |давай |всё |все )?пока|до свидания|отбой|xayr|хайр")),
)
PHRASES = {
    "ru": {"hear": ("Да, {t}, слышу вас хорошо.", "Я здесь, {t}. Слушаю."),
           "thanks": ("Всегда рада помочь, {t}!", "Пожалуйста, {t}!"),
           "bye": ("До связи, {t}!", "Всего доброго, {t}!"),
           "wait": ("Секунду, {t}.", "Сейчас посмотрю, {t}.", "Минутку, {t}.")},
    "uz": {"hear": ("Ha, {t}, eshitaman.", "Shu yerdaman, {t}. Eshitaman."),
           "thanks": ("Arzimaydi, {t}!", "Doim xizmatingizda, {t}!"),
           "bye": ("Xayr, {t}!", "Salomat bo'ling, {t}!"),
           "wait": ("Bir soniya, {t}.", "Hozir qarayman, {t}.")},
}
_TITLES = {"ru": ("сэр", "сэр", "шеф"), "uz": ("ser", "ser", "shef")}
# 03.10 тест будильника: он сказал что-то, модель ответила «-» (шум) — в трубке тишина, и будильник «вообще не говорит». На подъёме
# молчать в ответ нельзя: мягко переспрашиваем (не чаще раза в REASK_GAP_S)
WAKE_REASK = {"ru": ("Не расслышала, {t}. Вы проснулись?", "{t}, вы меня слышите? Скажите пару слов, пожалуйста."),
              "uz": ("Eshitolmadim, {t}. Uyg'ondingizmi?", "{t}, meni eshityapsizmi? Bir-ikki so'z ayting.")}
REASK_GAP_S = 10.0


def quick_kind(text: str) -> str | None:
    """«алло», «ты меня слышишь», «спасибо», «пока» — целиком, без продолжения («пока не надо» — не прощание)."""
    t = re.sub(r"[^\w' ]+", " ", str(text or "").lower().replace("ё", "е")).strip()
    t = " ".join(t.split())
    for kind, pattern in _QUICK:
        if t and pattern.fullmatch(t):
            return kind
    return None


def phrase_texts(persona: Persona) -> list[str]:
    """Все записанные фразы этого голоса — озвучить заранее (prewarm)."""
    lang = persona.lang if persona.lang in PHRASES else "ru"
    langs = [lang] + (["uz"] if persona.mirror and lang != "uz" else [])
    return [p.format(t=t) for lg in langs for group in PHRASES[lg].values() for p in group for t in dict.fromkeys(_TITLES[lg])]

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


def is_repeat(heard: str, before: str) -> bool:
    """Тот же вопрос ещё раз («сколько потрачено сегодня?» → «сколько сегодня потрачено?»): большинство слов совпадает."""
    h, b = {w for w in _words(heard) if len(w) >= 3}, {w for w in _words(before) if len(w) >= 3}
    return bool(h) and bool(b) and len(h & b) / min(len(h), len(b)) >= 0.6


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
        self.queue: asyncio.Queue[tuple[bytes, bool]] = asyncio.Queue()   # (фраза, началась поверх ответа JES)
        self._voice_at = -1e9          # когда JES последний раз звучал (для проверки эха)
        self.last_said = ""
        self.last_heard = ""           # его последний вопрос (для «повторил, пока я думала»)
        self.thinking = False          # идёт ход: модель думает или работает инструмент
        self.speaking = False
        # end_call/snooze: положить трубку, когда договорит прощание. Не hangup_after_speech: playout Live кладёт трубку через
        # 0.6 с тишины, а здесь прощание ещё только пишется и озвучивается
        self.hang_after_turn = False
        self._say_task: asyncio.Task | None = None
        self._reask_at = -1e9
        self.result.model = f"{ai.agent_model} (экономно)"

    # --- слух
    async def listen(self, incoming: asyncio.Queue) -> None:
        """Звук из трубки → фразы. Говорит поверх ответа JES — перебил: замолкаем."""
        from .phone_cheap import Segmenter

        seg = Segmenter(RATE, silence_ms=CALL_SILENCE_MS)
        loop = asyncio.get_running_loop()
        overlap_s = 0.0
        overlapped = False
        while not self.stop.is_set():
            try:
                chunk = await asyncio.wait_for(incoming.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            # 29.09 «он вообще не отвечает мне»: «перебил» считался и пока ответ ещё озвучивался (звука в трубке нет) — он
            # говорил «алло?» в тишину, и ответ отменялся, не прозвучав. Теперь — только пока голос JES правда звучит
            if self.out:
                self._voice_at = loop.time()
            started, phrase, _noise = seg.feed(chunk)
            if started:
                # 29.09: начал говорить, пока JES думает, — может быть повтор того же вопроса («она не слышала»): сначала
                # расшифровка, и повтор второй раз не отвечаем
                overlapped = loop.time() - self._voice_at < ECHO_WINDOW_S or self.thinking
            if seg.active and self.out:
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
                self.queue.put_nowait((phrase, overlapped))

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
        """Всегда голос JES (Google: бесплатным ключом, пока есть квота, дальше платно — ~$0.001 за ответ). 29.09 его выбор:
        утром квота кончилась, ответил голос Microsoft — «другой голос, какого-то мужика». Microsoft — только если Google
        не ответил вовсе."""
        from . import free_voice, phone

        spoken = phone.speakable(text)
        order = ["google", "microsoft"] if free_voice.available() else ["google"]
        self.speaking = True
        try:
            for how in order:
                if how == "microsoft":
                    pcm = await free_voice.synthesize(spoken)
                    if pcm:
                        self.out.extend(pcm)
                        return
                    continue
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
                if got:
                    return
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
            pcm, overlapped = await self.queue.get()
            try:
                await self.on_phrase(pcm, overlapped=overlapped)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("cheap voice: ход сорвался")
                await self.say("Секунду, не расслышала — повторите, пожалуйста." if self.persona.lang != "uz" else "Kechirasiz, qaytaring.")
            if self.hang_after_turn:
                await self.wait_quiet()
                await asyncio.sleep(0.5)  # последние кадры прощания — до телефона
                self.stop.set()

    async def on_phrase(self, pcm: bytes, *, overlapped: bool = False) -> None:
        from .phone_live import pcm_to_wav

        wav = pcm_to_wav(pcm, RATE)
        if overlapped:
            # началась поверх ответа JES (или сразу после): может быть эхо из трубки — сначала расшифровка, и эхо до модели
            # не доходит (иначе «Записала сорок тысяч» эхом записалось бы второй раз)
            heard = await self._transcribe(wav)
            if not heard or is_echo(heard, self.last_said):
                logger.info("cheap voice: эхо/шум поверх ответа — пропускаю «%s»", heard[:60])
                return
            if is_repeat(heard, self.last_heard):
                logger.info("cheap voice: повторил тот же вопрос, пока я думала, — второй раз не отвечаю «%s»", heard[:60])
                self.result.transcript.append("он (повтор): " + heard)
                return
            self.result.transcript.append("он: " + heard)
            self.last_heard = heard
            await self.turn([{"text": f"{_stamp(self.profile)} {heard}"}], stt=None)
            return
        if self.mode != "wake" and len(pcm) / 2 / RATE <= QUICK_MAX_S and await self._quick(wav):
            return
        stt = asyncio.create_task(self._transcribe(wav), name="cheap-stt")
        audio = {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(wav).decode()}}
        await self.turn([{"text": _stamp(self.profile)}, audio], stt=stt)

    async def _quick(self, wav: bytes) -> bool:
        """«Алло», «ты меня слышишь», «спасибо», «пока» — свой распознаватель (~0.1 с) и записанная фраза: без модели и токенов."""
        from . import wakeword

        started = time.monotonic()
        try:
            heard = await wakeword.check(wav)
        except Exception:
            logger.warning("cheap voice: распознаватель", exc_info=True)
            return False
        said = (heard or {}).get("text") or ""
        kind = quick_kind(said)
        if kind is None:
            return False
        answer = self.phrase(kind, uz=bool(re.search(r"рахмат|раҳмат|rahmat|хайр|xayr", said.lower())))
        self.result.transcript.append("он: " + said)
        # модель знает, что уже ответили, — если разговор продолжится
        self.contents += [{"role": "user", "parts": [{"text": f"{_stamp(self.profile)} {said}"}]},
                          {"role": "model", "parts": [{"text": answer}]}]
        logger.info("cheap voice: мгновенно «%s» → «%s» за %.2f с (записанная фраза, без модели)", said, answer, time.monotonic() - started)
        if kind == "bye":
            self.hang_after_turn = True
        await self.say(answer)
        return True

    def phrase(self, kind: str, *, uz: bool = False) -> str:
        lang = "uz" if uz and self.persona.mirror else (self.persona.lang if self.persona.lang in PHRASES else "ru")
        return random.choice(PHRASES[lang][kind]).format(t=random.choice(_TITLES[lang]))

    def recorded(self, kind: str) -> bytes | None:
        """Записанная фраза с диска — сразу, без сети (ещё не записана — None, её озвучит prewarm)."""
        from . import ai as ai_mod
        from . import phone

        path = ai_mod._tts_cache_path(ai_mod.tts_model_now(), self.persona.voice, phone.speakable(self.phrase(kind)))
        try:
            return path.read_bytes() if path is not None and path.exists() else None
        except OSError:
            return None

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
        started = time.monotonic()
        idx = len(self.contents)
        self.contents.append({"role": "user", "parts": parts})
        self.thinking = True
        try:
            await self._turn(parts, stt=stt, started=started, idx=idx)
        finally:
            self.thinking = False

    async def _turn(self, parts: list[dict[str, Any]], *, stt: asyncio.Task | None, started: float, idx: int) -> None:
        from .handlers.agent import trim_history

        text, calls = "", []
        # 29.09 его выбор «быстро»: платным ключом сразу (Flash-Lite, доли цента) — бесплатный уровень утром был перегружен
        # (503), и ответ на «Алло» шёл 7.6 с
        for _ in range(MAX_STEPS):
            step = await ai.agent_step(self.contents, system=self.system, tools=self.decls, thinking_budget=0, max_tokens=400)
            self.contents.append({"role": "model", "parts": step.parts or [{"text": step.text or "-"}]})
            if not step.calls:
                text = step.text
                break
            if not calls and self.mode != "wake" and not self.out and any(n in SLOW_TOOLS for n, _ in step.calls):
                # поиск, погода, дела из чата — это 1–3 с: сразу «Секунду, сэр» записанным голосом, чтобы не было тишины
                # (в тишину он говорил «алло?» и перебивал ответ)
                wait = self.recorded("wait")
                if wait:
                    self.out.extend(wait)
            responses = []
            for name, args in step.calls:
                calls.append(name)
                res = await self._run_one({"name": name, "args": args, "id": None})
                if self.hangup_after_speech:
                    self.hangup_after_speech, self.hang_after_turn = False, True
                responses.append({"functionResponse": {"name": name, "response": _jsonable(res.get("response") or {})}})
            self.contents.append({"role": "user", "parts": responses})
        heard = ""
        if stt is not None:
            try:
                heard = await asyncio.wait_for(asyncio.shield(stt), timeout=STT_WAIT_S)
            except (asyncio.TimeoutError, Exception):
                heard = ""
            self.contents[idx] = {"role": "user", "parts": [{"text": f"{parts[0]['text']} {heard or '(неразборчиво)'}"}]}
            if heard:
                self.result.transcript.append("он: " + heard)
                self.last_heard = heard
        self.contents = trim_history(self.contents, max_messages=HISTORY_MESSAGES, max_chars=HISTORY_CHARS)
        say = "" if re.fullmatch(r"[\W_]*", text or "") else text
        if stt is not None and not calls and (not heard or is_echo(heard, self.last_said)):
            say = ""  # шум или эхо собственного ответа из трубки — не отвечаем на то, чего он не говорил
        if not say and self.mode == "wake" and stt is not None and not calls:
            say = self.wake_reask(heard)  # подъём: тишиной на его слова не отвечаем
            if say and self.contents and self.contents[-1].get("role") == "model":
                self.contents[-1] = {"role": "model", "parts": [{"text": say}]}  # модель знает, что переспросила, а не промолчала
        logger.info("cheap voice: %.1f с, инструменты %s, «%s» → «%s»", time.monotonic() - started, calls, heard[:60], say[:60])
        if say:
            await self.say(say)

    def wake_reask(self, heard: str) -> str:
        """Подъём: он что-то сказал или хрипнул, а ответа нет — мягко переспросить. Не во время речи JES, не на эхо её слов
        и не чаще раза в REASK_GAP_S; уже подтвердил подъём — молчим."""
        now = time.monotonic()
        if self.out or self.speaking or self.result.confirmed or now - self._reask_at < REASK_GAP_S:
            return ""
        if heard and is_echo(heard, self.last_said):
            return ""
        self._reask_at = now
        lang = "uz" if self.persona.lang == "uz" else "ru"
        return random.choice(WAKE_REASK[lang]).format(t=random.choice(_TITLES[lang]))

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


_prewarming: set[asyncio.Task] = set()


async def prewarm(persona: Persona) -> int:
    """Озвучить заранее записанные фразы этого голоса, которых ещё нет на диске (один раз навсегда, ~$0.0003 за фразу).
    По одной с паузой — у озвучки лимит запросов в минуту. Возвращает, сколько записала."""
    from . import ai as ai_mod
    from . import phone

    made = 0
    for text in phrase_texts(persona):
        spoken = phone.speakable(text)
        path = ai_mod._tts_cache_path(ai_mod.tts_model_now(), persona.voice, spoken)
        if path is None or path.exists():
            continue
        try:
            async for _ in ai.speak_stream(spoken, voice=persona.voice):
                pass
            made += 1
        except Exception as exc:
            logger.info("cheap voice: фразу «%s» не записала: %s", text, str(exc)[:120])
        await asyncio.sleep(1.5)
    if made:
        logger.info("cheap voice: записала %s фраз голосом %s", made, persona.voice)
    return made


def _prewarm_soon(persona: Persona) -> None:
    if _prewarming:
        return
    task = asyncio.create_task(prewarm(persona), name="cheap-prewarm")
    _prewarming.add(task)
    task.add_done_callback(_prewarming.discard)


async def run_call(profile: Profile, persona: Persona, dial: asyncio.Task, *, mode: str, topic: str = "",
                   wake: dict[str, Any] | None = None) -> LiveResult:
    """Звонок уже набирается (dial): готовим приветствие (запись с диска), взяли трубку — разговор экономным голосом."""
    from . import agent_tools_extra as extra
    from . import billing

    memory = await extra.memory_prompt(profile.telegram_id) if mode == "assistant" else ""
    system = live_call.system_instruction(profile, persona, mode=mode, memory=memory, wake=wake, topic=topic, engine="cheap") + CHEAP_RULES
    if mode == "assistant":
        system += billing.voice_note()
    decls = live_call.tool_declarations(mode, full=True)
    sess = CheapSession(profile, persona, mode=mode, system=system, decls=decls)
    if mode != "wake":
        _prewarm_soon(persona)
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
    system = live_call.system_instruction(profile, persona, mode="assistant", memory=memory, engine="cheap") + CHEAP_RULES
    sess = CheapSession(profile, persona, mode="assistant", system=system, decls=live_call.tool_declarations("assistant", full=True))
    _prewarm_soon(persona)
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

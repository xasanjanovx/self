"""Звонок на Gemini Live: живой голос, ответ за ~1 секунду, можно перебивать.

Схема:
  Telegram-звонок (pytgcalls)  ──входящий звук 24 кГц──▶  Gemini Live (websocket)
                               ◀──голос модели 24 кГц───
  Модель сама слышит паузы и перебивания (VAD на стороне Gemini), отвечает голосом
  и вызывает те же инструменты, что Джарвис в чате: добавить цель, удалить операцию,
  записать калории, ответить по данным. Всё, что изменено в звонке, после разговора
  приходит в чат одним сообщением с кнопкой «Отменить».

Режимы:
  assistant — «позвони»: обычный разговор с доступом к данным;
  wake      — подъём на фаджр: убедиться, что встал (confirm_awake), дать задание.

Трубку положили → разговор окончен, перезвона нет (кроме подъёма, где перезвон —
смысл функции, пока подъём не подтверждён).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from . import caller
from .about import ABOUT_SELF
from .persona import LANG_CODES, Persona, human_rules, lang_rule, style_rules
from .profile import Profile

logger = logging.getLogger(__name__)

WS_URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.{ver}.GenerativeService.BidiGenerateContent"
MODELS = ("gemini-3.8-live", "gemini-3.1-flash-live-preview", "gemini-2.5-flash-native-audio-latest")
UPLINK_BATCH_MS = 40         # шлём звук в Gemini пачками по 40 мс
MAX_SECONDS = 600            # 10 минут — страховка
DRAIN_SECONDS = 8.0          # после «до связи» даём договорить фразу

# инструменты чата, которые в голосе не нужны или мешают
_SKIP_TOOLS = {"hand_off", "open_screen", "ask_user", "call_me"}

_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


@dataclass
class LiveResult:
    answered: bool = False
    error: str | None = None
    confirmed: bool = False            # wake: подтвердил подъём
    snooze_minutes: int | None = None  # wake: попросил отложить
    transcript: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    mutated: bool = False
    model: str | None = None


# ------------------------------------------------------------------ промпт и инструменты
def system_instruction(profile: Profile, p: Persona, *, mode: str, snapshot: str = "", memory: str = "",
                       wake: dict[str, Any] | None = None, topic: str = "") -> str:
    now = profile.now
    name = p.name_for(profile.first_name) or "пользователь"
    base = (
        f"Ты — Джарвис, личный помощник {name}. Сейчас ты говоришь с ним ПО ТЕЛЕФОНУ (звонок в Telegram). "
        "Голос у тебя женский — о себе говори в женском роде («поняла», «записала»). "
        f"Сейчас {_WEEKDAYS[now.weekday()]}, {now:%d.%m.%Y %H:%M}, Андижан, Узбекистан. Валюта — сум.\n\n"
        f"{lang_rule(p)}\n\n{human_rules(p)}\n{style_rules(p, spoken=True)}\n"
        "Речь: без списков, эмодзи и markdown; суммы словами («двадцать пять тысяч сум»), не называй id записей. "
        "Если перебили — сразу остановись и слушай.\n"
    )
    if mode == "wake":
        w = wake or {}
        return base + (
            "\nЗАДАЧА ЗВОНКА: разбудить его на утренний намаз (бомдод/фаджр). "
            f"Такбир в {w.get('takbir') or 'скоро'}, до него {w.get('minutes_left', '')} минут. "
            f"Задание на утро: {w.get('task') or 'выпить стакан воды'}.\n"
            "1) Поздоровайся («Ассалому алайкум»), скажи, сколько осталось до такбира. "
            "2) Убедись, что он РЕАЛЬНО проснулся: попроси ответить осмысленно, выпить стакан воды, встать с кровати. "
            "Сонное «угу», «щас», «ещё пять минут» — НЕ подтверждение: мягко, но настойчиво продолжай будить, можно с лёгкой шуткой. "
            "3) Когда он ясно и связно ответил, что встал, — вызови confirm_awake, коротко скажи задание и время такбира, попрощайся и вызови end_call. "
            "Если просит отложить — не соглашайся больше чем на 5 минут; если настаивает — snooze(minutes). "
            "Без нотаций и проповедей."
        )
    rules = (
        "\nТЫ — ПОЛНОЦЕННЫЙ ПОМОЩНИК, А НЕ ТОЛЬКО ФИНАНСОВЫЙ. Отвечай на ЛЮБЫЕ вопросы, как умный знающий человек: "
        "жизнь, здоровье, спорт, учёба, работа, техника, религия, история, советы, перевод, посчитать, придумать, объяснить, просто поболтать. "
        "Свежие факты (новости, цены, курсы, погода, адреса, расписания, «что такое…», «кто такой…») — сначала web_search (ищет в Google), "
        "потом ответь своими словами; погода — weather, курс валют — currency_rates, сложный расчёт — calculate. "
        f"Твои знания устарели: всё, что могло измениться за последние два года (спорт, новости, цены, законы, версии, «последний/новый/текущий»), "
        f"ОБЯЗАТЕЛЬНО проверь поиском — сейчас {now.year} год. "
        "ЗАПРЕЩЕНО отвечать «не могу», «у меня нет информации», «я только финансовый помощник»: если точных данных нет — "
        "найди, прикинь, оцени или скажи, как узнать. Вопросы про тебя самого (что умеешь, как устроен, сколько стоишь) — из блока «О СЕБЕ».\n"
        "ДЕЙСТВИЯ: у тебя те же инструменты, что в чате: операции, долги, счета, цели, задачи, заметки, питание, напоминания, будильник, память. "
        "Просьбы выполняй сразу, без «точно?» («добавь цель…», «удали вчерашнее такси», «запиши обед сорок тысяч», «напомни завтра в девять», "
        "«я съел плов» — сам оцени калории и запиши через add_calorie_logs; трату — add_finance_entries). "
        "После действия одной живой фразой скажи, что сделано. Вопросы по его данным — сначала инструмент, потом ответ цифрами. "
        "Узнал о нём что-то важное и надолго (люди, планы, предпочтения) — сохрани remember_about_me, не говоря об этом. "
        "Просит «скинь/отправь мне в чат» (список, рецепт, текст, ссылку, план) — send_to_chat с готовым текстом и скажи, что отправила.\n"
        "ЧЕСТНОСТЬ: не обещай того, чего не сделаешь инструментами. Договорились о фото («пришлю фото челленджа — отмечай», «буду слать чеки») — "
        "СРАЗУ вызови expect_photo с подробной инструкцией и сроком: тогда его фото в чате придут Джарвису с этой инструкцией, а не в подсчёт калорий. "
        "Хочет что-то сложное — ищи способ своими инструментами; по-настоящему невозможное — честно одной фразой и ближайшая замена.\n"
        "Когда он прощается («всё», «пока», «rahmat», «xayr», «bo'ldi») — тепло и коротко попрощайся и вызови end_call.\n"
    )
    opening = (f"Начни разговор с темы, которую он попросил: «{topic}». Поздоровайся одной фразой и сразу к делу.\n" if topic
               else "Поздоровайся одной короткой живой фразой (по имени, учитывая время суток) и жди.\n")
    return (base + rules + opening + "\n" + ABOUT_SELF
            + (f"\n{memory}\n" if memory else "") + (f"\nДАННЫЕ:\n{snapshot}" if snapshot else ""))


def _control_tools(mode: str) -> list[dict[str, Any]]:
    tools = [{"name": "end_call", "description": "Положить трубку — когда разговор окончен или человек попрощался.",
              "parameters": {"type": "OBJECT", "properties": {}}}]
    if mode == "wake":
        tools += [
            {"name": "confirm_awake", "description": "Человек ясно и связно подтвердил, что проснулся и встаёт.",
             "parameters": {"type": "OBJECT", "properties": {}}},
            {"name": "snooze", "description": "Отложить звонок на несколько минут (не больше 10).",
             "parameters": {"type": "OBJECT", "properties": {"minutes": {"type": "INTEGER", "description": "на сколько минут"}}, "required": ["minutes"]}},
        ]
    return tools


_SEND_TO_CHAT = {"name": "send_to_chat",
                 "description": "Отправить ему в Telegram-чат текст (список, рецепт, план, ссылку, адрес, черновик сообщения) — когда просит «скинь/отправь в чат» или это удобнее прочитать, чем слушать.",
                 "parameters": {"type": "OBJECT", "properties": {"text": {"type": "STRING", "description": "готовый текст сообщения, можно с переносами строк"}}, "required": ["text"]}}


def tool_declarations(mode: str) -> list[dict[str, Any]]:
    from . import agent_tools

    decls = _control_tools(mode)
    if mode == "assistant":
        decls += [d for d in agent_tools.declarations() if d["name"] not in _SKIP_TOOLS]
        decls.append(_SEND_TO_CHAT)
    else:
        decls += [d for d in agent_tools.declarations() if d["name"] in {"prayer_times", "get_wake"}]
    return decls


# ------------------------------------------------------------------ сессия
class _Session:
    def __init__(self, profile: Profile, persona: Persona, *, mode: str, system: str) -> None:
        from . import agent_tools

        self.profile = profile
        self.persona = persona
        self.mode = mode
        self.system = system
        self.uid = profile.telegram_id
        self.result = LiveResult()
        self.out = bytearray()                 # голос модели, ждущий отправки в звонок
        self.stop = asyncio.Event()
        self.hangup_after_speech = False
        self.ctx = agent_tools.ToolContext(profile=profile, text="(звонок)")
        self._in_text: list[str] = []
        self._out_text: list[str] = []

    # --- websocket
    async def connect(self, session):  # noqa: ANN001 — aiohttp.ClientSession
        from .context import settings

        last_error = None
        # сначала с жёстким языком речи (languageCode); модель не приняла — та же модель без него
        for model, rich in ((m, r) for m in MODELS for r in (True, False)):
            try:
                ws = await session.ws_connect(WS_URL.format(ver="v1beta") + f"?key={settings.gemini_api_key}",
                                              heartbeat=20, max_msg_size=0)
            except Exception as exc:
                last_error = f"connect: {exc}"
                continue
            await ws.send_str(json.dumps(self.setup_payload(model, rich=rich)))
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=15)
            except asyncio.TimeoutError:
                await ws.close()
                last_error = f"{model}: setup timeout"
                continue
            data = _decode(msg)
            if data is not None and "setupComplete" in data:
                self.result.model = model
                logger.info("live: модель %s, голос %s, язык %s, расширенный режим %s", model, self.persona.voice, self.persona.lang, rich)
                return ws
            last_error = f"{model}: {getattr(msg, 'extra', None) or getattr(msg, 'data', '')!s}"[:300]
            logger.warning("live setup failed: %s", last_error)
            await ws.close()
        raise RuntimeError(last_error or "live setup failed")

    def setup_payload(self, model: str, *, rich: bool) -> dict[str, Any]:
        """rich — жёсткий язык речи (languageCode из настроек). Не включаем: enableAffectiveDialog
        (модель отвечает «invalid argument» на первую же реплику) и встроенный googleSearch
        (модель им не пользуется — поиск идёт через обычный инструмент web_search)."""
        speech: dict[str, Any] = {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.persona.voice}}}
        gen: dict[str, Any] = {"responseModalities": ["AUDIO"], "speechConfig": speech}
        tools: list[dict[str, Any]] = [{"functionDeclarations": tool_declarations(self.mode)}]
        if rich:
            speech["languageCode"] = LANG_CODES.get(self.persona.lang, "uz-UZ")
        return {"setup": {
            "model": f"models/{model}",
            "generationConfig": gen,
            "systemInstruction": {"parts": [{"text": self.system}]},
            "tools": tools,
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
        }}

    # --- задачи
    async def uplink(self, ws, incoming: asyncio.Queue) -> None:  # noqa: ANN001
        """Звук собеседника → Gemini (пачками по 40 мс)."""
        batch = bytearray()
        need = caller.LIVE_RATE // 1000 * UPLINK_BATCH_MS * 2
        while not self.stop.is_set():
            try:
                chunk = await asyncio.wait_for(incoming.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            batch.extend(chunk)
            if len(batch) >= need:
                payload = {"realtimeInput": {"audio": {"data": base64.b64encode(bytes(batch)).decode(), "mimeType": f"audio/pcm;rate={caller.LIVE_RATE}"}}}
                batch.clear()
                try:
                    await ws.send_str(json.dumps(payload))
                except Exception:
                    self.stop.set()
                    return

    async def downlink(self, ws) -> None:  # noqa: ANN001
        """Ответы Gemini: голос, перебивания, расшифровки, вызовы инструментов."""
        while not self.stop.is_set():
            msg = await ws.receive()
            data = _decode(msg)
            if data is None:
                if msg.type.name in {"CLOSE", "CLOSED", "CLOSING", "ERROR"}:
                    logger.info("live: соединение с моделью закрыто (%s)", getattr(msg, "extra", ""))
                    self.stop.set()
                    return
                continue
            sc = data.get("serverContent") or {}
            if sc.get("interrupted"):
                self.out.clear()  # перебили — замолкаем сразу
            for part in ((sc.get("modelTurn") or {}).get("parts") or []):
                blob = part.get("inlineData") or {}
                if blob.get("data"):
                    self.out.extend(base64.b64decode(blob["data"]))
            if (t := (sc.get("inputTranscription") or {}).get("text")):
                self._in_text.append(t)
            if (t := (sc.get("outputTranscription") or {}).get("text")):
                self._out_text.append(t)
            if sc.get("turnComplete"):
                self._flush_transcript()
            if "toolCall" in data:
                await self._run_tools(ws, data["toolCall"].get("functionCalls") or [])
            if "goAway" in data:
                logger.info("live: сервер просит завершить сессию")
                self.hangup_after_speech = True

    async def playout(self) -> None:
        """Голос модели → звонок, ровно в реальном времени (кадры по 10 мс)."""
        loop = asyncio.get_running_loop()
        frame = caller.FRAME_BYTES
        silence = bytes(frame)
        tick = caller.FRAME_MS / 1000
        next_at = loop.time()
        drained_at = None
        while not self.stop.is_set():
            if len(self.out) >= frame:
                chunk = bytes(self.out[:frame])
                del self.out[:frame]
            else:
                chunk = silence
                if self.hangup_after_speech:
                    drained_at = drained_at or loop.time()
                    if loop.time() - drained_at > 0.6:  # договорил прощание — кладём трубку
                        self.stop.set()
                        return
            if not await caller.send_audio(self.uid, chunk):
                self.stop.set()
                return
            next_at += tick
            delay = next_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -0.2:  # отстали (сеть/CPU) — не пытаемся догнать рывком
                next_at = loop.time()

    # --- инструменты
    async def _run_tools(self, ws, calls: list[dict[str, Any]]) -> None:  # noqa: ANN001
        from . import agent_tools

        responses = []
        for call in calls:
            name, args, cid = call.get("name"), call.get("args") or {}, call.get("id")
            logger.info("live tool %s %s", name, json.dumps(args, ensure_ascii=False)[:200])
            if name == "end_call":
                self.hangup_after_speech = True
                result: dict[str, Any] = {"ok": True}
            elif name == "confirm_awake":
                self.result.confirmed = True
                result = {"ok": True}
            elif name == "send_to_chat":
                result = await _send_to_chat(self.uid, str(args.get("text") or ""))
                if result.get("ok"):
                    self.result.actions.append("send_to_chat")
            elif name == "snooze":
                self.result.snooze_minutes = max(1, min(10, int(args.get("minutes") or 5)))
                self.hangup_after_speech = True
                result = {"ok": True, "minutes": self.result.snooze_minutes}
            else:
                result = await agent_tools.run(str(name), args, self.ctx)
                if not result.get("error"):
                    self.result.actions.append(str(name))
            responses.append({"id": cid, "name": name, "response": _jsonable(result)})
        try:
            await ws.send_str(json.dumps({"toolResponse": {"functionResponses": responses}}, ensure_ascii=False, default=str))
        except Exception:
            self.stop.set()

    def _flush_transcript(self) -> None:
        if self._in_text:
            self.result.transcript.append("он: " + "".join(self._in_text).strip())
            self._in_text.clear()
        if self._out_text:
            self.result.transcript.append("я: " + "".join(self._out_text).strip())
            self._out_text.clear()


async def _send_to_chat(uid: int, text: str) -> dict[str, Any]:
    """Текст из звонка в чат — он сам попросил прислать, поэтому обычным сообщением (не исчезает)."""
    text = text.strip()
    if not text:
        return {"error": "empty text"}
    try:
        from .context import bot_instance

        await bot_instance().send_message(uid, text[:4000], parse_mode=None)
        return {"ok": True}
    except Exception as exc:
        logger.warning("send_to_chat failed", exc_info=True)
        return {"error": str(exc)[:120]}


def _decode(msg) -> dict[str, Any] | None:  # noqa: ANN001
    try:
        if msg.type.name == "TEXT":
            return json.loads(msg.data)
        if msg.type.name == "BINARY":
            return json.loads(msg.data.decode("utf-8"))
    except Exception:
        logger.debug("live: не разобрал сообщение", exc_info=True)
    return None


def _jsonable(value: Any) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return {"result": str(value)[:2000]}


# ------------------------------------------------------------------ точка входа
async def run(profile: Profile, *, mode: str = "assistant", topic: str = "", wake: dict[str, Any] | None = None,
              ring_seconds: int = 45) -> LiveResult:
    """Позвонить и провести разговор целиком. Возвращает итог (что сказано, что сделано)."""
    import aiohttp

    from . import services, undo

    uid = profile.telegram_id
    persona = await services.persona(uid)
    snapshot = memory = ""
    if mode == "assistant":
        from . import agent_tools
        from . import agent_tools_extra as extra

        try:
            snapshot = await agent_tools.snapshot(profile)
            memory = await extra.memory_prompt(uid)
        except Exception:
            logger.debug("live: snapshot failed", exc_info=True)
    system = system_instruction(profile, persona, mode=mode, snapshot=snapshot, memory=memory, wake=wake, topic=topic)
    sess = _Session(profile, persona, mode=mode, system=system)

    async with aiohttp.ClientSession() as http:
        # модель подключаем заранее, пока идут гудки — чтобы ответить сразу, как возьмут трубку
        try:
            ws = await sess.connect(http)
        except Exception as exc:
            sess.result.error = f"live: {exc}"
            logger.warning("live connect failed: %s", exc)
            return sess.result
        call = await caller.open_stream_call(uid, username=profile.username, ring_seconds=ring_seconds)
        if not call.get("answered"):
            sess.result.error = call.get("error")
            await ws.close()
            return sess.result
        sess.result.answered = True
        undo.begin_turn(uid)
        # «трубку взяли» — пусть модель заговорит первой
        kick = "[Звонок соединён. Начинай.]"
        await ws.send_str(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{"text": kick}]}], "turnComplete": True}}))
        tasks = [
            asyncio.create_task(sess.uplink(ws, call["incoming"]), name="live-up"),
            asyncio.create_task(sess.downlink(ws), name="live-down"),
            asyncio.create_task(sess.playout(), name="live-play"),
        ]
        ended: asyncio.Event = call["ended"]
        watcher = asyncio.create_task(ended.wait(), name="live-ended")
        stopper = asyncio.create_task(sess.stop.wait(), name="live-stop")
        try:
            await asyncio.wait({watcher, stopper}, timeout=MAX_SECONDS, return_when=asyncio.FIRST_COMPLETED)
            if ended.is_set():
                logger.info("call %s: собеседник положил трубку", uid)
        finally:
            sess.stop.set()
            for task in (*tasks, watcher, stopper):
                task.cancel()
            await asyncio.gather(*tasks, watcher, stopper, return_exceptions=True)
            sess._flush_transcript()
            await caller.hang_up(uid)
            try:
                await ws.close()
            except Exception:
                pass
            sess.result.mutated = bool(undo.end_turn(uid))
    logger.info("call %s: итог — модель %s, реплик %s, действия %s", uid, sess.result.model, len(sess.result.transcript), sess.result.actions)
    return sess.result


__all__ = ["run", "LiveResult", "system_instruction", "tool_declarations", "MODELS"]

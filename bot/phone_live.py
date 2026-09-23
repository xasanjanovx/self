"""Живой разговор с Джарвисом на телефоне (как Gemini Live): телефон ⇄ WebSocket ⇄ Gemini Live.

Тот же голос, характер и инструменты, что в звонках бота (bot/live_call.py, режим "phone"), плюс
телефонные действия (bot/phone.py) и управление любым приложением на экране (control_phone):
телефон присылает список элементов экрана и скриншот, модель выбирает одно действие, телефон
его выполняет — и так по шагам, пока цель не достигнута.

Протокол /jarvis/v1/live (заголовок Authorization: Bearer <JARVIS_TOKEN>):
  телефон → сервер
    {"type":"hello","device":{…},"greet":bool,"text":str?} — первым сообщением
    бинарные кадры — микрофон, PCM s16le 16 кГц моно
    {"type":"text","text":…} — написанная реплика; {"type":"greet"} — позвали только по имени
    {"type":"result","id":N,…} — ответ на запрос экрана/действия; {"type":"action_error","text":…}; {"type":"bye"}
  сервер → телефон
    бинарные кадры — голос Джарвиса, PCM s16le 24 кГц моно
    {"type":"ready"} {"type":"user","text":…} {"type":"jarvis","text":…} {"type":"turn_complete"} {"type":"interrupted"}
    {"type":"action","action":{…}} {"type":"status","text":…} {"type":"control","on":bool} {"type":"need_contacts"}
    {"type":"screen","id":N} {"type":"ui","id":N,"op":…,"args":{…}} {"type":"end"} {"type":"error","text":…}
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

from . import live_call
from . import phone
from . import services
from . import undo
from .context import ai
from .live_call import MAX_SECONDS, _decode, _jsonable, _send_to_chat, _Session

logger = logging.getLogger(__name__)

INPUT_RATE = 16000
SCREEN_MODEL = os.getenv("SCREEN_MODEL") or "gemini-3.8-flash"
MAX_CONTROL_STEPS = 30
CONTROL_SECONDS = 180
STOP_WORDS = ("стоп", "хватит", "остановись", "прекрати", "отмена", "to'xta", "toxta", "bas qil", "stop")
GREET = "[Он позвал тебя по имени и ждёт. Откликнись одним-двумя словами («Да?», «Слушаю»), без приветствий.]"

_TOOL_STATUS = {
    "web_search": "Ищу в интернете…", "weather": "Смотрю погоду…", "currency_rates": "Смотрю курс…",
    "telegram_read": "Читаю Telegram…", "telegram_send": "Готовлю сообщение…", "deep_analysis": "Анализирую…",
    "list_finance_entries": "Смотрю операции…", "get_finance_stats": "Считаю…",
}

CONTROL_PHONE = {
    "name": "control_phone",
    "description": (
        "Сделать что-то в ЛЮБОМ приложении на экране телефона, чего нет среди других инструментов: написать в WhatsApp/Instagram, "
        "найти видео в YouTube, включить Wi-Fi/Bluetooth/режим «не беспокоить», поменять настройку, заказать такси, посмотреть что-то в "
        "приложении, прочитать что на экране. Джарвис сам нажимает, печатает и листает по шагам (до минуты). "
        "Если вернёт need_confirmation — спроси пользователя этим вопросом; согласился → снова control_phone с тем же goal и confirmed=true."
    ),
    "parameters": {"type": "OBJECT", "properties": {
        "goal": {"type": "STRING", "description": "подробная цель со всеми деталями: приложение, кому, точный текст, что найти"},
        "confirmed": {"type": "BOOLEAN", "description": "true — пользователь подтвердил важное действие, о котором спрашивали"},
    }, "required": ["goal"]},
}


def phone_declarations() -> list[dict[str, Any]]:
    return [t.declaration() for t in phone.PHONE_TOOLS.values()] + [CONTROL_PHONE]


# ------------------------------------------------------------------ управление экраном
def _obj(props: dict[str, Any] | None = None, required: tuple[str, ...] = ()) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "OBJECT", "properties": props or {}}
    if required:
        out["required"] = list(required)
    return out


_INDEX = {"type": "INTEGER", "description": "номер элемента [N] из списка"}
SCREEN_TOOLS = [
    {"name": "tap", "description": "Нажать на элемент.", "parameters": _obj({"index": _INDEX}, ("index",))},
    {"name": "long_press", "description": "Долгое нажатие на элемент.", "parameters": _obj({"index": _INDEX}, ("index",))},
    {"name": "type_text", "description": "Ввести текст в поле ввода (заменяет его содержимое).", "parameters": _obj({
        "index": _INDEX, "text": {"type": "STRING", "description": "текст"},
        "submit": {"type": "BOOLEAN", "description": "после ввода нажать Enter / Поиск / Готово"}}, ("index", "text"))},
    {"name": "tap_xy", "description": "Нажать в точку экрана (пиксели экрана, как @x,y у элементов) — только если нужного элемента нет в списке, но он виден на скриншоте.",
     "parameters": _obj({"x": {"type": "INTEGER", "description": "x"}, "y": {"type": "INTEGER", "description": "y"}}, ("x", "y"))},
    {"name": "scroll", "description": "Прокрутить экран (или элемент index) — показать то, что ниже/выше/левее/правее.", "parameters": _obj({
        "direction": {"type": "STRING", "description": "up | down | left | right", "enum": ["up", "down", "left", "right"]},
        "index": {"type": "INTEGER", "description": "номер прокручиваемого элемента (необязательно)"}}, ("direction",))},
    {"name": "back", "description": "Кнопка «Назад».", "parameters": _obj()},
    {"name": "home", "description": "На главный экран.", "parameters": _obj()},
    {"name": "open_app", "description": "Открыть приложение по названию (быстрее, чем искать иконку).",
     "parameters": _obj({"name": {"type": "STRING", "description": "название приложения"}}, ("name",))},
    {"name": "wait", "description": "Подождать, пока загрузится.", "parameters": _obj({"seconds": {"type": "NUMBER", "description": "1–5"}}, ("seconds",))},
    {"name": "done", "description": "Цель достигнута — кратко, что сделано или что увидел (ответ на вопрос).",
     "parameters": _obj({"summary": {"type": "STRING", "description": "итог одной-двумя фразами"}}, ("summary",))},
    {"name": "ask_user", "description": "Нужно подтверждение перед необратимым действием или не хватает данных.",
     "parameters": _obj({"question": {"type": "STRING", "description": "короткий вопрос пользователю"}}, ("question",))},
    {"name": "fail", "description": "Сделать невозможно (нужен вход, пароль, приложения нет…).",
     "parameters": _obj({"reason": {"type": "STRING", "description": "почему, одной фразой"}}, ("reason",))},
]

SCREEN_SYSTEM = (
    "Ты управляешь Android-телефоном владельца (Xiaomi, HyperOS; интерфейс в основном на английском, иногда на китайском) "
    "через специальные возможности. Тебе дают ЦЕЛЬ, список элементов текущего экрана и скриншот. За один шаг вызывай РОВНО ОДИН инструмент.\n"
    "Элементы: [N] тип «текст» (описание) #id {флаги} @x,y — центр в пикселях экрана. Флаги: click — можно нажать, edit — поле ввода, "
    "scroll — прокручивается, on/off — переключатель, sel — выбрано.\n"
    "ПРАВИЛА:\n"
    "1. Нажимай по номеру (tap). tap_xy — только если элемента нет в списке, но он виден на скриншоте.\n"
    "2. Текст — type_text в поле edit; submit=true, чтобы отправить поиск/форму.\n"
    "3. Нужного нет на экране — прокрути (scroll) или вернись (back). Приложение открывай open_app по названию.\n"
    "4. Смотри на новый экран после каждого действия; одно и то же больше 2 раз не повторяй — попробуй другой путь или fail.\n"
    "5. ПЕРЕД НЕОБРАТИМЫМ — отправить сообщение/письмо, оплатить, перевести деньги, купить, удалить, опубликовать, позвонить, "
    "изменить пароль/аккаунт — вызови ask_user с коротким вопросом («Отправить Алишеру "
    "«буду в семь»?»), ЕСЛИ в цели нет пометки «ПОДТВЕРЖДЕНО».\n"
    "6. Никогда не вводи пароли, PIN, коды из SMS и данные карт, не принимай соглашения за человека. Нужен вход — fail.\n"
    "7. Цель достигнута — done с итогом (если спрашивали, что на экране, — ответ по делу). Не выдумывай того, чего нет на экране.\n"
    "8. Всплывающие окна, реклама, «разрешить уведомления» — закрывай, если мешают (Close / Not now / ✕).\n"
    "Итоги и вопросы пиши по-русски."
)


def screen_parts(goal: str, step: int, screen: dict[str, Any]) -> list[dict[str, Any]]:
    text = (f"ЦЕЛЬ: {goal}\nШаг {step + 1}. Приложение: {screen.get('package') or '?'}; экран {screen.get('width')}×{screen.get('height')}.\n"
            f"ЭЛЕМЕНТЫ:\n{screen.get('tree') or '(список пуст — смотри скриншот)'}")
    parts: list[dict[str, Any]] = [{"text": text}]
    if screen.get("shot"):
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": screen["shot"]}})
    return parts


def strip_old_images(history: list[dict[str, Any]], keep: int = 1) -> list[dict[str, Any]]:
    """Скриншоты старых шагов заменяем пометкой — иначе запрос растёт на сотни КБ за шаг."""
    out: list[dict[str, Any]] = []
    seen = 0
    for msg in reversed(history):
        parts = msg.get("parts") or []
        if any(isinstance(p, dict) and "inline_data" in p for p in parts):
            seen += 1
            if seen > keep:
                parts = [p if not (isinstance(p, dict) and "inline_data" in p) else {"text": "[скриншот прошлого шага]"} for p in parts]
        out.append({**msg, "parts": parts})
    return list(reversed(out))


def describe(name: str, args: dict[str, Any]) -> str:
    return {
        "tap": "Нажимаю…", "long_press": "Нажимаю…", "tap_xy": "Нажимаю…", "type_text": f"Печатаю: {str(args.get('text') or '')[:40]}",
        "scroll": "Листаю…", "back": "Назад…", "home": "На главный экран…", "open_app": f"Открываю {args.get('name') or ''}…",
        "wait": "Жду…", "done": "Готово", "ask_user": "Нужно подтверждение", "fail": "Не получилось",
    }.get(name, "Работаю…")


_screen_model_ok = True


async def screen_step(history: list[dict[str, Any]]):
    """Один шаг «глаз и рук»: сильная flash-модель, при её недоступности — модель агента."""
    global _screen_model_ok
    kwargs = {"system": SCREEN_SYSTEM, "tools": SCREEN_TOOLS, "thinking_budget": 512, "max_tokens": 1024}
    if _screen_model_ok and SCREEN_MODEL:
        try:
            return await ai.agent_step(history, model=SCREEN_MODEL, **kwargs)
        except Exception as exc:
            _screen_model_ok = False
            logger.warning("screen model %s unavailable (%s) — using agent model", SCREEN_MODEL, str(exc)[:120])
    return await ai.agent_step(history, **kwargs)


# ------------------------------------------------------------------ сессия
class PhoneLive(_Session):
    def __init__(self, profile, persona, *, system: str, phone_ws, device: dict[str, Any]) -> None:  # noqa: ANN001
        super().__init__(profile, persona, mode="phone", system=system)
        self.phone_ws = phone_ws
        self.turn = phone.PhoneTurn(uid=self.uid, device=device)
        self.runner = phone.make_runner(self.turn)
        self._waiters: dict[int, asyncio.Future] = {}
        self._seq = 0
        self._phone_lock = asyncio.Lock()
        self._gem_lock = asyncio.Lock()
        self._tool_tasks: set[asyncio.Task] = set()
        self._control_history: list[dict[str, Any]] = []
        self._control_waiting = False
        self._control_cancel = False
        self.controlling = False
        self.user_lines: list[str] = []
        self.jarvis_lines: list[str] = []

    # --- связь с телефоном
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

    async def phone_request(self, payload: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
        self._seq += 1
        rid = self._seq
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters[rid] = fut
        await self.to_phone({**payload, "id": rid})
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return {"error": "телефон не ответил"}
        finally:
            self._waiters.pop(rid, None)

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
                if kind == "result":
                    fut = self._waiters.get(data.get("id"))
                    if fut is not None and not fut.done():
                        fut.set_result(data)
                elif kind == "text" and str(data.get("text") or "").strip():
                    await self.to_phone({"type": "user", "text": str(data["text"]), "final": True})
                    self.user_lines.append(str(data["text"]))
                    await self.say_text(gem, str(data["text"]))
                elif kind == "greet":
                    await self.say_text(gem, GREET)
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
                    logger.info("phone live: Gemini закрыл соединение (%s)", getattr(msg, "extra", ""))
                    self.stop.set()
                    return
                continue
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
                if self.controlling and any(w in t.lower() for w in STOP_WORDS):
                    self._control_cancel = True
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
            if name == "end_call":
                await self.to_phone({"type": "end"})
                result: dict[str, Any] = {"ok": True}
            elif name == "send_to_chat":
                result = await _send_to_chat(self.uid, str(args.get("text") or ""))
            elif name == "control_phone":
                result = await self.control(str(args.get("goal") or ""), bool(args.get("confirmed")))
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
            responses.append({"id": cid, "name": name, "response": _jsonable(result)})
        await self.to_gemini(ws, {"toolResponse": {"functionResponses": responses}})

    # --- управление экраном
    async def control(self, goal: str, confirmed: bool) -> dict[str, Any]:
        if not goal.strip():
            return {"error": "нужна цель"}
        continuing = confirmed and self._control_waiting and bool(self._control_history)
        history = self._control_history if continuing else []
        pending: list[dict[str, Any]] = ([{"functionResponse": {"name": "ask_user", "response": {"answer": "Да — ПОДТВЕРЖДЕНО пользователем, выполняй."}}}]
                                         if continuing else [])
        goal_text = goal + (" (ПОДТВЕРЖДЕНО пользователем)" if confirmed else "")
        self._control_waiting = False
        self._control_cancel = False
        self.controlling = True
        await self.to_phone({"type": "control", "on": True})
        started = time.monotonic()
        try:
            for step in range(MAX_CONTROL_STEPS):
                if self._control_cancel or self.stop.is_set():
                    return {"stopped": True, "note": "он сказал «стоп» — остановилась"}
                if time.monotonic() - started > CONTROL_SECONDS:
                    return {"error": "слишком долго — остановилась"}
                screen = await self.phone_request({"type": "screen"}, timeout=12)
                if screen.get("error"):
                    return {"error": str(screen["error"])}
                history = strip_old_images(history) + [{"role": "user", "parts": pending + screen_parts(goal_text, step, screen)}]
                res = await screen_step(history)
                history.append({"role": "model", "parts": res.parts or [{"text": res.text or "…"}]})
                if not res.calls:
                    return {"ok": True, "result": res.text or "готово"}
                name, args = res.calls[0]
                logger.info("screen step %s: %s %s", step + 1, name, json.dumps(args, ensure_ascii=False)[:160])
                await self.to_phone({"type": "status", "text": describe(name, args)})
                if name == "done":
                    return {"ok": True, "result": str(args.get("summary") or "готово")}
                if name == "fail":
                    return {"error": str(args.get("reason") or "не получилось")}
                if name == "ask_user":
                    self._control_history, self._control_waiting = history, True
                    return {"need_confirmation": str(args.get("question") or "Продолжить?")}
                result = await self.phone_request({"type": "ui", "op": name, "args": args}, timeout=20)
                result = {k: v for k, v in result.items() if k not in {"type", "id"}} or {"ok": True}
                pending = [{"functionResponse": {"name": name, "response": result}}]
                pending += [{"functionResponse": {"name": n, "response": {"skipped": "по одному действию за шаг"}}} for n, _ in res.calls[1:]]
            return {"error": f"не уложилась в {MAX_CONTROL_STEPS} шагов"}
        finally:
            self.controlling = False
            await self.to_phone({"type": "control", "on": False})


# ------------------------------------------------------------------ точка входа
async def run(uid: int, phone_ws, hello: dict[str, Any]) -> None:  # noqa: ANN001
    import aiohttp

    from . import agent_tools_extra as extra
    from .handlers.common import profile_by_id

    started = time.monotonic()
    profile = await profile_by_id(uid)
    persona = await services.persona(uid)
    snapshot, memory = await asyncio.gather(phone._snapshot(profile), extra.memory_prompt(uid))
    device = hello.get("device") if isinstance(hello.get("device"), dict) else {}
    system = live_call.system_instruction(profile, persona, mode="phone", snapshot=snapshot, memory=memory)
    if device.get("battery") is not None:
        system += f"\nТелефон: батарея {device.get('battery')}%{', заряжается' if device.get('charging') else ''}."
    sess = PhoneLive(profile, persona, system=system, phone_ws=phone_ws, device=device)
    async with aiohttp.ClientSession() as http:
        try:
            gem = await sess.connect(http)
        except Exception as exc:
            logger.warning("phone live: Gemini недоступен: %s", exc)
            await sess.to_phone({"type": "error", "text": "Не удалось подключиться к Gemini"})
            return
        await sess.to_phone({"type": "ready", "model": sess.result.model})
        logger.info("phone live %s: готов за %.1f с", uid, time.monotonic() - started)
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
    said = " / ".join(x for x in sess.user_lines if x)[:400]
    answered = " / ".join(x for x in sess.jarvis_lines if x)[:400]
    logger.info("phone live %s: %.0f с, действия %s, «%s» → «%s»", uid, time.monotonic() - started, sess.result.actions, said[:80], answered[:80])
    if said:
        phone._later(services.log_agent(uid, text=said, kind="phone_live", tools=",".join(sess.result.actions), reply=answered, ok=True))
        phone._later(extra.remember_exchange(uid, said, answered, when=profile.now.strftime("%d.%m %H:%M")))


# ------------------------------------------------------------------ «Да?» голосом бота — мгновенно, без ожидания Gemini
_GREETINGS = {
    "ru": ["Да?", "Слушаю.", "Да, {hon}?"],
    "uz": ["Ha?", "Eshitaman.", "Ha, {hon}?"],
    "en": ["Yes?", "I'm listening.", "Yes, {hon}?"],
}
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
    key = f"{persona.voice}_{persona.lang}_{persona.honorific}"
    cache_file = data_dir() / f"greetings_{key}.json"
    try:
        return json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    clips = []
    for t in texts:
        pcm = await ai.synthesize(t, voice=persona.voice)
        if pcm:
            clips.append({"text": t, "wav": base64.b64encode(pcm_to_wav(pcm)).decode()})
    out = {"key": key, "clips": clips}
    if clips:
        try:
            cache_file.write_text(json.dumps(out), encoding="utf-8")
        except OSError:
            logger.warning("greetings not cached", exc_info=True)
    return out


__all__ = ["run", "greetings", "phone_declarations", "PhoneLive", "SCREEN_TOOLS", "screen_parts", "strip_old_images"]

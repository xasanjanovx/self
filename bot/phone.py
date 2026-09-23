"""Голосовой Джарвис на телефоне: тот же агент, что в боте, плюс руки на телефоне.

Приложение (jarvis-android) слышит «Эй, Джарвис», записывает фразу и шлёт её сюда
(см. bot/phone_api.py). Агент получает все обычные инструменты бота (траты, задачи,
напоминания, цели, поиск…) и дополнительные — телефонные. Ответ — текст для озвучки
и список действий, которые выполнит само приложение: звонок, SMS, будильник, таймер,
открыть приложение, фонарик, громкость, музыка, ссылка, маршрут, системные кнопки.

Сообщения людям (SMS и Telegram от имени владельца) уходят только после «да»:
send_sms / telegram_send кладут их в ожидание, confirm_send отправляет.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus

from . import agent_tools as tools
from . import agent_tools_extra as extra
from . import cache
from . import names
from . import services
from . import tg_user
from . import undo
from .agent_tools import ARR, P, Tool, ToolContext, _str
from .voice import speakable

logger = logging.getLogger(__name__)

HISTORY_TTL = 2 * 3600.0
HISTORY_MAX_MESSAGES = 24
PENDING_TTL = 180.0
# инструменты бота, которым нужен экран Telegram — на телефоне их нет
EXCLUDED_BOT_TOOLS = {"hand_off", "open_screen", "expect_photo", "call_me"}


@dataclass
class PhoneTurn:
    uid: int
    device: dict[str, Any] = field(default_factory=dict)
    actions: list[dict[str, Any]] = field(default_factory=list)
    listen: bool = False           # после ответа сразу слушать (вопрос / подтверждение)
    need_contacts: bool = False    # на сервере нет контактов — приложение пусть пришлёт


# ------------------------------------------------------------------ contacts (приходят с телефона)
_contacts: dict[int, list[dict[str, Any]]] = {}


def _contacts_file(uid: int):
    return tg_user.data_dir() / f"contacts_{uid}.json"


def save_contacts(uid: int, raw: list[Any]) -> int:
    clean: list[dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("n") or item.get("name") or "").strip()
        phones = [str(p).strip() for p in (item.get("p") or item.get("phones") or []) if str(p).strip()]
        if name and phones:
            clean.append({"name": name[:80], "phones": phones[:4]})
    _contacts[uid] = clean
    try:
        _contacts_file(uid).write_text(json.dumps(clean, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.warning("contacts not persisted", exc_info=True)
    return len(clean)


def load_contacts(uid: int) -> list[dict[str, Any]]:
    if uid not in _contacts:
        try:
            _contacts[uid] = json.loads(_contacts_file(uid).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
    return _contacts[uid]


def find_contact(uid: int, who: Any, variants: Any) -> dict[str, Any]:
    queries = [who] + [v for v in (variants or []) if isinstance(v, str)]
    return names.resolve(queries, load_contacts(uid))


# ------------------------------------------------------------------ pending messages
def get_pending(uid: int) -> dict[str, Any] | None:
    value = cache.get(uid, ("phone_pending",))
    return value if isinstance(value, dict) else None


def set_pending(uid: int, pending: dict[str, Any] | None) -> None:
    cache.put(uid, ("phone_pending",), pending, PENDING_TTL if pending else 1)


def pending_question(p: dict[str, Any]) -> str:
    where = "SMS" if p["kind"] == "sms" else "в Telegram"
    return f"Отправить {where} — {p['name']}: «{p['text']}»?"


async def execute_pending(turn: PhoneTurn, p: dict[str, Any]) -> dict[str, Any]:
    set_pending(turn.uid, None)
    if p["kind"] == "sms":
        turn.actions.append({"type": "sms", "number": p["number"], "text": p["text"], "name": p["name"]})
        return {"ok": True, "sent": "sms", "to": p["name"]}
    chats = await tg_user.dialogs()
    chat = next((c for c in chats if c["id"] == p["chat_id"]), None) or {"id": p["chat_id"], "name": p["name"]}
    if not await tg_user.send(chat, p["text"]):
        return {"error": "Telegram не подключён или сессия слетела — переподключи в приложении"}
    return {"ok": True, "sent": "telegram", "to": p["name"]}


_YES = {"да", "ага", "угу", "давай", "отправь", "отправляй", "конечно", "ок", "окей", "хорошо", "верно", "точно", "можно", "go",
        "ha", "xa", "ҳа", "ха", "майли", "mayli", "yubor", "юбор", "albatta", "албатта", "bopti", "ok", "okay", "yes", "yep", "sure", "да да"}
_NO = {"нет", "не", "не надо", "не отправляй", "отмена", "отмени", "стоп", "не нужно", "yoq", "йок", "йўқ", "kerak emas", "керак эмас",
       "bekor", "бекор", "no", "nope", "cancel"}
_FILLER = {"джарвис", "jarvis", "пожалуйста", "please", "iltimos", "илтимос"}


def _short_answer(text: str) -> str:
    low = re.sub(r"[^\w\s']", " ", str(text or "").lower()).replace("'", "")
    words = [w for w in low.split() if w not in _FILLER]
    return " ".join(words)


def is_yes(text: str) -> bool:
    ans = _short_answer(text)
    if not ans or len(ans.split()) > 3:
        return False
    return ans in _YES or all(w in _YES for w in ans.split())


def is_no(text: str) -> bool:
    ans = _short_answer(text)
    if not ans or len(ans.split()) > 3:
        return False
    return ans in _NO or all(w in _NO for w in ans.split())


# ------------------------------------------------------------------ phone tools
PHONE_TOOLS: dict[str, Tool] = {}

WHO = P("STRING", "кому — как сказал пользователь, в именительном падеже («мама», «Алишер», «+998901234567»)")
VARIANTS = ARR({"type": "STRING"}, "как ещё может быть записан этот человек: другая транскрипция и родственные слова "
                                   "(мама → ойи, онам, ona, oyijon, mama; Алишер → Alisher, Alisher aka)")


def ptool(name: str, description: str, properties: dict[str, Any] | None = None, required: tuple[str, ...] = ()):
    def deco(fn):
        PHONE_TOOLS[name] = Tool(name, description, properties or {}, required, fn)
        return fn

    return deco


def _action(turn: PhoneTurn, kind: str, **fields: Any) -> dict[str, Any]:
    turn.actions.append({"type": kind, **{k: v for k, v in fields.items() if v not in (None, "")}})
    return {"ok": True, "done_on_phone": kind}


@ptool("phone_call", "Обычный звонок с телефона человеку из контактов или на номер («позвони маме», «набери Алишера», «qo'ng'iroq qil dadamga»).",
       {"who": WHO, "variants": VARIANTS}, ("who",))
async def _phone_call(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    number = names.as_phone_number(a.get("who"))
    if number:
        return _action(turn, "call", number=number, name=number)
    if not load_contacts(turn.uid):
        turn.need_contacts = True
        return {"error": "Контакты с телефона ещё не загружены — попроси повторить через пару секунд"}
    found = find_contact(turn.uid, a.get("who"), a.get("variants"))
    if "match" in found:
        c = found["match"]
        return _action(turn, "call", number=c["phones"][0], name=c["name"])
    if "candidates" in found:
        return {"candidates": [c["name"] for c in found["candidates"]], "hint": "спроси голосом, кому именно (ask_user с этими именами)"}
    return {"error": f"В контактах нет «{a.get('who')}»", "hint": "скажи, что не нашёл, и попроси назвать, как записан контакт"}


@ptool("send_sms", "SMS человеку из контактов. Сообщение НЕ уходит сразу: сначала спроси подтверждение (текст вопроса вернёт инструмент).",
       {"who": WHO, "variants": VARIANTS, "text": P("STRING", "текст SMS от первого лица владельца")}, ("who", "text"))
async def _send_sms(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    text = _str(a.get("text"))
    if not text:
        return {"error": "нет текста — спроси, что написать"}
    number = names.as_phone_number(a.get("who"))
    name = number
    if not number:
        if not load_contacts(turn.uid):
            turn.need_contacts = True
            return {"error": "Контакты с телефона ещё не загружены"}
        found = find_contact(turn.uid, a.get("who"), a.get("variants"))
        if "candidates" in found:
            return {"candidates": [c["name"] for c in found["candidates"]], "hint": "спроси, кому именно"}
        if "match" not in found:
            return {"error": f"В контактах нет «{a.get('who')}»"}
        number, name = found["match"]["phones"][0], found["match"]["name"]
    pending = {"kind": "sms", "number": number, "name": name, "text": text}
    set_pending(turn.uid, pending)
    turn.listen = True
    return {"status": "awaiting_confirmation", "ask_exactly": pending_question(pending)}


@ptool("telegram_send", "Написать человеку или в группу в Telegram от имени владельца («напиши маме в телеграм, что буду в семь», «ответь Алишеру: ок»). "
       "Сообщение НЕ уходит сразу: сначала спроси подтверждение (текст вопроса вернёт инструмент).",
       {"who": WHO, "variants": VARIANTS, "text": P("STRING", "текст сообщения от первого лица владельца, на языке, на котором он продиктовал")},
       ("who", "text"))
async def _telegram_send(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    text = _str(a.get("text"))
    if not text:
        return {"error": "нет текста — спроси, что написать"}
    if not tg_user.configured():
        return {"error": "Telegram не подключён: в приложении Джарвис → раздел Telegram → «Подключить»"}
    found = await tg_user.find_chat([a.get("who")] + [v for v in (a.get("variants") or []) if isinstance(v, str)])
    if "candidates" in found:
        return {"candidates": [c["name"] for c in found["candidates"]], "hint": "спроси, кому именно"}
    if "match" not in found:
        return {"error": f"Не нашёл чат «{a.get('who')}» среди последних переписок"}
    chat = found["match"]
    pending = {"kind": "tg", "chat_id": chat["id"], "name": chat["name"], "text": text}
    set_pending(turn.uid, pending)
    turn.listen = True
    return {"status": "awaiting_confirmation", "ask_exactly": pending_question(pending)}


@ptool("confirm_send", "Пользователь подтвердил отправку ожидающего сообщения («да», «отправь», «ha») — отправить.")
async def _confirm_send(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    pending = get_pending(turn.uid)
    if not pending:
        return {"error": "нечего отправлять — ожидающих сообщений нет"}
    return await execute_pending(turn, pending)


@ptool("cancel_send", "Пользователь передумал отправлять ожидающее сообщение.")
async def _cancel_send(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    set_pending(turn.uid, None)
    return {"ok": True, "cancelled": True}


@ptool("telegram_read", "Прочитать Telegram владельца: без who — непрочитанное во всех чатах («что мне написали?», «есть новые сообщения?»); "
       "с who — последние сообщения в переписке с человеком («что пишет Алишер?»).",
       {"who": P("STRING", "с кем переписка (необязательно)"), "variants": VARIANTS, "limit": P("INTEGER", "сколько сообщений, по умолчанию 5")})
async def _telegram_read(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    if not tg_user.configured():
        return {"error": "Telegram не подключён: в приложении Джарвис → раздел Telegram → «Подключить»"}
    who = _str(a.get("who"))
    if not who:
        chats = await tg_user.unread()
        return {"unread_chats": chats} if chats else {"unread_chats": [], "note": "непрочитанных нет"}
    found = await tg_user.find_chat([who] + [v for v in (a.get("variants") or []) if isinstance(v, str)])
    if "candidates" in found:
        return {"candidates": [c["name"] for c in found["candidates"]], "hint": "спроси, чью переписку прочитать"}
    if "match" not in found:
        return {"error": f"Не нашёл чат «{who}»"}
    limit = max(1, min(int(a.get("limit") or 5), 15))
    return {"chat": found["match"]["name"], "messages": await tg_user.recent(found["match"], limit=limit)}


_TIME_RE = re.compile(r"^(\d{1,2})[:.\s](\d{2})$")
_WEEKDAY_KEYS = {"mon": 2, "tue": 3, "wed": 4, "thu": 5, "fri": 6, "sat": 7, "sun": 1}  # java.util.Calendar


@ptool("set_alarm", "Будильник на телефоне («разбуди в 7», «будильник на 6:30 по будням»). Подъём на фаджр со звонком — это set_wake, не сюда.",
       {"time": P("STRING", "HH:MM, 24 часа"), "label": P("STRING", "подпись (необязательно)"),
        "days": ARR({"type": "STRING", "enum": list(_WEEKDAY_KEYS)}, "повтор по дням (mon…sun), пусто — один раз")},
       ("time",))
async def _set_alarm(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    m = _TIME_RE.match(_str(a.get("time")) or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return {"error": "время нужно в формате HH:MM"}
    days = [_WEEKDAY_KEYS[d] for d in (a.get("days") or []) if d in _WEEKDAY_KEYS]
    return _action(turn, "alarm", hour=int(m.group(1)), minute=int(m.group(2)), label=_str(a.get("label")) or "Джарвис", days=days or None)


@ptool("set_timer", "Таймер на телефоне («засеки 10 минут»).",
       {"seconds": P("INTEGER", "длительность в секундах"), "label": P("STRING", "подпись (необязательно)")}, ("seconds",))
async def _set_timer(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    seconds = int(a.get("seconds") or 0)
    if not 1 <= seconds <= 24 * 3600:
        return {"error": "таймер от 1 секунды до 24 часов"}
    return _action(turn, "timer", seconds=seconds, label=_str(a.get("label")) or "Джарвис")


@ptool("open_app", "Открыть приложение на телефоне («открой ютуб», «камеру», «настройки», «Click»).",
       {"name": P("STRING", "название приложения"), "variants": ARR({"type": "STRING"}, "другие написания (ютуб → YouTube)")}, ("name",))
async def _open_app(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    name = _str(a.get("name"))
    if not name:
        return {"error": "какое приложение?"}
    return _action(turn, "open_app", name=name, variants=[v for v in (a.get("variants") or []) if isinstance(v, str)][:6])


@ptool("flashlight", "Фонарик: включить / выключить.", {"on": P("BOOLEAN", "true — включить, false — выключить")}, ("on",))
async def _flashlight(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    return _action(turn, "flashlight", on=bool(a.get("on")))


@ptool("set_volume", "Громкость медиа: точный уровень в процентах или up / down / mute / unmute.",
       {"percent": P("INTEGER", "0–100 (необязательно)"), "direction": P("STRING", "up | down | mute | unmute", enum=["up", "down", "mute", "unmute"])})
async def _set_volume(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    percent = a.get("percent")
    if percent is not None:
        return _action(turn, "volume", percent=max(0, min(100, int(percent))))
    direction = _str(a.get("direction"))
    if direction not in {"up", "down", "mute", "unmute"}:
        return {"error": "нужно percent или direction"}
    return _action(turn, "volume", direction=direction)


@ptool("media", "Управление музыкой/видео: play, pause, next, previous.",
       {"command": P("STRING", "play | pause | next | previous", enum=["play", "pause", "next", "previous"])}, ("command",))
async def _media(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    command = _str(a.get("command"))
    if command not in {"play", "pause", "next", "previous"}:
        return {"error": "command: play | pause | next | previous"}
    return _action(turn, "media", command=command)


@ptool("open_link", "Открыть ссылку или поиск в браузере на телефоне (когда просит именно ОТКРЫТЬ; просто вопрос — web_search и ответ голосом).",
       {"url": P("STRING", "https://… (необязательно)"), "query": P("STRING", "что искать (если нет url)")})
async def _open_link(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    url = _str(a.get("url"))
    if not url and _str(a.get("query")):
        url = "https://www.google.com/search?q=" + quote_plus(_str(a.get("query")) or "")
    if not url or not url.startswith(("http://", "https://")):
        return {"error": "нужен url (https://…) или query"}
    return _action(turn, "url", url=url)


@ptool("navigate", "Проложить маршрут в картах («как доехать до Chorsu», «маршрут домой»).",
       {"destination": P("STRING", "куда"), "mode": P("STRING", "drive | walk | transit", enum=["drive", "walk", "transit"])}, ("destination",))
async def _navigate(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    destination = _str(a.get("destination"))
    if not destination:
        return {"error": "куда?"}
    return _action(turn, "navigate", destination=destination, mode=_str(a.get("mode")) or "drive")


_GLOBAL = ["home", "back", "recents", "lock", "screenshot", "notifications", "quick_settings", "power_dialog"]


@ptool("device_action", "Системная кнопка телефона: home (домой), back (назад), recents (недавние), lock (заблокировать экран), "
       "screenshot, notifications (шторка), quick_settings, power_dialog (меню питания).",
       {"action": P("STRING", " | ".join(_GLOBAL), enum=_GLOBAL)}, ("action",))
async def _device_action(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    action = _str(a.get("action"))
    if action not in _GLOBAL:
        return {"error": "неизвестное действие"}
    return _action(turn, "global", action=action)


@ptool("undo_last", "Откатить последнее изменение данных в боте («отмени последнее», «верни как было»).")
async def _undo_last(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    if not undo.peek(turn.uid):
        return {"error": "откатывать нечего"}
    ok = await undo.apply(turn.uid, tz_name=ctx.profile.tz_name)
    return {"ok": bool(ok)}


def declarations() -> list[dict[str, Any]]:
    bot_tools = [d for d in tools.declarations() if d["name"] not in EXCLUDED_BOT_TOOLS]
    return bot_tools + [t.declaration() for t in PHONE_TOOLS.values()]


# ------------------------------------------------------------------ quick replies
# Простое действие на телефоне («звоню», «ставлю будильник») или вопрос «отправить?» — ответ известен
# заранее, второй запрос к модели ради одной фразы не делаем: для голоса важна каждая секунда.
def ru_plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _minutes(seconds: int, uz: bool) -> str:
    if seconds % 60:
        return f"{seconds} soniya" if uz else f"{seconds} {ru_plural(seconds, 'секунду', 'секунды', 'секунд')}"
    m = seconds // 60
    if m % 60 == 0:
        h = m // 60
        return f"{h} soat" if uz else f"{h} {ru_plural(h, 'час', 'часа', 'часов')}"
    return f"{m} daqiqa" if uz else f"{m} {ru_plural(m, 'минуту', 'минуты', 'минут')}"


def action_phrase(action: dict[str, Any], lang: str = "ru") -> str:
    uz = lang == "uz"
    kind = action.get("type")
    if kind == "call":
        return f"{action.get('name')}ga qo'ng'iroq qilyapman." if uz else f"Звоню: {action.get('name')}."
    if kind == "alarm":
        hm = f"{int(action.get('hour', 0)):02d}:{int(action.get('minute', 0)):02d}"
        return f"Budilnik {hm} ga qo'yildi." if uz else f"Ставлю будильник на {hm}."
    if kind == "timer":
        return f"Taymer: {_minutes(int(action.get('seconds') or 0), True)}." if uz else f"Таймер на {_minutes(int(action.get('seconds') or 0), False)}."
    if kind == "open_app":
        return f"{action.get('name')} ochyapman." if uz else f"Открываю {action.get('name')}."
    if kind == "flashlight":
        return ("Chiroq yoqildi." if action.get("on") else "Chiroq o'chirildi.") if uz else ("Фонарик включён." if action.get("on") else "Выключаю фонарик.")
    if kind == "navigate":
        return f"{action.get('destination')} gacha yo'l." if uz else f"Строю маршрут: {action.get('destination')}."
    if kind == "media":
        ru = {"play": "Включаю.", "pause": "Пауза.", "next": "Следующий.", "previous": "Предыдущий."}
        return "Bo'ldi." if uz else ru.get(str(action.get("command")), "Готово.")
    if kind == "url":
        return "Ochyapman." if uz else "Открываю."
    return "Bo'ldi." if uz else "Готово."


def quick_reply(turn: PhoneTurn, contents: list[dict[str, Any]], lang: str) -> str | None:
    """Если последний ход модели — только телефонные действия (успешно) или одна отправка «на подтверждение»,
    вернуть готовую фразу; иначе None — пусть отвечает модель."""
    if not contents or contents[-1].get("role") != "user":
        return None
    responses = [p.get("functionResponse") for p in contents[-1].get("parts") or [] if isinstance(p, dict)]
    if not responses or not all(isinstance(r, dict) for r in responses):
        return None
    names_ = [r.get("name") for r in responses]
    results = [r.get("response") or {} for r in responses]
    if len(responses) == 1 and names_[0] in {"telegram_send", "send_sms"} and results[0].get("ask_exactly"):
        return str(results[0]["ask_exactly"])
    if len(responses) == 1 and names_[0] == "confirm_send" and results[0].get("ok"):
        return "Yuborildi." if lang == "uz" else "Отправил."
    if not all(n in QUICK_TOOLS and r.get("ok") for n, r in zip(names_, results)):
        return None
    done = turn.actions[-len(responses):]
    return " ".join(action_phrase(a, lang) for a in done) or None


QUICK_TOOLS = {"phone_call", "set_alarm", "set_timer", "open_app", "flashlight", "set_volume", "media", "open_link", "navigate", "device_action"}


def make_step(turn: PhoneTurn, lang: str, step_fn=None):
    async def step(contents: list[dict[str, Any]], **kwargs: Any):
        from .ai import AgentStep
        from .context import ai

        text = quick_reply(turn, contents, lang)
        if text:
            return AgentStep(parts=[{"text": text}], text=text, calls=[], finish="STOP")
        return await (step_fn or ai.agent_step)(contents, **kwargs)

    return step


def make_runner(turn: PhoneTurn):
    async def run(name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        t = PHONE_TOOLS.get(name)
        if t is None:
            if name in EXCLUDED_BOT_TOOLS:
                return {"error": f"{name} недоступен в голосовом режиме"}
            return await tools.run(name, args, ctx)
        ctx.calls.append(name)
        try:
            return await t.handler(turn, ctx, args or {})
        except Exception as exc:
            logger.exception("phone tool %s failed", name)
            return {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    return run


# ------------------------------------------------------------------ prompt
def system_extra(turn: PhoneTurn, pending: dict[str, Any] | None) -> str:
    d = turn.device or {}
    device = []
    if d.get("battery") is not None:
        device.append(f"батарея {d.get('battery')}%" + (", заряжается" if d.get("charging") else ""))
    if d.get("model"):
        device.append(str(d.get("model"))[:40])
    lines = [
        "\n\nРЕЖИМ ТЕЛЕФОНА. С тобой говорят ГОЛОСОМ через приложение «Джарвис» на Android-телефоне владельца; твой ответ будет ПРОИЗНЕСЁН вслух.",
        "• Ответ — 1–2 коротких разговорных предложения. Без эмодзи, списков, markdown, ссылок и id. Длинные данные — только итог "
        "(«за сентябрь 3,2 миллиона, больше всего на еду»). Это главнее правила 8.",
        "• Экранов бота здесь нет: hand_off, open_screen, call_me недоступны. Трату/доход записывай сразу add_finance_entries (категорию выбери сам), "
        "еду — add_calorie_logs со своей оценкой ккал и БЖУ.",
        "• «Позвони/набери маме» → phone_call (обычный звонок с телефона). SMS → send_sms. «Напиши/ответь … в телеграм» → telegram_send; "
        "если не сказано куда — по умолчанию Telegram. В variants всегда передавай другие написания и родственные слова.",
        "• Сообщения уходят только после подтверждения: send_sms/telegram_send вернут ask_exactly — произнеси его. "
        "Согласие («да», «отправь», «ha») → confirm_send; отказ → cancel_send; просит изменить текст — снова telegram_send/send_sms с новым текстом.",
        "• Текст сообщения — от первого лица владельца, как он продиктовал, без приписок от Джарвиса; язык — как диктовал (узбекский — латиницей).",
        "• Несколько кандидатов (candidates) → ask_user, а в вопросе назови варианты голосом («Какой маме: Ойижон или Мама Билайн?»).",
        "• «Что мне написали», «новые сообщения», «что пишет Алишер» → telegram_read и перескажи коротко: кто и о чём.",
        "• Будильник («разбуди в 7», «будильник на 6:30») → set_alarm; подъём на фаджр со звонком — set_wake, как раньше. "
        "«Засеки 10 минут» → set_timer. «Напомни через 2 часа …» → add_reminder (придёт в Telegram-бот).",
        "• «Открой …» → open_app; фонарик, громкость, музыка (пауза/дальше), «маршрут до …» → navigate; «домой», «назад», «заблокируй экран», «скриншот», «шторка» → device_action. "
        "«Отмени последнее» → undo_last.",
        "• Говори «звоню», «открываю», «готово» только если инструмент вернул ok. Если фраза — явно не тебе (обрывок, шум, разговор с кем-то) — ответь одним словом «Слушаю?».",
    ]
    if device:
        lines.append("Телефон: " + ", ".join(device) + ".")
    if pending:
        lines.append(f"ОЖИДАЕТ ПОДТВЕРЖДЕНИЯ: {pending_question(pending)} — согласие → confirm_send, отказ → cancel_send, правка → новый send.")
    return "\n".join(lines)


# ------------------------------------------------------------------ history
def load_history(uid: int) -> list[dict[str, Any]]:
    value = cache.get(uid, ("phone_history",))
    return list(value) if isinstance(value, list) else []


def save_history(uid: int, contents: list[dict[str, Any]]) -> None:
    from .handlers.agent import trim_history

    cache.put(uid, ("phone_history",), trim_history(contents, max_messages=HISTORY_MAX_MESSAGES), HISTORY_TTL)


def _append_history(uid: int, user_text: str, reply: str) -> None:
    history = load_history(uid)
    history += [{"role": "user", "parts": [{"text": user_text}]}, {"role": "model", "parts": [{"text": reply}]}]
    save_history(uid, history)


def _speech_langs(device: dict[str, Any]) -> set[str]:
    return {str(x).lower()[:2] for x in (device.get("tts_langs") or []) if x}


# ------------------------------------------------------------------ entry point
_background: set[asyncio.Task[Any]] = set()


def _later(coro) -> None:
    """Журнал и память — после ответа: пользователь ждёт голос, а не запись в БД."""
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _persona_rules(uid: int, fallback_lang: str) -> tuple[str, str]:
    try:
        from . import persona as persona_mod

        persona = await services.persona(uid)
        return persona.lang, "ХАРАКТЕР (настройки пользователя): " + persona_mod.style_rules(persona)
    except Exception:
        logger.debug("persona rules failed", exc_info=True)
        return fallback_lang, ""


async def _snapshot(profile: Any) -> str:
    try:
        return await tools.snapshot(profile)
    except Exception:
        logger.exception("phone snapshot failed")
        return "(данные временно недоступны)"


async def prefetch(uid: int) -> None:
    """Телефон услышал «Джарвис» — пока человек договаривает, прогреваем кэши профиля и данных для промпта."""
    from .handlers.common import profile_by_id

    try:
        profile = await profile_by_id(uid)
        await asyncio.gather(_snapshot(profile), extra.memory_prompt(uid), _persona_rules(uid, profile.lang))
    except Exception:
        logger.debug("phone prefetch failed", exc_info=True)


async def handle(uid: int, text: str, device: dict[str, Any] | None = None, *, step_fn=None) -> dict[str, Any]:
    """Одна реплика с телефона → {"say", "actions", "listen", "need_contacts"}."""
    from .handlers.agent import run_agent
    from .handlers.common import profile_by_id

    started = time.monotonic()
    turn = PhoneTurn(uid=uid, device=device or {})
    pending = get_pending(uid)
    if pending and is_yes(text):
        result = await execute_pending(turn, pending)
        say = f"Отправил, {pending['name']}." if result.get("ok") and pending["kind"] == "tg" else (
            "Отправляю." if result.get("ok") else result.get("error", "Не получилось."))
        _append_history(uid, text, say)
        _later(services.log_agent(uid, text=text, kind="phone", tools="confirm_send", reply=say, ok=bool(result.get("ok"))))
        return {"say": say, "actions": turn.actions, "listen": False}
    if pending and is_no(text):
        set_pending(uid, None)
        _append_history(uid, text, "Не отправляю.")
        return {"say": "Хорошо, не отправляю.", "actions": [], "listen": False}

    profile = await profile_by_id(uid)
    snapshot, memory, (reply_lang, rules) = await asyncio.gather(_snapshot(profile), extra.memory_prompt(uid), _persona_rules(uid, profile.lang))
    if rules:
        memory = f"{memory}\n\n{rules}" if memory else rules
    langs = _speech_langs(turn.device)
    if langs and reply_lang not in langs:
        reply_lang = "ru" if "ru" in langs else next(iter(langs))  # голос телефона не умеет этот язык

    undo.begin_turn(uid)
    try:
        result = await run_agent(profile, text, load_history(uid), snapshot=snapshot, memory=memory, reply_lang=reply_lang,
                                 step_fn=make_step(turn, reply_lang, step_fn), run_tool=make_runner(turn), decls=declarations(),
                                 system_extra=system_extra(turn, pending))
    finally:
        mutated = undo.end_turn(uid)
    save_history(uid, result.contents)
    ctx = result.ctx
    say = speakable(result.text)
    if ctx.ask:
        options = [o for o in (ctx.ask.get("options") or []) if o]
        if options and not any(o.lower() in say.lower() for o in options):
            say = f"{say} {' или '.join(options)}?"
        turn.listen = True
    logger.info("phone: %.1fs tools=%s mutated=%s actions=%s listen=%s", time.monotonic() - started, ctx.calls, mutated,
                [a["type"] for a in turn.actions], turn.listen)
    _later(services.log_agent(uid, text=text, kind="phone", tools=",".join(ctx.calls), reply=say, ok=bool(ctx.calls or say)))
    _later(extra.remember_exchange(uid, text, say, when=profile.now.strftime("%d.%m %H:%M")))
    return {"say": say, "actions": turn.actions, "listen": turn.listen, "need_contacts": turn.need_contacts}


__all__ = ["handle", "save_contacts", "load_contacts", "declarations", "is_yes", "is_no", "PHONE_TOOLS", "PhoneTurn"]

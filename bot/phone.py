"""Голосовой JES на телефоне: тот же агент, что в боте, плюс руки на телефоне.

Приложение (jarvis-android) слышит «Эй, JES», записывает фразу и шлёт её сюда
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
from .agent_tools import ARR, P, Tool, ToolContext, _bool, _str
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
            entry: dict[str, Any] = {"name": name[:80], "phones": phones[:4]}
            try:
                calls = int(item.get("c") or item.get("calls") or 0)
            except (TypeError, ValueError):
                calls = 0
            if calls > 0:
                entry["calls"] = calls  # звонков с ним за 90 дней — кто «свой» (приложение 2.1+)
            clean.append(entry)
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


def find_contact(uid: int, who: Any, variants: Any, device: dict[str, Any] | None = None) -> dict[str, Any]:
    """Выученное имя («брат» → SIROJBEK AKAM) — сразу; иначе похожесть с поправкой на то, кому он чаще звонит.
    Двое почти одинаковых и оба редкие — {"ask": [первый, второй]}: переспросить коротко (его выбор 26.09)."""
    queries = [who] + [v for v in (variants or []) if isinstance(v, str)]
    contacts = load_contacts(uid)
    found = names.pick(queries, contacts, boosts=call_boosts(contacts, device or {}), alias=alias_for(uid, "phone", queries))
    if found.get("ambiguous") and not names.as_phone_number(who):
        return {"ask": [found["match"]["name"], found["ambiguous"]["name"]]}
    return found


def ask_which(found: dict[str, Any]) -> dict[str, Any]:
    first, second = found["ask"][:2]
    return {"ask_exactly": f"{first} или {second}?",
            "hint": "спроси коротко ровно это и жди ответа; потом вызови инструмент снова с точным именем, которое он выбрал"}


# ------------------------------------------------------------------ как он называет людей («мама» → ONAJONIM)
def _aliases_file(uid: int):
    return tg_user.data_dir() / f"aliases_{uid}.json"


def aliases(uid: int) -> dict[str, dict[str, str]]:
    try:
        data = json.loads(_aliases_file(uid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {"phone": dict(data.get("phone") or {}), "tg": dict(data.get("tg") or {})}


def alias_for(uid: int, kind: str, queries: list[Any]) -> str | None:
    known = aliases(uid).get(kind) or {}
    for q in queries:
        hit = known.get(names.kin_root(q) or names.norm(q))
        if hit:
            return hit
    return None


def learn_alias(uid: int, kind: str, who: Any, name: str) -> None:
    """Запомнить, кого он имел в виду, — в следующий раз сразу этот человек («брату», «akamga» → по корню «brat»)."""
    key = names.kin_root(who) or names.norm(who)
    if not key or not name or names.as_phone_number(who) or key == names.norm(name):
        return
    data = aliases(uid)
    if data[kind].get(key) == name:
        return
    data[kind][key] = name
    try:
        _aliases_file(uid).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.warning("aliases not saved", exc_info=True)


def _digits(number: Any) -> str:
    return re.sub(r"\D", "", str(number or ""))[-9:]


def call_boosts(contacts: list[dict[str, Any]], device: dict[str, Any]) -> dict[str, float]:
    """Кому он звонит чаще (журнал с телефона) — тот и «мама», если похожих несколько."""
    counts: dict[str, int] = {}
    for c in device.get("calls") or []:
        d = _digits(c.get("number")) if isinstance(c, dict) else ""
        if d:
            counts[d] = counts.get(d, 0) + 1
    out: dict[str, float] = {}
    for contact in contacts:
        recent = max((counts.get(_digits(p), 0) for p in contact.get("phones") or []), default=0)
        # за 90 дней (приложение 2.1+) + последние звонки из журнала
        n = int(contact.get("calls") or 0) + recent
        if n:
            key = names.norm(contact.get("name"))
            out[key] = max(out.get(key, 0.0), names.frequency_boost(n))
    return out


def people_line(uid: int, limit: int = 12) -> str:
    """Для промпта телефона: кто у него кто («брат — SIROJBEK AKAM») и кому он чаще звонит — чтобы модель узнавала
    имена в плохо расслышанной речи («Сарочубек акам» → SIROJBEK AKAM)."""
    known = aliases(uid).get("phone") or {}
    kin = [f"{k} — {v}" for k, v in known.items() if names.kin_root(k) == k]
    contacts = sorted((c for c in load_contacts(uid) if int(c.get("calls") or 0) > 0), key=lambda c: -int(c.get("calls") or 0))
    frequent: list[str] = []
    for c in contacts:
        if c["name"] not in frequent:
            frequent.append(c["name"])
        if len(frequent) >= limit:
            break
    parts = []
    if kin:
        parts.append("Родные: " + "; ".join(kin) + ".")
    if frequent:
        parts.append("Чаще всего звонит: " + ", ".join(frequent) + ".")
    return ("ЛЮДИ (имена — как в его контактах): " + " ".join(parts)) if parts else ""


def people_vocabulary(uid: int, limit: int = 20) -> list[str]:
    """Имена родных и частых — подсказка распознаванию речи Live (customVocabulary)."""
    known = list((aliases(uid).get("phone") or {}).values())
    contacts = sorted((c for c in load_contacts(uid) if int(c.get("calls") or 0) > 0), key=lambda c: -int(c.get("calls") or 0))
    out: list[str] = []
    for name in known + [c["name"] for c in contacts]:
        if name not in out:
            out.append(name)
    return out[:limit]


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
        learn_alias(turn.uid, "phone", p.get("who"), p["name"])
        return {"ok": True, "sent": "sms", "to": p["name"]}
    chats = await tg_user.dialogs()
    chat = next((c for c in chats if c["id"] == p["chat_id"]), None) or {"id": p["chat_id"], "name": p["name"]}
    if not await tg_user.send(chat, p["text"]):
        return {"error": "Telegram не подключён или сессия слетела — переподключи в приложении"}
    learn_alias(turn.uid, "tg", p.get("who"), p["name"])
    return {"ok": True, "sent": "telegram", "to": p["name"]}


_YES = {"да", "ага", "угу", "давай", "отправь", "отправляй", "конечно", "ок", "окей", "хорошо", "верно", "точно", "можно", "go",
        "ha", "xa", "ҳа", "ха", "майли", "mayli", "yubor", "юбор", "albatta", "албатта", "bopti", "ok", "okay", "yes", "yep", "sure", "да да"}
_NO = {"нет", "не", "не надо", "не отправляй", "отмена", "отмени", "стоп", "не нужно", "yoq", "йок", "йўқ", "kerak emas", "керак эмас",
       "bekor", "бекор", "no", "nope", "cancel"}
_FILLER = {"джес", "джесс", "jes", "jess", "пожалуйста", "please", "iltimos", "илтимос"}


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
    found = find_contact(turn.uid, a.get("who"), a.get("variants"), turn.device)
    if "ask" in found:
        return ask_which(found)
    if "match" in found:
        c = found["match"]
        if found.get("clear"):  # 26.09: запоминал и ошибочный выбор («брат» → случайный «… Aka») — теперь только уверенный
            learn_alias(turn.uid, "phone", a.get("who"), c["name"])
        return _action(turn, "call", number=c["phones"][0], name=c["name"])
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
        found = find_contact(turn.uid, a.get("who"), a.get("variants"), turn.device)
        if "ask" in found:
            return ask_which(found)
        if "match" not in found:
            return {"error": f"В контактах нет «{a.get('who')}»"}
        number, name = found["match"]["phones"][0], found["match"]["name"]
    pending = {"kind": "sms", "number": number, "name": name, "text": text, "who": a.get("who")}
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
        return {"error": "Telegram не подключён: в приложении JES → раздел Telegram → «Подключить»"}
    queries = [a.get("who")] + [v for v in (a.get("variants") or []) if isinstance(v, str)]
    found = await tg_user.find_chat(queries, alias=alias_for(turn.uid, "tg", queries))
    if "match" not in found:
        return {"error": f"Не нашёл чат «{a.get('who')}» среди последних переписок"}
    chat = found["match"]
    pending = {"kind": "tg", "chat_id": chat["id"], "name": chat["name"], "text": text, "who": a.get("who")}
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
        return {"error": "Telegram не подключён: в приложении JES → раздел Telegram → «Подключить»"}
    who = _str(a.get("who"))
    if not who:
        chats = await tg_user.unread()
        return {"unread_chats": chats} if chats else {"unread_chats": [], "note": "непрочитанных нет"}
    queries = [who] + [v for v in (a.get("variants") or []) if isinstance(v, str)]
    found = await tg_user.find_chat(queries, alias=alias_for(turn.uid, "tg", queries))
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
    return _action(turn, "alarm", hour=int(m.group(1)), minute=int(m.group(2)), label=_str(a.get("label")) or "JES", days=days or None)


@ptool("set_timer", "Таймер на телефоне («засеки 10 минут»).",
       {"seconds": P("INTEGER", "длительность в секундах"), "label": P("STRING", "подпись (необязательно)")}, ("seconds",))
async def _set_timer(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    seconds = int(a.get("seconds") or 0)
    if not 1 <= seconds <= 24 * 3600:
        return {"error": "таймер от 1 секунды до 24 часов"}
    return _action(turn, "timer", seconds=seconds, label=_str(a.get("label")) or "JES")


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


# ------------------------------------------------------------------ звонки: журнал, перезвон, переадресация
_CALL_KINDS = {"in": "входящий", "out": "исходящий", "missed": "пропущенный", "rejected": "отклонённый"}


def recent_calls(device: dict[str, Any]) -> list[dict[str, Any]]:
    """Журнал звонков, который телефон прислал в hello/device (последние ~10, свежие первыми)."""
    out = []
    for c in device.get("calls") or []:
        if isinstance(c, dict) and (c.get("number") or c.get("name")):
            out.append({"name": str(c.get("name") or "")[:60], "number": str(c.get("number") or "")[:30],
                        "kind": _CALL_KINDS.get(str(c.get("type")), str(c.get("type") or "")), "when": str(c.get("when") or "")[:20],
                        "seconds": int(c.get("duration") or 0)})
    return out


@ptool("recent_calls", "Кто звонил / кому звонил он: журнал последних звонков телефона («кто мне звонил?», «пропущенные есть?»).")
async def _recent_calls(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    calls = recent_calls(turn.device)
    if not calls:
        if turn.device.get("calls_denied"):
            return {"error": "нет доступа к журналу звонков", "hint": "попроси в приложении JES выдать «Журнал звонков»"}
        return {"calls": [], "note": "журнал пуст"}
    return {"calls": calls}


@ptool("call_back", "Перезвонить: последнему звонившему, последнему пропущенному или последнему, с кем говорил («перезвони», «набери, кто сейчас звонил»).",
       {"which": P("STRING", "last_incoming | last_missed | last_any", enum=["last_incoming", "last_missed", "last_any"])})
async def _call_back(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    which = _str(a.get("which")) or "last_incoming"
    want = {"last_missed": {"пропущенный"}, "last_incoming": {"входящий", "пропущенный", "отклонённый"}}.get(which)
    for c in recent_calls(turn.device):
        if c["number"] and (want is None or c["kind"] in want):
            return _action(turn, "call", number=c["number"], name=c["name"] or c["number"])
    return {"error": "в журнале нет подходящего звонка"}


_FORWARD = {"always": "21", "busy": "67", "no_answer": "61", "unreachable": "62"}


@ptool("call_forwarding", "Переадресация обычных звонков SIM на другой номер (USSD-код оператора) или её отключение. "
       "Перед включением ОДНОЙ фразой переспроси номер и условие, если он их не назвал явно.",
       {"to": WHO, "variants": VARIANTS,
        "when": P("STRING", "always (всегда) | busy (занято) | no_answer (не ответил) | unreachable (недоступен) | off (отключить всё)",
                  enum=["always", "busy", "no_answer", "unreachable", "off"])}, ("when",))
async def _call_forwarding(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    when = _str(a.get("when")) or "always"
    if when == "off":
        return _action(turn, "ussd", code="##002#", label="переадресация отключена")
    number = names.as_phone_number(a.get("to"))
    name = number
    if not number:
        if not load_contacts(turn.uid):
            turn.need_contacts = True
            return {"error": "Контакты с телефона ещё не загружены"}
        found = find_contact(turn.uid, a.get("to"), a.get("variants"), turn.device)
        if "match" not in found:
            return {"error": f"В контактах нет «{a.get('to')}»"}
        number, name = found["match"]["phones"][0], found["match"]["name"]
    code = _FORWARD.get(when, "21")
    return _action(turn, "ussd", code=f"**{code}*{number}#", label=f"переадресация на {name}")


# ------------------------------------------------------------------ система
@ptool("phone_status", "Состояние телефона: заряд батареи, зарядка, громкость, режим звонка, «не беспокоить», Wi-Fi, Bluetooth, версия приложения.")
async def _phone_status(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    d = turn.device or {}
    keys = ("battery", "charging", "volume", "ringer", "dnd", "wifi", "bluetooth", "brightness", "model", "app_version", "locked")
    return {k: d.get(k) for k in keys if d.get(k) is not None} or {"error": "телефон не прислал состояние"}


@ptool("brightness", "Яркость экрана: уровень в процентах, ярче/темнее или авто.",
       {"percent": P("INTEGER", "0–100 (необязательно)"), "direction": P("STRING", "up | down | auto", enum=["up", "down", "auto"])})
async def _brightness(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    if a.get("percent") is not None:
        return _action(turn, "brightness", percent=max(0, min(100, int(a.get("percent")))))
    direction = _str(a.get("direction"))
    if direction not in {"up", "down", "auto"}:
        return {"error": "нужно percent или direction"}
    return _action(turn, "brightness", direction=direction)


@ptool("do_not_disturb", "Режим «Не беспокоить»: включить / выключить.", {"on": P("BOOLEAN", "true — включить")}, ("on",))
async def _dnd(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    return _action(turn, "dnd", on=bool(a.get("on")))


@ptool("ringer_mode", "Режим звонка телефона: normal (со звуком), vibrate (вибрация), silent (без звука).",
       {"mode": P("STRING", "normal | vibrate | silent", enum=["normal", "vibrate", "silent"])}, ("mode",))
async def _ringer(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    mode = _str(a.get("mode"))
    if mode not in {"normal", "vibrate", "silent"}:
        return {"error": "mode: normal | vibrate | silent"}
    return _action(turn, "ringer", mode=mode)


_PANELS = ["wifi", "bluetooth", "internet", "volume", "nfc", "hotspot", "airplane", "battery", "display", "location", "sound",
           "notifications", "apps", "app_info", "storage", "security", "language", "date", "accessibility", "developer", "about", "settings"]


@ptool("settings_panel", "Открыть нужный раздел настроек телефона: wifi, bluetooth, internet (моб. данные), volume, nfc, hotspot, airplane, "
       "battery, display, location, sound, notifications, apps (все приложения), app_info (страница одного приложения — app), storage (память), "
       "security, language, date, accessibility, developer, about (о телефоне), settings. Wi-Fi/Bluetooth Android не даёт включать самим — "
       "открываем, он нажимает один раз.",
       {"panel": P("STRING", " | ".join(_PANELS), enum=_PANELS), "app": P("STRING", "для app_info: название приложения")}, ("panel",))
async def _panel(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    panel = _str(a.get("panel"))
    if panel not in _PANELS:
        return {"error": "неизвестная панель"}
    return _action(turn, "panel", panel=panel, app=_str(a.get("app")))


# ------------------------------------------------------------------ WhatsApp, экран, галерея
@ptool("whatsapp_send", "Написать человеку в WhatsApp: откроется его чат с уже набранным текстом — «Отправить» он нажмёт сам "
       "(Android не даёт приложениям отправлять за него). Скажи: «Открыла WhatsApp, текст набран — нажмите отправить».",
       {"who": WHO, "variants": VARIANTS, "text": P("STRING", "текст сообщения от первого лица владельца")}, ("who", "text"))
async def _whatsapp(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    text = _str(a.get("text"))
    if not text:
        return {"error": "нет текста — спроси, что написать"}
    number = names.as_phone_number(a.get("who"))
    name = number
    if not number:
        if not load_contacts(turn.uid):
            turn.need_contacts = True
            return {"error": "Контакты с телефона ещё не загружены"}
        found = find_contact(turn.uid, a.get("who"), a.get("variants"), turn.device)
        if "ask" in found:
            return ask_which(found)
        if "match" not in found:
            return {"error": f"В контактах нет «{a.get('who')}»"}
        number, name = found["match"]["phones"][0], found["match"]["name"]
        if found.get("clear"):
            learn_alias(turn.uid, "phone", a.get("who"), name)
    return _action(turn, "whatsapp", number=number, text=text, name=name)


@ptool("screen_look", "Посмотреть на экран телефона вместе с ним (как в Gemini): «посмотри на экран», «что тут написано», «переведи это», "
       "«объясни, что на экране». Первый раз Android спросит разрешение (он нажмёт «Начать»), дальше, пока экран включён, — сразу. "
       "Кадры экрана придут тебе в разговор; "
       "нажимать внутри приложений ты не можешь. Выключить — on=false.",
       {"on": P("BOOLEAN", "true — начать (по умолчанию), false — перестать смотреть")})
async def _screen_look(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    on = _bool(a.get("on"))
    res = _action(turn, "screen", on=on is not False)
    if on is not False:
        res["note"] = "кадр экрана — в разговоре (если разрешения ещё нет — телефон спросит его)"
    return res


@ptool("gallery", "Галерея: show_last — посмотреть последние фото (придут тебе в разговор, опиши их); delete_last — удалить последние фото "
       "(в «Недавно удалённые», можно вернуть 30 дней). Перед удалением одной фразой скажи, что удаляешь.",
       {"op": P("STRING", "show_last | delete_last", enum=["show_last", "delete_last"]), "count": P("INTEGER", "сколько последних фото, 1–10")}, ("op",))
async def _gallery(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    op = _str(a.get("op"))
    if op not in {"show_last", "delete_last"}:
        return {"error": "op: show_last | delete_last"}
    count = max(1, min(10, int(a.get("count") or 1)))
    return _action(turn, "gallery", op=op, count=count)


# ------------------------------------------------------------------ музыка, YouTube, поиск в Telegram, такси
@ptool("youtube_search", "Найти видео на YouTube («найди на ютубе…», «есть видео про…»): вернёт названия, каналы, длительность и id. "
       "Открыть/включить найденное — play_media с video_id.", {"query": P("STRING", "что искать")}, ("query",))
async def _youtube_search(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    from . import media

    query = _str(a.get("query"))
    if not query:
        return {"error": "что искать?"}
    found = await media.youtube_search(query, 5)
    return {"videos": found} if found else {"error": "YouTube не ответил — попробуй другие слова"}


@ptool("play_media", "Включить музыку или видео: «поставь Шахзоду», «включи нашиды», «включи видео про…». Музыка — в YouTube Music, "
       "видео — в YouTube. video_id — если уже нашла через youtube_search.",
       {"query": P("STRING", "что включить: песня / исполнитель / тема"), "kind": P("STRING", "music | video", enum=["music", "video"]),
        "video_id": P("STRING", "id видео с YouTube (необязательно)")}, ("query",))
async def _play_media(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    from . import media

    query = _str(a.get("query")) or ""
    kind = _str(a.get("kind")) or "music"
    vid, title = _str(a.get("video_id")), None
    if not vid:
        found = await media.youtube_search(query + (" music" if kind == "music" else ""), 1)
        if found:
            vid, title = found[0]["id"], found[0]["title"]
    res = _action(turn, "play", query=query, kind=kind, video_id=vid, title=title)
    if title:
        res["title"] = title
    return res


@ptool("telegram_search", "Найти в его Telegram по словам — во всех чатах, группах и каналах («найди в телеграме, где писали про квартиру»).",
       {"query": P("STRING", "слова для поиска"), "limit": P("INTEGER", "сколько сообщений, по умолчанию 6")}, ("query",))
async def _telegram_search(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    if not tg_user.configured():
        return {"error": "Telegram не подключён: в приложении JES → раздел Telegram → «Подключить»"}
    query = _str(a.get("query"))
    if not query:
        return {"error": "что искать?"}
    found = await tg_user.search(query, max(1, min(15, int(a.get("limit") or 6))))
    return {"messages": found} if found else {"messages": [], "note": "ничего не нашлось"}


_VAGUE_PLACE = re.compile(r"^\s*(на |в |до |к )?(эт[уоа]\w*|ту|сюда|туда|здесь|там|текущ\w*|мою|моё|мое|shu|bu|u)?\s*"
                          r"(геолокац\w*|локац\w*|мест\w*|точк\w*|адрес\w*|location|joy\w*)?\s*$", re.IGNORECASE)


@ptool("taxi", "Такси через Яндекс Go: «вызови такси до Чорсу», «сколько до вокзала на такси». Откроется Яндекс Go с готовым маршрутом "
       "от того места, где он сейчас, — цену и время подачи видно сразу, «Заказать» он нажимает сам.",
       {"to": P("STRING", "куда ехать: адрес или место"), "tariff": P("STRING", "econom | comfort | business", enum=["econom", "comfort", "business"])},
       ("to",))
async def _taxi(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    from . import media

    to = _str(a.get("to"))
    if not to or _VAGUE_PLACE.search(to):
        # «на эту геолокацию», «сюда» — адреса нет: пусть спросит, а не открывает пустой маршрут (25.09 — дважды)
        return {"error": "куда ехать? Нужен адрес или место словами (улица, район, заведение)"}
    loc = turn.device.get("location") if isinstance(turn.device.get("location"), dict) else None
    near = (float(loc["lat"]), float(loc["lon"])) if loc and loc.get("lat") is not None else None
    place = await media.geocode(to, near)
    if not place:
        res = _action(turn, "taxi", to_name=to)
        res["note"] = "адрес не нашла на карте — Яндекс Go откроется, куда ехать он введёт сам"
        return res
    return _action(turn, "taxi", lat=place["lat"], lon=place["lon"], to_name=place["name"], tariff=_str(a.get("tariff")) or "econom")


@ptool("remember_contact", "Запомнить, кого он так называет: «запомни, брат — это Aziz», «мама — это ONAJONIM», «работа — Mashhur bek aka». "
       "Дальше звонки и сообщения по этому слову пойдут сразу этому человеку.",
       {"who": P("STRING", "как он называет («брат», «работа»)"), "contact": P("STRING", "как записан в контактах или Telegram")}, ("who", "contact"))
async def _remember_contact(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    who, contact = _str(a.get("who")), _str(a.get("contact"))
    if not who or not contact:
        return {"error": "нужно: как называет и кто это"}
    contacts = load_contacts(turn.uid)
    found = names.pick([contact], contacts, boosts=call_boosts(contacts, turn.device))
    if found.get("ambiguous"):
        return ask_which({"ask": [found["match"]["name"], found["ambiguous"]["name"]]})
    name = found["match"]["name"] if "match" in found else contact
    learn_alias(turn.uid, "phone", who, name)
    learn_alias(turn.uid, "tg", who, contact)
    return {"ok": True, "who": who, "contact": name}


# ------------------------------------------------------------------ камера
@ptool("look", "Включить камеру и смотреть вместе с ним, как Gemini Live: «посмотри», «что это?», «что у меня в руках», "
       "«прочитай/переведи, что тут написано», «отсканируй документ». Кадры придут тебе в разговор — описывай, что видишь, "
       "отвечай на вопросы. Документ — перепиши текст и, если просит, send_to_chat. Выключить — look с on=false.",
       {"on": P("BOOLEAN", "true — включить (по умолчанию), false — выключить"),
        "camera": P("STRING", "back (основная) | front (селфи)", enum=["back", "front"])})
async def _look(turn: PhoneTurn, ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    on = _bool(a.get("on"))
    if on is False:
        return _action(turn, "camera", on=False)
    res = _action(turn, "camera", on=True, facing=_str(a.get("camera")) or "back")
    res["note"] = "камера включена — кадр в разговоре"
    return res


def device_prompt(device: dict[str, Any]) -> str:
    """Строки о телефоне для системного промпта живого разговора."""
    d = device or {}
    parts = []
    if d.get("battery") is not None:
        parts.append(f"батарея {d.get('battery')}%" + (", заряжается" if d.get("charging") else ""))
    if d.get("app_version"):
        parts.append(f"приложение JES v{d.get('app_version')}")
    if d.get("model"):
        parts.append(str(d.get("model"))[:40])
    missed = [c for c in recent_calls(d) if c["kind"] == "пропущенный"][:3]
    line = ("\nТелефон: " + ", ".join(parts) + ".") if parts else ""
    if missed:
        line += " Пропущенные: " + "; ".join(f"{c['name'] or c['number']} ({c['when']})" for c in missed) + "."
    return line


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
        "\n\nРЕЖИМ ТЕЛЕФОНА. С тобой говорят ГОЛОСОМ через приложение «JES» на Android-телефоне владельца; твой ответ будет ПРОИЗНЕСЁН вслух.",
        "• Ответ — 1–2 коротких разговорных предложения. Без эмодзи, списков, markdown, ссылок и id. Длинные данные — только итог "
        "(«за сентябрь 3,2 миллиона, больше всего на еду»). Это главнее правила 8.",
        "• Экранов бота здесь нет: hand_off, open_screen, call_me недоступны. Трату/доход записывай сразу add_finance_entries (категорию выбери сам), "
        "еду — add_calorie_logs со своей оценкой ккал и БЖУ.",
        "• «Позвони/набери маме» → phone_call (обычный звонок с телефона). SMS → send_sms. «Напиши/ответь … в телеграм» → telegram_send; "
        "если не сказано куда — по умолчанию Telegram. В variants всегда передавай другие написания и родственные слова.",
        "• Сообщения уходят только после подтверждения: send_sms/telegram_send вернут ask_exactly — произнеси его. "
        "Согласие («да», «отправь», «ha») → confirm_send; отказ → cancel_send; просит изменить текст — снова telegram_send/send_sms с новым текстом.",
        "• Текст сообщения — от первого лица владельца, как он продиктовал, без приписок от JES; язык — как диктовал (узбекский — латиницей).",
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
    """Телефон услышал «JES» — пока человек договаривает, прогреваем кэши профиля и данных для промпта."""
    from .handlers.common import profile_by_id

    try:
        profile = await profile_by_id(uid)
        await asyncio.gather(_snapshot(profile), extra.memory_prompt(uid), _persona_rules(uid, profile.lang))
    except Exception:
        logger.debug("phone prefetch failed", exc_info=True)


# данные для промпта Джарвиса (services.*): холодная сборка — до 5 с (десяток запросов в Supabase)
WARM_KEYS = {"fin_entries", "fin_settings", "budgets", "recurring", "reminders", "notes", "tasks", "goals", "debt_deadlines",
             "weights", "checkins", "persona", "memory", "user_settings", "kcal_today", "kcal_days", "nutri_profile", "profile"}
WARM_EVERY = 120.0


async def keep_warm(uid: int) -> None:
    """Держим данные владельца свежими в памяти: «JES» → разговор готов за ~0.4 с (подключение к Gemini),
    а не 1.5–5 с. Раз в 2 минуты обновляем то, что истечёт до следующего раза; изменения данных
    сбрасывают кэш как раньше, так что устаревшего JES не видит."""
    while True:
        try:
            cache.drop_expiring(uid, WARM_EVERY + 15, WARM_KEYS)
            await prefetch(uid)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("keep warm failed", exc_info=True)
        await asyncio.sleep(WARM_EVERY)


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

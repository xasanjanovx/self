"""Будильник в приложении JES — НЕОБЯЗАТЕЛЬНЫЙ дополнительный слой к звонку Telegram.

04.10.2026 его слова: «он должен будить по звонку Telegram ВСЕГДА». Основной подъём — звонок Telegram от JES в назначенное время
(bot/wake_runner.py): он не ждёт приложение и не отменяется тапом в приложении. Будильник Android в приложении можно ВКЛЮЧИТЬ
дополнительно (кнопка «📱 Будильник в приложении» в боте, по умолчанию выключен): он звенит мелодией и голосом JES в то же
время, что и звонок (работает без интернета и в «Не беспокоить»), а когда он берёт трубку Telegram, приложение замолкает само.
Тап «Проснулся» в приложении подъём НЕ засчитывает (04.10 он нажал его через 8 секунд, сервер решил «встал» и Telegram не позвонил) —
засчитывает только голос в звонке (или JES на телефоне, когда он ответил ей голосом).
Состояние — DATA_DIR/app_alarm.json: {uid: {"day", "at_ms", "set_at", "rang_day", "rang_at"}, "prefs": {uid: {"app": bool}},
"guard": {uid: день вечерней проверки}}.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# APP_ALARM=0 в .env сервера — будильник приложения нельзя включить вовсе
ENABLED = (os.getenv("APP_ALARM") or "1").strip() != "0"
CALL_ANSWERED_TTL = 600.0   # столько секунд после «взял трубку» приложение молчит (если звонок оборвался — Telegram перезвонит сам)
_call_answered: dict[int, float] = {}


def _file():
    from .tg_user import data_dir

    return data_dir() / "app_alarm.json"


def _load() -> dict[str, Any]:
    try:
        return json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(data: dict[str, Any]) -> None:
    try:
        _file().write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        logger.warning("app alarm: не сохранил", exc_info=True)


def app_enabled(uid: int) -> bool:
    """Включил ли он дополнительный будильник в приложении (по умолчанию — нет: будит звонок Telegram)."""
    return ENABLED and bool(((_load().get("prefs") or {}).get(str(uid)) or {}).get("app", False))


def set_app(uid: int, on: bool) -> None:
    data = _load()
    prefs = data.get("prefs") or {}
    prefs[str(uid)] = {"app": bool(on)}
    data["prefs"] = prefs
    _save(data)


def call_answered(uid: int) -> None:
    """Он взял трубку звонка-будильника Telegram — звонящий будильник в приложении может замолчать."""
    _call_answered[int(uid)] = time.monotonic()


def in_call(uid: int) -> bool:
    at = _call_answered.get(int(uid))
    return at is not None and time.monotonic() - at < CALL_ANSWERED_TTL


def scheduled(uid: int, day_iso: str, at_ms: int) -> None:
    """Приложение поставило будильник Android на этот день и время."""
    data = _load()
    before = data.get(str(uid)) or {}
    fresh: dict[str, Any] = {"day": day_iso, "at_ms": int(at_ms), "set_at": datetime.now(timezone.utc).isoformat()}
    if before.get("day") == day_iso and before.get("rang_day") == day_iso:
        fresh.update({"rang_day": before["rang_day"], "rang_at": before.get("rang_at")})  # переустановка не стирает «уже звонил»
    data[str(uid)] = fresh
    _save(data)


def rang(uid: int, day_iso: str) -> None:
    """Приложение сообщило: будильник на телефоне зазвонил (повторно — после «ещё 5 минут»)."""
    data = _load()
    st = data.get(str(uid)) or {}
    st.update({"rang_day": day_iso, "rang_at": datetime.now(timezone.utc).isoformat()})
    data[str(uid)] = st
    _save(data)


def tomorrow_ready(uid: int, day_iso: str) -> bool:
    """В телефоне стоит будильник на этот день (приложение прислало «scheduled»)."""
    return (_load().get(str(uid)) or {}).get("day") == day_iso


def _uses_app(uid: int) -> bool:
    """Приложение с будильником у человека было (прислало «scheduled» за последние 14 дней) — клиентам без него не пишем."""
    try:
        set_at = datetime.fromisoformat(str((_load().get(str(uid)) or {}).get("set_at")))
    except (TypeError, ValueError):
        return False
    return datetime.now(timezone.utc) - set_at < timedelta(days=14)


GUARD_FROM_HOUR, GUARD_FROM_MIN = 21, 30   # вечерняя проверка: после этого времени и до полуночи, один раз за вечер


async def evening_problem(profile) -> str | None:  # noqa: ANN001
    """Что не так с будильником на завтра: "off" (выключен), "no_phone_alarm" (в телефоне не стоит) или None — всё в порядке.
    03.10: он проспал — будильник был выключен (3 нажатия на переключатель в приложении), а об этом никто не сказал."""
    from . import services, wake_runner

    tomorrow = profile.today + timedelta(days=1)
    s, plan = await wake_runner.plan_for(profile, tomorrow)
    if not s.enabled:
        history = await services.wake_history(profile.telegram_id, days=7)
        return "off" if any(r.get("woke_at") for r in history) else None   # писать только тем, кто будильником пользуется
    if plan.active and app_enabled(profile.telegram_id) and _uses_app(profile.telegram_id) \
            and not tomorrow_ready(profile.telegram_id, tomorrow.isoformat()):
        return "no_phone_alarm"   # только если он сам включил будильник в приложении; звонок Telegram идёт в любом случае
    return None


async def evening_guard(bot, profile) -> bool:  # noqa: ANN001
    """Вечером, один раз: будильник на завтра выключен или не стоит в телефоне — сказать об этом сейчас, а не утром."""
    from aiogram.types import InlineKeyboardMarkup

    from . import wake_runner
    from .keyboards import _btn

    now = profile.now
    if (now.hour, now.minute) < (GUARD_FROM_HOUR, GUARD_FROM_MIN):
        return False
    uid = profile.telegram_id
    data = _load()
    guard = data.get("guard") or {}
    today = profile.today.isoformat()
    if guard.get(str(uid)) == today:
        return False
    problem = await evening_problem(profile)
    guard[str(uid)] = today   # один раз за вечер — даже если не вышло отправить (не заспамить)
    data = _load()
    data["guard"] = guard
    _save(data)
    if problem is None:
        return False
    s, plan = await wake_runner.plan_for(profile, profile.today + timedelta(days=1))
    at = plan.wake_at.astimezone(profile.tz).strftime("%H:%M") if plan.wake_at else ""
    markup = None
    if problem == "off":
        text = profile.tr(f"⚠️ <b>Будильник выключен</b> — завтра{(' в ' + at) if at else ''} я не разбужу. Включить?",
                          f"⚠️ <b>Budilnik o'chiq</b> — ertaga{(' ' + at + ' da') if at else ''} uyg'otmayman. Yoqaymi?")
        markup = InlineKeyboardMarkup(inline_keyboard=[[_btn(profile.tr("⏰ Включить будильник", "⏰ Budilnikni yoqish"),
                                                             "wakeset:toggle:enabled", style="success")]])
    else:
        text = profile.tr(f"⚠️ Будильник на завтра{(' (' + at + ')') if at else ''} в телефоне не стоит — приложение JES давно не отвечало. "
                          "Откройте JES (достаточно один раз) — тогда он поставится. Если не откроете, утром всё равно позвонит Telegram.",
                          f"⚠️ Ertangi budilnik{(' (' + at + ')') if at else ''} telefonda qo'yilmagan — JES ilovasi uzoq vaqt javob bermadi. "
                          "JESni bir marta oching — shunda qo'yiladi. Ochmasangiz ham, ertalab Telegram qo'ng'iroq qiladi.")
    try:
        from . import screen as screen_mod

        # 07.10: липкая заметка — стирает только срок (утром сама исчезнет); нажатие чужой кнопки её не уберёт
        if await screen_mod.send_note(bot, uid, text, markup, ttl=12 * 3600, sticky=True, disable_notification=False) is None:
            return False
    except Exception:
        logger.warning("alarm guard: не отправилось", exc_info=True)
        return False
    logger.info("alarm guard %s: %s", uid, problem)
    return True


async def next_plan(profile) -> dict[str, Any]:  # noqa: ANN001
    """Ближайший подъём: сегодня (если ещё впереди и не встал) или завтра."""
    from . import services, wake_runner

    from . import places

    now = datetime.now(timezone.utc)
    app_on = app_enabled(profile.telegram_id)
    s = None
    for day in (profile.today, profile.today + timedelta(days=1)):
        s, plan = await wake_runner.plan_for(profile, day)
        if not plan.active or plan.wake_at is None or plan.wake_at <= now:
            continue
        log = await services.wake_log(profile.telegram_id, day) or {}
        if log.get("woke_at"):
            continue
        # enabled — ставить ли будильник Android в приложении (иначе приложение отменяет свой); on — подъём вообще включён
        # (звонит Telegram): приложение показывает время и «звонит Telegram», а не «Выключен»
        return {"enabled": app_on, "on": True, "app": app_on, "day": day.isoformat(), "at_ms": int(plan.wake_at.timestamp() * 1000),
                "wake_at": plan.wake_at.strftime("%H:%M"), "takbir": plan.takbir, "fajr": plan.fajr,
                "window": list(plan.window) if plan.window else None, "offset_min": s.offset_min,
                "place": places.label(profile.telegram_id, profile.lang)}
    return {"enabled": False, "on": bool(s and s.enabled), "app": app_on, "offset_min": s.offset_min if s else None,
            "place": places.label(profile.telegram_id, profile.lang)}


def morning_note(profile, persona) -> str:  # noqa: ANN001
    """Пометка для JES на телефоне: он только что встал по будильнику — доброе утро и вопрос дня (как в звонке)."""
    from . import islam_quiz

    title = {"shef": "Шеф", "ser": "Сэр", "boss": "Босс", "mix": "Шеф"}.get(persona.honorific, profile.first_name or "")
    quiz = islam_quiz.for_day_set(profile.telegram_id, profile.today)
    return (f"[Утро: он только что встал по будильнику JES. Бодро и тепло: «Доброе утро, {title}!» — и ТРИ ВОПРОСА ДНЯ ниже, "
            "по одному, коротко, как викторину. Ответит: верно — коротко похвали; неверно или не знает — спокойно скажи правильный ответ; "
            "дуа или аят — арабский текст ТОЧНО как написан, слово в слово, потом коротко смысл. В конце одной фразой — "
            "«Пусть Аллах примет ваш намаз». Коротко, без лекций.]\n" + islam_quiz.prompt_block_set(quiz))


__all__ = ["ENABLED", "app_enabled", "set_app", "call_answered", "in_call", "scheduled", "rang", "tomorrow_ready", "evening_problem",
           "evening_guard", "next_plan", "morning_note"]

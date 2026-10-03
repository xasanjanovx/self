"""Будильник на фаджр в самом приложении JES (26.09.2026, его выбор «у меня — на программе, у других — Telegram»).

Приложение раз в несколько часов берёт время подъёма (GET /jarvis/v1/wake_plan) и ставит настоящий будильник
Android: он звенит через канал будильника — громко, на беззвучном и в «Не беспокоить», без интернета и бесплатно.
Звонит мелодией и ГОЛОСОМ JES («Доброе утро, шеф! Пора вставать на фаджр» — записи из bot/alarm_voice.py).
Нажал «Проснулся» — JES голосом: доброе утро + вопросы дня; будильник стихает, когда он ответил.
«Ещё 5 минут» — пауза. Не встал за APP_GRACE после того, как будильник прозвенел, — запасной звонок в Telegram.
Не прозвенел вовсе (приложение убили, будильник стёрла прошивка) — Telegram звонит через RING_WAIT после времени подъёма.
Состояние — DATA_DIR/app_alarm.json: {uid: {"day": "2026-09-27", "at_ms": …, "rang_day": "…", "rang_at": "…"}}.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

APP_GRACE = timedelta(minutes=3)   # будильник прозвенел, а он не встал — звонит ещё и Telegram
RING_WAIT = timedelta(seconds=90)  # будильник должен был прозвенеть, а приложение не сказало «звоню» — звонит Telegram
# 02.10 он выбрал «только звонок Telegram» — 03.10 он проспал фаджр: звонок не дошёл до спящего телефона (HyperOS без Google-
# сервисов), а будильник в приложении был выключен. Теперь будильник приложения — основной слой (работает без интернета и
# в «Не беспокоить»), Telegram-звонок — второй. Выключить: APP_ALARM=0 в .env сервера.
ENABLED = (os.getenv("APP_ALARM") or "1").strip() != "0"


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


def _rang_at(st: dict[str, Any], day_iso: str) -> datetime | None:
    if st.get("rang_day") != day_iso:
        return None
    try:
        return datetime.fromisoformat(str(st.get("rang_at")))
    except (TypeError, ValueError):
        return None


def holds_telegram(uid: int, day_iso: str, wake_at: datetime, now: datetime) -> bool:
    """Будильник в приложении на этот день стоит и работает — Telegram пока молчит.

    Молчит, пока: (а) время ещё не пришло + RING_WAIT на «приложение сказало, что звонит»; (б) после «звоню» — APP_GRACE.
    Будильник в телефоне стоит на другое время, чем в плане сервера (поменяли в боте, приложение не обновилось), или не
    зазвонил вовсе — Telegram звонит сразу, без ожидания."""
    if not ENABLED:
        return False
    st = _load().get(str(uid)) or {}
    if st.get("day") != day_iso:
        return False
    try:
        at = datetime.fromtimestamp(int(st.get("at_ms") or 0) / 1000, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return False
    rang_at = _rang_at(st, day_iso)
    if rang_at is not None:
        return now < rang_at + APP_GRACE
    if abs((at - wake_at).total_seconds()) > 90:
        return False  # в телефоне стоит устаревшее время
    return now < wake_at + RING_WAIT


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
    if plan.active and ENABLED and _uses_app(profile.telegram_id) and not tomorrow_ready(profile.telegram_id, tomorrow.isoformat()):
        return "no_phone_alarm"
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
        await bot.send_message(uid, text, reply_markup=markup, disable_notification=False)
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
    s = None
    for day in (profile.today, profile.today + timedelta(days=1)):
        s, plan = await wake_runner.plan_for(profile, day)
        if not ENABLED:
            break  # будильник приложения выключен: enabled=false → приложение отменяет свой будильник
        if not plan.active or plan.wake_at is None or plan.wake_at <= now:
            continue
        log = await services.wake_log(profile.telegram_id, day) or {}
        if log.get("woke_at"):
            continue
        return {"enabled": True, "day": day.isoformat(), "at_ms": int(plan.wake_at.timestamp() * 1000), "wake_at": plan.wake_at.strftime("%H:%M"),
                "takbir": plan.takbir, "fajr": plan.fajr, "window": list(plan.window) if plan.window else None,
                "offset_min": s.offset_min, "place": places.label(profile.telegram_id, profile.lang)}
    return {"enabled": False, "on": bool(s and s.enabled), "offset_min": s.offset_min if s else None,
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


__all__ = ["APP_GRACE", "RING_WAIT", "ENABLED", "scheduled", "rang", "holds_telegram", "tomorrow_ready", "evening_problem", "evening_guard", "next_plan", "morning_note"]

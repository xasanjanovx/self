"""Исполнение подъёма: собрать текст, позвонить, дождаться подтверждения, перезвонить.

Чистая логика — в bot/wake.py, звонок — в bot/caller.py, расписание — bot/workers.py.
Здесь склейка: что именно сказать в трубку, что прислать в чат, как отметить подъём.
"""
from __future__ import annotations

import logging
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

from . import caller
from . import daily
from . import prayer
from . import services
from . import voice
from . import wake as wake_mod
from .profile import Profile

logger = logging.getLogger(__name__)

# состояние текущих подъёмов в памяти: uid → {"task": …, "plan": …, "attempts": int, "last": datetime}
_active: dict[int, dict[str, Any]] = {}


def active_task(uid: int) -> dict[str, Any] | None:
    """Задание, которое сейчас ждёт подтверждения (для роутера сообщений)."""
    state = _active.get(uid)
    if not state:
        return None
    if state.get("expires") and datetime.now(timezone.utc) > state["expires"]:
        _active.pop(uid, None)
        return None
    return state.get("task")


def clear(uid: int) -> None:
    _active.pop(uid, None)


def wake_keyboard(lang: str) -> InlineKeyboardMarkup:
    from .keyboards import _btn

    uz = lang == "uz"
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✅ " + ("Turdim" if uz else "Проснулся"), "wake:up", style="primary")],
        [_btn("😴 +10 " + ("daqiqa" if uz else "мин"), "wake:snooze"), _btn("🚫 " + ("Bugun buzmang" if uz else "Не будить сегодня"), "wake:skip")],
    ])


async def _settings(uid: int) -> wake_mod.WakeSettings:
    return wake_mod.WakeSettings.from_row(await services.wake_settings(uid))


async def plan_for(profile: Profile, day: date | None = None) -> tuple[wake_mod.WakeSettings, wake_mod.DayPlan]:
    s = await _settings(profile.telegram_id)
    day = day or profile.today
    rows = await prayer.timings(day, latitude=s.latitude, longitude=s.longitude, method=s.calc_method)
    return s, wake_mod.plan_for_day(s, day, tz=profile.tz, timings=rows)


async def _speech_file(text: str) -> str | None:
    """Текст → OGG для звонка. Возвращает путь к временному файлу."""
    audio = await voice.make_voice(text)
    if not audio:
        return None
    fd, path = tempfile.mkstemp(prefix="wake_", suffix=".ogg")
    with os.fdopen(fd, "wb") as fh:
        fh.write(audio)
    return path


async def _task_for(profile: Profile, s: wake_mod.WakeSettings, plan: wake_mod.DayPlan) -> tuple[dict[str, Any], dict[str, Any] | None]:
    verse = await daily.verse_of_day(plan.day)
    task = wake_mod.make_task(s, plan.day, lang=profile.lang, verse_ref=(verse or {}).get("ref"))
    return task, verse


async def run_attempt(bot: Bot, profile: Profile, s: wake_mod.WakeSettings, plan: wake_mod.DayPlan, log: dict[str, Any] | None) -> dict[str, Any]:
    """Одна попытка разбудить: звонок (если доступен) + сообщение с заданием."""
    uid = profile.telegram_id
    now = datetime.now(timezone.utc)
    attempts = int((log or {}).get("attempts") or 0) + 1
    state = _active.get(uid) or {}
    task, verse = (state.get("task"), state.get("verse")) if state.get("task") else await _task_for(profile, s, plan)
    minutes_left = int((plan.takbir_at - now.astimezone(profile.tz)).total_seconds() // 60) if plan.takbir_at else None
    _active[uid] = {"task": task, "verse": verse, "plan": plan, "attempts": attempts, "last": now,
                    "expires": now + timedelta(hours=3)}

    answered = False
    call_error = None
    if s.call_enabled and caller.available():
        script = wake_mod.call_script(
            name=profile.first_name or ("do'st" if profile.lang == "uz" else "друг"),
            takbir=plan.takbir, minutes_left=minutes_left, task_text=str(task.get("text") or ""),
            verse_text=daily.speakable_verse(verse) if attempts == 1 else "",
            lang="uz" if profile.lang == "uz" else "ru",
        )
        path = await _speech_file(script)
        if path:
            try:
                result = await caller.call(uid, path, ring_seconds=max(20, s.retry_seconds), play_seconds=45)
                answered, call_error = bool(result.get("answered")), result.get("error")
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
        else:
            call_error = "tts unavailable"

    text = wake_mod.wake_message(name=profile.first_name or "", plan=plan, task=task, attempt=attempts, lang=profile.lang)
    if attempts == 1 and verse:
        block = daily.verse_block(verse, "uz" if profile.lang == "uz" else "ru")
        if block:
            text += "\n\n" + block
    try:
        await bot.send_message(uid, text, reply_markup=wake_keyboard(profile.lang), disable_notification=False)
    except Exception:
        logger.exception("wake message failed for %s", uid)

    fields: dict[str, Any] = {"attempts": attempts, "planned_at": plan.wake_at.isoformat() if plan.wake_at else None,
                              "takbir_at": plan.takbir, "task_kind": task.get("kind"), "task_text": task.get("text")}
    if attempts == 1:
        fields["first_call_at"] = now.isoformat()
    await services.save_wake_log(uid, plan.day, fields)
    if call_error:
        logger.info("wake call error for %s: %s", uid, call_error)
    return {"attempts": attempts, "answered": answered, "task": task, "call_error": call_error}


async def mark_awake(bot: Bot, profile: Profile, *, source: str, notify: bool = True) -> dict[str, Any]:
    """Отметить подъём: звонки прекращаются, приходит план утра."""
    uid = profile.telegram_id
    now = datetime.now(timezone.utc)
    s, plan = await plan_for(profile)
    local_now = now.astimezone(profile.tz)
    before = bool(plan.takbir_at and local_now <= plan.takbir_at)
    state = _active.pop(uid, None)
    fields = {"woke_at": now.isoformat(), "woke_source": source, "before_takbir": before,
              "takbir_at": plan.takbir, "planned_at": plan.wake_at.isoformat() if plan.wake_at else None}
    if state and state.get("task"):
        fields.update({"task_kind": state["task"].get("kind"), "task_text": state["task"].get("text")})
    await services.save_wake_log(uid, plan.day, fields)
    history = await services.wake_history(uid, days=60)
    streak = wake_mod.streak_days([{**r, **({"woke_at": fields["woke_at"], "before_takbir": before} if str(r.get("day"))[:10] == plan.day.isoformat() else {})} for r in history] or [{"day": plan.day.isoformat(), **fields}], plan.day)
    if notify:
        try:
            await bot.send_message(uid, wake_mod.done_message(plan=plan, now=local_now, lang=profile.lang, streak=streak))
        except Exception:
            logger.debug("wake done message failed", exc_info=True)
    return {"before_takbir": before, "streak": streak, "plan": plan}


async def task_done(bot: Bot, profile: Profile, *, ok: bool, comment: str = "") -> None:
    uid = profile.telegram_id
    if ok:
        await services.save_wake_log(uid, profile.today, {"task_done_at": datetime.now(timezone.utc).isoformat()})
        _active.pop(uid, None)


async def snooze(profile: Profile, minutes: int) -> datetime:
    """Отложить звонки на N минут (по факту — сдвигаем время последней попытки)."""
    uid = profile.telegram_id
    state = _active.get(uid) or {}
    until = datetime.now(timezone.utc) + timedelta(minutes=max(1, min(30, minutes)))
    state["snooze_until"] = until
    state.setdefault("expires", until + timedelta(hours=2))
    _active[uid] = state
    return until


def snoozed_until(uid: int) -> datetime | None:
    return (_active.get(uid) or {}).get("snooze_until")


async def skip_today(profile: Profile) -> None:
    await services.save_wake_log(profile.telegram_id, profile.today, {"woke_at": datetime.now(timezone.utc).isoformat(), "woke_source": "skip"})
    _active.pop(profile.telegram_id, None)


async def morning_extra(profile: Profile) -> str:
    """Строка с временами намаза для утренней сводки."""
    s, plan = await plan_for(profile)
    rows = await prayer.timings(profile.today, latitude=s.latitude, longitude=s.longitude, method=s.calc_method)
    if not rows:
        return ""
    return "🕌 " + prayer.summary(rows, profile.lang, takbir=plan.takbir)


__all__ = ["run_attempt", "mark_awake", "task_done", "snooze", "snoozed_until", "skip_today", "plan_for",
           "active_task", "clear", "wake_keyboard", "morning_extra"]

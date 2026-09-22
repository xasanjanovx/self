"""Экран и реакции подъёма: кнопки «Проснулся / +10 мин / Не будить», приём задания.

Подтверждением считается любое действие, которое невозможно сделать во сне:
фото пустого стакана, голосовое с подсчётом приседаний или чтением аята, ответ на
пример. Как только подтвердил — звонки прекращаются и приходит план утра.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from .. import screen as screen_mod
from .. import services
from .. import wake as wake_mod
from .. import wake_runner
from ..context import db
from ..profile import Profile
from .common import answer_now, get_profile, safe_delete

router = Router(name="wake")
logger = logging.getLogger(__name__)

TASK_TTL = 30 * 60   # задание нужно, пока его выполняют
NOTE_TTL = 3 * 60    # «позвоню в 05:10», «не сходится» — короткие реплики


async def _say(bot, uid: int, text: str, ttl: int = NOTE_TTL) -> None:  # noqa: ANN001
    """Реплика подъёма — временная: сама исчезнет, чат не копит."""
    try:
        await screen_mod.send_ephemeral(bot, uid, text, keep_previous=True, ttl=ttl)
    except Exception:
        logger.debug("wake reply failed", exc_info=True)


@router.callback_query(F.data == "wake:up")
async def cb_wake_up(callback: CallbackQuery) -> None:
    profile = await get_profile(callback.from_user)
    await answer_now(callback, profile.tr("Отлично!", "Zo'r!"))
    task = wake_runner.active_task(profile.telegram_id)
    await wake_runner.mark_awake(callback.bot, profile, source="button")
    if task:
        await _say(callback.bot, profile.telegram_id, profile.tr("Задание: ", "Vazifa: ") + str(task.get("text") or ""), TASK_TTL)


@router.callback_query(F.data == "wake:snooze")
async def cb_wake_snooze(callback: CallbackQuery) -> None:
    profile = await get_profile(callback.from_user)
    until = await wake_runner.snooze(profile, 10)
    local = until.astimezone(profile.tz)
    await answer_now(callback, profile.tr(f"Позвоню в {local:%H:%M}", f"{local:%H:%M} da qo'ng'iroq qilaman"))


@router.callback_query(F.data == "wake:skip")
async def cb_wake_skip(callback: CallbackQuery) -> None:
    profile = await get_profile(callback.from_user)
    await wake_runner.skip_today(profile, callback.bot)
    await answer_now(callback, profile.tr("Сегодня не бужу", "Bugun uyg'otmayman"))


async def handle_awake_text(message: Message, profile: Profile, text: str) -> bool:
    """«Проснулся» / «ещё 10 минут» в свободном тексте. True — сообщение обработано."""
    if not db.available("wake_log"):
        return False
    minutes = wake_mod.snooze_minutes(text)
    if minutes and wake_runner.active_task(profile.telegram_id):
        until = await wake_runner.snooze(profile, minutes)
        local = until.astimezone(profile.tz)
        await safe_delete(message)
        await _say(message.bot, profile.telegram_id, profile.tr(f"😴 Хорошо, позвоню в {local:%H:%M}", f"😴 Mayli, {local:%H:%M} da qo'ng'iroq qilaman"))
        return True
    if not wake_mod.looks_awake(text):
        return False
    await safe_delete(message)
    task = wake_runner.active_task(profile.telegram_id)
    result = await wake_runner.mark_awake(message.bot, profile, source="message" if task else "early")
    if task:
        await _say(message.bot, profile.telegram_id, profile.tr("Задание: ", "Vazifa: ") + str(task.get("text") or ""), TASK_TTL)
    elif result["plan"].active and result["plan"].wake_at:
        # проснулся сам, до звонка — звонка не будет, но задание всё равно даём
        s, plan = await wake_runner.plan_for(profile)
        verse_ref = None
        task = wake_mod.make_task(s, plan.day, lang=profile.lang, verse_ref=verse_ref)
        await _say(message.bot, profile.telegram_id,
                   profile.tr("Звонка не будет 👍 Задание: ", "Qo'ng'iroq bo'lmaydi 👍 Vazifa: ") + str(task.get("text")), TASK_TTL)
    return True


async def handle_task_reply(message: Message, profile: Profile, *, text: str | None = None, transcript: str | None = None,
                            voice_seconds: int = 0, has_photo: bool = False) -> bool:
    """Ответ на задание подъёма (фото/голос/текст). True — сообщение обработано."""
    task = wake_runner.active_task(profile.telegram_id)
    if not task:
        return False
    ok, why = wake_mod.check_answer(task, text=text, has_photo=has_photo, voice_seconds=voice_seconds, transcript=transcript)
    if ok:
        await wake_runner.mark_awake(message.bot, profile, source="task", notify=False)
        await wake_runner.task_done(message.bot, profile, ok=True)
        s, plan = await wake_runner.plan_for(profile)
        history = await services.wake_history(profile.telegram_id, days=60)
        streak = wake_mod.streak_days(history, plan.day)
        await safe_delete(message)
        await _say(message.bot, profile.telegram_id, wake_mod.done_message(plan=plan, now=profile.now, lang=profile.lang, streak=streak),
                   wake_runner.DONE_TTL)
        return True
    hints = {
        "need_photo": ("Пришли фото пустого стакана 💧", "Bo'sh stakanni suratga olib yuboring 💧"),
        "need_voice": ("Жду голосовое 🎤", "Ovozli xabar kutyapman 🎤"),
        "too_short": ("Слишком коротко — запиши ещё раз 🎤", "Juda qisqa — yana bir bor yozing 🎤"),
        "wrong": ("Не сходится, посчитай ещё раз 🙂", "To'g'ri kelmadi, yana sanang 🙂"),
        "need_answer": ("Напиши ответ числом", "Javobni raqam bilan yozing"),
    }
    ru, uz = hints.get(why, hints["need_answer"])
    await safe_delete(message)
    await _say(message.bot, profile.telegram_id, profile.tr(ru, uz))
    return True


__all__ = ["router", "handle_awake_text", "handle_task_reply"]

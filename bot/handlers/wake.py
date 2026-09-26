"""Реакции подъёма в чате: кнопки «Проснулся / +10 мин / Не будить» и «проснулся» текстом.

Заданий и упражнений нет: будит JES в звонке мотивирующими словами и сам по голосу
убеждается, что человек встал. Написал «проснулся» — звонки прекращаются, звонящая трубка
кладётся, приходит короткий итог (и утренняя сводка, если она «после подъёма»).
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from .. import screen as screen_mod
from .. import wake as wake_mod
from .. import wake_runner
from ..context import db
from ..profile import Profile
from .common import answer_now, get_profile, safe_delete

router = Router(name="wake")
logger = logging.getLogger(__name__)

NOTE_TTL = 3 * 60    # «позвоню в 05:10», «звонка не будет» — короткие реплики


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
    await wake_runner.mark_awake(callback.bot, profile, source="button")


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
    if minutes and wake_runner.is_waking(profile.telegram_id):
        until = await wake_runner.snooze(profile, minutes)
        local = until.astimezone(profile.tz)
        await safe_delete(message)
        await _say(message.bot, profile.telegram_id, profile.tr(f"😴 Хорошо, позвоню в {local:%H:%M}", f"😴 Mayli, {local:%H:%M} da qo'ng'iroq qilaman"))
        return True
    if wake_mod.looks_skip(text) and await _wake_matters(profile):
        # «не звони», «не буди» — сегодня больше не звоним (и кладём трубку, если звонок идёт)
        await safe_delete(message)
        await wake_runner.skip_today(profile, message.bot)
        try:
            from .. import caller

            await caller.hang_up(profile.telegram_id)
        except Exception:
            pass
        await _say(message.bot, profile.telegram_id, profile.tr("👌 Хорошо, сегодня больше не звоню.", "👌 Mayli, bugun boshqa qo'ng'iroq qilmayman."))
        return True
    if not wake_mod.looks_awake(text):
        return False
    await safe_delete(message)
    waking = wake_runner.is_waking(profile.telegram_id)
    result = await wake_runner.mark_awake(message.bot, profile, source="message" if waking else "early")
    if not waking and result["plan"].active and result["plan"].wake_at:
        # проснулся сам, до звонка — звонка сегодня не будет
        await _say(message.bot, profile.telegram_id, profile.tr("Звонка сегодня не будет 👍 Пусть Аллах примет ваш намаз!",
                                                                "Bugun qo'ng'iroq bo'lmaydi 👍 Alloh namozingizni qabul qilsin!"))
    return True


async def _wake_matters(profile: Profile) -> bool:
    """«Не звони» относится к будильнику: подъём идёт или сегодня ещё будет (а не «не звони маме» днём)."""
    if wake_runner.is_waking(profile.telegram_id):
        return True
    try:
        _, plan = await wake_runner.plan_for(profile)
    except Exception:
        return False
    if not plan.active or plan.wake_at is None:
        return False
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    return plan.wake_at - timedelta(hours=10) <= now <= plan.wake_at + timedelta(hours=2)


__all__ = ["router", "handle_awake_text"]

"""Кнопки плана дня и вечернего разбора (30.09, bot/plan.py): 🔄 Пересоставить · ➡️ Перенести на завтра · 🗓 План на завтра."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from .. import access, plan
from .common import answer_now, get_profile

router = Router(name="plan")
logger = logging.getLogger(__name__)


@router.callback_query(F.data.startswith("plan:"))
async def cb_plan(callback: CallbackQuery) -> None:
    profile = await get_profile(callback.from_user)
    if not access.is_owner(profile.telegram_id) or callback.message is None:
        await answer_now(callback)
        return
    action = str(callback.data).split(":")[1]
    if action == "move":
        moved = await plan.move_open_to_tomorrow(profile)
        await answer_now(callback, profile.tr(f"➡️ Перенесено на завтра: {moved}", f"➡️ Ertaga ga ko'chirildi: {moved}"), alert=True)
        return
    if action not in {"today", "tomorrow"}:
        await answer_now(callback)
        return
    await answer_now(callback, profile.tr("Составляю план…", "Reja tuzyapman…"))
    try:
        await plan.send(callback.bot, profile, "tomorrow" if action == "tomorrow" else "morning")
    except Exception:
        logger.exception("plan button failed")
        await callback.message.answer(profile.tr("Не получилось составить план — попробуйте ещё раз.", "Reja tuzilmadi — yana urinib ko'ring."))

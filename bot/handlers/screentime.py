"""Кнопки экранного времени (04.10.2026, bot/screentime.py): лимиты мягче/строже, «я работаю», следить вкл/выкл, экран лимитов."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from .. import screentime
from .common import answer_now, get_profile

router = Router(name="screentime")
logger = logging.getLogger(__name__)


async def _render(callback: CallbackQuery, profile, notice: str | None = None) -> None:  # noqa: ANN001
    st = screentime.load(profile.telegram_id)
    text = screentime.settings_text(profile, st) + (f"\n\n{notice}" if notice else "")
    try:
        await callback.message.edit_text(text, reply_markup=screentime.settings_keyboard(profile, st))
    except Exception:
        try:
            await callback.message.answer(text, reply_markup=screentime.settings_keyboard(profile, st))
        except Exception:
            logger.warning("screentime: экран не показался", exc_info=True)


@router.callback_query(F.data.startswith("scr:"))
async def cb_screen(callback: CallbackQuery) -> None:
    profile = await get_profile(callback.from_user)
    uid = profile.telegram_id
    parts = callback.data.split(":")
    action = parts[1] if len(parts) > 1 else "show"
    uz = profile.lang == "uz"
    notice = None
    if action == "hard":
        screentime.adjust(uid, 0.8)
        notice = "➖ " + ("Qattiqroq" if uz else "Строже")
    elif action == "soft":
        screentime.adjust(uid, 1.2)
        notice = "➕ " + ("Yumshoqroq" if uz else "Мягче")
    elif action == "busy":
        minutes = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 60
        until = screentime.set_busy(uid, minutes or None)
        notice = (("😌 Eslatmayman: " if uz else "😌 Не напоминаю до ") + f"{until.astimezone(profile.tz):%H:%M}") if until else \
            ("Bandlik olib tashlandi" if uz else "«Занят» снят")
    elif action == "toggle":
        on = not bool(screentime.load(uid).get("enabled", True))
        screentime.set_enabled(uid, on)
        notice = ("📱 Kuzatish yoqildi" if uz else "📱 Слежу снова") if on else ("📱 Kuzatish o'chirildi" if uz else "📱 Больше не слежу")
    await answer_now(callback, notice or "")
    await _render(callback, profile, notice)

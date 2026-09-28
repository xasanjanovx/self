"""Кнопки напоминания «каждый день» (bot/daily_tasks.py): ✅ Сделал / ⏭ Не сегодня."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from .. import daily_tasks
from .common import answer_now, get_profile

router = Router(name="daily")


@router.callback_query(F.data.startswith("daily:"))
async def cb_daily(callback: CallbackQuery) -> None:
    _, action, hid = (str(callback.data).split(":") + ["", ""])[:3]
    profile = await get_profile(callback.from_user)
    uid = profile.telegram_id
    item = daily_tasks.find(uid, hid)
    if item is None:
        await answer_now(callback, profile.tr("Этого дела уже нет", "Bu ish endi yo'q"))
        return
    today = profile.now.date()
    if action == "done":
        item = daily_tasks.done(uid, item, today)
        extra = daily_tasks.progress(item, today)
        note = profile.tr("✅ Сделано", "✅ Bajarildi") + (f" · {extra}" if extra else "")
    elif action == "skip":
        daily_tasks.skip(uid, item, today)
        note = profile.tr("⏭ Сегодня пропускаем", "⏭ Bugun o'tkazib yuboramiz")
    else:
        await answer_now(callback)
        return
    await answer_now(callback, note)
    try:
        # кнопку «Продолжить урок» (ссылка) оставляем, отметки убираем
        rows = [row for row in (callback.message.reply_markup.inline_keyboard if callback.message and callback.message.reply_markup else [])
                if all(b.url for b in row)]
        from aiogram.types import InlineKeyboardMarkup

        from ..profile import h

        await callback.message.edit_text(f"🔁 <b>{h(item['title'])}</b>\n{note}", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)
    except Exception:
        pass

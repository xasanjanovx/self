"""Настройки: утренняя/вечерняя сводка, авто-отчёт, язык."""
from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from .. import cache
from .. import emoji as pe
from .. import services
from .. import ui
from ..context import db
from ..keyboards import settings_keyboard
from ..profile import Profile
from .common import answer_now, get_profile, safe_edit

router = Router(name="settings")
logger = logging.getLogger(__name__)


async def report_prefs(uid: int) -> dict:
    return await cache.remember(uid, ("report_prefs",), 600, lambda: db.get_report_preferences(uid))


async def render_settings(callback: CallbackQuery, profile: Profile) -> None:
    lang = profile.lang
    if await db.ensure_available("user_settings"):
        us, prefs = await asyncio.gather(services.user_settings(profile.telegram_id), report_prefs(profile.telegram_id))
        hint = ""
    else:
        us = {"brief_morning": False, "brief_evening": False}
        prefs = await report_prefs(profile.telegram_id)
        hint = "\n\n⚠️ " + profile.tr(
            "Сводки недоступны: выполни <code>sql/migrations/004_v2_features.sql</code> в Supabase → SQL Editor.",
            "Xulosalar ishlamaydi: Supabase → SQL Editor da <code>sql/migrations/004_v2_features.sql</code> ni bajaring.",
        )
    if lang == "uz":
        text = ui.join(
            ui.title(pe.SETTINGS, "Sozlamalar"),
            ui.card("<b>🌅 Ertalabki xulosa</b> · 08:00", ["balans, hafta xarajati, bugungi kaloriya rejasi, yaqin to'lovlar"]),
            ui.card("<b>🌙 Kechki eslatma</b> · 21:00", ["ovqat yoki xarajat yozilmagan bo'lsa — eslatadi; hammasi yozilgan bo'lsa — kun natijasi"]),
            ui.card("<b>📊 Avto-hisobot</b>", ["yakshanba 20:00 (haftalik) yoki oyning 1-kuni (oylik)"]),
        )
    else:
        text = ui.join(
            ui.title(pe.SETTINGS, "Настройки"),
            ui.card("<b>🌅 Утренняя сводка</b> · 08:00", ["баланс, траты за неделю, план калорий на день, ближайшие платежи"]),
            ui.card("<b>🌙 Вечернее напоминание</b> · 21:00", ["если сегодня не записал еду или расходы — напомнит; если всё записано — итог дня"]),
            ui.card("<b>📊 Авто-отчёт</b>", ["воскресенье 20:00 (недельный) или 1-го числа (месячный)"]),
        )
    await safe_edit(
        callback,
        text + hint,
        settings_keyboard(
            lang,
            morning=bool(us.get("brief_morning", True)),
            evening=bool(us.get("brief_evening", True)),
            report_enabled=bool(prefs.get("enabled", True)),
            report_frequency=str(prefs.get("frequency") or "weekly"),
        ),
    )


@router.callback_query(F.data == "menu:settings")
async def cb_settings(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    await state.clear()
    await render_settings(callback, await get_profile(callback.from_user))


@router.callback_query(F.data.startswith("settings:brief:"))
async def cb_brief_toggle(callback: CallbackQuery) -> None:
    profile = await get_profile(callback.from_user)
    which = callback.data.split(":")[-1]
    if not await db.ensure_available("user_settings"):
        await answer_now(callback, profile.tr("Сначала выполни миграцию 004", "Avval 004 migratsiyasini bajaring"), alert=True)
        return
    us = await services.user_settings(profile.telegram_id)
    field = "brief_morning" if which == "morning" else "brief_evening"
    new_value = not bool(us.get(field, True))
    await services.save_user_settings(profile.telegram_id, {field: new_value})
    label = profile.tr("Утро", "Ertalab") if which == "morning" else profile.tr("Вечер", "Kechqurun")
    await answer_now(callback, f"{label}: {'✅' if new_value else '⛔'}")
    await render_settings(callback, profile)


@router.callback_query(F.data.startswith("report:set:"))
async def cb_report_set(callback: CallbackQuery) -> None:
    profile = await get_profile(callback.from_user)
    mode = callback.data.split(":")[-1]
    prefs = await report_prefs(profile.telegram_id)
    if mode == "off":
        enabled, frequency = False, str(prefs.get("frequency") or "weekly")
        note = profile.tr("Авто-отчёт выключен", "Avto-hisobot o'chirildi")
    else:
        enabled, frequency = True, ("monthly" if mode == "monthly" else "weekly")
        note = profile.tr("Раз в месяц ✅" if frequency == "monthly" else "Раз в неделю ✅", "Oyda bir ✅" if frequency == "monthly" else "Haftada bir ✅")
    await answer_now(callback, note)
    await db.save_report_preferences(profile.telegram_id, enabled=enabled, frequency=frequency, last_sent_key=prefs.get("last_sent_key"))
    cache.invalidate(profile.telegram_id, "report_prefs")
    await render_settings(callback, profile)

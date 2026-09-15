"""Аналитика: сводка за 7/30/90 дней и графики."""
from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from .. import cache
from .. import categories as cats
from .. import charts as charts_mod
from .. import emoji as pe
from .. import finance as fin
from .. import insights
from .. import screen as screen_mod
from .. import services
from ..context import ai
from ..keyboards import dashboard_keyboard
from ..profile import Profile, h
from ..reports import build_summary
from .common import answer_now, get_profile, safe_delete, safe_edit

router = Router(name="analytics")
logger = logging.getLogger(__name__)

_PERIODS = {"7d": 7, "30d": 30, "90d": 90}


async def render_dashboard(target: Message | CallbackQuery, profile: Profile, period_code: str) -> None:
    days = _PERIODS.get(period_code, 7)
    payload, nutrition_profile = await asyncio.gather(
        services.period_payload(profile, days),
        services.nutrition_profile(profile.telegram_id),
    )
    title = profile.tr(f"📊 <b>Аналитика — {days} дн.</b>", f"📊 <b>Tahlil — {days} kun</b>")
    summary = build_summary(
        profile,
        days=days,
        entries=payload["all_finance_entries"],
        logs=payload["calorie_logs"],
        nutrition_profile=nutrition_profile,
        title=title,
    )
    kb = dashboard_keyboard(period_code, profile.lang)
    bot = target.bot
    chat_id = target.message.chat.id if isinstance(target, CallbackQuery) else target.chat.id
    await screen_mod.drop_chart(bot, chat_id)
    if isinstance(target, CallbackQuery):
        await safe_edit(target, summary.text, kb)
    else:
        await screen_mod.show_screen(bot, chat_id, summary.text, kb)

    # Инсайт — после показа экрана, чтобы не ждать AI. Кэш на день.
    key = ("insight", period_code, profile.today.isoformat())
    insight = cache.get(profile.telegram_id, key)
    if insight is None:
        insight = await insights.generate_insight(ai, summary.stats, summary.nutrition, currency=profile.currency, lang=profile.lang) or ""
        cache.put(profile.telegram_id, key, insight, 6 * 3600)
    if insight:
        await screen_mod.show_screen(bot, chat_id, f"{summary.text}\n\n{pe.IDEA} <i>{h(insight)}</i>", kb)


@router.message(Command("dashboard"))
@router.message(Command("report"))
async def cmd_dashboard(message: Message, state: FSMContext) -> None:
    profile = await get_profile(message.from_user)
    await state.clear()
    await safe_delete(message)
    await render_dashboard(message, profile, "7d")


@router.callback_query(F.data.in_({"menu:dashboard", "dash:7d", "dash:30d", "dash:90d"}))
async def cb_dashboard(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    await state.clear()
    code = callback.data.split(":")[-1] if callback.data.startswith("dash:") else "7d"
    await render_dashboard(callback, profile, code)


@router.callback_query(F.data.startswith("dash:kcal:") | F.data.startswith("dash:cats:"))
async def cb_chart(callback: CallbackQuery) -> None:
    profile = await get_profile(callback.from_user)
    _, kind, code = callback.data.split(":")
    days = _PERIODS.get(code, 7)
    await answer_now(callback, profile.tr("Строю график…", "Grafik tayyorlanmoqda…"))
    payload = await services.period_payload(profile, days)
    chart: bytes | None = None
    caption = ""
    try:
        if kind == "kcal":
            nutrition_profile = await services.nutrition_profile(profile.telegram_id)
            target = int((nutrition_profile or {}).get("daily_calories") or 0) or None
            chart = await asyncio.to_thread(
                charts_mod.calorie_trend_chart, payload["calorie_logs"], end_date=profile.today, days=days, target=target, tz=profile.tz, lang=profile.lang
            )
            caption = profile.tr("🍱 Калории по дням", "🍱 Kunlik kaloriya")
        else:
            stats = fin.compute_stats(payload["all_finance_entries"], fin.Period("custom", payload["start_date"], payload["end_date"], payload["start_date"], payload["start_date"]))
            items = [(cats.label(k, profile.lang, with_emoji=False), a) for k, a, _ in stats.by_category]
            chart = await asyncio.to_thread(charts_mod.expense_categories_chart, items, currency=profile.currency, lang=profile.lang)
            caption = profile.tr(f"🏷 Расходы по категориям — {days} дн.", f"🏷 Xarajatlar toifalar bo'yicha — {days} kun")
    except Exception:
        logger.exception("dashboard chart %s failed", kind)
    if not chart or callback.message is None:
        await answer_now(callback, profile.tr("Недостаточно данных для графика", "Grafik uchun ma'lumot yetarli emas"), alert=True)
        return
    await screen_mod.send_chart(callback.bot, callback.message.chat.id, BufferedInputFile(chart, filename=f"{kind}.png"), caption=caption)


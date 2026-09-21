"""Фоновая задача: авто-отчёт (раз в неделю по воскресеньям / раз в месяц 1-го числа)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

from . import cache
from . import insights
from . import services
from .context import ai, db, settings
from .handlers.common import profile_by_id
from .keyboards import back_to_menu_keyboard
from .profile import h
from .reports import build_summary
from . import emoji as pe

logger = logging.getLogger(__name__)


def _due_key(local_now: datetime, frequency: str) -> str | None:
    if frequency == "monthly":
        return f"{local_now.year:04d}-{local_now.month:02d}" if local_now.day == 1 else None
    if local_now.weekday() != 6:
        return None
    iso = local_now.isocalendar()
    return f"{int(iso[0]):04d}-W{int(iso[1]):02d}"


async def _send_report(bot: Bot, telegram_id: int, frequency: str, due_key: str) -> None:
    profile = await profile_by_id(telegram_id)
    days = 30 if frequency == "monthly" else 7
    payload, nutrition_profile = await asyncio.gather(services.period_payload(profile, days), services.nutrition_profile(telegram_id))
    title = profile.tr(
        "📊 <b>Месячный отчёт</b>" if frequency == "monthly" else "📊 <b>Недельный отчёт</b>",
        "📊 <b>Oylik hisobot</b>" if frequency == "monthly" else "📊 <b>Haftalik hisobot</b>",
    )
    summary = build_summary(profile, days=days, entries=payload["all_finance_entries"], logs=payload["calorie_logs"], nutrition_profile=nutrition_profile, title=title)
    text = summary.text
    insight = await insights.generate_insight(ai, summary.stats, summary.nutrition, currency=profile.currency, lang=profile.lang)
    if insight:
        text += f"\n\n{pe.IDEA} <i>{h(insight)}</i>"
    await bot.send_message(telegram_id, text, reply_markup=back_to_menu_keyboard(profile.lang))
    await db.save_report_preferences(telegram_id, enabled=True, frequency=frequency, last_sent_key=due_key)
    cache.invalidate(telegram_id, "report_prefs")


async def report_worker(bot: Bot) -> None:
    interval = max(300, settings.weekly_report_check_seconds)
    logger.info("Report worker started (interval=%ds)", interval)
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            users = await db.list_users()
            for user in users:
                telegram_id = int(user["telegram_id"])
                if not settings.is_allowed(telegram_id):
                    continue
                profile = await profile_by_id(telegram_id)
                local_now = now_utc.astimezone(profile.tz)
                if (local_now.hour, local_now.minute) < (settings.weekly_report_hour, settings.weekly_report_minute):
                    continue
                prefs = await db.get_report_preferences(telegram_id)
                if not prefs.get("enabled", True):
                    continue
                frequency = str(prefs.get("frequency") or "weekly")
                due_key = _due_key(local_now, frequency)
                if due_key is None or prefs.get("last_sent_key") == due_key:
                    continue
                try:
                    await _send_report(bot, telegram_id, frequency, due_key)
                except TelegramForbiddenError:
                    logger.info("Report skipped: user %s blocked the bot", telegram_id)
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(float(exc.retry_after) + 1)
                except Exception:
                    logger.exception("Report failed for %s", telegram_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Report worker iteration failed")
        await asyncio.sleep(interval)


async def _brief_tick(bot: Bot) -> None:
    from . import briefs
    from .keyboards import recurring_prompt_keyboard
    from . import screen as screen_mod
    from . import finance as fin

    if not db.available("user_settings"):
        return
    now_utc = datetime.now(timezone.utc)
    if settings.allowed_telegram_ids:
        user_ids = sorted(settings.allowed_telegram_ids)
    else:
        user_ids = [int(u["telegram_id"]) for u in await db.list_users()]
    for telegram_id in user_ids:
        profile = await profile_by_id(telegram_id)
        local_now = now_utc.astimezone(profile.tz)
        today_key = local_now.date().isoformat()
        minutes_now = local_now.hour * 60 + local_now.minute
        us = await services.user_settings(telegram_id)

        # --- утро: сводка + вопросы по регулярным платежам
        m_h, m_m = briefs.parse_hhmm(us.get("brief_morning_time"), (8, 0))
        if minutes_now >= m_h * 60 + m_m and us.get("last_morning_key") != today_key:
            await services.save_user_settings(telegram_id, {"last_morning_key": today_key})
            # после рестарта днём не шлём «утро» задним числом (окно 3 часа)
            if us.get("brief_morning", True) and minutes_now - (m_h * 60 + m_m) <= 180:
                try:
                    text = await briefs.morning_brief(profile)
                    await screen_mod.send_ephemeral(bot, telegram_id, text, keep_previous=True)
                except Exception:
                    logger.exception("morning brief failed for %s", telegram_id)
            if db.available("recurring_payments"):
                try:
                    items = await services.recurring(telegram_id)
                    for it in fin.recurring_due_today(items, local_now.date()):
                        q = profile.tr(
                            f"🔁 Оплатил <b>{it.get('title')}</b> — {fin.fmt_money(float(it.get('amount') or 0))} {profile.currency}?",
                            f"🔁 <b>{it.get('title')}</b> — {fin.fmt_money(float(it.get('amount') or 0))} {profile.currency} to'ladingizmi?",
                        )
                        await bot.send_message(telegram_id, q, reply_markup=recurring_prompt_keyboard(it["id"], profile.lang))
                        await db.update_recurring(telegram_id, it["id"], {"last_asked_key": local_now.strftime("%Y-%m")})
                    services.invalidate_recurring(telegram_id)
                except Exception:
                    logger.exception("recurring prompts failed for %s", telegram_id)

        # --- вечер
        e_h, e_m = briefs.parse_hhmm(us.get("brief_evening_time"), (21, 0))
        if minutes_now >= e_h * 60 + e_m and us.get("last_evening_key") != today_key:
            await services.save_user_settings(telegram_id, {"last_evening_key": today_key})
            if us.get("brief_evening", True) and minutes_now - (e_h * 60 + e_m) <= 120:
                try:
                    text = await briefs.evening_brief(profile)
                    if text:
                        await screen_mod.send_ephemeral(bot, telegram_id, text, keep_previous=True)
                except Exception:
                    logger.exception("evening brief failed for %s", telegram_id)


async def brief_worker(bot: Bot) -> None:
    logger.info("Brief worker started")
    tick = 0
    while True:
        try:
            tick += 1
            if db.missing_tables and tick % 10 == 1:
                # таблицы могли появиться после выполнения миграции — перепроверяем раз в 10 минут
                await db.health_check()
            await _brief_tick(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Brief worker iteration failed")
        await asyncio.sleep(60)


async def _reminder_tick(bot: Bot) -> None:
    """Напоминания (в т.ч. видео-уроки): раз в минуту, по локальному времени пользователя."""
    from . import reminders as rem
    from .keyboards import back_to_menu_keyboard

    now_utc = datetime.now(timezone.utc)
    try:
        rows = await db.list_reminders_all()
    except Exception:
        logger.debug("reminders table unavailable", exc_info=True)
        return
    for row in rows:
        telegram_id = int(row.get("telegram_id") or 0)
        if not settings.is_allowed(telegram_id):
            continue
        profile = await profile_by_id(telegram_id)
        local_now = now_utc.astimezone(profile.tz)
        today_key = local_now.date().isoformat()
        if str(row.get("last_sent_key") or "") == today_key:
            continue
        days = {int(d) for d in (row.get("days_of_week") or [1, 2, 3, 4, 5, 6, 7])}
        if (local_now.weekday() + 1) not in days:
            continue
        payload = rem.payload_of(row)
        if payload.get("date") and str(payload["date"])[:10] != today_key:
            continue
        hhmm = str(row.get("reminder_time") or "")[:5]
        try:
            hh, mm = (int(x) for x in hhmm.split(":"))
        except Exception:
            continue
        minutes_now = local_now.hour * 60 + local_now.minute
        if minutes_now < hh * 60 + mm or minutes_now - (hh * 60 + mm) > 180:
            if minutes_now - (hh * 60 + mm) > 180:
                await db.update_reminder(telegram_id, row["id"], {"last_sent_key": today_key})
            continue
        text, next_idx = rem.message(row)
        try:
            await bot.send_message(telegram_id, text, reply_markup=back_to_menu_keyboard(profile.lang), link_preview_options=LinkPreviewOptions(is_disabled=False, prefer_large_media=True))
        except Exception:
            logger.exception("reminder send failed for %s", telegram_id)
            continue
        fields: dict = {"last_sent_key": today_key}
        payload["idx"] = next_idx
        if payload.get("once"):
            fields["enabled"] = False
        fields["reminder_text"] = rem.encode(payload)
        await db.update_reminder(telegram_id, row["id"], fields)
        services.invalidate_reminders(telegram_id)


async def _task_tick(bot: Bot) -> None:
    """Задачи со временем («позвонить маме в 18:00») — напоминание в срок."""
    from . import proactive
    from .keyboards import back_to_menu_keyboard

    if not db.available("tasks"):
        return
    try:
        rows = await db.list_tasks_all()
    except Exception:
        logger.debug("tasks table unavailable", exc_info=True)
        return
    by_user: dict[int, list[dict]] = {}
    for row in rows:
        by_user.setdefault(int(row.get("telegram_id") or 0), []).append(row)
    for telegram_id, tasks in by_user.items():
        if not settings.is_allowed(telegram_id):
            continue
        profile = await profile_by_id(telegram_id)
        now = datetime.now(timezone.utc).astimezone(profile.tz)
        for t in proactive.due_tasks(tasks, now):
            text = f"📝 <b>{profile.tr('Напоминание', 'Eslatma')}:</b> {h(t.get('text'))}"
            try:
                await bot.send_message(telegram_id, text, reply_markup=back_to_menu_keyboard(profile.lang))
            except Exception:
                logger.exception("task reminder failed for %s", telegram_id)
                continue
            await db.update_task(telegram_id, t["id"], {"notified_key": now.date().isoformat()})
        services.invalidate(telegram_id, "tasks")


async def _proactive_tick(bot: Bot) -> None:
    """Подсказки по правилам (bot/proactive.py): раз в 10 минут, только новые (журнал alerts_log)."""
    from . import proactive
    from . import screen as screen_mod
    from .keyboards import _btn, back_to_menu_keyboard
    from aiogram.types import InlineKeyboardMarkup

    if not db.available("alerts_log") or not db.available("user_settings"):
        return
    user_ids = sorted(settings.allowed_telegram_ids) or [int(u["telegram_id"]) for u in await db.list_users()]
    for telegram_id in user_ids:
        profile = await profile_by_id(telegram_id)
        us = await services.user_settings(telegram_id)
        if not us.get("proactive", True):
            continue
        try:
            alerts = await proactive.collect(profile)
        except Exception:
            logger.exception("proactive collect failed for %s", telegram_id)
            continue
        for alert in alerts:
            try:
                if await db.alert_was_sent(telegram_id, alert.key):
                    continue
                await db.mark_alert_sent(telegram_id, alert.key)  # сначала отмечаем — не задвоим при ошибке отправки
                text = alert.text
                if alert.prompt:
                    from . import agent_tools
                    from .handlers.agent import render_reply, run_agent

                    result = await run_agent(profile, alert.prompt, [], snapshot=await agent_tools.snapshot(profile))
                    text = render_reply(result.text) if result.text else None
                if not text:
                    continue
                if alert.copy_text:
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [_btn("📋 " + profile.tr("Скопировать сообщение", "Xabarni nusxalash"), copy_text=alert.copy_text[:256])],
                        [_btn(profile.tr("В меню", "Menyuga"), "menu:open")],
                    ])
                else:
                    kb = back_to_menu_keyboard(profile.lang)
                if alert.persistent:
                    await bot.send_message(telegram_id, text, reply_markup=kb)
                else:
                    await screen_mod.send_ephemeral(bot, telegram_id, text, reply_markup=kb, keep_previous=True)
                logger.info("proactive alert %s sent to %s", alert.key, telegram_id)
            except Exception:
                logger.exception("proactive alert %s failed for %s", alert.key, telegram_id)


async def proactive_worker(bot: Bot) -> None:
    logger.info("Proactive worker started")
    await asyncio.sleep(20)
    while True:
        try:
            await _proactive_tick(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Proactive worker iteration failed")
        await asyncio.sleep(600)


async def reminder_worker(bot: Bot) -> None:
    logger.info("Reminder worker started")
    while True:
        try:
            await _reminder_tick(bot)
            await _task_tick(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reminder worker iteration failed")
        await asyncio.sleep(60)


__all__ = ["report_worker", "brief_worker", "reminder_worker", "proactive_worker"]

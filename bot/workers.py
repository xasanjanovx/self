"""Фоновая задача: авто-отчёт (раз в неделю по воскресеньям / раз в месяц 1-го числа)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

from . import cache
from . import services
from .context import db, settings
from .handlers.common import profile_by_id
from .keyboards import back_to_menu_keyboard
from .profile import h
from .reports import build_summary

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


async def send_morning(bot: Bot, profile, us: dict | None = None, *, late_ok: bool = True) -> bool:  # noqa: ANN001
    """Утренняя сводка + вопросы по регулярным платежам — один раз в день.

    Зовётся по расписанию и сразу после подтверждённого подъёма (если утро «после подъёма»).
    """
    from . import briefs
    from . import finance as fin
    from . import screen as screen_mod
    from .keyboards import recurring_prompt_keyboard

    telegram_id = profile.telegram_id
    us = us if us is not None else await services.user_settings(telegram_id)
    local_now = profile.now
    today_key = local_now.date().isoformat()
    if us.get("last_morning_key") == today_key:
        return False
    await services.save_user_settings(telegram_id, {"last_morning_key": today_key})
    if us.get("brief_morning", True) and late_ok:
        try:
            text = await briefs.morning_brief(profile)
            p = await services.persona(telegram_id)
            from . import voice_brief

            # «Утро голосом» — Джарвис рассказывает сам; не вышло — обычный текст
            if not (p.morning_voice and await voice_brief.send(bot, profile, p, text)):
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
    return True


async def _brief_tick(bot: Bot) -> None:
    from . import briefs
    from . import screen as screen_mod

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

        # --- утро: в заданное время или «после подъёма» (её шлёт wake_runner.mark_awake;
        # если в этот день будильника нет — в 08:00; если не проснулся — через 2 ч после такбира)
        raw_morning = str(us.get("brief_morning_time") or "08:00")
        if raw_morning == "wake":
            m_h, m_m = 8, 0
            try:
                from . import wake_runner

                _, plan = await wake_runner.plan_for(profile)
                if plan.active and plan.takbir_at:
                    late = plan.takbir_at + timedelta(hours=2)
                    m_h, m_m = late.hour, late.minute
            except Exception:
                logger.debug("wake plan for morning brief failed", exc_info=True)
        else:
            m_h, m_m = briefs.parse_hhmm(raw_morning, (8, 0))
        if minutes_now >= m_h * 60 + m_m and us.get("last_morning_key") != today_key:
            # после рестарта днём не шлём «утро» задним числом (окно 3 часа)
            await send_morning(bot, profile, us, late_ok=minutes_now - (m_h * 60 + m_m) <= 180)

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
        hints_on = us.get("proactive", True)
        if not hints_on and not (await services.persona(telegram_id)).alert_calls:
            continue
        try:
            alerts = await proactive.collect(profile)
        except Exception:
            logger.exception("proactive collect failed for %s", telegram_id)
            continue
        fresh: list = []
        for alert in alerts:
            try:
                if await db.alert_was_sent(telegram_id, alert.key):
                    continue
                await db.mark_alert_sent(telegram_id, alert.key)  # сначала отмечаем — не задвоим при ошибке отправки
                fresh.append(alert)
                if not hints_on:
                    continue  # подсказки выключены — только звонок о важном (ниже)
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
        try:
            await _maybe_alert_call(profile, fresh)
        except Exception:
            logger.exception("alert call failed for %s", telegram_id)


# что достойно звонка: срок долга сегодня/просрочен, лимит под угрозой или превышен, цель отстаёт
IMPORTANT_ALERTS = ("debt_today", "debt_overdue", "budget_proj", "goal_pace", "goal_over", "goal_day")
ALERT_CALL_HOURS = (10, 21)


def important_alerts(alerts: list) -> list:
    return [a for a in alerts if str(a.key).split(":", 1)[0] in IMPORTANT_ALERTS and a.text]


async def _maybe_alert_call(profile, fresh: list) -> bool:  # noqa: ANN001
    """«Джарвис сам звонит, если важное» — только если включено в настройках Джарвиса,
    не чаще раза в день и в разумное время. Тексты подсказок при этом приходят как обычно."""
    import re

    from . import call_assistant
    from . import caller

    important = important_alerts(fresh)
    if not important or not caller.available():
        return False
    p = await services.persona(profile.telegram_id)
    now = profile.now
    if not p.alert_calls or not ALERT_CALL_HOURS[0] <= now.hour < ALERT_CALL_HOURS[1]:
        return False
    row = await db.get_assistant_settings(profile.telegram_id)
    if str(row.get("alert_call_day") or "")[:10] == now.date().isoformat():
        return False
    await services.save_persona(profile.telegram_id, {"alert_call_day": now.date().isoformat()})
    facts = "; ".join(re.sub(r"<[^>]+>", "", a.text) for a in important[:4])
    topic = ("ВАЖНОЕ (ты звонишь сама, потому что это важно): " + facts
             + ". Коротко объясни, что случилось, и предложи 1–2 конкретных шага; спроси, что сделать.")
    logger.info("alert call for %s: %s", profile.telegram_id, [a.key for a in important])
    return call_assistant.call_in_background(profile, topic=topic) is not None


async def _wake_tick(bot: Bot) -> None:
    """Подъём на фаджр: раз в 20 секунд смотрим, не пора ли звонить (и перезванивать)."""
    from . import wake as wake_mod
    from . import wake_runner

    if not db.available("wake_settings") or not db.available("wake_log"):
        return
    user_ids = sorted(settings.allowed_telegram_ids) or [int(u["telegram_id"]) for u in await db.list_users()]
    now_utc = datetime.now(timezone.utc)
    for telegram_id in user_ids:
        try:
            profile = await profile_by_id(telegram_id)
            s, plan = await wake_runner.plan_for(profile)
            if not plan.active:
                continue
            log = await services.wake_log(telegram_id, plan.day)
            snoozed = wake_runner.snoozed_until(telegram_id)
            if snoozed and now_utc < snoozed:
                continue
            state_last = None
            if log and log.get("attempts"):
                # последняя попытка держится в памяти процесса; после рестарта считаем, что пауза прошла
                state_last = wake_runner._active.get(telegram_id, {}).get("last")
            ok, reason = wake_mod.should_call(plan, {**(log or {}), "last_attempt_at": state_last}, now_utc, s)
            if not ok:
                continue
            await wake_runner.run_attempt(bot, profile, s, plan, log)
        except Exception:
            logger.exception("wake tick failed for %s", telegram_id)


async def wake_worker(bot: Bot) -> None:
    from . import caller

    logger.info("Wake worker started (caller: %s)", caller.status())
    if caller.configured():
        await caller.start()
    while True:
        try:
            await _wake_tick(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Wake worker iteration failed")
        await asyncio.sleep(20)


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
            from . import screen as screen_mod

            await screen_mod.sweep(bot)  # временные сообщения с вышедшим сроком (переживает перезапуск)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reminder worker iteration failed")
        await asyncio.sleep(60)


__all__ = ["report_worker", "brief_worker", "reminder_worker", "proactive_worker", "wake_worker"]

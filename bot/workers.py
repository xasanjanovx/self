"""Фоновая задача: авто-отчёт (раз в неделю по воскресеньям / раз в месяц 1-го числа)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

from . import access, blocked
from . import cache
from . import services
from .context import db, settings
from .handlers.common import profile_by_id
from .profile import h
from .reports import build_digest

logger = logging.getLogger(__name__)

NOTE_MIN_AGE_S = 600   # свежее напоминание нельзя стереть нажатием кнопки в первые 10 минут — чтобы он успел его увидеть


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
    text = build_digest(profile, days=days, entries=payload["all_finance_entries"], logs=payload["calorie_logs"],
                        nutrition_profile=nutrition_profile, title=title)
    # 07.10: короткий отчёт с выводами вместо простыни цифр; заметка — исчезает при нажатии любой кнопки и сама через сутки.
    # Данных нет — не пишем (но неделя засчитана)
    from . import screen as screen_mod

    if text:
        await screen_mod.send_note(bot, telegram_id, text, ttl=24 * 3600)
    await db.save_report_preferences(telegram_id, enabled=True, frequency=frequency, last_sent_key=due_key)
    cache.invalidate(telegram_id, "report_prefs")


async def report_worker(bot: Bot) -> None:
    interval = max(300, settings.weekly_report_check_seconds)
    logger.info("Report worker started (interval=%ds)", interval)
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            users = [u for u in await db.list_users() if not blocked.is_blocked(u.get("telegram_id"))]
            for user in users:
                telegram_id = int(user["telegram_id"])
                if not access.is_allowed(telegram_id):
                    continue
                profile = await profile_by_id(telegram_id)
                local_now = now_utc.astimezone(profile.tz)
                if (local_now.hour, local_now.minute) < (settings.weekly_report_hour, settings.weekly_report_minute):
                    continue
                prefs = await db.get_report_preferences(telegram_id)
                frequency = str(prefs.get("frequency") or "weekly")
                due_key = _due_key(local_now, frequency)
                if prefs.get("enabled", True) and due_key is not None and prefs.get("last_sent_key") != due_key:
                    try:
                        if frequency == "weekly" and access.is_owner(telegram_id):
                            # владельцу в воскресенье приходят «Итоги недели» (bot/weekly.py) — отдельный отчёт за те же 7 дней
                            # был бы вторым сообщением о том же (07.10)
                            await db.save_report_preferences(telegram_id, enabled=True, frequency=frequency, last_sent_key=due_key)
                            cache.invalidate(telegram_id, "report_prefs")
                        else:
                            await _send_report(bot, telegram_id, frequency, due_key)
                    except TelegramForbiddenError:
                        logger.info("Report skipped: user %s blocked the bot", telegram_id)
                    except TelegramRetryAfter as exc:
                        await asyncio.sleep(float(exc.retry_after) + 1)
                    except Exception:
                        logger.exception("Report failed for %s", telegram_id)
                # 29.09 его выбор: в воскресенье вечером — итоги недели голосом JES (деньги, задачи, уроки, подъёмы)
                week_key = _due_key(local_now, "weekly")
                if week_key is not None and access.is_owner(telegram_id):
                    try:
                        from . import weekly

                        await weekly.maybe_send(bot, profile, week_key)
                        # 02.10 его решение: воскресной планёрки (3 цели недели) больше нет — по просьбе остаётся инструмент week_plan
                    except TelegramForbiddenError:
                        pass
                    except Exception:
                        logger.exception("weekly voice failed for %s", telegram_id)
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
            text = await briefs.morning_brief(profile)   # None — сегодня нечего сказать, молчим (07.10)
            p = await services.persona(telegram_id)
            from . import voice_brief

            # «Утро голосом» — Джарвис рассказывает сам; не вышло — обычный текст
            if text and not (p.morning_voice and await voice_brief.send(bot, profile, p, text)):
                await screen_mod.send_note(bot, telegram_id, text, ttl=10 * 3600)
        except TelegramForbiddenError:
            logger.info("morning brief skipped: user %s blocked the bot", telegram_id)  # не ошибка — он заблокировал бота
        except Exception:
            logger.exception("morning brief failed for %s", telegram_id)
    if late_ok and access.is_owner(telegram_id) and us.get("day_plan", True):
        try:
            from . import plan

            await plan.send(bot, profile, "morning")  # 30.09 его выбор: утром готовый мягкий план дня
        except TelegramForbiddenError:
            pass
        except Exception:
            logger.exception("day plan failed for %s", telegram_id)
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
    user_ids = access.user_ids() or [int(u["telegram_id"]) for u in await db.list_users()]
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
                    text = await briefs.evening_brief(profile)   # None — ничего важного, молчим (07.10)
                    if text:
                        await screen_mod.send_note(bot, telegram_id, text, ttl=9 * 3600)   # к утру сама исчезнет
                except TelegramForbiddenError:
                    logger.info("evening brief skipped: user %s blocked the bot", telegram_id)
                except Exception:
                    logger.exception("evening brief failed for %s", telegram_id)
                # 02.10 его решение: вечернего разбора дня («отчёта» по плану) больше нет


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

    now_utc = datetime.now(timezone.utc)
    try:
        rows = [r for r in await db.list_reminders_all() if not blocked.is_blocked(r.get("telegram_id"))]
    except Exception:
        logger.debug("reminders table unavailable", exc_info=True)
        return
    for row in rows:
        telegram_id = int(row.get("telegram_id") or 0)
        if not access.is_allowed(telegram_id):
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
        # 07.10: напоминание — заметка (он стирал их руками): исчезает при нажатии кнопки (не раньше чем через 10 минут) и через 10 часов
        from . import screen as screen_mod

        if await screen_mod.send_note(bot, telegram_id, text, ttl=10 * 3600, min_age=NOTE_MIN_AGE_S,
                                      link_preview_options=LinkPreviewOptions(is_disabled=False, prefer_large_media=True)) is None:
            logger.error("reminder send failed for %s", telegram_id)
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

    if not db.available("tasks"):
        return
    try:
        rows = [r for r in await db.list_tasks_all() if not blocked.is_blocked(r.get("telegram_id"))]
    except Exception:
        logger.debug("tasks table unavailable", exc_info=True)
        return
    by_user: dict[int, list[dict]] = {}
    for row in rows:
        by_user.setdefault(int(row.get("telegram_id") or 0), []).append(row)
    for telegram_id, tasks in by_user.items():
        if not access.is_allowed(telegram_id):
            continue
        profile = await profile_by_id(telegram_id)
        now = datetime.now(timezone.utc).astimezone(profile.tz)
        for t in proactive.due_tasks(tasks, now):
            text = f"📝 <b>{profile.tr('Напоминание', 'Eslatma')}:</b> {h(t.get('text'))}"
            from . import screen as screen_mod

            if await screen_mod.send_note(bot, telegram_id, text, ttl=10 * 3600, min_age=NOTE_MIN_AGE_S) is None:
                logger.error("task reminder failed for %s", telegram_id)
                continue
            await db.update_task(telegram_id, t["id"], {"notified_key": now.date().isoformat()})
        services.invalidate(telegram_id, "tasks")


async def _proactive_tick(bot: Bot) -> None:
    """Подсказки по правилам (bot/proactive.py): раз в 10 минут, только новые (журнал alerts_log)."""
    from . import debt_tasks, proactive
    from . import screen as screen_mod
    from .keyboards import _btn, back_to_menu_keyboard
    from aiogram.types import InlineKeyboardMarkup

    if not db.available("alerts_log") or not db.available("user_settings"):
        return
    user_ids = access.user_ids() or [int(u["telegram_id"]) for u in await db.list_users()]
    for telegram_id in user_ids:
        profile = await profile_by_id(telegram_id)
        await debt_tasks.sync_safe(profile)  # долг со сроком → задача за день до срока (независимо от настройки подсказок)
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
                    await bot.send_message(telegram_id, text, reply_markup=kb)   # долг со сроком — действие, не заметка
                else:
                    await screen_mod.send_note(bot, telegram_id, text, kb, ttl=8 * 3600)
                logger.info("proactive alert %s sent to %s", alert.key, telegram_id)
            except Exception:
                logger.exception("proactive alert %s failed for %s", alert.key, telegram_id)
        try:
            await _maybe_alert_call(profile, fresh)
        except Exception:
            logger.exception("alert call failed for %s", telegram_id)
        if hints_on and access.is_owner(telegram_id):
            try:
                from . import advice

                await advice.maybe_send(bot, profile)  # 30.09 его выбор: советы днём, не чаще 3 раз
            except TelegramForbiddenError:
                pass
            except Exception:
                logger.exception("advice failed for %s", telegram_id)


# что достойно звонка: срок долга сегодня/просрочен, лимит под угрозой или превышен, цель отстаёт
IMPORTANT_ALERTS = ("debt_today", "debt_overdue", "budget_proj", "goal_pace", "goal_over", "goal_day")
ALERT_CALL_HOURS = (10, 21)


def important_alerts(alerts: list) -> list:
    return [a for a in alerts if str(a.key).split(":", 1)[0] in IMPORTANT_ALERTS and a.text]


async def _maybe_alert_call(profile, fresh: list) -> bool:  # noqa: ANN001
    """«JES сам звонит, если важное» — только если включено в настройках JES,
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
    from . import app_alarm
    from . import wake_runner

    if not db.available("wake_settings") or not db.available("wake_log"):
        return
    user_ids = access.user_ids() or [int(u["telegram_id"]) for u in await db.list_users()]
    now_utc = datetime.now(timezone.utc)
    for telegram_id in user_ids:
        if telegram_id in _wake_calls:
            continue  # звонок этому человеку ещё идёт
        try:
            profile = await profile_by_id(telegram_id)
            try:
                await app_alarm.evening_guard(bot, profile)  # вечером: будильник на завтра выключен / не стоит в телефоне — скажем сейчас
            except Exception:
                logger.warning("alarm guard failed for %s", telegram_id, exc_info=True)
            try:
                from . import watch

                await watch.evening_guard(bot, profile)  # часы сняты / почти разряжены — утром не провибрируют
            except Exception:
                logger.warning("watch guard failed for %s", telegram_id, exc_info=True)
            s, plan = await wake_runner.plan_for(profile)
            if not plan.active:
                continue
            log = await services.wake_log(telegram_id, plan.day)
            snoozed = wake_runner.snoozed_until(telegram_id)
            if snoozed and now_utc < snoozed:
                continue
            # 04.10 его слова: «он должен будить по звонку Telegram ВСЕГДА» — звонок не ждёт будильник в приложении
            state_last = None
            if log and log.get("attempts"):
                # последняя попытка держится в памяти процесса; после рестарта считаем, что пауза прошла
                state_last = wake_runner._active.get(telegram_id, {}).get("last")
            ok, reason = wake_mod.should_call(plan, {**(log or {}), "last_attempt_at": state_last}, now_utc, s)
            if not ok:
                continue
            # у каждого свой звонок фоном: пока идёт разговор с одним, другого будим вовремя
            task = asyncio.create_task(wake_runner.run_attempt(bot, profile, s, plan, log), name=f"wake-{telegram_id}")
            _wake_calls[telegram_id] = task
            task.add_done_callback(lambda t, uid=telegram_id: _wake_done(uid, t))
        except Exception:
            logger.exception("wake tick failed for %s", telegram_id)


_wake_calls: dict[int, asyncio.Task] = {}


def _wake_done(uid: int, task: asyncio.Task) -> None:
    if _wake_calls.get(uid) is task:
        _wake_calls.pop(uid, None)
    if not task.cancelled() and task.exception() is not None:
        logger.error("wake attempt failed for %s", uid, exc_info=task.exception())


async def wake_worker(bot: Bot) -> None:
    from . import caller

    logger.info("Wake worker started (caller: %s)", caller.status())
    if caller.configured():
        await caller.start()
    try:
        from . import alarm_voice
        from . import phone_api

        owner = phone_api.owner_id()
        if owner is not None:
            async def warm_voices(uid: int) -> None:
                from . import nudge_voice

                await alarm_voice.warm(uid)   # фразы будильника записаны заранее
                await nudge_voice.warm(uid)   # и нейтральные фразы экранного времени — по очереди (у озвучки лимит в минуту)

            asyncio.create_task(warm_voices(owner), name="voices-warm")
    except Exception:
        logger.warning("alarm voice warm-up not started", exc_info=True)
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


async def _daily_tick(bot: Bot) -> None:
    """Каждый день (29.09): в срок — «🔁 Урок английского» с кнопками [✅ Сделал] [⏭ Не сегодня] и, если есть ссылка или
    он смотрел YouTube, [▶️ Продолжить урок с 12:34] — ролик открывается с той секунды, где остановился."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from . import daily_tasks, lessons
    from .keyboards import _btn

    for telegram_id in access.user_ids():
        try:
            profile = await profile_by_id(telegram_id)
            now = profile.now
            for item in daily_tasks.due(telegram_id, now):
                daily_tasks.mark_reminded(telegram_id, item, now.date())  # сначала отметка — не повторим, даже если отправка упадёт
                rows = [[_btn("✅ " + profile.tr("Сделал", "Bajardim"), f"daily:done:{item['id']}", style="success"),
                         _btn("⏭ " + profile.tr("Не сегодня", "Bugun emas"), f"daily:skip:{item['id']}")]]
                if item.get("link"):
                    res = await lessons.resume(telegram_id, link=item["link"])
                    if not res.get("error"):
                        label = profile.tr("Продолжить урок", "Darsni davom ettirish") + (f" · {res['position']}" if res.get("start") else "")
                        rows.append([InlineKeyboardButton(text="▶️ " + label, url=lessons.url(res))])
                extra = daily_tasks.progress(item, now.date())
                text = f"🔁 <b>{h(item['title'])}</b>" + (f"\n{extra}" if extra else "")
                await bot.send_message(telegram_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except TelegramForbiddenError:
            logger.info("daily skipped: user %s blocked the bot", telegram_id)
        except Exception:
            logger.exception("daily reminder failed for %s", telegram_id)


async def reminder_worker(bot: Bot) -> None:
    logger.info("Reminder worker started")
    while True:
        try:
            await _reminder_tick(bot)
            await _task_tick(bot)
            await _daily_tick(bot)
            from . import screen as screen_mod

            await screen_mod.sweep(bot)  # временные сообщения с вышедшим сроком (переживает перезапуск)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reminder worker iteration failed")
        await asyncio.sleep(60)


__all__ = ["report_worker", "brief_worker", "reminder_worker", "proactive_worker", "wake_worker"]

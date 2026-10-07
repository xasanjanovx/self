"""Умные сводки утром и вечером (07.10).

Он сказал: «слишком много лишних сообщений, текста я даже не читаю и удаляю каждый раз — сделай намного умнее». Раньше вечерняя сводка
(21:00) была простынёй: деньги, питание, цели, экранное время, расход на ИИ, промахи JES, клиенты — и почти всё это уже есть на главном
экране. Теперь сводка — это ТОЛЬКО то, что требует внимания (срок долга, лимит почти исчерпан, дела не закрыты, цель отстаёт, не записано,
что-то необычное), не больше четырёх строк, отсортированных по важности. Ничего важного — сообщения нет вовсе. Приходит она заметкой
(screen.send_note): исчезает при нажатии любой кнопки главного меню и сама через несколько часов.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any

from . import access
from . import finance as fin
from . import nutrition as nutri
from . import services
from . import tasks as tasks_mod
from .profile import Profile, h

logger = logging.getLogger(__name__)

EVENING_MAX = 4
MORNING_MAX = 5
DEBT_DAYS_EVENING = 1     # вечером про долг — если срок сегодня/завтра или прошёл
DEBT_DAYS_MORNING = 3
SCREEN_HEAVY_MIN = 240    # экран в телефоне за день, с которого вечером об этом напоминаем (4 часа)
AI_SPEND_FLOOR_USD = 1.0  # расход на ИИ за день, о котором владельцу стоит знать (ниже — не пишем)
MISSES_MIN = 3            # серьёзных сбоев JES за день, с которых вечером о них напоминаем
SERIOUS_MISSES = {"wrong_call", "tool_error", "app_crash", "agent_error"}   # «переспросил» и «не тот язык» — мелочь, в сводку не идут


def _item(weight: int, text: str) -> tuple[int, str]:
    return weight, text


def render(head: str, summary: str, items: list[tuple[int, str]], *, limit: int) -> str | None:
    """Шапка + одна строка итога + самые важные строки. Нет строк — None (сообщения нет)."""
    top = [t for _, t in sorted(dict.fromkeys(items), key=lambda it: -it[0])][:limit]
    if not top:
        return None
    return "\n".join([head, *([summary] if summary else []), *top])


def _titles(rows: list[dict[str, Any]], n: int = 2) -> str:
    return "; ".join(h(str(r.get("text") or r.get("title") or "")[:40]) for r in rows[:n])


# ------------------------------------------------------------------ общие куски
def debt_items(profile: Profile, rows: list[dict[str, Any]], today: date, *, within: int) -> list[tuple[int, str]]:
    uz = profile.lang == "uz"
    out: list[tuple[int, str]] = []
    for r in rows:
        try:
            d = date.fromisoformat(str(r.get("due_date"))[:10])
        except ValueError:
            continue
        left = (d - today).days
        if left > within or left < -30:
            continue
        who = h(r.get("person")) + (f" {fin.fmt_money(float(r['amount']))}" if r.get("amount") else "")
        if left < 0:
            out.append(_item(95, f"⚠️ {who}: " + (f"muddat {-left} kun oldin o'tgan" if uz else f"срок прошёл {-left} дн. назад")))
        elif left == 0:
            out.append(_item(92, f"⏳ {who}: " + ("bugun qaytarish muddati" if uz else "сегодня срок возврата")))
        else:
            out.append(_item(85, f"⏳ {who}: " + (f"qaytarish {d:%d.%m}" if uz else f"возврат {d:%d.%m}")))
    return out


def budget_items(profile: Profile, month: fin.Stats | None, limits: dict[str, float]) -> list[tuple[int, str]]:
    if not limits or month is None:
        return []
    return [_item(80, line) for line in fin.budget_warnings(fin.budget_statuses(month, limits), lang=profile.lang)[:2]]


def payment_items(profile: Profile, recurring: list[dict[str, Any]], today: date) -> list[tuple[int, str]]:
    """Регулярные платежи, срок которых сегодня или в ближайшие 3 дня, — одной строкой."""
    if not recurring:
        return []
    uz = profile.lang == "uz"
    _, pending = fin.recurring_remaining(recurring, today)
    parts: list[str] = []
    for p in pending:
        day = fin.recurring_due_day(int(p.get("day_of_month") or 1), today.year, today.month)
        left = day - today.day
        if 0 <= left <= 3:
            when = ("bugun" if uz else "сегодня") if left == 0 else (f"{left} kundan keyin" if uz else f"через {left} дн.")
            parts.append(f"{h(p.get('title'))} {fin.fmt_money(float(p.get('amount') or 0))} ({when})")
    if not parts:
        return []
    return [_item(75, f"🔁 {'To`lovlar' if uz else 'Платежи'}: " + "; ".join(parts[:3]))]


def task_items(profile: Profile, tasks: list[dict[str, Any]], today: date, *, evening: bool) -> list[tuple[int, str]]:
    uz = profile.lang == "uz"
    g = tasks_mod.group(tasks, today)
    out: list[tuple[int, str]] = []
    if g.overdue:
        out.append(_item(65, f"⚠️ {'Muddati o`tgan' if uz else 'Просрочено'} ({len(g.overdue)}): {_titles(g.overdue)}"))
    if g.today:
        label = ("Yopilmagan" if uz else "Не закрыто сегодня") if evening else ("Bugun" if uz else "Сегодня")
        more = f" +{len(g.today) - 2}" if len(g.today) > 2 else ""
        out.append(_item(60 if evening else 70, f"📝 {label} ({len(g.today)}): {_titles(g.today)}{more}"))
    return out


async def goal_items(profile: Profile, *, evening: bool) -> list[tuple[int, str]]:
    """Цели: вечером — только где что-то не так (⚠️), утром — шаг на сегодня (одна строка)."""
    from . import goals as goals_mod

    goals = await services.goals(profile.telegram_id)
    if not goals:
        return []
    try:
        statuses, data = await goals_mod.statuses_for(profile, goals=goals)
        lines = goals_mod.plan_lines(statuses, profile.lang, meals=data.meals, when="evening" if evening else "morning")
    except Exception:
        logger.debug("digest: цели", exc_info=True)
        return []
    if evening:
        return [_item(40, ln) for ln in [x for x in lines if x.startswith("⚠️")][:2]]
    return [_item(30, ln) for ln in lines[:1]]


def ops_items(profile: Profile) -> list[tuple[int, str]]:
    """Владельцу: расход на ИИ, промахи JES и клиенты — только когда есть что-то необычное (раньше — каждый вечер, всё подряд)."""
    if not access.is_owner(profile.telegram_id):
        return []
    from . import billing, journal

    uz = profile.lang == "uz"
    out: list[tuple[int, str]] = []
    try:
        s = billing.status()
        spent, limit = float(s.get("spent_today_usd") or 0), float(billing.limit_usd() or 0)
        if s.get("need_topup") or s.get("exhausted"):
            left = s.get("balance_usd")
            out.append(_item(88, "💳 " + ("Gemini balansi tugayapti" if uz else "Баланс Gemini заканчивается") + (f" (~${float(left):.2f})" if left is not None else "")))
        elif spent >= max(AI_SPEND_FLOOR_USD, limit * 1.5 if limit else 0):
            out.append(_item(70, f"🤖 {'AI bugun' if uz else 'ИИ сегодня'} ${spent:.2f}" + (f" ({'limit' if uz else 'лимит'} ${limit:.2f})" if limit else "")))
    except Exception:
        logger.debug("digest: расход на ИИ", exc_info=True)
    try:
        journal.cleanup()
        misses = [i for i in journal.day_entries(profile.today, profile.telegram_id) if i.get("kind") in SERIOUS_MISSES]
        if len(misses) >= MISSES_MIN:
            out.append(_item(65, f"🛠 JES: {'bugun xatolar' if uz else 'сбоев за день'} — {len(misses)} · {'JES sozlamalari' if uz else 'Настройки JES'}"))
    except Exception:
        logger.debug("digest: журнал", exc_info=True)
    try:
        over = [r for r in billing.clients_report(30) if r["today"] >= billing.CLIENT_DAILY_LIMIT_USD > 0]
        if over:
            out.append(_item(45, f"👥 {'Mijoz limiti oshdi' if uz else 'Клиент превысил дневной лимит'}: {len(over)}"))
    except Exception:
        logger.debug("digest: клиенты", exc_info=True)
    return out


# ------------------------------------------------------------------ вечер
async def evening(profile: Profile) -> str | None:
    """«🌙 Итог дня» — только то, что требует внимания; ничего — None (сообщения нет)."""
    uid, uz, today = profile.telegram_id, profile.lang == "uz", profile.today
    snap, logs, recent_logs, nprofile, limits, tasks, deadlines = await asyncio.gather(
        services.finance_snapshot(profile), services.today_calorie_logs(profile), services.calorie_logs(profile, 3),
        services.nutrition_profile(uid), services.budgets(uid), services.tasks(uid), services.debt_due_rows(uid))
    items: list[tuple[int, str]] = []
    items += debt_items(profile, deadlines, today, within=DEBT_DAYS_EVENING)
    items += budget_items(profile, snap.month, limits)
    items += task_items(profile, tasks, today, evening=True)
    items += await goal_items(profile, evening=True)
    items += ops_items(profile)

    totals = nutri.totals(logs)
    eaten = int(totals["calories"])
    target = int((nprofile or {}).get("daily_calories") or 0)
    # не записано — только тем, кто обычно записывает (последние дни что-то было), чтобы новичка не пилить каждый вечер
    yesterday_start = today - timedelta(days=3)
    wrote_money = bool(fin.entries_between(snap.entries, yesterday_start, today - timedelta(days=1)))
    wrote_food = any(str(r.get("created_at") or "")[:10] < today.isoformat() for r in recent_logs)
    missing = []
    if not snap.today_entries and wrote_money:
        missing.append("xarajatlar" if uz else "расходы")
    if nprofile and not logs and wrote_food:
        missing.append("ovqat" if uz else "еда")
    if missing:
        items.append(_item(50, f"✍️ {'Bugun yozilmagan' if uz else 'Сегодня не записано'}: " + (" va " if uz else " и ").join(missing)))
    if nprofile and target and logs and (eaten > target * 1.25 or eaten < target * 0.6):
        items.append(_item(35, f"🍽 {eaten} / {target} {'kkal' if uz else 'ккал'} — " + (("me'yordan ko'p" if eaten > target else "me'yordan kam") if uz else ("выше нормы" if eaten > target else "ниже нормы"))))
    try:
        from . import screentime

        total = screentime.today_total(profile)
        if total is not None and total >= SCREEN_HEAVY_MIN:
            items.append(_item(30, f"📱 {'Telefonda bugun' if uz else 'В телефоне сегодня'} {screentime.fmt_min(total, uz)}"))
    except Exception:
        logger.debug("digest: экранное время", exc_info=True)

    parts = []
    if snap.today_expense:
        parts.append(f"💸 {fin.fmt_money(snap.today_expense)} {profile.currency}")
    if logs:
        parts.append(f"🍽 {eaten}" + (f" / {target}" if target else "") + f" {'kkal' if uz else 'ккал'}")
    return render(f"🌙 <b>{'Kun yakuni' if uz else 'Итог дня'}</b>", " · ".join(parts), items, limit=EVENING_MAX)


# ------------------------------------------------------------------ утро
async def morning(profile: Profile) -> str | None:
    """«🌅 Доброе утро» — деньги одной строкой + только то, что важно сегодня; пусто — None."""
    uid, uz, today = profile.telegram_id, profile.lang == "uz", profile.today
    snap, limits, recurring, tasks, deadlines = await asyncio.gather(
        services.finance_snapshot(profile), services.budgets(uid), services.recurring(uid), services.tasks(uid), services.debt_due_rows(uid))
    items: list[tuple[int, str]] = []
    items += debt_items(profile, deadlines, today, within=DEBT_DAYS_MORNING)
    items += budget_items(profile, snap.month, limits)
    items += payment_items(profile, recurring, today)
    items += task_items(profile, tasks, today, evening=False)
    try:
        from . import calendar_sync as cal

        if cal.synced_at(uid) is not None:
            rows = cal.day_lines(uid, today, profile.tz, profile.lang)
            if rows:
                items.append(_item(60, "📅 " + " · ".join(h(r) for r in rows[:3]) + (f" +{len(rows) - 3}" if len(rows) > 3 else "")))
    except Exception:
        logger.debug("digest: календарь", exc_info=True)
    try:
        from . import daily_tasks

        todo = [r for r in daily_tasks.today_lines(uid, today) if not r.startswith("✅")]
        if todo:
            items.append(_item(40, "🔁 " + "; ".join(h(r) for r in todo[:3])))
    except Exception:
        logger.debug("digest: ежедневные дела", exc_info=True)
    items += await goal_items(profile, evening=False)
    name = h(profile.first_name or ("Do`st" if uz else "друг"))
    return render(f"🌅 <b>{'Xayrli tong' if uz else 'Доброе утро'}, {name}</b>",
                  f"💼 {fin.fmt_money(snap.wallet)} {profile.currency}", items, limit=MORNING_MAX)


__all__ = ["evening", "morning", "render", "debt_items", "budget_items", "payment_items", "task_items", "ops_items"]

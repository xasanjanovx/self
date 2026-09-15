"""Утренняя сводка и вечернее напоминание (тексты + расчёт)."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from . import categories as cats
from . import emoji as pe
from . import finance as fin
from . import nutrition as nutri
from . import services
from .profile import Profile, h


async def morning_brief(profile: Profile) -> str:
    entries, settings, limits, recurring, nutrition_profile, yesterday_logs = await asyncio.gather(
        services.finance_entries(profile.telegram_id),
        services.finance_settings(profile.telegram_id),
        services.budgets(profile.telegram_id),
        services.recurring(profile.telegram_id),
        services.nutrition_profile(profile.telegram_id),
        services.calorie_logs(profile, 2),
    )
    lang, cur = profile.lang, profile.currency
    today = profile.today
    balances = fin.compute_balances(entries, settings)
    wallet = balances["card"] + balances["cash"]
    week = fin.compute_stats(entries, fin.period_for("week", today))
    month = fin.compute_stats(entries, fin.period_for("month", today))
    yday = fin.compute_stats(entries, fin.period_for("day", today - timedelta(days=1)))
    weekday = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"][today.weekday()] if lang != "uz" else \
        ["dushanba", "seshanba", "chorshanba", "payshanba", "juma", "shanba", "yakshanba"][today.weekday()]

    lines = [f"🌅 <b>{'Xayrli tong' if lang == 'uz' else 'Доброе утро'}, {h(profile.first_name or ('Do`st' if lang == 'uz' else 'друг'))}!</b>",
             f"{pe.CALENDAR} {weekday}, {today:%d.%m.%Y}", ""]
    lines.append(f"{pe.WALLET} {'Balans' if lang == 'uz' else 'Баланс'}: <b>{fin.fmt_money(wallet)} {cur}</b> (💳 {fin.fmt_money(balances['card'])} · 💵 {fin.fmt_money(balances['cash'])})")
    if yday.expense:
        lines.append(f"{'Kecha' if lang == 'uz' else 'Вчера'}: {pe.EXPENSE} {fin.fmt_money(yday.expense)}" + (f" · {cats.label(yday.by_category[0][0], lang)}" if yday.by_category else ""))
    lines.append(f"{'7 kun' if lang == 'uz' else 'За 7 дней'}: {pe.EXPENSE} {fin.fmt_money(week.expense)} · {'kuniga' if lang == 'uz' else 'в день'} ~{fin.fmt_money(week.avg_per_day)}")
    change = month.expense_change_pct()
    lines.append(f"{fin.period_title(month.period, lang)}: {pe.EXPENSE} {fin.fmt_money(month.expense)}" + (f" ({'▲' if change > 0 else '▼'}{abs(change):.0f}%)" if change is not None else ""))

    if limits:
        warns = fin.budget_warnings(fin.budget_statuses(month, limits), lang=lang)
        lines.extend(warns[:3])

    if recurring:
        remaining, pending = fin.recurring_remaining(recurring, today)
        if remaining > 0:
            lines.append(f"🔁 {'To`lovlar qoldi' if lang == 'uz' else 'Обязательные платежи'}: {fin.fmt_money(remaining)} → {'erkin' if lang == 'uz' else 'свободно'} <b>{fin.fmt_money(wallet - remaining)}</b>")
            soon = [p for p in pending if fin.recurring_due_day(int(p.get("day_of_month") or 1), today.year, today.month) - today.day in range(0, 4)]
            for p in soon[:3]:
                day = fin.recurring_due_day(int(p.get("day_of_month") or 1), today.year, today.month)
                when = ("bugun" if lang == "uz" else "сегодня") if day == today.day else f"{day:02d}"
                lines.append(f"   • {when}: {h(p.get('title'))} — {fin.fmt_money(float(p.get('amount') or 0))}")

    lines.append("")
    if nutrition_profile:
        target = int(nutrition_profile.get("daily_calories") or 0)
        y_kcal = 0.0
        y_key = (today - timedelta(days=1)).isoformat()
        for row in yesterday_logs:
            created = str(row.get("created_at") or "")
            try:
                from datetime import datetime

                d = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(profile.tz).date().isoformat()
            except Exception:
                d = created[:10]
            if d == y_key:
                y_kcal += float(row.get("calories") or 0)
        lines.append(f"{pe.NUTRITION} {'Bugungi reja' if lang == 'uz' else 'План на день'}: <b>{target} {'kkal' if lang == 'uz' else 'ккал'}</b>"
                     + (f" · {'kecha' if lang == 'uz' else 'вчера'} {int(y_kcal)}" if y_kcal else f" · {'kecha yozilmagan' if lang == 'uz' else 'вчера не записано'}"))
    lines.append("")
    lines.append("<i>" + ("Yaxshi kun tilayman! Xarajatni bir qatorda yozing: «taksi 25000»." if lang == "uz"
                         else "Хорошего дня! Расход — одной строкой: «такси 25000».") + "</i>")
    return "\n".join(lines)


async def evening_brief(profile: Profile) -> str | None:
    """Напоминание, если сегодня пусто; иначе — короткий итог дня."""
    entries, logs, nutrition_profile = await asyncio.gather(
        services.finance_entries(profile.telegram_id),
        services.today_calorie_logs(profile),
        services.nutrition_profile(profile.telegram_id),
    )
    lang, cur = profile.lang, profile.currency
    today = profile.today
    day = fin.compute_stats(entries, fin.period_for("day", today))
    has_finance = day.ops > 0
    has_food = bool(logs)
    missing = []
    if not has_finance:
        missing.append("xarajatlar" if lang == "uz" else "расходы")
    if not has_food and nutrition_profile:
        missing.append("ovqat" if lang == "uz" else "еда")
    if missing:
        what = " va ".join(missing) if lang == "uz" else " и ".join(missing)
        return (f"🌙 {'Bugun yozilmagan' if lang == 'uz' else 'Сегодня не записано'}: <b>{what}</b>.\n"
                + ("Bir qatorda yuboring — 10 soniya: «tushlik 40000», «osh yedim»." if lang == "uz"
                   else "Скинь одной строкой — это 10 секунд: «обед 40000», «съел плов»."))
    totals = nutri.totals(logs)
    target = int((nutrition_profile or {}).get("daily_calories") or 0)
    lines = [f"🌙 <b>{'Kun yakuni' if lang == 'uz' else 'Итог дня'}</b>",
             f"{pe.EXPENSE} {fin.fmt_money(day.expense)} {cur}" + (f" · {cats.label(day.by_category[0][0], lang)} {fin.fmt_money(day.by_category[0][1])}" if day.by_category else "")]
    if day.income:
        lines.append(f"{pe.INCOME} {fin.fmt_money(day.income)} {cur}")
    if nutrition_profile:
        lines.append(f"{pe.NUTRITION} {int(totals['calories'])}" + (f" / {target}" if target else "") + f" {'kkal' if lang == 'uz' else 'ккал'} · {int(totals['meals'])} {'qabul' if lang == 'uz' else 'приёмов'}")
    return "\n".join(lines)


def parse_hhmm(value: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        hh, mm = str(value or "").split(":")[:2]
        return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
    except Exception:
        return default


__all__: list[Any] = ["morning_brief", "evening_brief", "parse_hhmm"]

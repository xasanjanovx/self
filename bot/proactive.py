"""Проактивные подсказки: бот сам пишет, когда есть повод.

Правила считаются на данных пользователя (без AI), каждая подсказка имеет ключ —
одна и та же не отправляется дважды (журнал `alerts_log`). Время суток и «не чаще
раза в неделю/месяц» зашиты в ключи и гейты правил. Еженедельный обзор — единственная
подсказка, текст которой пишет Джарвис (поле `prompt`).

Правила:
  debt_due / debt_today / debt_overdue — сроки возврата долгов (+ готовое сообщение должнику);
  spike        — сегодняшние траты ≥ 3× обычного дневного уровня (вечером);
  nutri_low/high — среднее за последние дни сильно ниже/выше нормы (раз в неделю, вечером);
  rec_low      — регулярный платёж через ≤ 2 дня, а на счетах не хватает;
  budget_proj  — при текущем темпе лимит категории будет превышен (с 10-го числа, раз в месяц);
  goal_pace    — цель накопления не успевается при текущем темпе (с 20-го числа, раз в месяц);
  weekly       — воскресный обзор недели от Джарвиса.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from . import analysis
from . import categories as cats
from . import finance as fin
from . import services
from .agent_tools import fuzzy_contains
from .profile import Profile, h

SPIKE_FACTOR = 3.0
SPIKE_MIN = 100_000
NUTRI_LOW = 0.65
NUTRI_HIGH = 1.30
DISCRETIONARY = ("food", "shopping", "entertainment", "beauty", "clothes", "gifts")


@dataclass
class Alert:
    key: str
    text: str | None = None
    copy_text: str | None = None  # готовое сообщение (например, должнику) — кнопка «скопировать»
    prompt: str | None = None  # если задан — текст пишет агент по этому запросу
    persistent: bool = False  # не удалять при следующем взаимодействии


def _m(v: float) -> str:
    return fin.fmt_money(v)


# ------------------------------------------------------------------ debts
def debtor_message(person: str, amount: float, lang: str, *, overdue_days: int = 0) -> str:
    """Готовый текст должнику (для кнопки «скопировать»)."""
    if lang == "uz":
        core = f"Assalomu alaykum, {person}! Eslatib qo'yay: {_m(amount)} so'm qarz"
        return core + (f" muddati {overdue_days} kun oldin o'tgan. Qachon qaytara olasiz?" if overdue_days > 0 else " muddati ertaga. Rahmat 🙏")
    core = f"Салом, {person}! Напоминаю про {_m(amount)} сум"
    return core + (f" — срок прошёл {overdue_days} дн. назад. Когда сможешь вернуть?" if overdue_days > 0 else ", срок возврата завтра. Спасибо 🙏")


def debt_alerts(deadlines: list[dict[str, Any]], ledger: dict[str, list[tuple[str, float]]], today: date, *, lang: str, hour: int) -> list[Alert]:
    if hour < 9:
        return []
    out: list[Alert] = []
    uz = lang == "uz"
    for row in deadlines:
        person, side = str(row.get("person") or ""), str(row.get("side") or "lent")
        try:
            due = date.fromisoformat(str(row.get("due_date"))[:10])
        except ValueError:
            continue
        amount = next((a for n, a in ledger.get(side, []) if n and (fuzzy_contains(person, n) or fuzzy_contains(n, person))), 0.0)
        if amount <= 0:
            continue
        left = (due - today).days
        who = h(person)
        if left == 1:
            text = (f"⏳ Ertaga muddat: <b>{who}</b> {_m(amount)} qaytarishi kerak." if side == "lent" else f"⏳ Ertaga <b>{who}</b>ga {_m(amount)} qaytarish kerak.") if uz else \
                (f"⏳ Завтра срок: <b>{who}</b> должен вернуть {_m(amount)}." if side == "lent" else f"⏳ Завтра нужно вернуть <b>{who}</b> {_m(amount)}.")
            out.append(Alert(f"debt_due:{person}:{side}:{due}", text, debtor_message(person, amount, lang) if side == "lent" else None, persistent=True))
        elif left == 0:
            text = (f"📅 Bugun muddat: <b>{who}</b> — {_m(amount)}." if uz else f"📅 Сегодня срок: <b>{who}</b> — {_m(amount)}.")
            out.append(Alert(f"debt_today:{person}:{side}:{due}", text, debtor_message(person, amount, lang) if side == "lent" else None, persistent=True))
        elif left < 0:
            overdue = -left
            week = overdue // 7
            if side == "lent":
                text = (f"⚠️ Muddat {overdue} kun oldin o'tgan: <b>{who}</b> — {_m(amount)}. Eslatamizmi?" if uz
                        else f"⚠️ Просрочено {overdue} дн.: <b>{who}</b> — {_m(amount)}. Напомнить ему? Текст сообщения — по кнопке.")
            else:
                text = (f"⚠️ Siz <b>{who}</b>ga {_m(amount)} qaytarishni {overdue} kun kechiktirdingiz." if uz
                        else f"⚠️ Ты просрочил возврат <b>{who}</b> на {overdue} дн.: {_m(amount)}.")
            out.append(Alert(f"debt_overdue:{person}:{side}:{due}:{week}", text, debtor_message(person, amount, lang, overdue_days=overdue) if side == "lent" else None, persistent=True))
    return out


# ------------------------------------------------------------------ spending
def spike_alert(entries: list[dict[str, Any]], today: date, *, lang: str, hour: int) -> Alert | None:
    if hour < 19:
        return None
    rows = [r for r in fin.entries_between(entries, today - timedelta(days=30), today) if r.get("entry_type") != "income" and not fin.is_transfer(r)]
    by_day: dict[str, float] = {}
    for r in rows:
        d = str(r.get("entry_date"))[:10]
        by_day[d] = by_day.get(d, 0.0) + float(r.get("amount") or 0)
    today_sum = by_day.pop(today.isoformat(), 0.0)
    if len(by_day) < 7 or today_sum < SPIKE_MIN:
        return None
    avg = sum(by_day.values()) / len(by_day)
    if avg <= 0 or today_sum < SPIKE_FACTOR * avg:
        return None
    top = max((r for r in rows if str(r.get("entry_date"))[:10] == today.isoformat()), key=lambda r: float(r.get("amount") or 0))
    top_txt = f"{cats.label(fin.entry_category_key(top), lang)} {_m(float(top.get('amount') or 0))}" + (f" «{h(fin.clean_note(top.get('note')))}»" if fin.clean_note(top.get("note")) else "")
    if lang == "uz":
        text = f"🔥 Bugun {_m(today_sum)} sarflandi — odatdagidan {today_sum / avg:.0f} barobar ko'p (~{_m(avg)}/kun). Eng kattasi: {top_txt}. Hammasi joyidami?"
    else:
        text = f"🔥 Сегодня {_m(today_sum)} — в {today_sum / avg:.0f} раза больше обычного (~{_m(avg)}/день). Самое крупное: {top_txt}. Всё ок?"
    return Alert(f"spike:{today}", text)


def recurring_alerts(recurring: list[dict[str, Any]], wallet: float, today: date, *, lang: str, hour: int) -> list[Alert]:
    if hour < 9:
        return []
    out = []
    _, pending = fin.recurring_remaining(recurring, today)
    for it in pending:
        day = fin.recurring_due_day(int(it.get("day_of_month") or 1), today.year, today.month)
        amount = float(it.get("amount") or 0)
        if 0 <= day - today.day <= 2 and wallet < amount:
            title = h(it.get("title"))
            text = (f"💳 {day:02d}-kuni <b>{title}</b> — {_m(amount)}, hisoblarda esa {_m(wallet)}. {_m(amount - wallet)} yetmaydi." if lang == "uz"
                    else f"💳 {day:02d}-го <b>{title}</b> — {_m(amount)}, а на счетах {_m(wallet)}. Не хватает {_m(amount - wallet)}.")
            out.append(Alert(f"rec_low:{it.get('id')}:{today:%Y-%m}", text))
    return out


def budget_alerts(forecast: dict[str, Any], today: date, *, lang: str, hour: int) -> list[Alert]:
    if today.day < 10 or hour < 9:
        return []
    out = []
    days_left = int(forecast.get("days_left") or 0)
    for b in forecast.get("budget_alerts") or []:
        per_day_ok = max(0.0, float(b["limit"]) - float(b["spent"])) / max(1, days_left)
        label = cats.label(b["category"], lang)
        if lang == "uz":
            text = (f"🎯 {label}: shu sur'atda oy oxirida {_m(b['projected'])} bo'ladi, limit {_m(b['limit'])} (+{b['over_pct']:.0f}%). "
                    f"Sig'ish uchun {days_left} kun davomida kuniga ~{_m(per_day_ok)}.")
        else:
            text = (f"🎯 {label}: при текущем темпе к концу месяца выйдет {_m(b['projected'])} при лимите {_m(b['limit'])} (+{b['over_pct']:.0f}%). "
                    f"Чтобы уложиться — не больше ~{_m(per_day_ok)} в день оставшиеся {days_left} дн.")
        out.append(Alert(f"budget_proj:{b['category']}:{today:%Y-%m}", text))
    return out


def goal_alerts(goals: list[dict[str, Any]], forecast: dict[str, Any], month_stats: fin.Stats | None, today: date, *, lang: str, hour: int) -> list[Alert]:
    if today.day < 20 or hour < 9 or not goals:
        return []
    projected = float(forecast.get("income_this_month") or 0) - float(forecast.get("projected_month_expense") or 0) - float(forecast.get("recurring_remaining") or 0)
    cut = []
    if month_stats:
        cut = [(k, a) for k, a, _ in month_stats.by_category if k in DISCRETIONARY][:2]
    out = []
    for g in goals:
        st = analysis.goal_status(g, today, projected_saving_month=projected)
        if st.get("done") or "needed_per_month" not in st or st.get("on_track", True):
            continue
        cut_txt = ", ".join(f"{cats.label(k, lang, with_emoji=False)} ({_m(a)})" for k, a in cut)
        if lang == "uz":
            text = (f"🎯 «{h(st['title'])}»: oyiga {_m(st['needed_per_month'])} yig'ish kerak, hozirgi sur'atda ~{_m(max(0.0, projected))} chiqadi."
                    + (f" Qisqartirish mumkin: {cut_txt}." if cut_txt else ""))
        else:
            text = (f"🎯 Цель «{h(st['title'])}»: нужно откладывать {_m(st['needed_per_month'])}/мес, при текущем темпе выйдет ~{_m(max(0.0, projected))}."
                    + (f" Где урезать: {cut_txt}." if cut_txt else ""))
        out.append(Alert(f"goal_pace:{st['id']}:{today:%Y-%m}", text))
    return out


def nutrition_alert(logs: list[dict[str, Any]], plan: dict[str, Any] | None, today: date, *, tz: Any, lang: str, hour: int) -> Alert | None:
    if hour < 20 or not plan or not plan.get("daily_calories"):
        return None
    na = analysis.nutrition_analysis(logs, today, tz=tz, plan=plan, days=5)
    if not na or na["days_logged"] < 3:
        return None
    target = float(plan["daily_calories"])
    avg = float(na["avg_kcal"])
    week = f"{today.isocalendar()[0]}-{today.isocalendar()[1]}"
    if avg < target * NUTRI_LOW:
        text = (f"🍽 So'nggi kunlarda o'rtacha {int(avg)} kkal, maqsad {int(target)}. «{h(plan.get('title') or '')}» rejasi shunday ishlamaydi — kuniga {int(target - avg)} kkal qo'shing (tuxum, tvorog, yong'oq, guruch)." if lang == "uz"
                else f"🍽 За последние дни в среднем {int(avg)} ккал при цели {int(target)}. План «{h(plan.get('title') or '')}» так не сработает — добавь ~{int(target - avg)} ккал в день (яйца, творог, орехи, рис).")
        return Alert(f"nutri_low:{week}", text)
    if avg > target * NUTRI_HIGH:
        text = (f"🍽 So'nggi kunlarda o'rtacha {int(avg)} kkal, maqsad {int(target)} — {int(avg - target)} kkal ortiqcha." if lang == "uz"
                else f"🍽 За последние дни в среднем {int(avg)} ккал при цели {int(target)} — перебор на {int(avg - target)} ккал/день.")
        return Alert(f"nutri_high:{week}", text)
    return None


def weekly_alert(today: date, *, lang: str, hour: int) -> Alert | None:
    if today.weekday() != 6 or hour < 19:
        return None
    week = f"{today.isocalendar()[0]}-{today.isocalendar()[1]}"
    prompt = ("Haftalik qisqa sharh yoz (deep_analysis ishlat): asosiy natija, 2–3 muhim topilma raqamlar bilan, keyingi haftaga 1 aniq maslahat. 8 qatordan oshmasin." if lang == "uz"
              else "Напиши короткий обзор прошедшей недели (используй deep_analysis): главный итог, 2–3 находки с цифрами, 1 конкретный совет на следующую неделю. Не больше 8 строк, начни с «🗓 Итог недели».")
    return Alert(f"weekly:{week}", prompt=prompt)


# ------------------------------------------------------------------ collect
async def collect(profile: Profile) -> list[Alert]:
    """Все подсказки, которые уместны прямо сейчас (без учёта журнала отправленных)."""
    uid = profile.telegram_id
    now = profile.now
    today, hour, lang = now.date(), now.hour, profile.lang
    snap = await services.finance_snapshot(profile)
    recurring, budgets, goals, deadlines, plan, logs = (
        await services.recurring(uid), await services.budgets(uid), await services.goals(uid),
        await services.debt_deadlines(uid), await services.nutrition_profile(uid), await services.calorie_logs(profile, 5),
    )
    alerts: list[Alert] = []
    if deadlines:
        alerts += debt_alerts(deadlines, fin.debt_ledger(snap.entries, snap.settings), today, lang=lang, hour=hour)
    if (a := spike_alert(snap.entries, today, lang=lang, hour=hour)):
        alerts.append(a)
    alerts += recurring_alerts(recurring, snap.wallet, today, lang=lang, hour=hour)
    if budgets or goals:
        fc = analysis.forecast(snap.entries, today, balances=snap.balances, recurring=recurring, budgets=budgets)
        alerts += budget_alerts(fc, today, lang=lang, hour=hour)
        alerts += goal_alerts(goals, fc, snap.month, today, lang=lang, hour=hour)
    if (a := nutrition_alert(logs, plan, today, tz=profile.tz, lang=lang, hour=hour)):
        alerts.append(a)
    if snap.entries and (a := weekly_alert(today, lang=lang, hour=hour)):
        alerts.append(a)
    return alerts


def due_tasks(tasks: list[dict[str, Any]], now: Any) -> list[dict[str, Any]]:
    """Задачи со временем, которые пора напомнить (окно 3 часа после срока), ещё не напоминали сегодня."""
    today_key = now.date().isoformat()
    minutes_now = now.hour * 60 + now.minute
    out = []
    for t in tasks:
        if t.get("done") or not t.get("due_time"):
            continue
        due_day = str(t.get("due_date") or today_key)[:10]
        if due_day != today_key or str(t.get("notified_key") or "") == today_key:
            continue
        try:
            hh, mm = (int(x) for x in str(t["due_time"]).split(":")[:2])
        except Exception:
            continue
        if 0 <= minutes_now - (hh * 60 + mm) <= 180:
            out.append(t)
    return out


__all__ = ["Alert", "collect", "due_tasks", "debtor_message", "debt_alerts", "spike_alert", "recurring_alerts", "budget_alerts", "goal_alerts", "nutrition_alert", "weekly_alert"]

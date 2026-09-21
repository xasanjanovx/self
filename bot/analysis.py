"""Глубокий анализ данных: тренды по месяцам, аномалии, прогноз до конца месяца,
«где переплачиваю», питание. Чистые вычисления без AI — результат (dict) отдаётся
агенту, который пишет человеческий разбор.
"""
from __future__ import annotations

import calendar
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import categories as cats
from . import finance as fin

MONTHS_BACK = 3  # полных месяцев до текущего
MIN_OPS_FOR_STATS = 4


# ------------------------------------------------------------------ helpers
def _month_key(d: date) -> str:
    return d.strftime("%Y-%m")


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _prev_months(today: date, n: int) -> list[tuple[int, int]]:
    """(год, месяц) для n предыдущих полных месяцев, от старого к новому."""
    out = []
    y, m = today.year, today.month
    for _ in range(n):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
        out.append((y, m))
    return list(reversed(out))


def _local_day(value: Any, tz: Any) -> date | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).date()


def _r(v: float) -> float:
    return round(float(v), 2)


def _plain_expenses(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in entries if r.get("entry_type") != "income" and not fin.is_transfer(r)]


# ------------------------------------------------------------------ monthly trend
def monthly_trend(entries: list[dict[str, Any]], today: date, *, months_back: int = MONTHS_BACK) -> list[dict[str, Any]]:
    """Расход/доход/чистый итог и топ категорий по месяцам (предыдущие полные + текущий)."""
    out = []
    spans = [_month_bounds(y, m) for y, m in _prev_months(today, months_back)] + [(today.replace(day=1), today)]
    for start, end in spans:
        period = fin.Period("custom", start, end, start, start)
        st = fin.compute_stats(entries, period)
        if st.ops == 0 and (start, end) != spans[-1]:
            continue  # месяц без данных (до начала ведения) не показываем
        days = (end - start).days + 1
        out.append({
            "month": _month_key(start), "days": days, "partial": end == today and today.day < calendar.monthrange(today.year, today.month)[1],
            "expense": _r(st.expense), "income": _r(st.income), "net": _r(st.net), "ops": st.ops,
            "avg_expense_per_day": _r(st.expense / max(1, days)),
            "top_categories": [{"category": k, "amount": _r(a), "count": c} for k, a, c in st.by_category[:6]],
        })
    return out


def category_trends(entries: list[dict[str, Any]], today: date, *, months_back: int = MONTHS_BACK) -> list[dict[str, Any]]:
    """Средний расход в день по категории: текущий месяц против среднего за прошлые месяцы."""
    prev_spans = [_month_bounds(y, m) for y, m in _prev_months(today, months_back)]
    prev_daily: dict[str, list[float]] = defaultdict(list)
    prev_days_with_data = 0
    for start, end in prev_spans:
        st = fin.compute_stats(entries, fin.Period("custom", start, end, start, start))
        if st.ops == 0:
            continue
        prev_days_with_data += 1
        days = (end - start).days + 1
        seen = set()
        for k, a, _ in st.by_category:
            prev_daily[k].append(a / days)
            seen.add(k)
        for k in list(prev_daily):
            if k not in seen:
                prev_daily[k].append(0.0)
    cur_start = today.replace(day=1)
    cur = fin.compute_stats(entries, fin.Period("custom", cur_start, today, cur_start, cur_start))
    cur_days = max(1, today.day)
    out = []
    for k, a, c in cur.by_category:
        now_daily = a / cur_days
        history = prev_daily.get(k)
        if not history:
            out.append({"category": k, "this_month": _r(a), "per_day": _r(now_daily), "change_pct": None, "new": True})
            continue
        base = sum(history) / len(history)
        change = ((now_daily - base) / base * 100.0) if base > 0 else None
        out.append({"category": k, "this_month": _r(a), "per_day": _r(now_daily), "prev_per_day": _r(base), "change_pct": _r(change) if change is not None else None, "new": False})
    out.sort(key=lambda x: -(x["this_month"]))
    return out


# ------------------------------------------------------------------ forecast
def forecast(entries: list[dict[str, Any]], today: date, *, balances: dict[str, float], recurring: list[dict[str, Any]], budgets: dict[str, float],
             months_back: int = MONTHS_BACK) -> dict[str, Any]:
    """Прогноз расходов до конца месяца по текущему темпу (с учётом истории) и что останется."""
    cur_start = today.replace(day=1)
    month_days = calendar.monthrange(today.year, today.month)[1]
    days_left = month_days - today.day
    cur = fin.compute_stats(entries, fin.Period("custom", cur_start, today, cur_start, cur_start))
    now_rate = cur.expense / max(1, today.day)
    hist_rates = []
    for y, m in _prev_months(today, months_back):
        start, end = _month_bounds(y, m)
        st = fin.compute_stats(entries, fin.Period("custom", start, end, start, start))
        if st.ops:
            hist_rates.append(st.expense / ((end - start).days + 1))
    hist_rate = sum(hist_rates) / len(hist_rates) if hist_rates else now_rate
    # чем больше дней месяца прошло, тем больше доверяем текущему темпу
    w = min(1.0, today.day / 15.0)
    rate = now_rate * w + hist_rate * (1 - w) if hist_rates else now_rate
    projected_rest = rate * days_left
    rec_total, rec_items = fin.recurring_remaining(recurring, today)
    wallet = float(balances.get("card", 0)) + float(balances.get("cash", 0))
    budget_alerts = []
    for key, limit in budgets.items():
        spent = next((a for k, a, _ in cur.by_category if k == key), 0.0)
        proj = spent + (spent / max(1, today.day)) * days_left
        if limit > 0 and proj > limit:
            budget_alerts.append({"category": key, "limit": limit, "spent": _r(spent), "projected": _r(proj), "over_pct": _r((proj - limit) / limit * 100)})
    return {
        "today": today.isoformat(), "days_left": days_left,
        "spent_this_month": _r(cur.expense), "income_this_month": _r(cur.income),
        "rate_per_day_now": _r(now_rate), "rate_per_day_history": _r(hist_rate), "rate_used": _r(rate),
        "projected_month_expense": _r(cur.expense + projected_rest),
        "projected_rest_of_month": _r(projected_rest),
        "recurring_remaining": _r(rec_total), "recurring_items": [{"title": r.get("title"), "amount": _r(float(r.get("amount") or 0)), "day": r.get("day_of_month")} for r in rec_items],
        "wallet_now": _r(wallet),
        "wallet_end_of_month": _r(wallet - projected_rest - rec_total),
        "budget_alerts": budget_alerts,
    }


# ------------------------------------------------------------------ anomalies
def anomalies(entries: list[dict[str, Any]], today: date, *, days: int = 90) -> dict[str, Any]:
    """Необычно крупные операции, дорогие дни, дубли, новые категории."""
    start = today - timedelta(days=days - 1)
    rows = [r for r in _plain_expenses(fin.entries_between(entries, start, today))]
    by_cat: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_cat[fin.entry_category_key(r)].append(float(r.get("amount") or 0))
    big = []
    for r in rows:
        k = fin.entry_category_key(r)
        vals = by_cat[k]
        amt = float(r.get("amount") or 0)
        if len(vals) >= MIN_OPS_FOR_STATS:
            med = statistics.median(vals)
            if med > 0 and amt >= max(3 * med, 100000):
                big.append({"id": str(r.get("id")), "date": str(r.get("entry_date"))[:10], "amount": _r(amt), "category": k, "note": fin.clean_note(r.get("note")), "x_median": _r(amt / med)})
    big.sort(key=lambda x: -x["amount"])

    by_day: dict[str, float] = defaultdict(float)
    for r in rows:
        by_day[str(r.get("entry_date"))[:10]] += float(r.get("amount") or 0)
    daily = list(by_day.values())
    expensive_days = []
    if len(daily) >= 5:
        avg = sum(daily) / len(daily)
        for d, v in sorted(by_day.items(), key=lambda x: -x[1])[:5]:
            if v >= 2.5 * avg:
                expensive_days.append({"date": d, "amount": _r(v), "x_avg": _r(v / avg)})

    seen: dict[tuple, list[str]] = defaultdict(list)
    for r in rows:
        seen[(str(r.get("entry_date"))[:10], fin.entry_category_key(r), round(float(r.get("amount") or 0)))].append(str(r.get("id")))
    duplicates = [{"date": k[0], "category": k[1], "amount": k[2], "ids": v} for k, v in seen.items() if len(v) > 1]

    cur_start = today.replace(day=1)
    before = {fin.entry_category_key(r) for r in _plain_expenses(entries) if str(r.get("entry_date"))[:10] < cur_start.isoformat()}
    new_cats = sorted({fin.entry_category_key(r) for r in _plain_expenses(fin.entries_between(entries, cur_start, today))} - before) if before else []
    return {"window_days": days, "big_entries": big[:8], "expensive_days": expensive_days, "duplicates": duplicates[:8], "new_categories_this_month": new_cats}


# ------------------------------------------------------------------ overpaying
def overpaying(entries: list[dict[str, Any]], today: date, *, days: int = 90) -> dict[str, Any]:
    """Где утекают деньги: частые мелкие траты, повторяющиеся комментарии, кафе vs продукты, скрытые подписки."""
    start = today - timedelta(days=days - 1)
    rows = _plain_expenses(fin.entries_between(entries, start, today))
    months = max(1.0, days / 30.0)
    total = sum(float(r.get("amount") or 0) for r in rows) or 1.0

    per_cat: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        per_cat[fin.entry_category_key(r)].append(float(r.get("amount") or 0))
    frequent_small = []
    for k, vals in per_cat.items():
        if len(vals) / months >= 8:
            s = sum(vals)
            frequent_small.append({"category": k, "count_per_month": _r(len(vals) / months), "avg_amount": _r(s / len(vals)), "per_month": _r(s / months), "per_year": _r(s / months * 12), "share_pct": _r(s / total * 100)})
    frequent_small.sort(key=lambda x: -x["per_month"])

    by_note: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        note = (fin.clean_note(r.get("note")) or "").strip().casefold()
        if note:
            by_note[note].append(float(r.get("amount") or 0))
    repeated = [{"note": n, "count": len(v), "total": _r(sum(v)), "per_month": _r(sum(v) / months)} for n, v in by_note.items() if len(v) >= 3]
    repeated.sort(key=lambda x: -x["total"])

    cafe = sum(per_cat.get("food", []))
    groceries = sum(per_cat.get("groceries", []))
    eating = {"cafe": _r(cafe), "groceries": _r(groceries), "cafe_share_pct": _r(cafe / (cafe + groceries) * 100) if cafe + groceries else None}

    # скрытые подписки: одинаковая сумма и комментарий/категория ≥ 2 месяца подряд
    monthly: dict[tuple, set] = defaultdict(set)
    for r in rows:
        key = (fin.entry_category_key(r), (fin.clean_note(r.get("note")) or "").casefold(), round(float(r.get("amount") or 0)))
        monthly[key].add(str(r.get("entry_date"))[:7])
    subscriptions = [{"category": k[0], "note": k[1] or None, "amount": k[2], "months": sorted(v)} for k, v in monthly.items() if len(v) >= 2 and k[2] >= 10000]
    subscriptions.sort(key=lambda x: -x["amount"])

    shares = sorted(((k, sum(v)) for k, v in per_cat.items()), key=lambda x: -x[1])
    return {
        "window_days": days, "total_expense": _r(total),
        "frequent_small": frequent_small[:6], "repeated_notes": repeated[:8], "eating_out": eating,
        "possible_subscriptions": subscriptions[:8],
        "category_shares": [{"category": k, "amount": _r(a), "share_pct": _r(a / total * 100)} for k, a in shares[:8]],
    }


# ------------------------------------------------------------------ nutrition
def nutrition_analysis(logs: list[dict[str, Any]], today: date, *, tz: Any, plan: dict[str, Any] | None, days: int = 30) -> dict[str, Any] | None:
    by_day: dict[date, dict[str, float]] = defaultdict(lambda: {"calories": 0.0, "protein": 0.0, "meals": 0})
    for r in logs:
        d = _local_day(r.get("created_at"), tz)
        if d is None or d < today - timedelta(days=days - 1):
            continue
        by_day[d]["calories"] += float(r.get("calories") or 0)
        by_day[d]["protein"] += float(r.get("protein") or 0)
        by_day[d]["meals"] += 1
    if not by_day:
        return None
    target = int((plan or {}).get("daily_calories") or 0)
    protein_target = float((plan or {}).get("protein") or 0)
    kcal = [v["calories"] for v in by_day.values()]
    prot = [v["protein"] for v in by_day.values()]
    weekend = [v["calories"] for d, v in by_day.items() if d.weekday() >= 5]
    weekday = [v["calories"] for d, v in by_day.items() if d.weekday() < 5]
    over = [d for d, v in by_day.items() if target and v["calories"] > target * 1.25]
    under = [d for d, v in by_day.items() if target and v["calories"] < target * 0.6]
    # текущая серия дней с записями
    streak = 0
    d = today
    while d in by_day:
        streak += 1
        d -= timedelta(days=1)
    return {
        "window_days": days, "days_logged": len(by_day), "streak_days": streak,
        "avg_kcal": _r(sum(kcal) / len(kcal)), "target_kcal": target or None,
        "avg_protein": _r(sum(prot) / len(prot)), "target_protein": protein_target or None,
        "avg_weekday_kcal": _r(sum(weekday) / len(weekday)) if weekday else None,
        "avg_weekend_kcal": _r(sum(weekend) / len(weekend)) if weekend else None,
        "days_over_target": sorted(x.isoformat() for x in over)[-5:], "days_far_under_target": sorted(x.isoformat() for x in under)[-5:],
        "avg_meals_per_day": _r(sum(v["meals"] for v in by_day.values()) / len(by_day)),
    }


# ------------------------------------------------------------------ everything
def full_analysis(
    *, entries: list[dict[str, Any]], logs: list[dict[str, Any]], today: date, tz: Any, balances: dict[str, float], settings: dict[str, float],
    recurring: list[dict[str, Any]], budgets: dict[str, float], plan: dict[str, Any] | None, focus: str = "all",
) -> dict[str, Any]:
    out: dict[str, Any] = {"today": today.isoformat()}
    if focus in {"all", "finance"}:
        ledger = fin.debt_ledger(entries, settings)
        out.update({
            "balances": {k: _r(v) for k, v in balances.items()},
            "monthly_trend": monthly_trend(entries, today),
            "category_trends": category_trends(entries, today)[:10],
            "forecast": forecast(entries, today, balances=balances, recurring=recurring, budgets=budgets),
            "anomalies": anomalies(entries, today),
            "overpaying": overpaying(entries, today),
            "debts": {"lent": [{"name": n or "", "amount": _r(v)} for n, v in ledger["lent"]], "debt": [{"name": n or "", "amount": _r(v)} for n, v in ledger["debt"]]},
        })
    if focus in {"all", "nutrition"}:
        out["nutrition"] = nutrition_analysis(logs, today, tz=tz, plan=plan)
    out["category_labels"] = {k: cats.label(k, "ru", with_emoji=False) for k in _used_categories(out)}
    return out


def _used_categories(data: dict[str, Any]) -> set[str]:
    found: set[str] = set()

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            if isinstance(v.get("category"), str):
                found.add(v["category"])
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(data)
    return found


__all__ = ["full_analysis", "monthly_trend", "category_trends", "forecast", "anomalies", "overpaying", "nutrition_analysis"]


# ------------------------------------------------------------------ savings goals
def months_between(start: date, end: date) -> float:
    """Сколько месяцев (дробно) от start до end; минимум 0.25."""
    days = (end - start).days
    return max(0.25, days / 30.4375)


def goal_status(goal: dict[str, Any], today: date, *, projected_saving_month: float | None = None) -> dict[str, Any]:
    """Прогресс цели: сколько осталось, сколько нужно откладывать в месяц и успеваем ли при текущем темпе."""
    target = float(goal.get("target_amount") or 0)
    saved = float(goal.get("saved_amount") or 0)
    remaining = max(0.0, target - saved)
    ratio = min(1.0, saved / target) if target > 0 else 0.0
    out: dict[str, Any] = {
        "id": str(goal.get("id")), "title": goal.get("title"), "target": _r(target), "saved": _r(saved), "remaining": _r(remaining),
        "ratio": round(ratio, 3), "deadline": str(goal.get("deadline") or "")[:10] or None, "done": bool(goal.get("done")) or remaining <= 0,
    }
    deadline = None
    try:
        deadline = date.fromisoformat(str(goal.get("deadline"))[:10]) if goal.get("deadline") else None
    except ValueError:
        deadline = None
    if deadline and remaining > 0:
        months = months_between(today, deadline)
        out["months_left"] = round(months, 1)
        out["needed_per_month"] = _r(remaining / months)
        if projected_saving_month is not None:
            out["projected_saving_month"] = _r(projected_saving_month)
            out["on_track"] = projected_saving_month >= remaining / months
            if projected_saving_month > 0:
                out["months_at_current_pace"] = round(remaining / projected_saving_month, 1)
    return out

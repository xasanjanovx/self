"""Привычки пользователя, выведенные из его же данных (без AI и без БД).

Что бот «знает» о человеке, просто глядя на дневник еды и траты за последние недели:
  meals    — что он обычно ест на завтрак/обед/ужин (по локальному часу записи), во сколько,
             сколько это ккал; средняя калорийность дня;
  spending — сколько в среднем уходит в день (будни/выходные), на что чаще всего,
             сколько «обязательных» (дом/связь/долги) и сколько «по желанию».
Результат идёт в системный промпт Джарвиса, в утреннюю сводку и в советы по целям
(«обычно на завтрак у тебя омлет ~450 ккал — тогда на ужин нужно ещё 900»).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import categories as cats
from . import finance as fin

MEAL_ORDER = ("breakfast", "lunch", "dinner", "snack")
MEAL_RU = {"breakfast": "завтрак", "lunch": "обед", "dinner": "ужин", "snack": "перекус"}
MEAL_UZ = {"breakfast": "nonushta", "lunch": "tushlik", "dinner": "kechki ovqat", "snack": "yengil taom"}
ESSENTIAL = ("home", "telecom", "debt", "health", "education")


def meal_slot(hour: int) -> str:
    if 5 <= hour < 11:
        return "breakfast"
    if 11 <= hour < 16:
        return "lunch"
    if 16 <= hour < 23:
        return "dinner"
    return "snack"


def _local(value: Any, tz: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def _norm_dish(desc: str) -> str:
    return " ".join(str(desc or "").casefold().replace("ё", "е").split())[:60]


# ------------------------------------------------------------------ meals
def meal_patterns(logs: list[dict[str, Any]], *, tz: Any, today: date, days: int = 30) -> dict[str, Any]:
    """Типичные приёмы пищи по слотам + средняя калорийность дня."""
    since = today - timedelta(days=days - 1)
    per_day: dict[date, float] = defaultdict(float)
    per_slot_day: dict[str, dict[date, float]] = {s: defaultdict(float) for s in MEAL_ORDER}
    slot_hours: dict[str, list[int]] = {s: [] for s in MEAL_ORDER}
    dishes: dict[str, dict[str, dict[str, Any]]] = {s: {} for s in MEAL_ORDER}
    for r in logs:
        dt = _local(r.get("created_at"), tz)
        if dt is None or dt.date() < since or dt.date() > today:
            continue
        kcal = float(r.get("calories") or 0)
        slot = meal_slot(dt.hour)
        per_day[dt.date()] += kcal
        per_slot_day[slot][dt.date()] += kcal
        slot_hours[slot].append(dt.hour * 60 + dt.minute)
        key = _norm_dish(r.get("meal_desc"))
        if key:
            d = dishes[slot].setdefault(key, {"dish": str(r.get("meal_desc") or "").strip()[:60], "count": 0, "kcal": 0.0, "protein": 0.0})
            d["count"] += 1
            d["kcal"] += kcal
            d["protein"] += float(r.get("protein") or 0)
    if not per_day:
        return {"days_logged": 0, "slots": {}}
    slots: dict[str, Any] = {}
    for s in MEAL_ORDER:
        days_with = per_slot_day[s]
        if not days_with:
            continue
        top = sorted(dishes[s].values(), key=lambda d: -d["count"])[:3]
        mins = sorted(slot_hours[s])
        median = mins[len(mins) // 2]
        slots[s] = {
            "days": len(days_with),
            "share": round(len(days_with) / len(per_day), 2),  # как часто этот приём вообще есть
            "avg_kcal": round(sum(days_with.values()) / len(days_with)),
            "usual_time": f"{median // 60:02d}:{median % 60:02d}",
            "typical": [{"dish": d["dish"], "count": d["count"], "kcal": round(d["kcal"] / d["count"]), "protein": round(d["protein"] / d["count"])} for d in top],
        }
    kcal_values = list(per_day.values())
    return {
        "days_logged": len(per_day),
        "avg_kcal": round(sum(kcal_values) / len(kcal_values)),
        "avg_meals": round(sum(len([1 for s in MEAL_ORDER if d in per_slot_day[s]]) for d in per_day) / len(per_day), 1),
        "slots": slots,
    }


def expected_by_now(meals: dict[str, Any], hour: int) -> float:
    """Сколько ккал обычно уже съедено к этому часу (по средним слотам, которые уже прошли)."""
    total = 0.0
    for s, info in (meals.get("slots") or {}).items():
        try:
            hh = int(str(info.get("usual_time") or "00:00").split(":")[0])
        except ValueError:
            continue
        if hh + 1 <= hour:
            total += float(info.get("avg_kcal") or 0) * float(info.get("share") or 0)
    return total


# ------------------------------------------------------------------ spending
def spending_patterns(entries: list[dict[str, Any]], *, today: date, days: int = 30) -> dict[str, Any]:
    """Средний расход в день (все дни, будни, выходные), по категориям, обязательные/по желанию."""
    since = today - timedelta(days=days - 1)
    rows = [r for r in fin.entries_between(entries, since, today) if r.get("entry_type") != "income" and not fin.is_transfer(r)]
    if not rows:
        return {"days": 0}
    by_day: dict[date, float] = defaultdict(float)
    by_cat: dict[str, float] = defaultdict(float)
    by_cat_days: dict[str, set[date]] = defaultdict(set)
    notes: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = date.fromisoformat(str(r.get("entry_date"))[:10])
        amount = float(r.get("amount") or 0)
        by_day[d] += amount
        key = fin.entry_category_key(r)
        by_cat[key] += amount
        by_cat_days[key].add(d)
        note = fin.clean_note(r.get("note"))
        if note:
            n = notes.setdefault(note.casefold(), {"note": note[:40], "category": key, "count": 0, "sum": 0.0})
            n["count"] += 1
            n["sum"] += amount
    span_days = (today - since).days + 1
    total = sum(by_day.values())
    weekday = [v for d, v in by_day.items() if d.weekday() < 5]
    weekend = [v for d, v in by_day.items() if d.weekday() >= 5]
    essential = sum(v for k, v in by_cat.items() if k in ESSENTIAL)
    top_cats = sorted(by_cat.items(), key=lambda kv: -kv[1])[:6]
    frequent = sorted((n for n in notes.values() if n["count"] >= 3), key=lambda n: -n["count"])[:5]
    return {
        "days": span_days,
        "days_with_expense": len(by_day),
        "total": round(total),
        "avg_per_day": round(total / span_days),
        "avg_weekday": round(sum(weekday) / len(weekday)) if weekday else None,
        "avg_weekend": round(sum(weekend) / len(weekend)) if weekend else None,
        "essential_per_month": round(essential / span_days * 30),
        "discretionary_per_month": round((total - essential) / span_days * 30),
        "top_categories": [{"category": k, "sum": round(v), "per_day": round(v / span_days), "days": len(by_cat_days[k])} for k, v in top_cats],
        "frequent": [{"note": n["note"], "category": n["category"], "count": n["count"], "avg": round(n["sum"] / n["count"])} for n in frequent],
    }


# ------------------------------------------------------------------ text
def _slot_line(slot: str, info: dict[str, Any], lang: str) -> str:
    name = (MEAL_UZ if lang == "uz" else MEAL_RU)[slot]
    dishes = ", ".join(f"{d['dish']} (~{d['kcal']})" for d in info.get("typical") or [])
    return f"{name} ~{info.get('usual_time')} ≈{info.get('avg_kcal')} {'kkal' if lang == 'uz' else 'ккал'}" + (f": {dishes}" if dishes else "")


def prompt_lines(meals: dict[str, Any], spending: dict[str, Any], lang: str = "ru") -> list[str]:
    """Строки для системного промпта Джарвиса: что человек обычно ест и на что тратит."""
    out: list[str] = []
    if meals.get("days_logged"):
        slots = "; ".join(_slot_line(s, info, "ru") for s, info in meals["slots"].items())
        out.append(f"Привычки в еде (за {meals['days_logged']} дн. с записями, в среднем {meals.get('avg_kcal')} ккал/день, {meals.get('avg_meals')} приёма): {slots}.")
    if spending.get("days"):
        cats_txt = ", ".join(f"{cats.label(c['category'], 'ru', with_emoji=False)} ~{fin.fmt_money(c['per_day'])}/день" for c in spending["top_categories"][:4])
        freq = ", ".join(f"{f['note']} ×{f['count']} (~{fin.fmt_money(f['avg'])})" for f in spending["frequent"][:4])
        out.append(
            f"Привычки в тратах (30 дн.): в среднем {fin.fmt_money(spending['avg_per_day'])}/день"
            + (f", будни {fin.fmt_money(spending['avg_weekday'])}, выходные {fin.fmt_money(spending['avg_weekend'])}" if spending.get("avg_weekday") and spending.get("avg_weekend") else "")
            + f"; обязательные ~{fin.fmt_money(spending['essential_per_month'])}/мес, по желанию ~{fin.fmt_money(spending['discretionary_per_month'])}/мес. "
            + f"Чаще всего: {cats_txt}." + (f" Регулярно: {freq}." if freq else "")
        )
    return out


def habits_card(meals: dict[str, Any], spending: dict[str, Any], lang: str = "ru") -> list[str]:
    """Короткая карточка «Что я о тебе знаю» для экрана/сводки."""
    uz = lang == "uz"
    out: list[str] = []
    if meals.get("days_logged"):
        out.append(f"🍽 {'O`rtacha' if uz else 'В среднем'} <b>{meals.get('avg_kcal')}</b> {'kkal/kun' if uz else 'ккал/день'} · {meals.get('days_logged')} {'kun' if uz else 'дн.'}")
        for s in MEAL_ORDER:
            info = meals["slots"].get(s)
            if info and info.get("typical"):
                out.append(f"   • {_slot_line(s, info, lang)}")
    if spending.get("days"):
        out.append(f"💸 {'O`rtacha' if uz else 'В среднем'} <b>{fin.fmt_money(spending['avg_per_day'])}</b>/{'kun' if uz else 'день'}"
                   + (f" · {'ish kunlari' if uz else 'будни'} {fin.fmt_money(spending['avg_weekday'])} · {'dam olish' if uz else 'выходные'} {fin.fmt_money(spending['avg_weekend'])}"
                      if spending.get("avg_weekday") and spending.get("avg_weekend") else ""))
        for c in spending["top_categories"][:3]:
            out.append(f"   • {cats.label(c['category'], lang)} ~{fin.fmt_money(c['per_day'])}/{'kun' if uz else 'день'}")
    return out


__all__ = ["meal_patterns", "spending_patterns", "expected_by_now", "prompt_lines", "habits_card", "meal_slot", "MEAL_ORDER"]

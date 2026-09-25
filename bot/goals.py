"""Движок целей: любая цель считается по реальным данным, а не «напоминалкой».

Виды (savings_goals.kind):
  save      — накопить сумму к дате (прогресс — saved_amount; темп — из прогноза месяца);
  spend_cap — тратить не больше X в месяц (всё или одна категория): считается по операциям,
              норма на день пересчитывается каждый день, «где урезать» — по категориям месяца;
  weight    — дойти до X кг: взвешивания (weight_logs) + дневник еды + план КБЖУ →
              нужный темп кг/нед, нужная калорийность, сколько добрать/убрать сегодня;
  habit     — N раз в неделю: отметки (goal_checkins), «сегодня надо, иначе не успеешь»;
  custom    — свободная цель в % к сроку: темп, отставание, еженедельный вопрос о прогрессе.

`status()` — чистая функция над `GoalData` (всё, что нужно, уже загружено), возвращает
словарь с цифрами (для ZEKI и экрана) и `flags`. Тексты — `lines()`, `morning_line()`,
`evening_line()`, `midday_alert()`. Асинхронный загрузчик — `statuses_for(profile)` внизу.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from . import analysis
from . import categories as cats
from . import finance as fin
from . import habits
from . import nutrition as nutri
from .profile import h

KCAL_PER_KG = 7700.0
MAX_DELTA = 800.0  # предел отклонения от TDEE в день, ккал
DEFAULT_PACE = {"gain": 0.35, "loss": 0.5}  # кг/нед, если срок не задан
DISCRETIONARY = ("food", "shopping", "fun", "clothes", "gifts", "other")
KINDS = ("save", "spend_cap", "weight", "habit", "custom")
GENERIC_ADD = {"ru": "творог 200 г (~300), 2 яйца + хлеб (~350), орехи 50 г (~300), рис с курицей (~600)", "uz": "tvorog 200 g (~300), 2 tuxum + non (~350), yong'oq 50 g (~300), guruch + tovuq (~600)"}
GENERIC_LIGHT = {"ru": "салат с курицей (~300), кефир + творог (~250), омлет из 2 яиц (~250)", "uz": "tovuqli salat (~300), kefir + tvorog (~250), 2 tuxumli omlet (~250)"}


@dataclass
class GoalData:
    today: date
    entries: list[dict[str, Any]] = field(default_factory=list)
    plan: dict[str, Any] | None = None  # nutrition_profiles
    logs: list[dict[str, Any]] = field(default_factory=list)  # calorie_logs за ~30 дней
    today_logs: list[dict[str, Any]] = field(default_factory=list)
    weights: list[dict[str, Any]] = field(default_factory=list)  # weight_logs (новые сверху)
    checkins: list[dict[str, Any]] = field(default_factory=list)  # goal_checkins всех целей
    meals: dict[str, Any] = field(default_factory=dict)  # habits.meal_patterns
    projected_saving_month: float | None = None
    tz: Any = None
    hour: int = 12


def _m(v: float) -> str:
    return fin.fmt_money(v)


def _r(v: float, nd: int = 1) -> float:
    return round(float(v), nd)


def _day(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def kind_of(goal: dict[str, Any]) -> str:
    k = str(goal.get("kind") or "save")
    return k if k in KINDS else "save"


def params_of(goal: dict[str, Any]) -> dict[str, Any]:
    p = goal.get("params")
    return dict(p) if isinstance(p, dict) else {}


def _base(goal: dict[str, Any], today: date) -> dict[str, Any]:
    deadline = _day(goal.get("deadline"))
    out = {"id": str(goal.get("id")), "kind": kind_of(goal), "title": goal.get("title"), "deadline": deadline.isoformat() if deadline else None,
           "done": bool(goal.get("done")), "unit": goal.get("unit"), "flags": []}
    if deadline:
        out["days_left"] = (deadline - today).days
    return out


# ------------------------------------------------------------------ save
def _status_save(goal: dict[str, Any], d: GoalData) -> dict[str, Any]:
    st = analysis.goal_status(goal, d.today, projected_saving_month=d.projected_saving_month)
    st.update({"kind": "save", "unit": "UZS", "flags": []})
    if st.get("done"):
        st["flags"].append("done")
    elif st.get("on_track") is False:
        st["flags"].append("behind")
    return st


# ------------------------------------------------------------------ spend cap
def _month_key(day: date) -> str:
    return f"{day:%Y-%m}"


def _status_spend_cap(goal: dict[str, Any], d: GoalData) -> dict[str, Any]:
    st = _base(goal, d.today)
    p = params_of(goal)
    limit = float(goal.get("target_amount") or 0)
    category = p.get("category") or None
    month = str(p.get("month") or "")[:7] or None
    today = d.today
    st.update({"limit": _r(limit, 0), "category": category, "month": month or _month_key(today), "baseline": p.get("baseline")})
    if month and month != _month_key(today):
        st["flags"].append("past" if month < _month_key(today) else "upcoming")
        return st
    start = today.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    rows = [r for r in fin.entries_between(d.entries, start, today) if r.get("entry_type") != "income" and not fin.is_transfer(r)]
    if category:
        rows = [r for r in rows if fin.entry_category_key(r) == category]
    spent = sum(float(r.get("amount") or 0) for r in rows)
    today_spent = sum(float(r.get("amount") or 0) for r in rows if str(r.get("entry_date"))[:10] == today.isoformat())
    days_left = (end - today).days + 1  # включая сегодня
    remaining = limit - spent
    avg_so_far = spent / max(1, today.day)
    projected = spent + avg_so_far * max(0, days_left - 1)
    allowed_today = max(0.0, remaining) / days_left
    after_today = max(0.0, remaining) / max(1, days_left - 1) if days_left > 1 else max(0.0, remaining)
    by_cat: dict[str, float] = {}
    if not category:
        for r in rows:
            k = fin.entry_category_key(r)
            by_cat[k] = by_cat.get(k, 0.0) + float(r.get("amount") or 0)
    cut = sorted(((k, v) for k, v in by_cat.items() if k in DISCRETIONARY), key=lambda kv: -kv[1])[:3]
    st.update({
        "spent": _r(spent, 0), "remaining": _r(remaining, 0), "today_spent": _r(today_spent, 0), "days_left": days_left,
        "avg_per_day_so_far": _r(avg_so_far, 0), "projected_month": _r(projected, 0), "allowed_per_day": _r(allowed_today, 0),
        "allowed_per_day_after_today": _r(after_today, 0), "ratio": round(min(1.0, spent / limit), 3) if limit > 0 else 0.0,
        "on_track": projected <= limit and remaining >= 0, "cut_candidates": [{"category": k, "sum": _r(v, 0)} for k, v in cut],
    })
    if p.get("baseline"):
        st["saved_vs_baseline"] = _r(float(p["baseline"]) / max(1, (end - start).days + 1) * today.day - spent, 0)
    if remaining < 0:
        st["flags"].append("over")
    elif not st["on_track"]:
        st["flags"].append("behind")
    if today_spent > allowed_today * 1.3 and today_spent > 20_000 and remaining > 0:
        st["flags"].append("today_over")
    return st


# ------------------------------------------------------------------ weight
def _weight_pace(weights: list[dict[str, Any]], today: date, *, window: int = 28) -> float | None:
    pts = sorted(((w, float(r.get("weight") or 0)) for r in weights if (w := _day(r.get("day"))) and w >= today - timedelta(days=window)), key=lambda x: x[0])
    if len(pts) < 2 or (pts[-1][0] - pts[0][0]).days < 5:
        return None
    return (pts[-1][1] - pts[0][1]) / (pts[-1][0] - pts[0][0]).days * 7


def _intake_avg(logs: list[dict[str, Any]], today: date, tz: Any, days: int = 7) -> tuple[float | None, int]:
    by_day: dict[date, float] = {}
    for r in logs:
        dd = analysis._local_day(r.get("created_at"), tz) if tz is not None else _day(r.get("created_at"))
        if dd is None or dd >= today or dd < today - timedelta(days=days):
            continue
        by_day[dd] = by_day.get(dd, 0.0) + float(r.get("calories") or 0)
    if not by_day:
        return None, 0
    return sum(by_day.values()) / len(by_day), len(by_day)


def _status_weight(goal: dict[str, Any], d: GoalData) -> dict[str, Any]:
    st = _base(goal, d.today)
    p = params_of(goal)
    target = float(goal.get("target_amount") or 0)
    start_w = float(p.get("start_weight") or (d.plan or {}).get("weight") or 0) or None
    latest = sorted(d.weights, key=lambda r: str(r.get("day")), reverse=True)
    current = float(latest[0]["weight"]) if latest else start_w
    last_day = _day(latest[0].get("day")) if latest else None
    direction = "gain" if (target - (start_w or current or target)) >= 0 else "loss"
    st.update({"unit": "kg", "target": target, "start_weight": start_w, "current": current, "direction": direction,
               "last_weigh_in": last_day.isoformat() if last_day else None, "days_since_weigh_in": (d.today - last_day).days if last_day else None})
    if current is None:
        st["flags"].append("no_weight")
        return st
    kg_left = target - current
    st["kg_left"] = _r(kg_left)
    if start_w and abs(target - start_w) > 0.05:
        st["ratio"] = round(max(0.0, min(1.0, (current - start_w) / (target - start_w))), 3)
    else:
        st["ratio"] = 1.0 if abs(kg_left) < 0.1 else 0.0
    if (direction == "gain" and kg_left <= 0.1) or (direction == "loss" and kg_left >= -0.1):
        st["done"] = True
        st["flags"].append("done")
        return st
    weeks_left = max(1.0, st["days_left"] / 7) if st.get("days_left") is not None and st["days_left"] > 0 else None
    needed_pace = abs(kg_left) / weeks_left if weeks_left else DEFAULT_PACE[direction]
    needed_pace = min(needed_pace, 1.0)
    delta = (needed_pace * KCAL_PER_KG / 7) * (1 if direction == "gain" else -1)
    delta = max(-MAX_DELTA, min(MAX_DELTA, delta))
    tdee = float((d.plan or {}).get("tdee") or 0) or None
    plan_kcal = float((d.plan or {}).get("daily_calories") or 0) or None
    recommended = round(tdee + delta) if tdee else None
    daily_target = plan_kcal or recommended
    pace = _weight_pace(d.weights, d.today)
    avg7, days7 = _intake_avg(d.logs, d.today, d.tz)
    today_kcal = nutri.totals(d.today_logs)["calories"]
    st.update({
        "needed_kg_per_week": _r(needed_pace, 2), "actual_kg_per_week": _r(pace, 2) if pace is not None else None,
        "kcal_delta_per_day": round(delta), "tdee": round(tdee) if tdee else None, "plan_kcal": round(plan_kcal) if plan_kcal else None, "recommended_kcal": recommended, "daily_target": round(daily_target) if daily_target else None,
        "avg_intake_7d": round(avg7) if avg7 is not None else None, "days_logged_7d": days7, "today_kcal": round(today_kcal),
        "today_remaining": round(daily_target - today_kcal) if daily_target else None,
        "weeks_at_actual_pace": _r(abs(kg_left) / abs(pace)) if pace and (pace > 0) == (direction == "gain") and abs(pace) > 0.02 else None,
    })
    if pace is not None:
        st["on_track"] = ((pace > 0) == (direction == "gain")) and abs(pace) >= needed_pace * 0.7
    elif avg7 is not None and daily_target:
        st["on_track"] = avg7 >= daily_target * 0.9 if direction == "gain" else avg7 <= daily_target * 1.1
    if st.get("on_track") is False:
        st["flags"].append("behind")
    if plan_kcal and recommended and abs(plan_kcal - recommended) > 150:
        st["flags"].append("plan_mismatch")
    if not d.plan:
        st["flags"].append("no_plan")
    if last_day is None or (d.today - last_day).days >= 7:
        st["flags"].append("weigh_in_due")
    if daily_target:
        expected = habits.expected_by_now(d.meals, d.hour) if d.meals else daily_target * min(1.0, max(0.0, (d.hour - 7) / 14))
        st["expected_by_now"] = round(expected)
        if direction == "gain" and d.hour >= 14 and today_kcal < max(daily_target * 0.35, expected * 0.6):
            st["flags"].append("today_low")
        if direction == "loss" and d.hour >= 14 and today_kcal > daily_target * 0.8:
            st["flags"].append("today_high")
    return st


# ------------------------------------------------------------------ habit
def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _status_habit(goal: dict[str, Any], d: GoalData) -> dict[str, Any]:
    st = _base(goal, d.today)
    per_week = int(float(goal.get("target_amount") or params_of(goal).get("per_week") or 1))
    gid = str(goal.get("id"))
    days = sorted({dd for r in d.checkins if str(r.get("goal_id")) == gid and (dd := _day(r.get("day")))}, reverse=True)
    ws = _week_start(d.today)
    this_week = [x for x in days if x >= ws]
    prev_week = [x for x in days if ws - timedelta(days=7) <= x < ws]
    days_left = 7 - d.today.weekday()  # включая сегодня
    remaining = max(0, per_week - len(this_week))
    today_checked = d.today in this_week
    # серия недель, где норма выполнена
    streak = 0
    w = ws - timedelta(days=7)
    while True:
        cnt = len([x for x in days if w <= x < w + timedelta(days=7)])
        if cnt < per_week:
            break
        streak += 1
        w -= timedelta(days=7)
    if len(this_week) >= per_week:
        streak += 1
    available = days_left - (1 if today_checked else 0)  # дней, в которые ещё можно отметиться
    st.update({"per_week": per_week, "this_week": len(this_week), "prev_week": len(prev_week), "remaining_this_week": remaining, "days_left_in_week": days_left,
               "today_checked": today_checked, "streak_weeks": streak, "total_checkins": len(days), "last_checkin": days[0].isoformat() if days else None,
               "ratio": round(min(1.0, len(this_week) / per_week), 3) if per_week else 1.0, "on_track": remaining <= available})
    if remaining > 0 and not today_checked and remaining >= days_left:
        st["flags"].append("must_today")
    if remaining > available:
        st["flags"].append("behind")
    if len(this_week) >= per_week:
        st["flags"].append("week_done")
    return st


# ------------------------------------------------------------------ custom
def _status_custom(goal: dict[str, Any], d: GoalData) -> dict[str, Any]:
    st = _base(goal, d.today)
    progress = max(0.0, min(100.0, float(goal.get("saved_amount") or 0)))
    created = _day(goal.get("created_at")) or d.today
    updated = _day(goal.get("updated_at")) or created
    deadline = _day(goal.get("deadline"))
    st.update({"progress_pct": _r(progress, 0), "ratio": round(progress / 100, 3), "last_update": updated.isoformat(), "days_since_update": (d.today - updated).days})
    if progress >= 100:
        st["done"] = True
        st["flags"].append("done")
        return st
    if deadline and deadline > created:
        elapsed = (d.today - created).days / max(1, (deadline - created).days)
        expected = min(100.0, elapsed * 100)
        st.update({"expected_pct": _r(expected, 0), "needed_pct_per_week": _r((100 - progress) / max(1.0, st.get("days_left", 7) / 7), 0), "on_track": progress >= expected - 10})
        if st["on_track"] is False:
            st["flags"].append("behind")
    if (d.today - updated).days >= 7:
        st["flags"].append("ask_progress")
    return st


def status(goal: dict[str, Any], d: GoalData) -> dict[str, Any]:
    kind = kind_of(goal)
    if kind == "spend_cap":
        return _status_spend_cap(goal, d)
    if kind == "weight":
        return _status_weight(goal, d)
    if kind == "habit":
        return _status_habit(goal, d)
    if kind == "custom":
        return _status_custom(goal, d)
    return _status_save(goal, d)


# ------------------------------------------------------------------ texts
def _dish_suggestions(meals: dict[str, Any], slot: str, budget: float, lang: str, *, light: bool = False) -> str:
    """Что съесть под остаток ккал: сначала — привычные блюда пользователя в этом слоте, иначе — общий список."""
    typical = ((meals.get("slots") or {}).get(slot) or {}).get("typical") or []
    fit = sorted((t for t in typical if 0 < float(t.get("kcal") or 0) <= budget * 1.15), key=lambda t: -float(t["kcal"]))[:3]
    generic = (GENERIC_LIGHT if light else GENERIC_ADD)[lang if lang in GENERIC_ADD else "ru"]
    if not fit:
        return generic
    out = ", ".join(f"{t['dish']} (~{t['kcal']})" for t in fit)
    if not light and max(float(t["kcal"]) for t in fit) < budget * 0.5:
        out += " + " + generic  # привычные блюда не закрывают остаток — добавляем, чем добрать
    return out


def lines(st: dict[str, Any], lang: str = "ru", *, meals: dict[str, Any] | None = None) -> list[str]:
    """Строки карточки цели на экране «Цели»."""
    uz = lang == "uz"
    kind = st.get("kind")
    out: list[str] = []
    if kind == "save":
        out += [f"{fin.bar(float(st.get('ratio') or 0), 12)} {int(float(st.get('ratio') or 0) * 100)}%",
                f"<b>{_m(float(st.get('saved') or 0))}</b> / {_m(float(st.get('target') or 0))} · {'qoldi' if uz else 'осталось'} {_m(float(st.get('remaining') or 0))}"]
        if st.get("needed_per_month"):
            out.append(f"{'Oyiga kerak' if uz else 'Нужно в месяц'}: <b>{_m(float(st['needed_per_month']))}</b>" + (f" · {'muddat' if uz else 'срок'} {st['deadline']}" if st.get("deadline") else ""))
        if "on_track" in st:
            out.append(("✅ " + ("Hozirgi sur'atda ulguramiz" if uz else "При текущем темпе успеваем")) if st["on_track"]
                       else ("⚠️ " + ("Hozirgi sur'atda ulgurmaymiz" if uz else "При текущем темпе не успеваем") + (f" (~{st['months_at_current_pace']} {'oy' if uz else 'мес.'})" if st.get("months_at_current_pace") else "")))
    elif kind == "spend_cap":
        if "spent" not in st:
            out.append(("Kelgusi oy uchun" if uz else "На следующий месяц") if "upcoming" in st["flags"] else ("O'tgan oy" if uz else "Прошлый месяц"))
            return out
        cat = cats.label(st["category"], lang) + " · " if st.get("category") else ""
        out += [f"{fin.bar(float(st.get('ratio') or 0), 12)} {int(float(st.get('ratio') or 0) * 100)}%",
                f"{cat}<b>{_m(st['spent'])}</b> / {_m(st['limit'])} · {'bugun' if uz else 'сегодня'} {_m(st['today_spent'])}"]
        if st["remaining"] < 0:
            out.append("🚫 " + (f"Limit {_m(-st['remaining'])} ga oshdi, oy oxirigacha {st['days_left']} kun" if uz else f"Лимит превышен на {_m(-st['remaining'])}, до конца месяца {st['days_left']} дн."))
        else:
            out.append(f"{'Kuniga mumkin' if uz else 'Можно в день'}: <b>{_m(st['allowed_per_day'])}</b> · {st['days_left']} {'kun' if uz else 'дн.'} · {'qoldi' if uz else 'осталось'} {_m(st['remaining'])}")
            out.append(("✅ " + (f"Shu sur'atda oy oxirida ~{_m(st['projected_month'])}" if uz else f"При текущем темпе к концу месяца ~{_m(st['projected_month'])}")) if st.get("on_track")
                       else ("⚠️ " + (f"Shu sur'atda ~{_m(st['projected_month'])} bo'ladi — limitdan {_m(st['projected_month'] - st['limit'])} ko'p" if uz
                                      else f"При текущем темпе выйдет ~{_m(st['projected_month'])} — на {_m(st['projected_month'] - st['limit'])} больше лимита")))
            if st.get("cut_candidates") and not st.get("on_track"):
                out.append(("Qisqartirish: " if uz else "Где урезать: ") + ", ".join(f"{cats.label(c['category'], lang, with_emoji=False)} {_m(c['sum'])}" for c in st["cut_candidates"]))
        if st.get("saved_vs_baseline") is not None:
            v = float(st["saved_vs_baseline"])
            out.append((f"💰 Odatdagidan {_m(abs(v))} {'kam' if v >= 0 else 'ko`p'} sarflandi" if uz else f"💰 По сравнению с обычным: {'сэкономлено' if v >= 0 else 'перерасход'} {_m(abs(v))}"))
    elif kind == "weight":
        if st.get("current") is None:
            out.append("⚖️ " + ("Vazningizni yozing: «vazn 72.5»" if uz else "Запиши текущий вес: «вес 72.5»"))
            return out
        out.append(f"{fin.bar(float(st.get('ratio') or 0), 12)} {int(float(st.get('ratio') or 0) * 100)}%")
        out.append(f"⚖️ <b>{st['current']}</b> → {st['target']} {'kg' if uz else 'кг'}" + (f" ({'yana' if uz else 'ещё'} {abs(float(st.get('kg_left') or 0)):.1f})" if not st.get("done") else " 🎉")
                   + (f" · {'o`lchov' if uz else 'взвешивание'} {st['last_weigh_in']}" if st.get("last_weigh_in") else ""))
        if st.get("done"):
            return out
        pace_txt = f"{st['needed_kg_per_week']} {'kg/hafta' if uz else 'кг/нед'}"
        actual = st.get("actual_kg_per_week")
        out.append(f"{'Kerakli sur`at' if uz else 'Нужный темп'}: {pace_txt}" + (f" · {'hozir' if uz else 'сейчас'} {actual:+.2f}" if actual is not None else ""))
        if st.get("daily_target"):
            out.append(f"🍽 {'Kuniga' if uz else 'Норма'}: <b>{int(st['daily_target'])}</b> {'kkal' if uz else 'ккал'} · {'bugun' if uz else 'сегодня'} {st['today_kcal']}"
                       + (f" · {'o`rtacha 7 kun' if uz else 'ср. 7 дн.'} {st['avg_intake_7d']}" if st.get("avg_intake_7d") else ""))
        if "plan_mismatch" in st["flags"] and st.get("recommended_kcal"):
            out.append("⚠️ " + (f"Maqsad uchun reja {st['recommended_kcal']} kkal bo'lishi kerak (hozir {int(st['plan_kcal'])}). «Rejani {st['recommended_kcal']} qil» deb yozing." if uz
                                else f"Для этой цели план должен быть {st['recommended_kcal']} ккал (сейчас {int(st['plan_kcal'])}). Напиши «поставь план {st['recommended_kcal']}»."))
        if "no_plan" in st["flags"]:
            out.append("⚠️ " + ("KBJU rejasi yo'q — «Ovqatlanish» bo'limida vazn/bo'y/yoshni kiriting." if uz else "Нет плана КБЖУ — задай вес/рост/возраст в «Питании», тогда посчитаю норму."))
        if "on_track" in st:
            out.append(("✅ " + ("Sur'at yetarli" if uz else "Темп достаточный")) if st["on_track"] else ("⚠️ " + ("Sur'at yetarli emas" if uz else "Темп ниже нужного")))
        if "weigh_in_due" in st["flags"]:
            out.append("⚖️ " + ("Bir haftadan beri o'lchov yo'q — «vazn 72.5» deb yozing" if uz else "Давно не взвешивался — напиши «вес 72.5»"))
    elif kind == "habit":
        out.append(f"{fin.bar(float(st.get('ratio') or 0), 12)} {st['this_week']}/{st['per_week']} {'bu hafta' if uz else 'на этой неделе'}")
        if st["remaining_this_week"] > 0:
            out.append(f"{'Qoldi' if uz else 'Осталось'}: {st['remaining_this_week']} {'marta' if uz else 'раз'} · {st['days_left_in_week']} {'kun' if uz else 'дн.'}"
                       + (" · " + ("bugun kerak!" if uz else "сегодня надо!") if "must_today" in st["flags"] else ""))
        else:
            out.append("✅ " + ("Hafta normasi bajarildi" if uz else "Норма недели выполнена"))
        if st.get("streak_weeks"):
            out.append(f"🔥 {st['streak_weeks']} {'hafta ketma-ket' if uz else 'нед. подряд'}")
        out.append(("Bugun: ✅" if uz else "Сегодня: ✅") if st.get("today_checked") else ("Bugun: —" if uz else "Сегодня: —"))
    else:  # custom
        out.append(f"{fin.bar(float(st.get('ratio') or 0), 12)} {int(st.get('progress_pct') or 0)}%")
        if st.get("expected_pct") is not None:
            out.append(f"{'Reja bo`yicha' if uz else 'По плану должно быть'}: {int(st['expected_pct'])}% · {'haftasiga' if uz else 'нужно в неделю'} +{int(st.get('needed_pct_per_week') or 0)}%")
        if "on_track" in st:
            out.append(("✅ " + ("Reja bo'yicha" if uz else "Идёшь по плану")) if st["on_track"] else ("⚠️ " + ("Rejadan orqada" if uz else "Отстаёшь от плана")))
        out.append(f"{'Oxirgi yangilanish' if uz else 'Обновлено'}: {st['last_update']}" + (" · " + ("progressni yozing: «60%»" if uz else "напиши прогресс: «выучил 60%»") if "ask_progress" in st["flags"] else ""))
    return out


def morning_line(st: dict[str, Any], lang: str = "ru", *, meals: dict[str, Any] | None = None) -> str | None:
    """Одна строка «что сегодня по этой цели» для утренней сводки."""
    uz = lang == "uz"
    k, title = st.get("kind"), h(st.get("title"))
    if st.get("done"):
        return None
    if k == "spend_cap" and "spent" in st:
        if st["remaining"] < 0:
            return f"🎯 {title}: " + (f"limit oshgan ({_m(-st['remaining'])}) — bugun sarflamaslikka harakat qiling" if uz else f"лимит уже превышен на {_m(-st['remaining'])} — сегодня лучше без трат")
        return f"🎯 {title}: " + (f"bugun ≤ <b>{_m(st['allowed_per_day'])}</b> · qoldi {_m(st['remaining'])} / {st['days_left']} kun" if uz
                                   else f"сегодня до <b>{_m(st['allowed_per_day'])}</b> · осталось {_m(st['remaining'])} на {st['days_left']} дн.")
    if k == "weight" and st.get("current") is not None:
        parts = [f"⚖️ {title}: {st['current']} → {st['target']}" + (f" ({'yana' if uz else 'ещё'} {abs(float(st.get('kg_left') or 0)):.1f} {'kg' if uz else 'кг'})")]
        if st.get("daily_target"):
            parts.append(f"{'bugun' if uz else 'сегодня'} <b>{int(st['daily_target'])}</b> {'kkal' if uz else 'ккал'}")
        if "weigh_in_due" in st["flags"]:
            parts.append("⚖️ " + ("o'lchaning" if uz else "взвесься"))
        return " · ".join(parts)
    if k == "habit":
        if st["remaining_this_week"] <= 0:
            return None
        need = "must_today" in st["flags"]
        return f"🔁 {title}: {st['this_week']}/{st['per_week']} · " + (("bugun kerak" if uz else "сегодня надо") if need else (f"qoldi {st['remaining_this_week']} / {st['days_left_in_week']} kun" if uz else f"ещё {st['remaining_this_week']} за {st['days_left_in_week']} дн."))
    if k == "custom":
        if "behind" in st["flags"] or "ask_progress" in st["flags"]:
            return f"🎯 {title}: {int(st.get('progress_pct') or 0)}%" + (f" ({'reja' if uz else 'план'} {int(st['expected_pct'])}%)" if st.get("expected_pct") is not None else "")
        return None
    if k == "save" and st.get("needed_per_month") and st.get("on_track") is False:
        return f"🎯 {title}: {_m(float(st['saved']))}/{_m(float(st['target']))} · " + (f"oyiga {_m(float(st['needed_per_month']))} kerak" if uz else f"нужно {_m(float(st['needed_per_month']))}/мес")
    return None


def evening_line(st: dict[str, Any], lang: str = "ru", *, meals: dict[str, Any] | None = None) -> str | None:
    """Итог дня по цели + что сделать прямо сейчас."""
    uz = lang == "uz"
    k, title = st.get("kind"), h(st.get("title"))
    if st.get("done"):
        return None
    if k == "spend_cap" and "spent" in st:
        ok = st["today_spent"] <= st["allowed_per_day"] * 1.05 or st["today_spent"] == 0
        head = f"{'✅' if ok else '⚠️'} {title}: {'bugun' if uz else 'сегодня'} {_m(st['today_spent'])} / {_m(st['allowed_per_day'])}"
        if st["remaining"] < 0:
            return head + " · " + (f"limit {_m(-st['remaining'])} ga oshgan" if uz else f"лимит превышен на {_m(-st['remaining'])}")
        return head + " · " + (f"ertadan kuniga ≤ {_m(st['allowed_per_day_after_today'])}" if uz else f"с завтра ≤ {_m(st['allowed_per_day_after_today'])}/день")
    if k == "weight" and st.get("daily_target"):
        rem = int(st.get("today_remaining") or 0)
        head = f"🍽 {title}: {st['today_kcal']} / {int(st['daily_target'])} {'kkal' if uz else 'ккал'}"
        if st["direction"] == "gain":
            if rem > 150:
                return head + " · " + (f"yana {rem} kkal kerak: " if uz else f"добери ещё {rem} ккал: ") + _dish_suggestions(meals or {}, "dinner", rem, lang)
            return head + " ✅"
        if rem < -100:
            return head + " · " + (f"{-rem} kkal ortiqcha — ertaga yengilroq" if uz else f"перебор {-rem} ккал — завтра полегче")
        if rem > 150:
            return head + " · " + (f"yana {rem} kkal mumkin (yengil): " if uz else f"можно ещё {rem} ккал (лёгкое): ") + _dish_suggestions(meals or {}, "dinner", rem, lang, light=True)
        return head + " ✅"
    if k == "habit":
        if st.get("today_checked"):
            return f"✅ {title}: {st['this_week']}/{st['per_week']}"
        if "must_today" in st["flags"]:
            return f"⚠️ {title}: " + (f"bugun bajarilmasa {st['per_week']}/hafta chiqmaydi" if uz else f"если не сегодня — {st['per_week']}/нед не выйдет") + " · " + ("bajardim → «qildim»" if uz else "сделал → напиши «сделал»")
        return None
    return None


def midday_alert(st: dict[str, Any], lang: str = "ru", *, hour: int, today: date, meals: dict[str, Any] | None = None) -> tuple[str, str] | None:
    """(key, text) — одно сообщение днём, только если явно выбиваешься из плана."""
    uz = lang == "uz"
    k, title, gid = st.get("kind"), h(st.get("title")), st.get("id")
    flags = st.get("flags") or []
    if k == "spend_cap" and "today_over" in flags and hour >= 13:
        text = (f"💸 {title}: bugun allaqachon {_m(st['today_spent'])} (norma {_m(st['allowed_per_day'])}). Kun oxirigacha sarflamang — keyin kuniga {_m(st['allowed_per_day_after_today'])} qoladi." if uz
                else f"💸 {title}: сегодня уже {_m(st['today_spent'])} при норме {_m(st['allowed_per_day'])}/день. До конца дня лучше без трат — иначе на остаток месяца будет по {_m(st['allowed_per_day_after_today'])}.")
        return f"goal_day:{gid}:{today}", text
    if k == "spend_cap" and "over" in flags and hour >= 13 and st.get("today_spent", 0) > 0:
        text = (f"🚫 {title}: limit {_m(-st['remaining'])} ga oshdi, oy oxirigacha {st['days_left']} kun. Faqat zarur xarajatlar." if uz
                else f"🚫 {title}: лимит превышен на {_m(-st['remaining'])}, до конца месяца {st['days_left']} дн. Только необходимое.")
        return f"goal_over:{gid}:{today}", text
    if k == "weight" and "today_low" in flags:
        rem = int(st.get("today_remaining") or 0)
        text = (f"🍽 {title}: bugun faqat {st['today_kcal']} kkal, odatda bu vaqtda ~{st.get('expected_by_now')}. Yana {rem} kerak — tushlik: " + _dish_suggestions(meals or {}, "lunch", rem, lang) if uz
                else f"🍽 {title}: к этому часу только {st['today_kcal']} ккал, обычно уже ~{st.get('expected_by_now')}. Нужно ещё {rem} — пообедай: " + _dish_suggestions(meals or {}, "lunch", rem, lang))
        return f"goal_day:{gid}:{today}", text
    if k == "weight" and "today_high" in flags:
        rem = int(st.get("today_remaining") or 0)
        text = (f"🍽 {title}: bugun {st['today_kcal']} / {int(st['daily_target'])} kkal — kechki ovqatga {max(0, rem)} qoldi, yengil: " + _dish_suggestions(meals or {}, "dinner", max(200, rem), lang, light=True) if uz
                else f"🍽 {title}: уже {st['today_kcal']} из {int(st['daily_target'])} ккал — на ужин осталось {max(0, rem)}, лучше лёгкое: " + _dish_suggestions(meals or {}, "dinner", max(200, rem), lang, light=True))
        return f"goal_day:{gid}:{today}", text
    if k == "habit" and "must_today" in flags and hour >= 17:
        text = (f"🔁 {title}: bu hafta {st['this_week']}/{st['per_week']}, {st['days_left_in_week']} kun qoldi — bugun kerak. Bajarsangiz «qildim» deb yozing." if uz
                else f"🔁 {title}: на этой неделе {st['this_week']}/{st['per_week']}, осталось {st['days_left_in_week']} дн. — сегодня надо. Сделаешь — напиши «сделал».")
        return f"goal_day:{gid}:{today}", text
    if k == "custom" and "ask_progress" in flags and hour >= 18 and today.weekday() == 6:
        text = (f"🎯 «{title}»: qanday ketyapti? Necha foiz bajarildi? (masalan: «{title} 60%»)" if uz
                else f"🎯 «{title}»: как продвигается? На сколько процентов готово? (напиши, например: «{title} — 60%»)")
        return f"goal_ask:{gid}:{today.isocalendar()[1]}", text
    return None


def plan_lines(statuses: list[dict[str, Any]], lang: str, *, meals: dict[str, Any] | None = None, when: str = "morning") -> list[str]:
    fn = morning_line if when == "morning" else evening_line
    out = []
    for st in statuses:
        line = fn(st, lang, meals=meals)
        if line:
            out.append(line)
    return out[:6]


def prompt_summary(statuses: list[dict[str, Any]]) -> str:
    """Компактная строка для системного промпта ZEKI."""
    parts = []
    for st in statuses:
        k = st.get("kind")
        head = f"[{st.get('id')}] {st.get('title')} ({k})"
        if k == "save":
            parts.append(f"{head}: {_m(float(st.get('saved') or 0))}/{_m(float(st.get('target') or 0))}" + (f", нужно {_m(float(st['needed_per_month']))}/мес" if st.get("needed_per_month") else ""))
        elif k == "spend_cap" and "spent" in st:
            parts.append(f"{head}: потрачено {_m(st['spent'])} из {_m(st['limit'])}, сегодня {_m(st['today_spent'])}, норма {_m(st['allowed_per_day'])}/день, осталось {st['days_left']} дн., прогноз {_m(st['projected_month'])}")
        elif k == "weight" and st.get("current") is not None:
            parts.append(f"{head}: {st['current']}→{st['target']} кг, нужно {st.get('needed_kg_per_week')} кг/нед, факт {st.get('actual_kg_per_week')}, норма {st.get('daily_target')} ккал, сегодня {st.get('today_kcal')}, ср.7д {st.get('avg_intake_7d')}")
        elif k == "habit":
            parts.append(f"{head}: {st['this_week']}/{st['per_week']} на этой неделе, сегодня {'✓' if st.get('today_checked') else '—'}, серия {st.get('streak_weeks')} нед.")
        elif k == "custom":
            parts.append(f"{head}: {int(st.get('progress_pct') or 0)}%" + (f" (план {int(st['expected_pct'])}%)" if st.get("expected_pct") is not None else "") + (f" до {st['deadline']}" if st.get("deadline") else ""))
        else:
            parts.append(head)
    return "; ".join(parts)


# ------------------------------------------------------------------ loader
async def statuses_for(profile: Any, *, goals: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], GoalData]:
    """Загрузить всё нужное и посчитать статусы всех активных целей."""
    import asyncio

    from . import services

    uid = profile.telegram_id
    rows = goals if goals is not None else await services.goals(uid)
    kinds = {kind_of(g) for g in rows}
    snap = await services.finance_snapshot(profile)
    plan, logs, today_logs, weights, checkins = await asyncio.gather(
        services.nutrition_profile(uid),
        services.calorie_logs(profile, 30) if "weight" in kinds else asyncio.sleep(0, result=[]),
        services.today_calorie_logs(profile) if "weight" in kinds else asyncio.sleep(0, result=[]),
        services.weight_logs(uid) if "weight" in kinds else asyncio.sleep(0, result=[]),
        services.checkins(uid) if "habit" in kinds else asyncio.sleep(0, result=[]),
    )
    projected = None
    if "save" in kinds:
        recurring, budgets = await asyncio.gather(services.recurring(uid), services.budgets(uid))
        fc = analysis.forecast(snap.entries, profile.today, balances=snap.balances, recurring=recurring, budgets=budgets)
        projected = float(fc["income_this_month"]) - float(fc["projected_month_expense"]) - float(fc["recurring_remaining"])
    meals = habits.meal_patterns(logs, tz=profile.tz, today=profile.today) if logs else {}
    data = GoalData(today=profile.today, entries=snap.entries, plan=plan, logs=logs, today_logs=today_logs, weights=weights, checkins=checkins,
                    meals=meals, projected_saving_month=projected, tz=profile.tz, hour=profile.now.hour)
    return [status(g, data) for g in rows], data


__all__ = ["GoalData", "KINDS", "status", "lines", "morning_line", "evening_line", "midday_alert", "plan_lines", "prompt_summary", "statuses_for", "kind_of", "params_of"]

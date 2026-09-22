"""Движок целей (bot/goals.py) и привычки из данных (bot/habits.py) — чистые расчёты."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot import goals, habits

TODAY = date(2026, 9, 21)  # понедельник, сентябрь — 30 дней
TZ = ZoneInfo("Asia/Tashkent")


def _e(day: date, amount: float, category: str = "food", *, kind: str = "expense", note: str = "") -> dict:
    return {"id": f"{day}-{amount}", "entry_type": kind, "amount": amount, "category": category, "note": f"[b:card] {note}".strip(), "entry_date": day.isoformat()}


def _log(day: date, hour: int, desc: str, kcal: float, protein: float = 20) -> dict:
    created = datetime(day.year, day.month, day.day, hour, 15, tzinfo=TZ).astimezone(timezone.utc)
    return {"id": f"{day}-{hour}", "meal_desc": desc, "calories": kcal, "protein": protein, "fat": 10, "carbs": 30, "created_at": created.isoformat()}


# ------------------------------------------------------------------ spend cap
def test_spend_cap_allowed_per_day_and_projection():
    entries = [_e(TODAY - timedelta(days=i), 100_000) for i in range(20)]  # 2 млн за 20 дней (с 2 по 21 сентября)
    entries.append(_e(TODAY, 250_000, "shopping"))
    goal = {"id": 5, "kind": "spend_cap", "title": "Не больше 3 млн", "target_amount": 3_000_000, "params": {}}
    st = goals.status(goal, goals.GoalData(today=TODAY, entries=entries, hour=15))
    assert st["spent"] == 2_250_000 and st["today_spent"] == 350_000
    assert st["days_left"] == 10 and st["remaining"] == 750_000
    assert st["allowed_per_day"] == 75_000
    assert st["on_track"] is False and "behind" in st["flags"]
    assert "today_over" in st["flags"]
    assert st["cut_candidates"][0]["category"] == "food"
    key, text = goals.midday_alert(st, "ru", hour=15, today=TODAY)
    assert key.startswith("goal_day:5:") and "350 000" in text
    assert "75 000" in goals.morning_line(st, "ru")
    card = goals.lines(st, "ru")
    assert any("Где урезать" in line for line in card)


def test_spend_cap_category_and_baseline_saving():
    entries = [_e(TODAY - timedelta(days=i), 50_000, "transport") for i in range(5)] + [_e(TODAY, 500_000, "food")]
    goal = {"id": 6, "kind": "spend_cap", "title": "Транспорт", "target_amount": 600_000, "params": {"category": "transport", "baseline": 900_000}}
    st = goals.status(goal, goals.GoalData(today=TODAY, entries=entries, hour=10))
    assert st["spent"] == 250_000 and st["on_track"] is True
    assert st["saved_vs_baseline"] == 900_000 / 30 * 21 - 250_000
    key, _ = goals.midday_alert(st, "ru", hour=15, today=TODAY)  # 50к сегодня при норме 35к — это уже повод написать
    assert key == f"goal_day:6:{TODAY}"
    st_ok = goals.status(goal, goals.GoalData(today=TODAY, entries=entries[1:], hour=10))
    assert goals.midday_alert(st_ok, "ru", hour=15, today=TODAY) is None


def test_spend_cap_other_month_is_inactive():
    goal = {"id": 7, "kind": "spend_cap", "title": "Октябрь", "target_amount": 1, "params": {"month": "2026-10"}}
    st = goals.status(goal, goals.GoalData(today=TODAY))
    assert "upcoming" in st["flags"] and "spent" not in st
    assert goals.morning_line(st, "ru") is None


# ------------------------------------------------------------------ weight
def _weights(*pairs):
    return [{"day": (TODAY - timedelta(days=d)).isoformat(), "weight": w} for d, w in pairs]


def test_weight_gain_needs_kcal_and_flags_low_day():
    plan = {"daily_calories": 2600, "tdee": 2400, "weight": 68}
    logs = [_log(TODAY - timedelta(days=i), 9, "омлет с хлебом", 450) for i in range(1, 8)] + \
           [_log(TODAY - timedelta(days=i), 13, "плов", 700) for i in range(1, 8)] + \
           [_log(TODAY - timedelta(days=i), 19, "курица с рисом", 600) for i in range(1, 8)]
    today_logs = [_log(TODAY, 9, "омлет с хлебом", 450)]
    meals = habits.meal_patterns(logs, tz=TZ, today=TODAY)
    goal = {"id": 8, "kind": "weight", "title": "Набрать до 75", "target_amount": 75, "deadline": (TODAY + timedelta(days=70)).isoformat(),
            "params": {"start_weight": 68, "start_date": (TODAY - timedelta(days=14)).isoformat()}}
    d = goals.GoalData(today=TODAY, plan=plan, logs=logs, today_logs=today_logs, weights=_weights((14, 68.0), (7, 68.4), (0, 69.0)), meals=meals, tz=TZ, hour=15)
    st = goals.status(goal, d)
    assert st["direction"] == "gain" and st["current"] == 69.0 and st["kg_left"] == 6.0
    assert 0.55 < st["needed_kg_per_week"] < 0.65  # 6 кг за 10 недель
    assert st["actual_kg_per_week"] == 0.5
    assert st["kcal_delta_per_day"] > 500 and st["recommended_kcal"] == 2400 + st["kcal_delta_per_day"]
    assert "plan_mismatch" in st["flags"]  # план 2600 vs нужно ~3060
    assert st["avg_intake_7d"] == 1750 and st["today_kcal"] == 450
    assert "today_low" in st["flags"]
    key, text = goals.midday_alert(st, "ru", hour=15, today=TODAY, meals=meals)
    assert "плов" in text  # предлагает привычный обед
    ev = goals.evening_line(st, "ru", meals=meals)
    assert "добери" in ev and "курица" in ev
    assert st["on_track"] is True  # 0.5 кг/нед ≥ 70% от нужных 0.6
    # weigh-in сегодня → не просим взвеситься
    assert "weigh_in_due" not in st["flags"]


def test_weight_loss_done_and_missing_weight():
    goal = {"id": 9, "kind": "weight", "title": "Сбросить до 80", "target_amount": 80, "params": {"start_weight": 90}}
    st = goals.status(goal, goals.GoalData(today=TODAY, weights=_weights((0, 79.8))))
    assert st["done"] is True and st["ratio"] == 1.0
    st2 = goals.status({"id": 10, "kind": "weight", "title": "x", "target_amount": 80, "params": {}}, goals.GoalData(today=TODAY))
    assert "no_weight" in st2["flags"]
    assert "вес 72.5" in goals.lines(st2, "ru")[0]


# ------------------------------------------------------------------ habit
def test_habit_week_progress_and_must_today():
    goal = {"id": 11, "kind": "habit", "title": "Зал", "target_amount": 3}
    checks = [{"goal_id": 11, "day": (TODAY - timedelta(days=d)).isoformat()} for d in (2, 4, 7, 9, 11)]  # прошлая неделя: 3 отметки → серия 1
    friday = TODAY + timedelta(days=4)
    st = goals.status(goal, goals.GoalData(today=friday, checkins=checks + [{"goal_id": 11, "day": (friday - timedelta(days=1)).isoformat()}]))
    assert st["this_week"] == 1 and st["remaining_this_week"] == 2 and st["days_left_in_week"] == 3
    assert st["on_track"] is True and "must_today" not in st["flags"]
    saturday = friday + timedelta(days=1)
    st2 = goals.status(goal, goals.GoalData(today=saturday, checkins=checks + [{"goal_id": 11, "day": (friday - timedelta(days=1)).isoformat()}]))
    assert "must_today" in st2["flags"] and st2["streak_weeks"] == 1
    key, text = goals.midday_alert(st2, "ru", hour=18, today=saturday)
    assert "сегодня надо" in text
    st3 = goals.status(goal, goals.GoalData(today=saturday, checkins=[{"goal_id": 11, "day": saturday.isoformat()}] + checks[2:]))
    assert st3["today_checked"] is True and "must_today" not in st3["flags"]


# ------------------------------------------------------------------ custom
def test_custom_progress_vs_plan():
    goal = {"id": 12, "kind": "custom", "title": "Выучить 500 слов", "target_amount": 100, "saved_amount": 20,
            "created_at": (TODAY - timedelta(days=30)).isoformat(), "updated_at": (TODAY - timedelta(days=10)).isoformat(), "deadline": (TODAY + timedelta(days=30)).isoformat()}
    st = goals.status(goal, goals.GoalData(today=TODAY))
    assert st["progress_pct"] == 20 and st["expected_pct"] == 50 and st["on_track"] is False
    assert "ask_progress" in st["flags"]
    sunday = TODAY + timedelta(days=6)
    key, text = goals.midday_alert(goals.status(goal, goals.GoalData(today=sunday)), "ru", hour=19, today=sunday)
    assert key.startswith("goal_ask:12:") and "%" in text


# ------------------------------------------------------------------ habits
def test_meal_patterns_and_expected_by_now():
    logs = [_log(TODAY - timedelta(days=i), 8, "Омлет", 400) for i in range(10)] + [_log(TODAY - timedelta(days=i), 13, "плов", 750) for i in range(10)] \
        + [_log(TODAY - timedelta(days=i), 20, "творог", 250) for i in range(0, 10, 2)]
    m = habits.meal_patterns(logs, tz=TZ, today=TODAY)
    assert m["days_logged"] == 10
    assert m["slots"]["breakfast"]["typical"][0]["dish"] == "Омлет" and m["slots"]["breakfast"]["avg_kcal"] == 400
    assert m["slots"]["lunch"]["usual_time"] == "13:15"
    assert m["slots"]["dinner"]["share"] == 0.5
    assert habits.expected_by_now(m, 12) == 400
    assert habits.expected_by_now(m, 15) == 1150
    text = habits.prompt_lines(m, {}, "ru")[0]
    assert "завтрак" in text and "Омлет" in text


def test_spending_patterns():
    entries = [_e(TODAY - timedelta(days=i), 60_000, "food", note="обед") for i in range(30)] + [_e(TODAY - timedelta(days=i), 1_000_000, "home") for i in (1, 15)]
    entries.append(_e(TODAY, 100_000, "salary", kind="income"))
    entries.append({"id": "t", "entry_type": "expense", "amount": 999, "category": "transfer", "note": "[x:card>cash]", "entry_date": TODAY.isoformat()})
    sp = habits.spending_patterns(entries, today=TODAY)
    assert sp["days"] == 30 and sp["total"] == 60_000 * 30 + 2_000_000
    assert sp["avg_per_day"] == round(3_800_000 / 30)
    assert sp["essential_per_month"] == 2_000_000
    assert sp["top_categories"][0]["category"] == "home" and sp["frequent"][0]["note"] == "обед"
    assert "126 667" in habits.prompt_lines({}, sp, "ru")[0]

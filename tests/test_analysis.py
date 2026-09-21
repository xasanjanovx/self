"""Глубокий анализ: чистые расчёты на синтетических данных."""
from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

from bot import analysis

TODAY = date(2026, 9, 21)
TZ = ZoneInfo("Asia/Tashkent")


def _e(id_, day: date, amount: float, category: str = "food", *, kind: str = "expense", note: str = "") -> dict:
    return {"id": id_, "entry_type": kind, "amount": amount, "category": category, "note": f"[b:card] {note}".strip(), "entry_date": day.isoformat()}


def _dataset() -> list[dict]:
    """Июнь–сентябрь: еда каждый день 30k (в сентябре 45k), такси через день 20k, зарплата 1-го, одна аномалия."""
    rows = []
    i = 0
    d = date(2026, 6, 1)
    while d <= TODAY:
        i += 1
        rows.append(_e(i, d, 45000 if d.month == 9 else 30000, "food", note="обед"))
        if d.day % 2 == 0:
            i += 1
            rows.append(_e(i, d, 20000, "transport", note="такси"))
        if d.day == 1:
            i += 1
            rows.append(_e(i, d, 5000000, "salary", kind="income"))
        if d.day == 5:
            i += 1
            rows.append(_e(i, d, 150000, "home", note="интернет"))
        d += timedelta(days=1)
    rows.append(_e(9001, date(2026, 9, 15), 900000, "food", note="банкет"))
    rows.append(_e(9002, date(2026, 9, 10), 45000, "food", note="обед"))  # дубль
    return rows


def test_monthly_trend_has_three_full_months_and_current():
    trend = analysis.monthly_trend(_dataset(), TODAY)
    assert [m["month"] for m in trend] == ["2026-06", "2026-07", "2026-08", "2026-09"]
    assert trend[-1]["partial"] is True and trend[0]["partial"] is False
    assert trend[0]["income"] == 5000000 and trend[0]["ops"] > 30
    assert trend[0]["top_categories"][0]["category"] == "food"


def test_category_trends_detect_food_growth():
    ct = analysis.category_trends(_dataset(), TODAY)
    food = next(c for c in ct if c["category"] == "food")
    assert food["change_pct"] and food["change_pct"] > 40  # 45k vs 30k в день + банкет
    transport = next(c for c in ct if c["category"] == "transport")
    assert abs(transport["change_pct"]) < 15


def test_forecast_projects_rest_of_month_and_budget_alert():
    fc = analysis.forecast(
        _dataset(), TODAY, balances={"card": 3000000, "cash": 200000}, budgets={"food": 1000000},
        recurring=[{"title": "Аренда", "amount": 500000, "day_of_month": 25, "enabled": True}],
    )
    assert fc["days_left"] == 9
    assert fc["projected_month_expense"] > fc["spent_this_month"] > 0
    assert fc["recurring_remaining"] == 500000 and fc["recurring_items"][0]["title"] == "Аренда"
    assert fc["wallet_end_of_month"] < fc["wallet_now"]
    assert fc["budget_alerts"] and fc["budget_alerts"][0]["category"] == "food"


def test_anomalies_find_big_entry_and_duplicate():
    an = analysis.anomalies(_dataset(), TODAY)
    assert an["big_entries"][0]["id"] == "9001" and an["big_entries"][0]["x_median"] > 10
    assert any(d["ids"] and "9002" in d["ids"] for d in an["duplicates"])
    assert an["expensive_days"][0]["date"] == "2026-09-15"


def test_overpaying_reports_frequent_and_subscriptions():
    op = analysis.overpaying(_dataset(), TODAY)
    cats_ = [x["category"] for x in op["frequent_small"]]
    assert "food" in cats_ and "transport" in cats_
    assert op["repeated_notes"][0]["note"] == "обед"
    subs = {s["note"] for s in op["possible_subscriptions"]}
    assert "интернет" in subs
    assert op["category_shares"][0]["category"] == "food"


def test_nutrition_analysis_weekday_weekend_and_streak():
    logs = []
    for i in range(10):
        d = TODAY - timedelta(days=i)
        kcal = 2600 if d.weekday() >= 5 else 1800
        logs.append({"created_at": f"{d.isoformat()}T07:00:00+00:00", "calories": kcal, "protein": 80})
    na = analysis.nutrition_analysis(logs, TODAY, tz=TZ, plan={"daily_calories": 2000, "protein": 120})
    assert na["days_logged"] == 10 and na["streak_days"] == 10
    assert na["avg_weekend_kcal"] > na["avg_weekday_kcal"]
    assert na["days_over_target"]  # выходные > 2500
    assert na["target_protein"] == 120


def test_full_analysis_shapes_and_labels():
    data = analysis.full_analysis(
        entries=_dataset(), logs=[], today=TODAY, tz=TZ, balances={"card": 1, "cash": 0, "lent": 0, "debt": 0}, settings={},
        recurring=[], budgets={}, plan=None, focus="all",
    )
    assert set(data) >= {"monthly_trend", "category_trends", "forecast", "anomalies", "overpaying", "debts", "nutrition", "category_labels"}
    assert data["nutrition"] is None
    assert data["category_labels"]["food"].startswith("Еда")
    fin_only = analysis.full_analysis(entries=[], logs=[], today=TODAY, tz=TZ, balances={}, settings={}, recurring=[], budgets={}, plan=None, focus="nutrition")
    assert "monthly_trend" not in fin_only

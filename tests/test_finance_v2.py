"""Лимиты, регулярные платежи, «голая» сумма, Excel."""
from datetime import date

from bot import export as export_mod
from bot import finance as fin


def _e(kind, amount, note, day, category):
    return {"entry_type": kind, "amount": amount, "note": note, "entry_date": day, "category": category}


ENTRIES = [
    _e("expense", 900_000, "[b:card] обеды", "2026-09-10", "food"),
    _e("expense", 300_000, "[b:card] такси", "2026-09-12", "transport"),
    _e("income", 5_000_000, "[b:card] зп", "2026-09-01", "salary"),
    _e("expense", 200_000, "[x:card>cash] снял", "2026-09-02", "transfer"),
]


def test_budget_statuses_and_warnings():
    stats = fin.compute_stats(ENTRIES, fin.period_for("month", date(2026, 9, 15)))
    statuses = fin.budget_statuses(stats, {"food": 1_000_000, "transport": 1_000_000, "fun": 500_000})
    assert statuses[0].category == "food" and abs(statuses[0].ratio - 0.9) < 1e-9
    assert statuses[-1].category == "fun" and statuses[-1].spent == 0
    warns = fin.budget_warnings(statuses, lang="ru")
    assert len(warns) == 1 and "Еда" in warns[0] and "100 000" in warns[0]
    over = fin.budget_statuses(stats, {"food": 500_000})
    assert "превышен" in fin.budget_warnings(over)[0]


def test_bare_amount():
    assert fin.bare_amount("25000") == ("expense", 25000.0)
    assert fin.bare_amount("+300000") == ("income", 300000.0)
    assert fin.bare_amount("доход 5 млн") == ("income", 5_000_000.0)
    assert fin.bare_amount("25000 наличными") == ("expense", 25000.0)
    assert fin.bare_amount("такси 25000") is None
    assert fin.bare_amount("50") is None


def test_parse_recurring():
    r = fin.parse_recurring("интернет 150000 5")
    assert r == {"title": "интернет", "amount": 150000.0, "day_of_month": 5, "category": "telecom", "bucket": "card"}
    r = fin.parse_recurring("аренда 2 млн 1 числа")
    assert r["amount"] == 2_000_000 and r["day_of_month"] == 1 and r["category"] == "home"
    r = fin.parse_recurring("kredit 1.2 mln har oyning 15")
    assert r["title"] == "kredit" and r["day_of_month"] == 15 and r["category"] == "debt"
    assert fin.parse_recurring("ютуб 50000") is None
    assert fin.parse_recurring("привет") is None


def test_recurring_due_and_remaining():
    items = [
        {"id": 1, "title": "аренда", "amount": 2_000_000, "day_of_month": 1, "enabled": True, "last_done_key": "2026-09"},
        {"id": 2, "title": "интернет", "amount": 150_000, "day_of_month": 5, "enabled": True, "last_done_key": "2026-08"},
        {"id": 3, "title": "кредит", "amount": 1_000_000, "day_of_month": 31, "enabled": True, "last_done_key": None, "last_asked_key": "2026-09"},
        {"id": 4, "title": "пауза", "amount": 999, "day_of_month": 2, "enabled": False},
    ]
    today = date(2026, 9, 15)
    remaining, pending = fin.recurring_remaining(items, today)
    assert remaining == 1_150_000 and [p["id"] for p in pending] == [2, 3]
    due = fin.recurring_due_today(items, today)
    assert [d["id"] for d in due] == [2]  # 3 — уже спрашивали в этом месяце, 31-е ещё не наступило
    assert fin.recurring_due_day(31, 2026, 2) == 28


def test_top_operations_puts_latest_first():
    entries = [
        _e("expense", 40_000, "[b:card] обед", "2026-09-15", "food"),
        _e("expense", 25_000, "[b:card] такси", "2026-09-14", "transport"),
        _e("expense", 25_000, "[b:card] такси", "2026-09-13", "transport"),
        _e("expense", 25_000, "[b:card] такси", "2026-09-12", "transport"),
    ]
    top = fin.top_operations(entries, limit=5)
    assert top[0]["note"] == "обед" and top[1]["note"] == "такси"


def test_build_xlsx_smoke():
    data = export_mod.build_xlsx(ENTRIES, period=fin.period_for("month", date(2026, 9, 15)), lang="ru", currency="UZS")
    assert data[:2] == b"PK" and len(data) > 3000

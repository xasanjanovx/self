"""Локальный разбор задач и группировка для экранов."""
from __future__ import annotations

from datetime import date

from bot import tasks as tasks_mod
from bot.keyboards import main_menu_keyboard, tasks_keyboard

TODAY = date(2026, 9, 22)  # вторник


def _p(text: str):
    r = tasks_mod.parse_task(text, TODAY)
    return r.text, r.due_date.isoformat() if r.due_date else None, r.due_time


def test_parse_relative_days_and_time():
    assert _p("позвонить маме завтра в 18:00") == ("позвонить маме", "2026-09-23", "18:00")
    assert _p("встреча послезавтра 14:30") == ("встреча", "2026-09-24", "14:30")
    assert _p("сегодня в 21 тренировка") == ("тренировка", "2026-09-22", "21:00")
    assert _p("ertaga soat 9 da shifokor") == ("shifokor", "2026-09-23", "09:00")
    # только время → сегодня
    assert _p("в 19:00 созвон") == ("созвон", "2026-09-22", "19:00")


def test_parse_dates_and_weekdays():
    assert _p("3 ноября поздравить брата") == ("поздравить брата", "2026-11-03", None)
    assert _p("в пятницу забрать посылку") == ("забрать посылку", "2026-09-25", None)
    assert _p("25.09 оплатить интернет") == ("оплатить интернет", "2026-09-25", None)
    assert _p("заплатить за свет 30.12.2026") == ("заплатить за свет", "2026-12-30", None)
    assert _p("5 числа отдать долг") == ("отдать долг", "2026-10-05", None)
    assert _p("через неделю сдать отчёт") == ("сдать отчёт", "2026-09-29", None)
    # прошедшая дата без года — следующий год
    assert _p("1 января позвонить") == ("позвонить", "2027-01-01", None)


def test_parse_without_date_keeps_text():
    assert _p("купить лампочку") == ("купить лампочку", None, None)
    assert _p("купить 2 лампочки") == ("купить 2 лампочки", None, None)


def test_group_and_dashboard_pick():
    rows = [
        {"id": 1, "text": "просрочено", "due_date": "2026-09-20"},
        {"id": 2, "text": "сегодня вечером", "due_date": "2026-09-22", "due_time": "20:00"},
        {"id": 3, "text": "сегодня утром", "due_date": "2026-09-22", "due_time": "09:00"},
        {"id": 4, "text": "завтра", "due_date": "2026-09-23"},
        {"id": 5, "text": "без даты 1"},
        {"id": 6, "text": "без даты 2"},
        {"id": 7, "text": "далеко", "due_date": "2026-12-01"},
        {"id": 8, "text": "сделано", "due_date": "2026-09-22", "done": True},
    ]
    g = tasks_mod.group(rows, TODAY)
    assert [t["id"] for t in g.overdue] == [1]
    assert [t["id"] for t in g.today] == [3, 2]  # по времени
    assert [t["id"] for t in g.upcoming] == [4, 7]
    assert [t["id"] for t in g.undated] == [5, 6]
    assert g.total == 7
    picked, hidden = tasks_mod.dashboard_pick(rows, TODAY)
    # просроченные + сегодня, затем 3 ближайших (сначала с датой)
    assert [t["id"] for t in picked] == [1, 3, 2, 4, 7, 5]
    assert hidden == 1
    # раньше задачи без срока терялись, если были цели: теперь всегда видны
    picked, hidden = tasks_mod.dashboard_pick([{"id": 5, "text": "без даты"}], TODAY)
    assert [t["id"] for t in picked] == [5] and hidden == 0


def test_when_label():
    assert tasks_mod.when_label({"due_date": "2026-09-22", "due_time": "18:00"}, TODAY) == "сегодня 18:00"
    assert tasks_mod.when_label({"due_date": "2026-09-23"}, TODAY) == "завтра"
    assert tasks_mod.when_label({"due_date": "2026-09-25"}, TODAY) == "пт 25.09"
    assert tasks_mod.when_label({"due_date": "2026-09-19"}, TODAY) == "просрочено 3 дн."
    assert tasks_mod.when_label({"text": "x"}, TODAY) == ""


def test_keyboards():
    kb = main_menu_keyboard("ru")
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "menu:tasks" in data and "menu:goals" in data
    assert data.index("menu:tasks") < data.index("menu:vacancy")
    kb = tasks_keyboard([{"id": 7, "text": "очень длинный текст задачи, который надо обрезать"}], "ru")
    first = kb.inline_keyboard[0][0]
    assert first.callback_data == "task:done:7" and first.text.endswith("…") and len(first.text) <= 32
    kb = tasks_keyboard([{"id": 7, "text": "x"}], "ru", done_view=True)
    assert kb.inline_keyboard[0][0].callback_data == "task:del:7"

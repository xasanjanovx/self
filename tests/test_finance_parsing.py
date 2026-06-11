"""Tests for the fragile finance/intent parsing helpers in bot.main.

conftest.py sets dummy credentials so importing bot.main is safe and offline.
All functions under test are pure string logic.
"""
from datetime import date

import bot.main as m


# ---- amount extraction ----

def test_extract_amount_basic():
    assert m._extract_amount_from_text("расход 25000 еда") == 25000.0


def test_extract_amount_with_spaces():
    assert m._extract_amount_from_text("доход 1 200 000 зарплата") == 1200000.0


def test_extract_amount_none_when_absent():
    assert m._extract_amount_from_text("нет суммы здесь") is None


# ---- finance operation detection ----

def test_looks_like_finance_expense():
    assert m._looks_like_finance_operation("расход 25000 еда") is True


def test_looks_like_finance_income():
    assert m._looks_like_finance_operation("доход 300000 зарплата") is True


def test_looks_like_finance_rejects_greeting():
    assert m._looks_like_finance_operation("привет как дела") is False


def test_looks_like_finance_rejects_food_without_marker():
    # has a number (3) but no finance marker -> should not be finance
    assert m._looks_like_finance_operation("омлет из 3 яиц") is False


# ---- transfer route inference ----

def test_transfer_card_to_cash():
    assert m._infer_transfer_route("перевел 100000 с карты на наличные") == ("card", "cash")


def test_transfer_withdraw_card_to_cash():
    assert m._infer_transfer_route("снял с карты 50000") == ("card", "cash")


def test_transfer_give_loan_with_account():
    # A loan needs an explicit source account to be treated as a transfer.
    assert m._infer_transfer_route("дал в долг 100000 с карты") == ("card", "lent")


def test_transfer_repay_loan_with_account():
    assert m._infer_transfer_route("вернул долг 50000 с карты") == ("card", "debt")


def test_transfer_loan_without_account_is_not_transfer():
    # Without an account the parser must not guess -> handled as a normal entry.
    assert m._infer_transfer_route("дал в долг 100000") is None


def test_transfer_plain_expense_is_not_transfer():
    assert m._infer_transfer_route("расход 25000 еда") is None


# ---- main-menu intent ----

def test_menu_intent_finance():
    assert m._main_menu_intent("финансы") == "finance"


def test_menu_intent_nutrition():
    assert m._main_menu_intent("питание") == "calorie"


def test_menu_intent_vacancy():
    assert m._main_menu_intent("вакансия") == "vacancy"


def test_menu_intent_none():
    assert m._main_menu_intent("просто привет") is None


# ---- progress bar / weekday ----

def test_progress_bar_half():
    assert m._progress_bar(0.5, 10) == "▰▰▰▰▰▱▱▱▱▱"


def test_progress_bar_clamps_over_one():
    assert m._progress_bar(2.0, 10) == "▰" * 10


def test_progress_bar_zero():
    assert m._progress_bar(0.0, 10) == "▱" * 10


def test_weekday_name_ru():
    # 2026-06-11 is a Thursday
    assert m._weekday_name(date(2026, 6, 11), "ru") == "четверг"


def test_weekday_name_uz():
    assert m._weekday_name(date(2026, 6, 11), "uz") == "payshanba"

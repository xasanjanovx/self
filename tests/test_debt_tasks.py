"""Долг со сроком → задача за день до срока (bot/debt_tasks.py): чистая сверка plan()."""
from __future__ import annotations

from datetime import date

from bot import debt_tasks as dt

TODAY = date(2026, 9, 30)


def _row(person: str, due: str, amount: float = 1_050_000, side: str = "debt") -> dict:
    return {"person": person, "side": side, "due_date": due, "amount": amount, "loan_ids": ["1"]}


def _task(id_: int, person: str, due: str, *, done: bool = False, text: str = "x") -> dict:
    return {"id": id_, "ref_key": dt.ref_key(person, date.fromisoformat(due)), "done": done, "text": text}


def test_task_is_created_one_day_before_due():
    create, retext, drop = dt.plan([_row("UZUM BANK", "2026-10-26")], [], TODAY, currency="сум")
    assert [c["due_date"] for c in create] == ["2026-10-25"]
    assert create[0]["ref_key"] == "debt:uzum bank:2026-10-26"
    assert "UZUM BANK" in create[0]["text"] and "1 050 000" in create[0]["text"] and "26.10" in create[0]["text"]
    assert retext == [] and drop == []


def test_due_tomorrow_or_today_gives_task_for_today():
    create, _, _ = dt.plan([_row("TEZ", "2026-10-01"), _row("Али", "2026-09-30")], [], TODAY)
    assert sorted(c["due_date"] for c in create) == ["2026-09-30", "2026-09-30"]


def test_only_my_debts_and_only_open_ones():
    rows = [_row("Асилбек", "2026-10-10", side="lent"), _row("TEZ", "2026-10-10", amount=0.4)]
    assert dt.plan(rows, [], TODAY) == ([], [], [])


def test_overdue_debt_gets_no_new_task():
    assert dt.plan([_row("TEZ", "2026-09-20")], [], TODAY) == ([], [], [])


def test_existing_task_is_not_duplicated_even_if_done_or_deleted_by_user():
    rows = [_row("UZUM BANK", "2026-10-26")]
    text = dt.task_text("UZUM BANK", 1_050_000, date(2026, 10, 26), uz=False, currency="")
    assert dt.plan(rows, [_task(1, "UZUM BANK", "2026-10-26", text=text)], TODAY) == ([], [], [])
    assert dt.plan(rows, [_task(1, "UZUM BANK", "2026-10-26", done=True)], TODAY) == ([], [], [])  # закрыта — не пересоздаём


def test_amount_change_updates_open_task_text():
    rows = [_row("UZUM BANK", "2026-10-26", amount=500_000)]
    create, retext, drop = dt.plan(rows, [_task(7, "UZUM BANK", "2026-10-26")], TODAY)
    assert create == [] and drop == [] and retext[0][0] == 7 and "500 000" in retext[0][1]


def test_repaid_debt_or_moved_due_removes_open_task_but_keeps_done_one():
    old = [_task(1, "UZUM BANK", "2026-10-26"), _task(2, "TEZ", "2026-10-05", done=True)]
    create, _, drop = dt.plan([_row("UZUM BANK", "2026-11-05")], old, TODAY)
    assert drop == [1]  # срок сдвинули → старая задача лишняя; закрытая остаётся как отметка
    assert [c["ref_key"] for c in create] == ["debt:uzum bank:2026-11-05"]
    assert dt.plan([], old, TODAY) == ([], [], [1])  # долг погашен


def test_uzbek_text():
    create, _, _ = dt.plan([_row("TEZ", "2026-10-10")], [], TODAY, uz=True, currency="so'm")
    assert create[0]["text"].startswith("💳 Qarzni qaytarish: TEZ") and "muddat 10.10" in create[0]["text"]

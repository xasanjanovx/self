"""Бухгалтер долгов: займы-транши со своими сроками, погашение по ближайшему сроку, перекредитование,
проценты при переплате, карта/наличные, инструменты record_debt / list_debts / set_debt_deadline."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from bot import agent_tools as tools
from bot import agent_tools_debts as dt
from bot import debts
from bot import finance as fin
from bot import proactive, undo
from bot.profile import Profile

TODAY = date(2026, 9, 26)


def _run(coro):
    return asyncio.run(coro)


def _profile(uid: int = 90, lang: str = "ru") -> Profile:
    return Profile(telegram_id=uid, lang=lang, tz_name="Asia/Tashkent", currency="UZS", first_name="Тест", username="t")


def _x(id_: int, day: str, amount: float, note: str) -> dict:
    return {"id": id_, "entry_type": "expense", "amount": amount, "category": "transfer", "note": note, "entry_date": day}


# его реальный случай на 26.09: старые долги Uzum и TEZ, срок Uzum 01.10 поставлен 22.09 «по человеку»
OWNER = [
    _x(7, "2026-09-15", 200_000, "[x:card>cash] снял наличные"),
    _x(15, "2026-09-16", 1_000_000, "[x:init>lent] Асилбек"),
    _x(19, "2026-09-16", 2_205_000, "[x:init>debt] UZUM BANK"),
    _x(20, "2026-09-16", 986_250, "[x:init>debt] TEZ"),
]
SETTINGS = {"card_base": 1_506_000, "cash_base": 22_000}
DEADLINES = [{"person": "UZUM BANK", "side": "debt", "due_date": "2026-10-01", "created_at": "2026-09-22T06:52:17+00:00"}]


def _plan(items, text="", entries=OWNER, lang="ru"):
    book = fin.debt_book(entries, SETTINGS, DEADLINES)
    return debts.plan(items, book=book, balances=fin.compute_balances(entries, SETTINGS), today=TODAY, text=text, lang=lang)


# ------------------------------------------------------------------ note tags
def test_tags_are_hidden_from_note_and_kept_on_edit():
    note = fin.note_with_transfer("UZUM BANK", "debt", "card", due="2026-10-26")
    assert note == "[x:debt>card] [due:2026-10-26] UZUM BANK"
    assert fin.clean_note(note) == "UZUM BANK" and fin.due_from_note(note) == date(2026, 10, 26)
    assert fin.transfer_from_note(note) == ("debt", "card")
    pay = fin.note_with_transfer("UZUM BANK", "card", "debt", targets=["19", "65"])
    assert fin.targets_from_note(pay) == ["19", "65"] and fin.clean_note(pay) == "UZUM BANK"
    assert fin.renote(note, "Uzum Bank") == "[x:debt>card] [due:2026-10-26] Uzum Bank"
    assert fin.renote(note, "UZUM BANK", due=None) == "[x:debt>card] UZUM BANK"
    assert fin.renote("[b:cash] обед", "ужин") == "[b:cash] ужин"


def test_forgiven_debt_reduces_balance():
    bal = {"card": 0.0, "cash": 0.0, "lent": 0.0, "debt": 0.0}
    fin.apply_to_balances(bal, kind="transfer", amount=500, src="init", dst="debt")
    fin.apply_to_balances(bal, kind="transfer", amount=200, src="debt", dst="init")
    fin.apply_to_balances(bal, kind="transfer", amount=100, src="debt", dst="card")
    fin.apply_to_balances(bal, kind="transfer", amount=50, src="card", dst="debt")
    assert bal["debt"] == 500 - 200 + 100 - 50 and bal["card"] == 50


# ------------------------------------------------------------------ книга займов
def test_book_keeps_loans_apart_and_repays_nearest_due_first():
    entries = OWNER + [
        _x(65, "2026-09-26", 1_050_000, "[x:debt>card] [due:2026-09-30] UZUM BANK"),  # новый займ со сроком РАНЬШЕ старого
        _x(66, "2026-09-26", 500_000, "[x:card>debt] UZUM BANK"),
    ]
    uzum = fin.debt_book(entries, SETTINGS, DEADLINES)["debt"]["uzum bank"]
    loans = {t.id: t for t in uzum.tranches}
    assert loans["19"].due == date(2026, 10, 1)  # срок «по человеку» — старому займу
    assert loans["65"].left == 550_000 and loans["19"].left == 2_205_000  # погашение ушло в займ с ближайшим сроком
    assert uzum.total == 2_755_000
    # явная цель погашения
    entries[-1] = _x(66, "2026-09-26", 500_000, "[x:card>debt] [t:19] UZUM BANK")
    uzum = fin.debt_book(entries, SETTINGS, DEADLINES)["debt"]["uzum bank"]
    assert {t.id: t.left for t in uzum.tranches} == {"19": 1_705_000, "65": 1_050_000}


def test_overpay_becomes_credit_and_eats_next_loan():
    entries = OWNER + [_x(64, "2026-09-26", 1_050_000, "[x:card>debt] TEZ")]
    tez = fin.debt_book(entries, SETTINGS)["debt"]["tez"]
    assert tez.total == pytest.approx(-63_750) and tez.credit == pytest.approx(63_750)
    assert ("TEZ", pytest.approx(-63_750)) in fin.debt_ledger(entries, SETTINGS)["debt"]
    entries.append(_x(70, "2026-09-27", 100_000, "[x:debt>card] TEZ"))
    assert fin.debt_book(entries, SETTINGS)["debt"]["tez"].total == pytest.approx(36_250)


def test_person_deadline_does_not_apply_to_later_loans():
    entries = OWNER + [_x(65, "2026-09-26", 1_050_000, "[x:debt>card] UZUM BANK")]
    rows = fin.effective_deadlines(fin.debt_book(entries, SETTINGS, DEADLINES))
    assert rows == [{"person": "UZUM BANK", "side": "debt", "due_date": "2026-10-01", "amount": 2_205_000, "loan_ids": ["19"]}]


def test_ledger_totals_unchanged_for_old_data():
    ledger = fin.debt_ledger(OWNER, SETTINGS)
    assert ledger["debt"] == [("UZUM BANK", 2_205_000), ("TEZ", 986_250)]
    assert ledger["lent"] == [("Асилбек", 1_000_000)]


# ------------------------------------------------------------------ план бухгалтера
def test_refinance_uzum_to_tez_leaves_rest_on_card():
    p = _plan([{"action": "borrow", "person": "узум", "amount": 1_050_000, "due_date": "26.10"}, {"action": "repay", "person": "тез"}],
              "взял у узум 1 050 000 до 26.10 и погасил тез")
    assert p.ask is None and p.error is None
    assert [(r["amount"], r["note"]) for r in p.rows] == [
        (1_050_000, "[x:debt>card] [due:2026-10-26] UZUM BANK"), (986_250, "[x:card>debt] [t:20] TEZ")]
    assert any("на карте осталось 63 750" in line for line in p.lines)
    assert p.after["card"] - p.before["card"] == pytest.approx(63_750)
    assert [debts.describe(c) for c in p.people] == ["UZUM BANK: 3 255 000 — 2 205 000 до 01.10, 1 050 000 до 26.10", "TEZ: долг закрыт"]


def test_bank_loan_without_due_asks_for_due_and_saves_nothing():
    p = _plan([{"action": "borrow", "person": "Uzum", "amount": 1_050_000}], "взял у узум 1 050 000")
    assert not p.rows and p.ask["question"] == "UZUM BANK 1 050 000 — до какого числа вернуть?"
    assert p.ask["options"] == ["Через месяц — 26.10", "Без срока"] and "no_due" in p.ask["hint"]
    p = _plan([{"action": "borrow", "person": "Uzum", "amount": 1_050_000, "no_due": True}], "без срока")
    assert p.rows[0]["note"] == "[x:debt>card] UZUM BANK"  # банк → карта без вопроса


def test_due_date_only_when_said_and_no_fake_names():
    # модель сама «дописала» срок, а он его не называл — банк: спросим срок
    p = _plan([{"action": "borrow", "person": "Uzum", "amount": 1_050_000, "due_date": "2026-10-26"}], "взял сегодня у узум 1 050 000")
    assert p.ask and "до какого числа" in p.ask["question"]
    p = _plan([{"action": "borrow", "person": "Uzum", "amount": 1_050_000, "due_date": "2026-10-26"}], "Через месяц — 26.10")
    assert p.rows[0]["note"] == "[x:debt>card] [due:2026-10-26] UZUM BANK"
    p = _plan([{"action": "borrow", "person": "без имени", "amount": 300_000}], "взял в долг 300 тысяч")
    assert p.ask["question"].startswith("У кого взяли")


def test_person_loan_asks_card_or_cash_unless_said():
    p = _plan([{"action": "borrow", "person": "Алишер", "amount": 500_000, "account": "card"}], "взял у Алишера 500к")
    assert not p.rows and p.ask["options"] == ["💳 Карта", "💵 Наличные"]  # модель угадала «card», а он не говорил
    p = _plan([{"action": "borrow", "person": "Алишер", "amount": 500_000, "account": "cash"}], "💵 Наличные")
    assert p.rows[0]["note"] == "[x:debt>cash] Алишер" and p.after["cash"] - p.before["cash"] == 500_000
    p = _plan([{"action": "lend", "person": "Алишер", "amount": 200_000}], "дал Алишеру 200к наличными")
    assert p.rows[0]["note"] == "[x:cash>lent] Алишер"
    p = _plan([{"action": "borrow", "person": "Алишер", "amount": 500_000}], "Alisher 500 ming berdi", lang="uz")
    assert p.ask["options"] == ["💳 Karta", "💵 Naqd"]


def test_overpayment_asks_interest_or_overpay():
    p = _plan([{"action": "repay", "person": "TEZ", "amount": 1_050_000}], "вернул тез 1 050 000 с карты")
    assert not p.rows and "Разница 63 750" in p.ask["question"] and p.ask["options"][0] == "Проценты / комиссия"
    p = _plan([{"action": "repay", "person": "TEZ", "amount": 1_050_000, "interest": 63_750}], "Проценты / комиссия")
    assert [(r["amount"], r["category"], r["entry_type"]) for r in p.rows] == [(986_250, "transfer", "expense"), (63_750, "debt", "expense")]
    assert p.after["debt"] == pytest.approx(p.before["debt"] - 986_250)
    p = _plan([{"action": "repay", "person": "TEZ", "amount": 1_050_000, "keep_overpay": True}], "Переплата")
    assert len(p.rows) == 1 and p.people[0].total == pytest.approx(-63_750)


def test_several_loans_without_amount_asks_which():
    entries = OWNER + [_x(65, "2026-09-26", 1_050_000, "[x:debt>card] [due:2026-10-26] UZUM BANK")]
    p = _plan([{"action": "repay", "person": "Uzum"}], "погасил узум", entries=entries)
    assert p.ask["options"] == ["Весь долг 3 255 000", "2 205 000 · до 01.10", "1 050 000 · до 26.10"]
    p = _plan([{"action": "repay", "person": "Uzum", "loan_id": "65"}], "1 050 000 · до 26.10", entries=entries)
    assert p.rows[0]["amount"] == 1_050_000 and p.rows[0]["note"] == "[x:card>debt] [t:65] UZUM BANK"
    p = _plan([{"action": "repay", "person": "Uzum", "amount": 3_000_000}], "вернул узум 3 млн", entries=entries)
    assert p.rows[0]["note"] == "[x:card>debt] [t:19,65] UZUM BANK"
    assert "закрыт займ 2 205 000 (до 01.10)" in p.lines[0] and "по займу до 26.10 осталось 255 000" in p.lines[0]


def test_unknown_creditor_repay_asks_before_writing():
    p = _plan([{"action": "repay", "person": "Хамкорбанк", "amount": 300_000}], "вернул хамкорбанку 300к")
    assert not p.rows and p.ask["options"][0] == "Долг был до учёта" and "Обычный расход" in p.ask["options"]
    p = _plan([{"action": "repay", "person": "Хамкорбанк", "amount": 300_000, "existed_before": True}], "Долг был до учёта")
    assert [r["note"] for r in p.rows] == ["[x:init>debt] Хамкорбанк", "[x:card>debt] Хамкорбанк"]
    assert p.after["debt"] == p.before["debt"] and p.after["card"] == p.before["card"] - 300_000


def test_model_picked_loan_is_ignored_unless_he_named_it():
    entries = OWNER + [_x(65, "2026-09-26", 1_050_000, "[x:debt>card] [due:2026-10-26] UZUM BANK")]
    p = _plan([{"action": "repay", "person": "UZUM BANK", "loan_id": "19"}], "погасил долг Узум", entries=entries)
    assert not p.rows and p.ask["options"][0] == "Весь долг 3 255 000"
    p = _plan([{"action": "repay", "person": "UZUM BANK", "loan_id": "19"}], "2 205 000 · до 01.10", entries=entries)
    assert p.rows[0]["amount"] == 2_205_000 and "[t:19]" in p.rows[0]["note"]


def test_wrong_direction_offers_flip():
    p = _plan([{"action": "repay", "person": "Асилбек", "amount": 200_000}], "вернул Асилбеку 200 тысяч")
    assert not p.rows and p.ask["options"][0] == "Наоборот: Асилбек вернул мне" and "action=collect" in p.ask["hint"]
    assert not any(o.startswith("Это ") for o in p.ask["options"])  # UZUM BANK на Асилбека не похож


def test_repay_of_closed_debt_asks_instead_of_interest():
    entries = OWNER + [_x(64, "2026-09-26", 986_250, "[x:card>debt] TEZ")]
    p = _plan([{"action": "repay", "person": "TEZ", "amount": 1_050_000}], "вернул тез 1 050 000", entries=entries)
    assert not p.rows and "Открытого долга перед «TEZ» нет (долг закрыт)" in p.ask["question"]
    assert p.ask["options"][-1] == "Уже записано" and "ничего не записывай" in p.ask["hint"]
    assert _plan([{"action": "creditor_forgave", "person": "TEZ"}], entries=entries).error


def test_new_loan_does_not_merge_similar_but_different_people():
    entries = OWNER + [_x(30, "2026-09-20", 100_000, "[x:card>lent] Абдулазиз")]
    p = _plan([{"action": "lend", "person": "Азиз", "amount": 50_000, "account": "card"}], "дал Азизу 50к с карты", entries=entries)
    assert p.rows[0]["note"] == "[x:card>lent] Азиз"
    p = _plan([{"action": "collect", "person": "Асельбек", "amount": 300_000}], "асельбек вернул 300к на карту")
    assert p.rows[0]["note"] == "[x:lent>card] [t:15] Асилбек"


def test_buy_on_credit_is_expense_plus_debt_without_card_change():
    p = _plan([{"action": "buy_on_credit", "person": "Uzum Nasiya", "amount": 3_000_000, "category": "shopping", "note": "телефон",
                "due_date": "2026-12-26"}], "купил телефон в рассрочку на 3 месяца")
    assert [r["category"] for r in p.rows] == ["transfer", "shopping"]
    assert p.after["card"] == p.before["card"] and p.after["debt"] == p.before["debt"] + 3_000_000
    assert p.rows[0]["note"] == "[x:debt>card] [due:2026-12-26] Uzum Nasiya"  # не сливается с UZUM BANK
    # вернуть «Uzum Nasiya», которой нет в учёте, — не гасим молча UZUM BANK, а спрашиваем
    p = _plan([{"action": "repay", "person": "Uzum Nasiya", "amount": 500_000}], "вернул uzum nasiya 500к")
    assert not p.rows and "Это UZUM BANK" in p.ask["options"]
    p = _plan([{"action": "repay", "person": "Узум", "amount": 500_000}], "вернул узум 500к")
    assert p.rows[0]["note"] == "[x:card>debt] [t:19] UZUM BANK"


def test_numbers_guard_keeps_accountant_numbers():
    from bot.handlers.agent import numbers_match

    fixed = "✅ Погашено TEZ 986 250 с карты\n💼 Карта: 374 300 → -675 700"
    assert numbers_match("Погасил TEZ на 986 250, на карте теперь −675 700.", fixed)
    assert not numbers_match("Погасил TEZ, на карте −547 700.", fixed)
    assert numbers_match("Готово, TEZ закрыт до 26.10 ✅", fixed)  # мелкие числа (даты) не проверяем
    assert not numbers_match("", fixed)


def test_agent_reply_with_wrong_numbers_is_replaced():
    from bot.ai import AgentStep
    from bot.handlers import agent

    steps = [AgentStep(parts=[{"functionCall": {"name": "record_debt", "args": {}}}], text="", calls=[("record_debt", {})]),
             AgentStep(parts=[{"text": "Готово, на карте −547 700"}], text="Готово, на карте −547 700", calls=[])]

    async def step_fn(contents, **kw):
        return steps.pop(0)

    async def run_tool(name, args, ctx):
        ctx.fixed_reply = "✅ Погашено TEZ 986 250\n💼 Карта: 374 300 → -675 700"
        ctx.mutated = True
        return {"done": ["Погашено TEZ 986 250"]}

    res = _run(agent.run_agent(_profile(), "вернул тез", [], snapshot="—", step_fn=step_fn, run_tool=run_tool))
    assert res.text.startswith("✅ Погашено TEZ 986 250") and res.contents[-1]["parts"][0]["text"] == res.text


def test_forgive_and_negative_card_warning():
    p = _plan([{"action": "i_forgave", "person": "Асилбек"}], "Асилбек не вернёт, списываю")
    assert p.rows[0]["note"] == "[x:lent>init] [t:15] Асилбек" and p.after["lent"] == 0 and p.after["card"] == p.before["card"]
    p = _plan([{"action": "repay", "person": "UZUM BANK"}], "погасил узум полностью с карты")
    assert p.rows[0]["amount"] == 2_205_000 and p.warnings and "карте получается" in p.warnings[0]


def test_existing_debt_and_bad_input():
    p = _plan([{"action": "owed_existing", "person": "Иззатилло ака", "amount": 1_100_000}], "мне должен Иззатилло ака 1.1 млн")
    assert p.rows[0]["note"] == "[x:init>lent] Иззатилло ака" and p.after["card"] == p.before["card"]
    assert _plan([{"action": "steal", "person": "x", "amount": 1}]).error
    p = _plan([{"action": "borrow", "person": "", "amount": 1}], "взял в долг 100к")
    assert p.ask and "У кого" in p.ask["question"]
    assert debts.parse_due("26.10", TODAY) == date(2026, 10, 26) and debts.parse_due("01.02", TODAY) == date(2027, 2, 1)


# ------------------------------------------------------------------ инструменты
class FakeDB:
    def __init__(self, entries):
        self.entries = [dict(r) for r in entries]
        self.deadlines: list[dict] = []
        self.next_id = 100

    async def add_finance_entries(self, uid, rows, *, entry_date, source="manual"):
        out = []
        for r in rows:
            self.next_id += 1
            row = {"id": self.next_id, **r, "entry_date": r.get("entry_date") or entry_date.isoformat()}
            self.entries.insert(0, row)
            out.append(row)
        return out

    async def get_finance_entry(self, uid, rid):
        return next((dict(r) for r in self.entries if str(r["id"]) == str(rid)), None)

    async def update_finance_entry(self, uid, rid, fields):
        for r in self.entries:
            if str(r["id"]) == str(rid):
                r.update(fields)

    async def delete_finance_entries(self, uid, ids):
        self.entries = [r for r in self.entries if r["id"] not in ids]

    async def ensure_available(self, name):
        return True

    async def upsert_debt_deadline(self, uid, *, person, side, due_date, note=None):
        self.deadlines = [d for d in self.deadlines if not (d["person"] == person and d["side"] == side)]
        row = {"person": person, "side": side, "due_date": due_date, "note": note, "created_at": TODAY.isoformat()}
        self.deadlines.append(row)
        return row

    async def delete_debt_deadline(self, uid, *, person, side):
        self.deadlines = [d for d in self.deadlines if not (d["person"] == person and d["side"] == side)]


@pytest.fixture
def fdb(monkeypatch):
    db = FakeDB(OWNER)

    async def entries(uid):
        return list(db.entries)

    async def settings(uid):
        return dict(SETTINGS)

    async def deadlines(uid):
        return list(db.deadlines)

    for mod in (dt, tools):
        monkeypatch.setattr(mod, "db", db)
    monkeypatch.setattr(undo, "db", db)
    monkeypatch.setattr(dt.services, "finance_entries", entries)
    monkeypatch.setattr(dt.services, "finance_settings", settings)
    monkeypatch.setattr(dt.services, "debt_deadlines", deadlines)
    return db


def test_record_debt_asks_then_writes_and_undoes(fdb):
    uid = 91
    ctx = tools.ToolContext(profile=_profile(uid), text="взял у Алишера 500к")
    out = _run(tools.run("record_debt", {"items": [{"action": "borrow", "person": "Алишер", "amount": 500000}]}, ctx))
    assert out["nothing_saved"] and ctx.ask["options"] == ["💳 Карта", "💵 Наличные"] and len(fdb.entries) == len(OWNER)
    undo.begin_turn(uid)
    ctx = tools.ToolContext(profile=_profile(uid), text="💳 Карта")
    out = _run(tools.run("record_debt", {"items": [{"action": "borrow", "person": "Алишер", "amount": 500000, "account": "card"}]}, ctx))
    assert out["done"] == ["Займ: Алишер дал 500 000 → на карту, без срока"] and out["debts_now"] == ["Алишер: 500 000"]
    assert out["balances"]["карта"].endswith("1 806 000") and ctx.mutated and fdb.entries[0]["note"] == "[x:debt>card] Алишер"
    assert undo.end_turn(uid)
    _run(undo.apply(uid, tz_name="Asia/Tashkent"))
    assert len(fdb.entries) == len(OWNER)


def test_list_debts_and_deadlines_by_loan(fdb):
    fdb.entries.insert(0, _x(65, "2026-09-26", 1_050_000, "[x:debt>card] UZUM BANK"))
    fdb.entries.insert(0, _x(19, "2026-09-16", 2_205_000, "[x:init>debt] [due:2026-10-01] UZUM BANK"))
    fdb.entries = [r for i, r in enumerate(fdb.entries) if not (r["id"] == 19 and i > 0)]
    ctx = tools.ToolContext(profile=_profile(92), text="")
    listed = _run(tools.run("list_debts", {}, ctx))
    uzum = next(p for p in listed["debt"] if p["name"] == "UZUM BANK")
    assert uzum["total"] == 3_255_000 and [(l["loan_id"], l["due"]) for l in uzum["loans"]] == [("19", "2026-10-01"), ("65", None)]
    # срок без указания займа — у Uzum два займа: спросит
    out = _run(tools.run("set_debt_deadline", {"person": "узум", "due_date": "2026-10-26"}, ctx))
    assert "asked" in out and ctx.ask["options"][-1] == "Все займы"
    ctx = tools.ToolContext(profile=_profile(92), text="")
    out = _run(tools.run("set_debt_deadline", {"person": "узум", "due_date": "2026-10-26", "loan_id": "65"}, ctx))
    assert out["loans"] == ["65"] and next(r for r in fdb.entries if r["id"] == 65)["note"] == "[x:debt>card] [due:2026-10-26] UZUM BANK"
    rows = _run(tools.run("list_debt_deadlines", {}, ctx))["deadlines"]
    assert [(r["due_date"], r["amount"], r["days_left"]) for r in rows] == [("2026-10-01", 2_205_000, 5), ("2026-10-26", 1_050_000, 30)]
    # один займ — срок ставится без вопроса; человек без займа-операции — старый срок «по человеку»
    out = _run(tools.run("set_debt_deadline", {"person": "Асельбек", "due_date": "2026-10-05"}, ctx))
    assert out["matched_person"] == "Асилбек" and out["loans"] == ["15"]
    out = _run(tools.run("clear_debt_deadline", {"person": "асил"}, ctx))
    assert out["cleared"][0]["loan_id"] == "15" and fin.due_from_note(next(r for r in fdb.entries if r["id"] == 15)["note"]) is None


def test_add_finance_entries_refuses_debts_and_points_to_record_debt(fdb):
    ctx = tools.ToolContext(profile=_profile(93), text="")
    out = _run(tools.run("add_finance_entries", {"items": [{"kind": "transfer", "amount": 100, "from_bucket": "debt", "to_bucket": "card", "note": "X"}]}, ctx))
    assert "record_debt" in out["error"]
    out = _run(tools.run("add_finance_entries", {"items": [
        {"kind": "transfer", "amount": 100, "from_bucket": "card", "to_bucket": "cash"},
        {"kind": "transfer", "amount": 100, "from_bucket": "card", "to_bucket": "lent", "note": "X"}]}, ctx))
    assert len(out["added"]) == 1 and "record_debt" in out["not_saved"]


def test_update_entry_switches_loan_to_cash_and_keeps_due(fdb):
    fdb.entries.insert(0, _x(65, "2026-09-26", 1_050_000, "[x:debt>card] [due:2026-10-26] UZUM BANK"))
    ctx = tools.ToolContext(profile=_profile(94), text="")
    _run(tools.run("update_finance_entry", {"id": "65", "bucket": "cash"}, ctx))
    assert fdb.entries[0]["note"] == "[x:debt>cash] [due:2026-10-26] UZUM BANK"


def test_debt_alerts_use_per_loan_amounts():
    rows = [{"person": "UZUM BANK", "side": "debt", "due_date": (TODAY + timedelta(days=1)).isoformat(), "amount": 1_050_000.0, "loan_ids": ["65"]}]
    alerts = proactive.debt_alerts(rows, {}, TODAY, lang="ru", hour=10)
    assert len(alerts) == 1 and "1 050 000" in alerts[0].text


def test_debt_phrase_routing():
    assert fin.is_debt_phrase("взял у узум 1 050 000 и погасил тез")
    assert fin.is_debt_phrase("Uzum kredit 2 mln oldim") and fin.is_debt_phrase("qarzimni qaytardim")
    assert not fin.is_debt_phrase("такси 25000")
    assert fin.is_institution("UZUM BANK") and fin.is_institution("TEZ") and fin.is_institution("Хамкорбанк")
    assert not fin.is_institution("Алишер") and not fin.is_institution("Асилбек")

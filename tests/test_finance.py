"""Финансы: локальный парсер, категории, балансы, статистика."""
from datetime import date

from bot import categories as cats
from bot import finance as fin


# ---- amounts ----
def test_parse_amount_basic():
    assert fin.parse_amount("расход 25000 еда")[0] == 25000.0


def test_parse_amount_with_spaces():
    assert fin.parse_amount("доход 1 200 000 зарплата")[0] == 1200000.0


def test_parse_amount_suffixes():
    assert fin.parse_amount("такси 25к")[0] == 25000.0
    assert fin.parse_amount("зарплата 1.5 млн")[0] == 1500000.0
    assert fin.parse_amount("oylik 5 mln")[0] == 5000000.0
    assert fin.parse_amount("tushlik 40k")[0] == 40000.0
    assert fin.parse_amount("obed 25.000")[0] == 25000.0


def test_parse_amount_none_when_absent():
    assert fin.parse_amount("нет суммы здесь") is None


# ---- detection ----
def test_looks_like_finance():
    assert fin.looks_like_finance("расход 25000 еда") is True
    assert fin.looks_like_finance("доход 300000 зарплата") is True
    assert fin.looks_like_finance("такси 25000") is True
    assert fin.looks_like_finance("привет как дела") is False
    assert fin.looks_like_finance("омлет из 3 яиц") is False


# ---- local parser (no AI) ----
def test_parse_local_simple_expense():
    ops = fin.parse_local("такси 25000")
    assert ops and ops[0]["kind"] == "expense" and ops[0]["amount"] == 25000 and ops[0]["category"] == "transport"


def test_parse_local_multiple_ops():
    ops = fin.parse_local("расход 40к обед, доход 5 млн зарплата")
    assert ops is not None and len(ops) == 2
    assert ops[0]["category"] == "food" and ops[0]["kind"] == "expense"
    assert ops[1]["category"] == "salary" and ops[1]["kind"] == "income" and ops[1]["amount"] == 5_000_000


def test_parse_local_uzbek():
    ops = fin.parse_local("taksi 25000 va tushlik 40000")
    assert ops is not None and [o["category"] for o in ops] == ["transport", "food"]


def test_parse_local_gives_up_on_debts_and_transfers():
    assert fin.parse_local("дал в долг 200000 наличными") is None
    assert fin.parse_local("снял с карты 300000") is None
    assert fin.parse_local("qarzga berdim 50000") is None


def test_parse_local_gives_up_on_unknown_category():
    assert fin.parse_local("хз 25000 штука") is None


# ---- categories ----
def test_normalize_legacy_labels():
    assert cats.normalize("еда", "expense") == "food"
    assert cats.normalize("транспорт", "expense") == "transport"
    assert cats.normalize("доход", "income") == "other_in"
    assert cats.normalize("transport", "expense") == "transport"
    assert cats.normalize("что-то странное", "expense") == "other"
    assert cats.normalize("такси до аэропорта", "expense") == "transport"


def test_guess_from_text():
    assert cats.guess_from_text("яндекс такси", "expense") == "transport"
    assert cats.guess_from_text("аптека витамины", "expense") == "health"
    assert cats.guess_from_text("oylik keldi", "income") == "salary"
    assert cats.guess_from_text("абракадабра", "expense") is None


def test_labels():
    assert cats.label("food", "ru") == "🍔 Еда (кафе)"
    assert cats.label("food", "uz", with_emoji=False) == "Ovqat (kafe)"
    assert cats.label("transfer", "ru") == "↔ Перевод"


# ---- notes / buckets ----
def test_note_roundtrip():
    note = fin.note_with_bucket("обед", "cash")
    assert fin.bucket_from_note(note) == "cash"
    assert fin.clean_note(note) == "обед"
    tnote = fin.note_with_transfer("снял", "card", "cash")
    assert fin.transfer_from_note(tnote) == ("card", "cash")
    assert fin.clean_note(tnote) == "снял"


# ---- balances ----
def _e(kind, amount, note, day="2026-09-10", category="other"):
    return {"entry_type": kind, "amount": amount, "note": note, "entry_date": day, "category": category}


def test_compute_balances_with_transfers_and_debt():
    entries = [
        _e("income", 1_000_000, "[b:card] зарплата"),
        _e("expense", 100_000, "[b:cash] обед"),
        _e("expense", 300_000, "[x:card>cash] снял"),
        _e("expense", 200_000, "[x:card>lent] дал Алишеру"),
        _e("expense", 500_000, "[x:debt>card] взял в долг"),
        _e("expense", 100_000, "[x:card>debt] вернул часть"),
    ]
    b = fin.compute_balances(entries, {"card_base": 0, "cash_base": 50_000})
    assert b["card"] == 1_000_000 - 300_000 - 200_000 + 500_000 - 100_000
    assert b["cash"] == 50_000 - 100_000 + 300_000
    assert b["lent"] == 200_000
    assert b["debt"] == 400_000


# ---- stats ----
def test_compute_stats_by_category_and_prev_period():
    today = date(2026, 9, 15)
    entries = [
        _e("expense", 50_000, "[b:card] обед", "2026-09-14", "food"),
        _e("expense", 30_000, "[b:card] такси", "2026-09-15", "transport"),
        _e("expense", 20_000, "[b:card] такси", "2026-09-15", "транспорт"),  # старое название
        _e("income", 5_000_000, "[b:card] зп", "2026-09-01", "salary"),
        _e("expense", 100_000, "[x:card>cash] снял", "2026-09-02", "transfer"),
        _e("expense", 80_000, "[b:card] обед", "2026-08-20", "food"),
    ]
    st = fin.compute_stats(entries, fin.period_for("month", today))
    assert st.expense == 100_000
    assert st.income == 5_000_000
    assert st.transfers == 1
    assert {k: a for k, a, _ in st.by_category} == {"food": 50_000, "transport": 50_000}
    assert st.prev_expense == 80_000
    assert st.top_day is not None and st.top_day[1] == 50_000


def test_period_for_prev_month_boundaries():
    p = fin.period_for("prev_month", date(2026, 3, 1))
    assert (p.start, p.end) == (date(2026, 2, 1), date(2026, 2, 28))
    assert (p.prev_start, p.prev_end) == (date(2026, 1, 1), date(2026, 1, 31))


def test_top_operations_ranks_by_count():
    entries = [
        _e("expense", 25_000, "[b:card] такси", "2026-09-15", "transport"),
        _e("expense", 25_000, "[b:card] такси", "2026-09-14", "transport"),
        _e("expense", 40_000, "[b:card] обед", "2026-09-14", "food"),
    ]
    top = fin.top_operations(entries, limit=5)
    assert top[0]["amount"] == 25_000 and top[0]["count"] == 2
    assert fin.quick_label(top[0]) == "➖25 000 такси"

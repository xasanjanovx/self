"""07.10: «лишние сообщения, которые я удаляю каждый раз» — заметки исчезают при нажатии кнопки, сводки только про важное, отчёты с выводами."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot import digest, reports, screen, services
from bot import finance as fin
from bot.profile import Profile

CHAT = 777


def _run(coro):  # noqa: ANN001, ANN202
    return asyncio.run(coro)


class _Bot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, dict]] = []
        self.deleted: list[tuple[int, int]] = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):  # noqa: ANN001, ANN003
        self.sent.append((chat_id, text, kw))
        return SimpleNamespace(message_id=1000 + len(self.sent))

    async def delete_message(self, chat_id, mid):  # noqa: ANN001
        self.deleted.append((chat_id, mid))


@pytest.fixture(autouse=True)
def _clean_screen(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(screen, "_trash", None)
    for d in (screen._ephemerals, screen._meta, screen._sent):
        d.pop(CHAT, None)
    yield
    for d in (screen._ephemerals, screen._meta, screen._sent):
        d.pop(CHAT, None)


# ------------------------------------------------------------------ заметки
def test_note_disappears_on_the_next_button_but_not_the_one_pressed():
    bot = _Bot()

    async def go():
        a = await screen.send_note(bot, CHAT, "💡 совет", ttl=None)
        b = await screen.send_note(bot, CHAT, "📊 отчёт", ttl=None)
        await screen.clear_ephemerals(bot, CHAT, keep=b)      # нажал кнопку на отчёте: отчёт остаётся, совет уходит
        return a, b

    a, b = _run(go())
    assert bot.deleted == [(CHAT, a)] and screen._ephemerals[CHAT] == [b]


def test_fresh_reminder_survives_a_button_press_but_not_later_ones(monkeypatch):
    bot = _Bot()

    async def go():
        note = await screen.send_note(bot, CHAT, "📝 Напоминание: позвонить маме", ttl=None, min_age=600)
        await screen.clear_ephemerals(bot, CHAT, honor_min_age=True)     # нажал кнопку через секунду после прихода — не стираем
        assert bot.deleted == []
        born, age = screen._meta[CHAT][note]
        screen._meta[CHAT][note] = (born - 601, age)                      # прошло больше 10 минут
        await screen.clear_ephemerals(bot, CHAT, honor_min_age=True)
        return note

    note = _run(go())
    assert bot.deleted == [(CHAT, note)] and note not in screen._meta[CHAT]


def test_sticky_note_is_not_removed_by_buttons_only_by_time():
    bot = _Bot()

    async def go():
        warn = await screen.send_note(bot, CHAT, "⚠️ Будильник выключен", ttl=None, sticky=True)
        await screen.clear_ephemerals(bot, CHAT)
        return warn

    warn = _run(go())
    assert bot.deleted == [] and warn not in screen._ephemerals.get(CHAT, [])


def test_sticky_rows_in_the_journal_are_swept_by_time_and_never_picked_up_by_a_button():
    """Липкие хранятся с минусом: после перезапуска нажатие кнопки их не подберёт, а срок — удалит."""
    rows: dict[int, dict[int, datetime]] = {}

    class Store:
        async def add_ephemeral(self, chat_id, message_id, at):  # noqa: ANN001
            rows.setdefault(chat_id, {})[message_id] = at

        async def list_ephemerals(self, *, chat_id=None, due_before=None):  # noqa: ANN001
            out = []
            for c, items in rows.items():
                for m, at in items.items():
                    if (chat_id is None or c == chat_id) and (due_before is None or at <= due_before):
                        out.append({"chat_id": c, "message_id": m})
            return out

        async def drop_ephemerals(self, chat_id, ids):  # noqa: ANN001
            for m in ids:
                rows.get(chat_id, {}).pop(m, None)

    screen._trash = Store()
    screen._trash_loaded.discard(CHAT)
    bot = _Bot()

    async def go():
        normal = await screen.send_note(bot, CHAT, "💡 совет", ttl=3600)
        sticky = await screen.send_note(bot, CHAT, "⚠️ будильник выключен", ttl=3600, sticky=True)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert rows[CHAT][normal] and rows[CHAT][-sticky]
        screen._ephemerals.pop(CHAT, None)                                # «перезапуск»: в памяти пусто, остался только журнал
        await screen.clear_ephemerals(bot, CHAT)                           # нажатие кнопки подбирает обычные, липкую — нет
        assert bot.deleted == [(CHAT, normal)]
        rows[CHAT][-sticky] = datetime.now(timezone.utc) - timedelta(seconds=1)   # срок вышел
        assert await screen.sweep(bot) == 1
        return sticky

    sticky = _run(go())
    assert (CHAT, sticky) in bot.deleted and -sticky not in rows[CHAT]
    screen._trash_loaded.discard(CHAT)


def test_tidy_middleware_clears_notes_on_any_callback(monkeypatch):
    from bot.middlewares import TidyMiddleware

    bot = _Bot()

    class _Cb:
        def __init__(self, message_id):  # noqa: ANN001
            self.bot = bot
            self.message = SimpleNamespace(chat=SimpleNamespace(id=CHAT), message_id=message_id)

    monkeypatch.setattr("bot.middlewares.CallbackQuery", _Cb)

    async def handler(event, data):  # noqa: ANN001
        return "ok"

    async def go():
        a = await screen.send_note(bot, CHAT, "💡 совет", ttl=None)
        b = await screen.send_note(bot, CHAT, "📝 напоминание", ttl=None, min_age=600)
        c = await screen.send_note(bot, CHAT, "📊 отчёт", ttl=None)
        assert await TidyMiddleware()(handler, _Cb(5), {}) == "ok"            # любая кнопка главного меню
        return a, b, c

    a, b, c = _run(go())
    assert {(CHAT, a), (CHAT, c)} == set(bot.deleted) and screen._ephemerals[CHAT] == [b]   # свежее напоминание осталось


# ------------------------------------------------------------------ сводки
def _profile() -> Profile:
    return Profile(telegram_id=1, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Хасан", username="x")


@pytest.fixture
def day(monkeypatch):  # noqa: ANN001
    now = datetime(2026, 10, 7, 21, 0, tzinfo=timezone(timedelta(hours=5)))
    monkeypatch.setattr(Profile, "now", property(lambda self: now))
    monkeypatch.setattr(Profile, "today", property(lambda self: now.date()))
    return now.date()


def _stub(monkeypatch, today, *, entries=(), logs=(), recent=(), nprofile=None, limits=None, tasks=(), debts=(), recurring=()):  # noqa: ANN001, ANN202
    entries = list(entries)
    snap = services.FinanceSnapshot(entries=entries, settings={}, balances={"card": 800000.0, "cash": 200000.0}, today=today)
    snap.today_entries = fin.entries_between(entries, today, today)
    snap.today_expense = sum(float(e["amount"]) for e in snap.today_entries)
    snap.month = fin.compute_stats(entries, fin.period_for("month", today))

    def const(value):  # noqa: ANN001, ANN202
        async def f(*a, **k):  # noqa: ANN002, ANN003
            return value
        return f

    monkeypatch.setattr(services, "finance_snapshot", const(snap))
    monkeypatch.setattr(services, "today_calorie_logs", const(list(logs)))
    monkeypatch.setattr(services, "calorie_logs", const(list(recent)))
    monkeypatch.setattr(services, "nutrition_profile", const(nprofile))
    monkeypatch.setattr(services, "budgets", const(limits or {}))
    monkeypatch.setattr(services, "tasks", const(list(tasks)))
    monkeypatch.setattr(services, "debt_due_rows", const(list(debts)))
    monkeypatch.setattr(services, "recurring", const(list(recurring)))
    monkeypatch.setattr(services, "goals", const([]))
    monkeypatch.setattr(digest.access, "is_owner", lambda uid: False)


def test_quiet_evening_sends_nothing(day, monkeypatch):
    _stub(monkeypatch, day)
    assert _run(digest.evening(_profile())) is None                 # ничего важного — сообщения нет


def test_evening_has_only_what_needs_attention_sorted_by_importance(day, monkeypatch):
    entries = [{"entry_date": day.isoformat(), "amount": 120000, "entry_type": "expense", "category": "food", "bucket": "card"},
               {"entry_date": day.isoformat(), "amount": 900000, "entry_type": "expense", "category": "transport", "bucket": "card"}]
    _stub(monkeypatch, day, entries=entries, limits={"transport": 1000000.0}, nprofile={"daily_calories": 2200},
          logs=[{"calories": 1500, "created_at": day.isoformat()}],
          tasks=[{"text": "Сдать отчёт", "due_date": (day - timedelta(days=2)).isoformat(), "done": False},
                 {"text": "Позвонить маме", "due_date": day.isoformat(), "done": False}],
          debts=[{"person": "Алишер", "amount": 200000, "due_date": (day + timedelta(days=1)).isoformat()}])
    text = _run(digest.evening(_profile()))
    lines = text.splitlines()
    assert lines[0].startswith("🌙") and "1 020 000" in lines[1].replace(" ", " ") and "1500 / 2200" in lines[1]
    body = lines[2:]
    assert len(body) <= digest.EVENING_MAX
    assert "Алишер" in body[0] and "возврат" in body[0]                 # долг со сроком завтра — самое важное
    assert any("Транспорт" in ln for ln in body)                          # лимит 90% — предупреждение
    assert any("Просрочено" in ln for ln in body) and any("Не закрыто сегодня" in ln for ln in body)
    assert not any("Google" in ln or "Клиент" in ln for ln in body)      # ничего про ИИ и клиентов, пока всё в порядке


def test_not_logged_nagging_only_for_people_who_usually_log(day, monkeypatch):
    # новичок: вчера и раньше ничего не записывал — не пилим
    _stub(monkeypatch, day, nprofile={"daily_calories": 2000})
    assert _run(digest.evening(_profile())) is None
    # записывал вчера, а сегодня пусто — одна короткая строка
    yesterday = (day - timedelta(days=1)).isoformat()
    _stub(monkeypatch, day, nprofile={"daily_calories": 2000},
          entries=[{"entry_date": yesterday, "amount": 50000, "entry_type": "expense", "category": "food", "bucket": "card"}],
          recent=[{"calories": 400, "created_at": yesterday}])
    text = _run(digest.evening(_profile()))
    assert "Сегодня не записано: расходы и еда" in text and len(text.splitlines()) <= 3


def test_owner_gets_ops_lines_only_when_something_is_unusual(day, monkeypatch):
    from bot import billing, journal

    _stub(monkeypatch, day)
    monkeypatch.setattr(digest.access, "is_owner", lambda uid: True)
    monkeypatch.setattr(billing, "status", lambda: {"spent_today_usd": 0.30, "need_topup": False, "exhausted": False})
    monkeypatch.setattr(billing, "limit_usd", lambda: 3.0)
    monkeypatch.setattr(billing, "clients_report", lambda days: [])
    monkeypatch.setattr(journal, "cleanup", lambda: None)
    monkeypatch.setattr(journal, "day_entries", lambda d, uid=None: [{"kind": "false_wake"}] * 40)   # ложные «Джес» — шум, не промах
    assert _run(digest.evening(_profile())) is None
    monkeypatch.setattr(billing, "status", lambda: {"spent_today_usd": 5.2, "balance_usd": 1.1, "need_topup": True, "exhausted": False})
    monkeypatch.setattr(journal, "day_entries", lambda d, uid=None: [{"kind": "wrong_call"}] * 2 + [{"kind": "ask_again"}] * 9)   # мелочь не считается
    assert "сбоев" not in _run(digest.evening(_profile()))
    monkeypatch.setattr(journal, "day_entries", lambda d, uid=None: [{"kind": "wrong_call"}] * 2 + [{"kind": "tool_error"}] * 2)
    text = _run(digest.evening(_profile()))
    assert "Баланс Gemini заканчивается" in text and "сбоев за день — 4" in text


def test_morning_is_short_and_silent_when_nothing_to_say(day, monkeypatch):
    _stub(monkeypatch, day)
    assert _run(digest.morning(_profile())) is None
    _stub(monkeypatch, day, tasks=[{"text": "Купить лампочку", "due_date": day.isoformat(), "done": False}],
          recurring=[{"title": "Аренда", "amount": 2000000, "day_of_month": day.day + 1, "enabled": True}])
    text = _run(digest.morning(_profile()))
    lines = text.splitlines()
    assert lines[0].startswith("🌅") and "💼" in lines[1] and len(lines) <= digest.MORNING_MAX + 2
    assert any("Аренда" in ln and "через 1 дн." in ln for ln in lines) and any("Купить лампочку" in ln for ln in lines)


# ------------------------------------------------------------------ отчёты
def _entries(day, spent_now, spent_prev, *, cat="transport", prev_cat="transport"):  # noqa: ANN001, ANN202
    return [{"entry_date": (day - timedelta(days=2)).isoformat(), "amount": spent_now, "entry_type": "expense", "category": cat, "bucket": "card"},
            {"entry_date": (day - timedelta(days=9)).isoformat(), "amount": spent_prev, "entry_type": "expense", "category": prev_cat, "bucket": "card"}]


def test_report_insights_say_what_changed_not_just_numbers(day):
    p = _profile()
    start = day - timedelta(days=6)
    period = fin.Period("custom", start, day, start - timedelta(days=7), start - timedelta(days=1))
    stats = fin.compute_stats(_entries(day, 1180000, 1000000), period)
    notes = reports.insights(stats, None, days=7, lang="ru", currency=p.currency)
    assert notes[0].startswith("📈 Расходы выросли 18%") and "Транспорт" in notes[0] and "(+18%)" in notes[0]
    huge = fin.compute_stats(_entries(day, 1100000, 40000), period)           # раньше было бы «+2650%»
    assert "+2650%" not in reports.insights(huge, None, days=7, lang="ru", currency=p.currency)[0]
    calm = fin.compute_stats(_entries(day, 1000000, 1000000), period)
    assert not [n for n in reports.insights(calm, None, days=7, lang="ru", currency=p.currency) if n.startswith(("📈", "📉"))]
    down = fin.compute_stats(_entries(day, 600000, 1000000), period)
    assert reports.insights(down, None, days=7, lang="ru", currency=p.currency)[0].startswith("📉 Расходы снизились 40%")


def test_auto_report_is_a_few_lines_with_the_main_point_first(day):
    p = _profile()
    text = reports.build_digest(p, days=7, entries=_entries(day, 1180000, 1000000), logs=[], nutrition_profile=None, title="📊 <b>Недельный отчёт</b>")
    lines = text.splitlines()
    assert len(lines) <= 5 and lines[0].startswith("📊") and "1 180 000" in lines[1].replace(" ", " ") and lines[2].startswith("📈")
    assert reports.build_digest(p, days=7, entries=[], logs=[], nutrition_profile=None, title="x") is None     # данных нет — не пишем


def test_analytics_screen_puts_the_conclusion_first(day):
    p = _profile()
    summary = reports.build_summary(p, days=7, entries=_entries(day, 1180000, 1000000), logs=[], nutrition_profile=None, title="📊 <b>Аналитика</b>")
    lines = summary.text.splitlines()
    assert lines[3].startswith("💡") and "Главное" in lines[3] and lines[4].startswith("📈")


def test_weekly_digest_is_short_and_has_a_takeaway():
    from bot import weekly

    f = {"start": date(2026, 9, 28), "end": date(2026, 10, 4),
         "money": {"spent": 1250000.0, "income": 0, "prev_spent": 1000000.0, "change_pct": 25.0, "ops": 14, "top": [("Еда", 600000.0), ("Такси", 200000.0)]},
         "tasks": {"done": 6, "open": 3, "overdue": 4, "done_titles": []}, "wake": "⏰ Встал 6 из 7 дней.",
         "jes": {"всего": 31}, "daily": [{"title": "Английский", "progress": "12/40"}] * 5, "lessons": [{"title": "L", "at": "1:00", "of": "2:00", "finished": False}] * 3}
    text = weekly.text(_profile(), f)
    assert len(text.splitlines()) <= 8 and "Расходы выросли 25%" in text and "JES: дел" not in text and text.count("Английский") == 2


def test_auto_report_goes_as_a_short_note_and_not_at_all_without_data(day, monkeypatch):
    from bot import workers

    sent: list[str] = []
    saved: list[str] = []

    async def note(bot, chat_id, text, reply_markup=None, **kw):  # noqa: ANN001, ANN003, ANN202
        sent.append(text)
        return 1

    async def profile_by_id(uid):  # noqa: ANN001, ANN202
        return _profile()

    async def payload(profile, days):  # noqa: ANN001, ANN202
        return {"all_finance_entries": rows["entries"], "calorie_logs": []}

    async def nprofile(uid):  # noqa: ANN001, ANN202
        return None

    async def save_prefs(uid, **kw):  # noqa: ANN001, ANN003, ANN202
        saved.append(kw["last_sent_key"])

    rows = {"entries": _entries(day, 1180000, 1000000)}
    monkeypatch.setattr(screen, "send_note", note)
    monkeypatch.setattr(workers, "profile_by_id", profile_by_id)
    monkeypatch.setattr(workers.services, "period_payload", payload)
    monkeypatch.setattr(workers.services, "nutrition_profile", nprofile)
    monkeypatch.setattr(workers.db, "save_report_preferences", save_prefs)
    _run(workers._send_report(_Bot(), 1, "weekly", "2026-W41"))
    assert len(sent) == 1 and len(sent[0].splitlines()) <= 5 and "Недельный отчёт" in sent[0] and saved == ["2026-W41"]
    rows["entries"] = []
    _run(workers._send_report(_Bot(), 1, "weekly", "2026-W42"))
    assert len(sent) == 1 and saved == ["2026-W41", "2026-W42"]          # данных нет — молчим, но неделя засчитана

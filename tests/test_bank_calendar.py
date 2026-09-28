"""28.09: траты из SMS/пушей банков (вопрос кнопкой) и календарь телефона."""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone

from bot import bank_events, calendar_sync, phone
from bot.agent_tools import ToolContext, run
from bot.profile import Profile

TZ = timezone(timedelta(hours=5))


def _profile(uid: int = 1) -> Profile:
    return Profile(telegram_id=uid, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Т", username="t")


# ------------------------------------------------------------------ банк
def test_amount_must_be_in_the_text():
    assert bank_events.amount_in_text(45000, "Uzum: оплата 45 000,00 UZS Korzinka, остаток 1 200 000")
    assert bank_events.amount_in_text(45000, "Karta *1234: xarid 45000 so'm")
    assert bank_events.amount_in_text(1250000, "Поступление 1 250 000 сум")
    assert not bank_events.amount_in_text(46000, "оплата 45 000 UZS")      # модель ошиблась — не записываем


def test_same_purchase_from_sms_and_push_is_one(monkeypatch):
    monkeypatch.setattr(bank_events, "_recent", [])
    assert not bank_events.duplicate("expense", 45000, now=1000.0)
    assert bank_events.duplicate("expense", 45000, now=1100.0)             # пуш банка следом за SMS
    assert not bank_events.duplicate("expense", 45000, now=1500.0)         # через 8 минут — уже новая покупка
    assert not bank_events.duplicate("income", 45000, now=1501.0)


def test_parse_and_ask(tmp_path, monkeypatch):
    from bot import context
    from bot.context import ai
    from bot.handlers import common

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bank_events, "_recent", [])

    async def fake_generate(parts, **kw):  # noqa: ANN001, ANN003
        return json.dumps({"kind": "expense", "amount": 45000, "currency": "UZS", "merchant": "Korzinka", "category": "groceries"})

    monkeypatch.setattr(ai, "generate", fake_generate)

    async def prof(uid):  # noqa: ANN001
        return _profile(uid)

    monkeypatch.setattr(common, "profile_by_id", prof)
    sent: list = []

    class Bot:
        async def send_message(self, uid, text, **kw):  # noqa: ANN001, ANN003
            sent.append((uid, text, kw.get("reply_markup")))

    monkeypatch.setattr(context, "bot_instance", lambda: Bot())
    res = asyncio.run(bank_events.receive(1, {"app": "Uzum Bank", "title": "Оплата", "text": "Оплата 45 000,00 UZS KORZINKA", "t": 0}))
    assert res.get("asked") and sent and "45 000" in sent[0][1] and "Продукты" in sent[0][1]
    buttons = [b.callback_data for row in sent[0][2].inline_keyboard for b in row]
    assert buttons == [f"bank:ok:{res['asked']}", f"bank:cats:{res['asked']}", f"bank:no:{res['asked']}"]
    assert bank_events.pending(res["asked"])["amount"] == 45000
    # тот же платёж пушем через минуту — второй раз не спрашиваем
    again = asyncio.run(bank_events.receive(1, {"app": "Uzum Bank", "title": "", "text": "Списание 45 000 UZS KORZINKA"}))
    assert again.get("skipped") == "дубль"


def test_codes_and_non_operations_are_skipped(tmp_path, monkeypatch):
    from bot.context import ai

    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    async def fake_generate(parts, **kw):  # noqa: ANN001, ANN003
        return json.dumps({"kind": "skip", "why": "код подтверждения"})

    monkeypatch.setattr(ai, "generate", fake_generate)
    res = asyncio.run(bank_events.receive(1, {"app": "SMS", "text": "Код подтверждения оплаты 45 000 UZS: 4821"}))
    assert res.get("skipped")


# ------------------------------------------------------------------ календарь
def test_calendar_events_and_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    ms = calendar_sync.start_ms(date(2026, 10, 2), "15:00", TZ)
    assert datetime.fromtimestamp(ms / 1000, TZ).strftime("%Y-%m-%d %H:%M") == "2026-10-02 15:00"
    calendar_sync.save_events(1, [
        {"id": "1", "title": "Встреча с Алишером", "start": ms, "end": ms + 3600_000, "day": "2026-10-02", "time": "15:00", "location": "офис"},
        {"id": "2", "title": "День рождения мамы", "start": ms, "all_day": True, "day": "2026-10-03"},
    ])
    rows = calendar_sync.events_between(1, date(2026, 10, 2), date(2026, 10, 3), TZ)
    assert [(r["date"], r["time"], r["title"]) for r in rows] == [("2026-10-02", "15:00", "Встреча с Алишером"),
                                                                ("2026-10-03", None, "День рождения мамы")]
    assert calendar_sync.day_lines(1, date(2026, 10, 2), TZ) == ["15:00 Встреча с Алишером (офис)"]
    op = calendar_sync.queue_add(1, "Стоматолог", ms, 30, 60)
    assert [p["id"] for p in calendar_sync.pending(1)] == [op]
    calendar_sync.mark_done(1, [op])
    assert calendar_sync.pending(1) == []


def test_calendar_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    ctx = ToolContext(profile=_profile(), text="")
    missing = asyncio.run(run("calendar_events", {"date_from": "today"}, ctx))
    assert "ещё не пришёл" in missing["error"]
    today = _profile().today
    ms = calendar_sync.start_ms(today, "10:00", _profile().tz)
    calendar_sync.save_events(1, [{"id": "1", "title": "Планёрка", "start": ms, "day": today.isoformat(), "time": "10:00"}])
    got = asyncio.run(run("calendar_events", {"date_from": "today"}, ctx))
    assert got["events"][0]["title"] == "Планёрка"
    added = asyncio.run(run("calendar_add_event", {"title": "Стоматолог", "date": "tomorrow", "time": "9:30"}, ctx))
    assert added["queued"] and added["time"] == "09:30" and calendar_sync.pending(1)[0]["reminder"] == 15


def test_phone_calendar_add_is_immediate_action():
    turn = phone.PhoneTurn(uid=1)
    ctx = ToolContext(profile=_profile(), text="")
    res = asyncio.run(phone.PHONE_TOOLS["calendar_add"].handler(turn, ctx, {"title": "Встреча", "date": "tomorrow", "time": "15:00"}))
    assert res.get("ok") and turn.actions[0]["type"] == "calendar_add" and turn.actions[0]["reminder"] == 15
    assert turn.actions[0]["title"] == "Встреча" and turn.actions[0]["minutes"] == 60

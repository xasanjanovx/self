"""Джарвис 1.4: учёт денег Gemini, поиск и массовые правки, чистый чат, звонки и телефонные инструменты."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from bot import agent_tools as tools
from bot import billing
from bot import cache
from bot import phone
from bot import undo
from bot.profile import Profile


def _run(coro):
    return asyncio.run(coro)


def _profile(uid: int = 1) -> Profile:
    return Profile(telegram_id=uid, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Тест", username="t")


# ------------------------------------------------------------------ billing
@pytest.fixture()
def fresh_billing(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(billing, "_state", None)
    monkeypatch.setattr(billing, "_last_check", 0.0)
    sent: list[str] = []
    monkeypatch.setattr(billing, "_notify", lambda text: sent.append(text))
    return sent


def test_live_turn_cost_matches_price_list():
    """Реальный usageMetadata одной реплики Gemini Live (снят на сервере): ~10.6k токенов контекста + голос."""
    usage = {"promptTokenCount": 10610, "responseTokenCount": 118, "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 7608},
             {"modality": "AUDIO", "tokenCount": 187}], "responseTokensDetails": [{"modality": "AUDIO", "tokenCount": 118}], "thoughtsTokenCount": 92}
    usd = billing.cost("gemini-3.8-live", usage)
    expected = ((7608 + 2815) * 0.75 + 187 * 3.0 + 92 * 4.5 + 118 * 12.0) / 1e6
    assert usd == pytest.approx(expected) and 0.008 < usd < 0.012


def test_tts_output_is_priced_as_audio_even_without_details():
    usd = billing.cost("gemini-2.5-flash-preview-tts", {"promptTokenCount": 20, "candidatesTokenCount": 1000})
    assert usd == pytest.approx((20 * 0.5 + 1000 * 10.0) / 1e6)
    assert billing.prices_for("models/gemini-3.5-flash-lite")["text_out"] == 2.5
    assert billing.prices_for("gemini-3.5-flash")["text_out"] == 9.0  # не спутать с flash-lite


def test_balance_topup_spend_and_forecast(fresh_billing):
    assert billing.status()["balance_usd"] is None
    billing.set_balance(usd=10.0)
    today = billing._today()
    st = billing._load()
    for i in range(1, 7):  # неделя истории: по $1 в день
        day = (datetime.fromisoformat(today) - timedelta(days=i)).date().isoformat()
        st["days"][day] = {"usd": 1.0, "calls": 10, "kinds": {}}
    st["spent_since"] = 2.0
    s = billing.status()
    assert s["balance_usd"] == 8.0 and 0.7 < s["daily_average_usd"] < 1.6 and s["days_left"] > 4 and not s["need_topup"]
    out = billing.set_balance(topup=5.0)
    assert out["balance_usd"] == 13.0


def test_real_balance_calibrates_factor(fresh_billing):
    billing.set_balance(usd=10.0)
    billing._load()["spent_since"] = 1.0       # насчитали $1
    billing.set_balance(usd=8.0)               # а ушло $2 — множитель растёт
    assert 1.4 <= billing._load()["factor"] <= 1.6


def test_low_balance_alerts_once_and_voice_note(fresh_billing):
    billing.set_balance(usd=1.0)
    st = billing._load()
    st["days"][(datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()] = {"usd": 0.8, "calls": 5, "kinds": {}}
    billing.record("gemini-3.8-live", {"promptTokenCount": 10000, "responseTokenCount": 100})
    assert len(fresh_billing) == 1 and "пополн" in fresh_billing[0].lower()
    billing._last_check = 0.0
    billing.record("gemini-3.8-live", {"promptTokenCount": 10000, "responseTokenCount": 100})
    assert len(fresh_billing) == 1  # не спамим
    note = billing.voice_note()
    assert "пополнить" in note and billing.voice_note() == ""  # в разговоре — раз в 6 часов


def test_exhausted_402_notifies_immediately(fresh_billing):
    assert billing.is_billing_error(402, "") and billing.is_billing_error(None, "Payment Required")
    assert not billing.is_billing_error(429, "Resource exhausted")
    billing.exhausted("402 Payment Required")
    billing.exhausted("again")
    assert len(fresh_billing) == 1 and "закончился" in fresh_billing[0]
    assert billing.status()["exhausted"] is True


def test_ai_status_tool_reports_money_and_version(fresh_billing):
    ctx = tools.ToolContext(profile=_profile(), text="")
    out = _run(tools.run("set_ai_balance", {"topup_usd": 10}, ctx))
    assert out["balance_usd"] == 10.0
    out = _run(tools.run("ai_status", {}, ctx))
    assert out["money"]["balance_usd"] == 10.0 and out["version"]["version"] and "voice_calls" in out["models"]
    assert "error" in _run(tools.run("set_ai_balance", {}, ctx))


# ------------------------------------------------------------------ поиск и массовые правки
ENTRIES = [
    {"id": 1, "entry_type": "expense", "amount": 25000, "category": "transport", "note": "[b:card] такси Асилбек", "entry_date": "2026-09-21"},
    {"id": 2, "entry_type": "expense", "amount": 150000, "category": "food", "note": "[b:cash] ресторан", "entry_date": "2026-09-20"},
    {"id": 3, "entry_type": "expense", "amount": 30000, "category": "other", "note": "[b:card] такси", "entry_date": "2026-09-19"},
]


@pytest.fixture()
def fake_data(monkeypatch):
    from bot import agent_tools_bulk as bulk

    async def entries(uid):
        return ENTRIES

    async def empty(*a, **k):
        return []

    for name in ("notes", "reminders", "goals", "recurring", "debt_deadlines"):
        monkeypatch.setattr(bulk.services, name, empty)
    monkeypatch.setattr(bulk.services, "calorie_logs", empty)
    monkeypatch.setattr(bulk.services, "finance_entries", entries)
    monkeypatch.setattr(tools.services, "finance_entries", entries)
    monkeypatch.setattr(bulk.db, "available", lambda name: False)
    return bulk


def test_find_records_by_words_and_amount(fake_data):
    ctx = tools.ToolContext(profile=_profile(), text="")
    out = _run(tools.run("find_records", {"query": "Асилбек"}, ctx))
    assert [r["id"] for r in out["found"]] == ["1"]
    out = _run(tools.run("find_records", {"query": "150000"}, ctx))
    assert [r["id"] for r in out["found"]] == ["2"]
    out = _run(tools.run("find_records", {"query": "такси"}, ctx))
    assert {r["id"] for r in out["found"]} == {"1", "3"}
    assert _run(tools.run("find_records", {"query": "самолёт"}, ctx))["found"] == []


def test_bulk_update_goes_through_single_tool_and_one_undo(monkeypatch, fake_data):
    updated = []

    async def get_entry(uid, rid):
        return next((dict(r) for r in ENTRIES if str(r["id"]) == str(rid)), None)

    async def update(uid, rid, fields):
        updated.append((rid, fields))

    monkeypatch.setattr(tools.db, "get_finance_entry", get_entry)
    monkeypatch.setattr(tools.db, "update_finance_entry", update)
    uid = 77
    undo.begin_turn(uid)
    ctx = tools.ToolContext(profile=_profile(uid), text="")
    out = _run(tools.run("update_finance_entries", {"ids": ["1", "3", "404"], "category": "transport"}, ctx))
    assert out["changed"] == 2 and out["failed"][0]["id"] == "404"
    assert [u[0] for u in updated] == [1, 3] and all(u[1]["category"] == "transport" for u in updated)
    assert undo.end_turn(uid) and len(undo.peek(uid)["steps"]) == 2  # одна кнопка «отменить» на всю пачку
    assert "error" in _run(tools.run("update_finance_entries", {"ids": ["1"]}, ctx))


def test_mass_delete_needs_confirmation(monkeypatch):
    many = [{"id": i, "entry_type": "expense", "amount": 1000, "category": "food", "note": "", "entry_date": "2026-09-21"} for i in range(1, 16)]
    deleted = []

    async def entries(uid):
        return many

    async def delete(uid, ids):
        deleted.extend(ids)

    monkeypatch.setattr(tools.services, "finance_entries", entries)
    monkeypatch.setattr(tools.db, "delete_finance_entries", delete)
    ctx = tools.ToolContext(profile=_profile(5), text="")
    ids = [str(i) for i in range(1, 16)]
    out = _run(tools.run("delete_finance_entries", {"ids": ids}, ctx))
    assert out["confirm_needed"] == 15 and not deleted
    out = _run(tools.run("delete_finance_entries", {"ids": ids, "confirm": True}, ctx))
    assert len(out["deleted"]) == 15 and len(deleted) == 15


# ------------------------------------------------------------------ телефон
def test_phone_tools_recent_calls_call_back_and_forwarding(monkeypatch):
    device = {"calls": [{"name": "Мама", "number": "+998901112233", "type": "missed", "when": "10:42"},
                        {"name": "", "number": "+998907778899", "type": "out", "when": "09:00"}], "battery": 41, "app_version": "1.4"}
    turn = phone.PhoneTurn(uid=1, device=device)
    run = phone.make_runner(turn)
    ctx = tools.ToolContext(profile=_profile(), text="")
    out = _run(run("recent_calls", {}, ctx))
    assert out["calls"][0]["kind"] == "пропущенный"
    _run(run("call_back", {"which": "last_missed"}, ctx))
    assert turn.actions[-1] == {"type": "call", "number": "+998901112233", "name": "Мама"}
    _run(run("call_forwarding", {"to": "+998909990000", "when": "busy"}, ctx))
    assert turn.actions[-1]["type"] == "ussd" and turn.actions[-1]["code"] == "**67*+998909990000#"
    _run(run("call_forwarding", {"when": "off"}, ctx))
    assert turn.actions[-1]["code"] == "##002#"
    assert "батарея 41%" in phone.device_prompt(device) and "Мама (10:42)" in phone.device_prompt(device)


def test_phone_system_and_camera_actions():
    turn = phone.PhoneTurn(uid=1, device={})
    run = phone.make_runner(turn)
    ctx = tools.ToolContext(profile=_profile(), text="")
    _run(run("brightness", {"percent": 0}, ctx))
    _run(run("do_not_disturb", {"on": False}, ctx))
    _run(run("settings_panel", {"panel": "wifi"}, ctx))
    _run(run("look", {}, ctx))
    _run(run("look", {"on": False}, ctx))
    kinds = [(a["type"], a.get("percent", a.get("on", a.get("panel")))) for a in turn.actions]
    assert kinds == [("brightness", 0), ("dnd", False), ("panel", "wifi"), ("camera", True), ("camera", False)]
    assert "error" in _run(run("settings_panel", {"panel": "rocket"}, ctx))


def test_new_phone_tools_need_unlock_and_are_declared():
    from bot import live_call, phone_live

    names = {d["name"] for d in live_call.tool_declarations("phone")}
    for n in ("recent_calls", "call_back", "call_forwarding", "phone_status", "brightness", "do_not_disturb", "ringer_mode",
              "settings_panel", "look", "ai_status", "set_ai_balance", "bot_task", "whatsapp_send", "screen_look", "gallery",
              "play_media", "youtube_search", "telegram_search", "taxi", "remember_contact"):
        assert n in names
    assert {"look", "screen_look", "open_app"} <= phone_live.NEED_UNLOCK
    assert not {"phone_call", "send_sms", "telegram_send", "recent_calls"} & phone_live.NEED_UNLOCK
    for mode in ("assistant", "wake", "phone"):
        decl = [d["name"] for d in live_call.tool_declarations(mode)]
        assert len(decl) == len(set(decl)), mode


# ------------------------------------------------------------------ кэш и чистый чат
def test_drop_expiring_refreshes_only_data_keys():
    uid = 991
    cache.put(uid, ("tasks",), [1], 30)
    cache.put(uid, ("notes",), [2], 3000)
    cache.put(uid, ("agent_ask",), ["x"], 30)
    assert cache.drop_expiring(uid, 135, {"tasks", "notes"}) == 1
    assert cache.get(uid, ("tasks",)) is None and cache.get(uid, ("notes",)) == [2] and cache.get(uid, ("agent_ask",)) == ["x"]


def test_tidy_deletes_user_message_and_sent_on_button(monkeypatch):
    from bot import screen
    from bot.middlewares import TidyMiddleware

    deleted = []

    class _Msg:
        def __init__(self):
            self.chat = type("C", (), {"type": "private", "id": 5})()

        async def delete(self):
            deleted.append("user")

    async def handler(event, data):
        return "ok"

    mw = TidyMiddleware()
    mw.DELAY = 0
    monkeypatch.setattr("bot.middlewares.Message", _Msg)

    async def scenario():
        assert await mw(handler, _Msg(), {}) == "ok"
        await asyncio.sleep(0.05)
        assert deleted == ["user"]
        await mw(handler, _Msg(), {"keep_message": True})
        await asyncio.sleep(0.05)
        assert deleted == ["user"]

    _run(scenario())

    bot_deleted = []

    class _Bot:
        async def delete_message(self, chat_id, mid):
            bot_deleted.append((chat_id, mid))

    monkeypatch.setattr(screen, "_trash", None)

    async def sent_then_button():
        screen.track_sent(5, 101)
        await screen.clear_sent(_Bot(), 5)

    _run(sent_then_button())
    assert bot_deleted == [(5, 101)] and not screen._sent.get(5)


def test_phone_api_routes_include_new_endpoints():
    from bot import phone_api

    routes = {r.resource.canonical for r in phone_api.build_app().router.routes() if r.resource is not None}
    assert {"/jarvis/v1/call_command", "/jarvis/v1/tg/quick_send", "/jarvis/v1/live"} <= routes



# ------------------------------------------------------------------ будильник 1.5
def test_wake_off_without_settings_and_respects_attempts():
    from bot import wake

    assert wake.WakeSettings.from_row({}).enabled is False           # не настраивал — не звоним
    assert wake.WakeSettings.from_row(None).enabled is False
    row = {"enabled": True, "max_attempts": 20}
    assert wake.WakeSettings.from_row(row).enabled and wake.WakeSettings.from_row(row).max_attempts == 20


def test_skip_phrases():
    from bot import wake

    assert wake.looks_skip("не звони мне сегодня") and wake.looks_skip("Bugun uyg'otmang")
    assert not wake.looks_skip("я проснулся")


def test_quiz_progresses_and_never_repeats(tmp_path, monkeypatch):
    from datetime import date, timedelta

    from bot import islam_quiz

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    d = date(2026, 9, 25)
    first = islam_quiz.for_day(7, d)
    assert islam_quiz.for_day(7, d) == first                 # весь день один вопрос (повторные звонки)
    seen = {first.id}
    for i in range(1, 40):
        q = islam_quiz.for_day(7, d + timedelta(days=i))
        assert q.id not in seen
        seen.add(q.id)
    order = [q.id for q in islam_quiz.BANK]
    assert order.index(q.id) > order.index(first.id)          # каждый день — дальше по сложности


def test_quiz_duas_have_arabic_and_source():
    from bot import islam_quiz

    ids = [q.id for q in islam_quiz.BANK]
    assert len(ids) == len(set(ids)) and len(ids) >= 90
    for q in islam_quiz.BANK:
        if "дуа" in q.q.lower():
            assert q.ar and q.ref, q.id
    yunus = next(q for q in islam_quiz.BANK if q.id == "yunus_dua")
    assert yunus.ar == "لَا إِلَٰهَ إِلَّا أَنتَ سُبْحَانَكَ إِنِّي كُنتُ مِنَ الظَّالِمِينَ" and "21:87" in yunus.ref
    block = islam_quiz.prompt_block(yunus)
    assert "СЛОВО В СЛОВО" in block and yunus.ar in block
    assert yunus.ar in islam_quiz.card(yunus)


def test_youtube_parse_desktop_and_mobile_formats():
    import json

    from bot import media

    data = {"contents": {"x": [{"videoRenderer": {"videoId": "abc123XYZ00", "title": {"runs": [{"text": "Nasheed"}]},
                                                   "ownerText": {"runs": [{"text": "Chan"}]}, "lengthText": {"simpleText": "3:41"}}}]}}
    desktop = "<script>var ytInitialData = " + json.dumps(data) + ";</script>"
    assert media.parse_youtube(desktop)[0]["id"] == "abc123XYZ00"
    escaped = json.dumps(data).replace("{", "\x7b").replace("}", "\x7d").replace('"', "\x22")
    mobile = "<script>var ytInitialData = '" + escaped + "';</script>"
    got = media.parse_youtube(mobile)
    assert got and got[0]["title"] == "Nasheed" and got[0]["duration"] == "3:41"

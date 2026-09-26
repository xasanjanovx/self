"""«Джарвис»: чистые части агента — фильтры, история, рендер, цикл с фейковой моделью, откат."""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from bot import agent_tools as tools
from bot import undo
from bot.ai import AgentStep
from bot.handlers import agent
from bot.profile import Profile


def _profile() -> Profile:
    return Profile(telegram_id=1, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Тест", username="t")


ENTRIES = [
    {"id": 1, "entry_type": "expense", "amount": 25000, "category": "transport", "note": "[b:card] такси", "entry_date": "2026-09-21"},
    {"id": 2, "entry_type": "expense", "amount": 40000, "category": "food", "note": "[b:cash] обед", "entry_date": "2026-09-21"},
    {"id": 3, "entry_type": "expense", "amount": 25000, "category": "transport", "note": "[b:card]", "entry_date": "2026-09-20"},
    {"id": 4, "entry_type": "income", "amount": 5000000, "category": "salary", "note": "[b:card] зарплата", "entry_date": "2026-09-05"},
    {"id": 5, "entry_type": "expense", "amount": 200000, "category": "transfer", "note": "[x:card>lent] Алишер", "entry_date": "2026-09-10"},
    {"id": 6, "entry_type": "expense", "amount": 100000, "category": "transfer", "note": "[x:init>lent]", "entry_date": "2026-09-01"},
]
TODAY = date(2026, 9, 21)


# ------------------------------------------------------------------ tool declarations
def test_declarations_are_valid_gemini_schemas():
    decls = tools.declarations()
    names = [d["name"] for d in decls]
    assert len(names) == len(set(names))
    for d in decls:
        params = d["parameters"]
        assert params["type"] == "OBJECT"
        for key, prop in params["properties"].items():
            assert prop["type"] in {"STRING", "NUMBER", "INTEGER", "BOOLEAN", "ARRAY", "OBJECT"}, (d["name"], key)
            if prop["type"] == "ARRAY":
                assert "items" in prop
        for req in params.get("required", []):
            assert req in params["properties"], (d["name"], req)


# ------------------------------------------------------------------ filters
def test_filter_by_category_and_date():
    found = tools.filter_entries(ENTRIES, today=TODAY, category="transport", date_from="today")
    assert [r["id"] for r in found] == [1]


def test_filter_by_amount_matches_all_days():
    found = tools.filter_entries(ENTRIES, today=TODAY, amount=25000)
    assert [r["id"] for r in found] == [1, 3]


def test_filter_lent_and_unnamed():
    lent = tools.filter_entries(ENTRIES, today=TODAY, kind="lent")
    assert [r["id"] for r in lent] == [5, 6]
    unnamed = tools.filter_entries(ENTRIES, today=TODAY, kind="lent", unnamed=True)
    assert [r["id"] for r in unnamed] == [6]


def test_filter_note_and_income():
    assert [r["id"] for r in tools.filter_entries(ENTRIES, today=TODAY, note_contains="алишер")] == [5]
    assert [r["id"] for r in tools.filter_entries(ENTRIES, today=TODAY, kind="income")] == [4]


def test_filter_date_range_and_amount_bounds():
    found = tools.filter_entries(ENTRIES, today=TODAY, date_from="2026-09-01", date_to="2026-09-10", min_amount=150000)
    assert [r["id"] for r in found] == [4, 5]


def test_entry_view_transfer_and_expense():
    v = tools.entry_view(ENTRIES[4])
    assert v["kind"] == "transfer" and v["from"] == "card" and v["to"] == "lent" and v["note"] == "Алишер" and v["debt"] == "lent"
    v2 = tools.entry_view(ENTRIES[1])
    assert v2 == {"id": "2", "date": "2026-09-21", "amount": 40000.0, "note": "обед", "kind": "expense", "category": "food", "bucket": "cash"}


def test_parse_day_keywords():
    assert tools.parse_day("yesterday", TODAY) == date(2026, 9, 20)
    assert tools.parse_day("2026-09-01", TODAY) == date(2026, 9, 1)
    assert tools.parse_day("garbage", TODAY) is None


# ------------------------------------------------------------------ history / rendering
def test_trim_history_keeps_pairs_and_starts_with_user_text():
    contents = []
    for i in range(30):
        contents.append({"role": "user", "parts": [{"text": f"msg {i}"}]})
        contents.append({"role": "model", "parts": [{"functionCall": {"name": "x", "args": {}}}]})
        contents.append({"role": "user", "parts": [{"functionResponse": {"name": "x", "response": {"entries": list(range(50))}}}]})
        contents.append({"role": "model", "parts": [{"text": "ok"}]})
    trimmed = agent.trim_history(contents, max_messages=10, max_chars=100000)
    assert len(trimmed) <= 10
    assert trimmed[0]["role"] == "user" and trimmed[0]["parts"][0].get("text")
    resp = trimmed[2]["parts"][0]["functionResponse"]["response"]["entries"]
    assert len(resp) == 11 and resp[-1].startswith("… ещё")


def test_render_reply_escapes_and_converts_markdown():
    out = agent.render_reply("**Удалил** <такси> 25 000\n* пункт\n- ещё\n## заголовок")
    assert out == "<b>Удалил</b> &lt;такси&gt; 25 000\n• пункт\n• ещё\nзаголовок"


def test_looks_like_command():
    assert agent.looks_like_command("удали последнее такси")
    assert agent.looks_like_command("покажи что я ел вчера")
    assert agent.looks_like_command("сколько потратил на еду")
    assert not agent.looks_like_command("такси 25000")
    assert not agent.looks_like_command("дал Алишеру 200000, а он вернул")


# ------------------------------------------------------------------ agent loop with a fake model
def _run(coro):
    return asyncio.run(coro)


def test_run_agent_executes_tools_then_answers():
    steps = [
        AgentStep(parts=[{"functionCall": {"name": "list_finance_entries", "args": {"category": "transport"}}}], text="", calls=[("list_finance_entries", {"category": "transport"})]),
        AgentStep(parts=[{"functionCall": {"name": "delete_finance_entries", "args": {"ids": ["1"]}}}], text="", calls=[("delete_finance_entries", {"ids": ["1"]})]),
        AgentStep(parts=[{"text": "Удалил такси 25 000."}], text="Удалил такси 25 000.", calls=[]),
    ]
    seen: list[tuple[str, dict]] = []

    async def step_fn(contents, **kw):
        # каждый вызов инструмента должен быть закрыт functionResponse
        calls = sum(1 for m in contents for p in m["parts"] if "functionCall" in p)
        resps = sum(1 for m in contents for p in m["parts"] if "functionResponse" in p)
        assert calls == resps
        return steps.pop(0)

    async def run_tool(name, args, ctx):
        seen.append((name, args))
        if name == "delete_finance_entries":
            ctx.mutated = True
        return {"ok": True}

    res = _run(agent.run_agent(_profile(), "удали такси", [], snapshot="—", step_fn=step_fn, run_tool=run_tool))
    assert res.text == "Удалил такси 25 000."
    assert [n for n, _ in seen] == ["list_finance_entries", "delete_finance_entries"]
    assert res.ctx.mutated and res.steps == 3
    assert res.contents[0]["parts"][0]["text"] == "удали такси"
    assert res.contents[-1]["parts"][0]["text"] == "Удалил такси 25 000."


def test_run_agent_handoff_stops_loop():
    steps = [AgentStep(parts=[{"functionCall": {"name": "hand_off", "args": {"module": "finance", "text": "такси 25000"}}}], text="", calls=[("hand_off", {"module": "finance", "text": "такси 25000"})])]

    async def step_fn(contents, **kw):
        return steps.pop(0)

    res = _run(agent.run_agent(_profile(), "такси 25000", [], snapshot="—", step_fn=step_fn))
    assert res.ctx.handoff == ("finance", "такси 25000")
    assert res.text == ""
    assert res.contents[-1]["role"] == "model"


def test_run_agent_step_limit_gives_fallback_text():
    async def step_fn(contents, **kw):
        return AgentStep(parts=[{"functionCall": {"name": "list_reminders", "args": {}}}], text="", calls=[("list_reminders", {})])

    async def run_tool(name, args, ctx):
        return {"reminders": []}

    res = _run(agent.run_agent(_profile(), "loop", [], snapshot="—", step_fn=step_fn, run_tool=run_tool, max_steps=3))
    assert "Слишком много шагов" in res.text and res.steps == 3


def test_run_agent_empty_answer_after_mutation_says_done():
    steps = [
        AgentStep(parts=[{"functionCall": {"name": "set_budget", "args": {}}}], text="", calls=[("set_budget", {})]),
        AgentStep(parts=[], text="", calls=[]),
    ]

    async def step_fn(contents, **kw):
        return steps.pop(0)

    async def run_tool(name, args, ctx):
        ctx.mutated = True
        return {"ok": True}

    res = _run(agent.run_agent(_profile(), "лимит", [], snapshot="—", step_fn=step_fn, run_tool=run_tool))
    assert res.text == "Готово."


# ------------------------------------------------------------------ undo with a fake db
class FakeDB:
    def __init__(self):
        self.calls: list[tuple] = []

    async def add_finance_entries(self, uid, rows, *, entry_date, source="manual"):
        self.calls.append(("add_finance_entries", [r["entry_date"] for r in rows]))
        return rows

    async def delete_finance_entries(self, uid, ids):
        self.calls.append(("delete_finance_entries", list(ids)))

    async def update_finance_entry(self, uid, entry_id, fields):
        self.calls.append(("update_finance_entry", entry_id, fields))

    async def delete_calorie_logs(self, uid, ids):
        self.calls.append(("delete_calorie_logs", list(ids)))

    async def add_calorie_logs(self, uid, rows):
        self.calls.append(("add_calorie_logs", [r["meal_desc"] for r in rows]))
        return rows

    async def update_reminder(self, uid, rid, fields):
        self.calls.append(("update_reminder", rid, fields))


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(undo, "db", db)
    return db


def test_undo_multi_applies_in_reverse(fake_db):
    uid = 42
    undo.begin_turn(uid)
    undo.push(uid, {"type": "restore_entries", "rows": [{"id": 1, "entry_type": "expense", "amount": 1, "category": "food", "note": "", "entry_date": "2026-09-20"}]})
    undo.push(uid, {"type": "delete_calorie_logs", "ids": [7]})
    undo.push(uid, {"type": "reminder_fields", "reminder_id": "r1", "fields": {"enabled": True}})
    assert undo.end_turn(uid) is True
    assert _run(undo.apply(uid, tz_name="Asia/Tashkent")) is True
    assert [c[0] for c in fake_db.calls] == ["update_reminder", "delete_calorie_logs", "add_finance_entries"]
    assert fake_db.calls[-1][1] == ["2026-09-20"]
    assert undo.peek(uid) is None
    assert _run(undo.apply(uid, tz_name="Asia/Tashkent")) is False


def test_undo_end_turn_without_changes_keeps_previous(fake_db):
    uid = 43
    undo.remember(uid, {"type": "delete_entries", "ids": [9]})
    undo.begin_turn(uid)
    assert undo.end_turn(uid) is False
    assert undo.peek(uid) == {"type": "delete_entries", "ids": [9]}


def test_undo_single_step_from_screens_is_compatible(fake_db):
    uid = 44
    undo.remember(uid, {"type": "restore_calorie_logs", "rows": [{"meal_desc": "плов", "calories": 500, "created_at": "2026-09-21T07:00:00+00:00"}]})
    assert _run(undo.apply(uid, tz_name="Asia/Tashkent"))
    assert fake_db.calls == [("add_calorie_logs", ["плов"])]


# ------------------------------------------------------------------ tools: handlers with fake services/db
def test_hand_off_and_open_screen_set_context():
    ctx = tools.ToolContext(profile=_profile(), text="съел плов")
    out = _run(tools.run("hand_off", {"module": "food", "text": "плов"}, ctx))
    assert out["ok"] and ctx.handoff == ("food", "плов") and ctx.calls == ["hand_off"]
    out = _run(tools.run("open_screen", {"screen": "budgets"}, ctx))
    assert out["screen"] == "budgets" and ctx.open_screen == "budgets"
    assert "error" in _run(tools.run("nope", {}, ctx))


def test_delete_entries_tool_records_undo(monkeypatch):
    calls = []

    async def entries(uid):
        return ENTRIES

    async def delete(uid, ids):
        calls.append(list(ids))

    monkeypatch.setattr(tools.services, "finance_entries", entries)
    monkeypatch.setattr(tools.db, "delete_finance_entries", delete)
    uid = 45
    undo.begin_turn(uid)
    ctx = tools.ToolContext(profile=Profile(telegram_id=uid, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="", username=""), text="")
    out = _run(tools.run("delete_finance_entries", {"ids": ["1", "3", "999"]}, ctx))
    assert [d["id"] for d in out["deleted"]] == ["1", "3"] and calls == [[1, 3]] and ctx.mutated
    assert undo.end_turn(uid)
    assert [r["id"] for r in undo.peek(uid)["steps"][0]["rows"]] == [1, 3]


def test_add_reminder_tool_parses_time_days_links(monkeypatch):
    added = {}

    async def add(uid, *, text, reminder_time, days_of_week, tz_name):
        added.update(text=text, time=reminder_time, days=days_of_week)
        return {"id": "r9", "reminder_text": text, "reminder_time": reminder_time, "days_of_week": days_of_week, "enabled": True}

    monkeypatch.setattr(tools.db, "add_reminder", add)
    ctx = tools.ToolContext(profile=_profile(), text="напоминай по будням в 9 https://youtu.be/x")
    out = _run(tools.run("add_reminder", {"text": "тренировка", "time": "9", "days": "weekdays"}, ctx))
    assert added["time"] == "09:00" and added["days"] == [1, 2, 3, 4, 5]
    assert out["added"]["links"] == ["https://youtu.be/x"] and out["added"]["text"] == "тренировка"
    assert "error" in _run(tools.run("add_reminder", {"text": "x", "time": "25:99"}, ctx))


def test_system_prompt_mentions_rules_and_snapshot():
    text = agent.system_prompt(_profile(), "Балансы: карта 1")
    assert "JES" in text and "Балансы: карта 1" in text and "hand_off" in text and "transport" in text


def test_compact_message_leaves_model_parts_untouched():
    msg = {"role": "model", "parts": [{"functionCall": {"name": "x", "args": {}}, "thoughtSignature": "abc"}]}
    assert agent.compact_message(msg) == msg


def test_tool_context_uid():
    ctx = tools.ToolContext(profile=_profile(), text="")
    assert ctx.uid == 1 and isinstance(ctx, SimpleNamespace) is False


def test_fuzzy_name_matching():
    assert tools.fuzzy_contains("Асельбек", "Асилбек")
    assert tools.fuzzy_contains("асадбек", "Асадбек ака")
    assert tools.fuzzy_contains("uzum", "UZUM BANK")
    assert tools.fuzzy_contains("Иззатилло", "Иззатилло ака")
    assert not tools.fuzzy_contains("такси", "обед")
    found = tools.filter_entries(ENTRIES, today=TODAY, note_contains="Алишир")
    assert [r["id"] for r in found] == [5]

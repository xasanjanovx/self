"""Ассистент: проактивные правила, цели, заметки/задачи (инструменты с фейковой БД), голос."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from bot import agent_tools as tools
from bot import agent_tools_assistant as asst
from bot import analysis, proactive, undo, voice
from bot.profile import Profile

TODAY = date(2026, 9, 21)  # понедельник
TZ = ZoneInfo("Asia/Tashkent")


def _run(coro):
    return asyncio.run(coro)


def _profile(uid: int = 7) -> Profile:
    return Profile(telegram_id=uid, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Тест", username="t")


def _e(id_, day: date, amount: float, category: str = "food", *, kind: str = "expense", note: str = "") -> dict:
    return {"id": id_, "entry_type": kind, "amount": amount, "category": category, "note": f"[b:card] {note}".strip(), "entry_date": day.isoformat()}


# ------------------------------------------------------------------ goals
def test_goal_status_needed_per_month_and_track():
    goal = {"id": 1, "title": "Ноутбук", "target_amount": 10_000_000, "saved_amount": 4_000_000, "deadline": "2027-01-01"}
    st = analysis.goal_status(goal, TODAY, projected_saving_month=900_000)
    assert st["remaining"] == 6_000_000 and 0.39 < st["ratio"] < 0.41
    assert 3.2 < st["months_left"] < 3.5
    assert 1_700_000 < st["needed_per_month"] < 1_900_000
    assert st["on_track"] is False and st["months_at_current_pace"] > 6
    done = analysis.goal_status({"id": 2, "title": "x", "target_amount": 100, "saved_amount": 100}, TODAY)
    assert done["done"] is True


# ------------------------------------------------------------------ proactive rules
def test_debt_alerts_due_tomorrow_and_overdue_with_copy_text():
    ledger = {"lent": [("Асилбек", 1_000_000.0), ("Иззатилло ака", 1_100_000.0)], "debt": [("UZUM BANK", 2_205_000.0)]}
    deadlines = [
        {"person": "Асилбек", "side": "lent", "due_date": (TODAY + timedelta(days=1)).isoformat()},
        {"person": "Иззатилло", "side": "lent", "due_date": (TODAY - timedelta(days=10)).isoformat()},
        {"person": "UZUM BANK", "side": "debt", "due_date": TODAY.isoformat()},
        {"person": "Никто", "side": "lent", "due_date": TODAY.isoformat()},
    ]
    alerts = proactive.debt_alerts(deadlines, ledger, TODAY, lang="ru", hour=10)
    keys = {a.key.split(":")[0] for a in alerts}
    assert keys == {"debt_due", "debt_overdue", "debt_today"}
    due = next(a for a in alerts if a.key.startswith("debt_due"))
    assert "Асилбек" in due.text and "1 000 000" in due.copy_text and due.persistent
    overdue = next(a for a in alerts if a.key.startswith("debt_overdue"))
    assert overdue.key.endswith(":1") and "10 дн." in overdue.text and "10 дн." in overdue.copy_text
    bank = next(a for a in alerts if a.key.startswith("debt_today"))
    assert bank.copy_text is None  # своему кредитору сообщение не пишем
    assert proactive.debt_alerts(deadlines, ledger, TODAY, lang="ru", hour=7) == []


def test_spike_alert_only_when_3x_usual():
    entries = [_e(i, TODAY - timedelta(days=i), 50_000, "food", note="обед") for i in range(1, 15)]
    assert proactive.spike_alert(entries, TODAY, lang="ru", hour=20) is None
    entries.append(_e(99, TODAY, 400_000, "shopping", note="куртка"))
    a = proactive.spike_alert(entries, TODAY, lang="ru", hour=20)
    assert a and a.key == f"spike:{TODAY}" and "куртка" in a.text and "8 раза" in a.text
    assert proactive.spike_alert(entries, TODAY, lang="ru", hour=12) is None


def test_recurring_and_budget_and_goal_alerts():
    rec = [{"id": 1, "title": "Аренда", "amount": 2_000_000, "day_of_month": 23, "enabled": True}]
    a = proactive.recurring_alerts(rec, 500_000, TODAY, lang="ru", hour=10)
    assert len(a) == 1 and "Аренда" in a[0].text and "1 500 000" in a[0].text
    assert proactive.recurring_alerts(rec, 5_000_000, TODAY, lang="ru", hour=10) == []

    fc = {"days_left": 9, "budget_alerts": [{"category": "food", "limit": 700_000, "spent": 600_000, "projected": 850_000, "over_pct": 21.4}]}
    b = proactive.budget_alerts(fc, TODAY, lang="ru", hour=10)
    assert len(b) == 1 and b[0].key == "budget_proj:food:2026-09" and "Еда" in b[0].text
    assert proactive.budget_alerts(fc, date(2026, 9, 5), lang="ru", hour=10) == []

    goals = [{"id": 5, "title": "Ноутбук", "target_amount": 10_000_000, "saved_amount": 1_000_000, "deadline": "2027-01-01"}]
    fc2 = {"income_this_month": 4_000_000, "projected_month_expense": 3_800_000, "recurring_remaining": 0}
    g = proactive.goal_alerts(goals, fc2, None, TODAY, lang="ru", hour=10)
    assert len(g) == 1 and g[0].key == "goal_pace:5:2026-09" and "Ноутбук" in g[0].text
    fc3 = {"income_this_month": 9_000_000, "projected_month_expense": 3_000_000, "recurring_remaining": 0}
    assert proactive.goal_alerts(goals, fc3, None, TODAY, lang="ru", hour=10) == []


def test_nutrition_alert_low_once_per_week():
    logs = [{"created_at": f"{(TODAY - timedelta(days=i)).isoformat()}T07:00:00+00:00", "calories": 1000, "protein": 30} for i in range(4)]
    a = proactive.nutrition_alert(logs, {"daily_calories": 2500, "title": "Набор"}, TODAY, tz=TZ, lang="ru", hour=21)
    assert a and a.key == "nutri_low:2026-39" and "1000 ккал" in a.text
    assert proactive.nutrition_alert(logs, {"daily_calories": 2500}, TODAY, tz=TZ, lang="ru", hour=12) is None
    assert proactive.nutrition_alert(logs[:2], {"daily_calories": 2500}, TODAY, tz=TZ, lang="ru", hour=21) is None


def test_weekly_alert_only_sunday_evening():
    sunday = date(2026, 9, 27)
    a = proactive.weekly_alert(sunday, lang="ru", hour=19)
    assert a and a.prompt and a.key == "weekly:2026-39"
    assert proactive.weekly_alert(sunday, lang="ru", hour=10) is None
    assert proactive.weekly_alert(TODAY, lang="ru", hour=20) is None


def test_due_tasks_window_and_notified_key():
    now = datetime(2026, 9, 21, 18, 30, tzinfo=TZ)
    tasks = [
        {"id": 1, "text": "позвонить маме", "due_date": "2026-09-21", "due_time": "18:00", "done": False},
        {"id": 2, "text": "уже напоминали", "due_date": "2026-09-21", "due_time": "18:00", "done": False, "notified_key": "2026-09-21"},
        {"id": 3, "text": "завтра", "due_date": "2026-09-22", "due_time": "09:00", "done": False},
        {"id": 4, "text": "слишком давно", "due_date": "2026-09-21", "due_time": "10:00", "done": False},
        {"id": 5, "text": "без времени", "due_date": "2026-09-21", "due_time": None, "done": False},
    ]
    assert [t["id"] for t in proactive.due_tasks(tasks, now)] == [1]


# ------------------------------------------------------------------ assistant tools with a fake db
class FakeDB:
    def __init__(self):
        self.notes: list[dict] = []
        self.tasks: list[dict] = []
        self.goals: list[dict] = []
        self.deadlines: list[dict] = []
        self._id = 0

    def available(self, name):
        return True

    async def ensure_available(self, name):
        return True

    def _next(self):
        self._id += 1
        return self._id

    async def list_notes(self, uid):
        return list(reversed(self.notes))

    async def add_note(self, uid, text):
        row = {"id": self._next(), "text": text, "created_at": "2026-09-21T00:00:00"}
        self.notes.append(row)
        return row

    async def delete_notes(self, uid, ids):
        self.notes = [n for n in self.notes if n["id"] not in set(ids)]

    async def list_tasks(self, uid, *, include_done=False):
        return [t for t in self.tasks if include_done or not t["done"]]

    async def add_task(self, uid, *, text, due_date, due_time):
        row = {"id": self._next(), "text": text, "due_date": due_date, "due_time": due_time, "done": False}
        self.tasks.append(row)
        return row

    async def update_task(self, uid, task_id, fields):
        for t in self.tasks:
            if t["id"] == task_id:
                t.update(fields)

    async def delete_tasks(self, uid, ids):
        self.tasks = [t for t in self.tasks if t["id"] not in set(ids)]

    async def delete_debt_deadline(self, uid, *, person, side):
        self.deadlines = [d for d in self.deadlines if not (d["person"] == person and d["side"] == side)]

    async def list_goals(self, uid, *, include_done=False):
        return list(self.goals)

    async def add_goal(self, uid, *, title, target_amount, saved_amount=0.0, deadline=None):
        row = {"id": self._next(), "title": title, "target_amount": target_amount, "saved_amount": saved_amount, "deadline": deadline, "done": False}
        self.goals.append(row)
        return row

    async def update_goal(self, uid, goal_id, fields):
        for g in self.goals:
            if g["id"] == goal_id:
                g.update(fields)

    async def list_debt_deadlines(self, uid):
        return list(self.deadlines)

    async def upsert_debt_deadline(self, uid, *, person, side, due_date, note=None):
        self.deadlines = [d for d in self.deadlines if not (d["person"] == person and d["side"] == side)]
        row = {"id": self._next(), "person": person, "side": side, "due_date": due_date, "note": note}
        self.deadlines.append(row)
        return row


def _wire(monkeypatch):
    fdb = FakeDB()
    monkeypatch.setattr(asst, "db", fdb)
    monkeypatch.setattr(asst.services, "db", fdb)
    monkeypatch.setattr(undo, "db", fdb)
    return fdb


def test_notes_and_tasks_tools(monkeypatch):
    fdb = _wire(monkeypatch)
    ctx = tools.ToolContext(profile=_profile(71), text="")
    undo.begin_turn(71)
    out = _run(tools.run("add_note", {"text": "у брата день рождения 3 ноября"}, ctx))
    assert out["added"]["text"].startswith("у брата")
    out = _run(tools.run("add_task", {"text": "поздравить брата", "due_date": "2026-11-03"}, ctx))
    assert out["added"]["due_date"] == "2026-11-03" and out["added"]["days_left"] == 43
    out = _run(tools.run("add_task", {"text": "позвонить маме", "due_time": "18"}, ctx))
    assert out["added"]["due_time"] == "18:00" and out["added"]["due_date"] == _profile().today.isoformat()
    listed = _run(tools.run("list_notes", {"query": "брат"}, ctx))
    assert listed["total"] == 1
    tid = fdb.tasks[0]["id"]
    out = _run(tools.run("complete_tasks", {"ids": [str(tid)]}, ctx))
    assert out["completed"][0]["id"] == str(tid) and fdb.tasks[0]["done"] is True
    assert undo.end_turn(71)
    _run(undo.apply(71, tz_name="Asia/Tashkent"))
    # откат в обратном порядке: снята отметка «выполнено», затем удалены обе задачи и заметка
    assert len(fdb.notes) == 0 and len(fdb.tasks) == 0


def test_goal_tools_add_amount_and_status(monkeypatch):
    fdb = _wire(monkeypatch)
    ctx = tools.ToolContext(profile=_profile(72), text="")
    out = _run(tools.run("add_goal", {"title": "Ноутбук", "target_amount": "10 млн", "deadline": "2027-01-01", "saved_amount": 1000000}, ctx))
    assert out["added"]["target"] == 10_000_000 and out["added"]["saved"] == 1_000_000 and "needed_per_month" in out["added"]
    gid = str(fdb.goals[0]["id"])
    out = _run(tools.run("update_goal", {"id": gid, "add_amount": "500к"}, ctx))
    assert out["goal"]["saved"] == 1_500_000
    assert "error" in _run(tools.run("update_goal", {"id": "999", "add_amount": 1}, ctx))


def test_set_debt_deadline_matches_fuzzy_name(monkeypatch):
    fdb = _wire(monkeypatch)
    entries = [{"id": 1, "entry_type": "expense", "amount": 1_000_000, "category": "transfer", "note": "[x:card>lent] Асилбек", "entry_date": "2026-09-16"}]

    async def snap(profile):
        from bot import finance as fin
        from bot.services import FinanceSnapshot

        return FinanceSnapshot(entries=entries, settings={}, balances=fin.compute_balances(entries), today=TODAY)

    monkeypatch.setattr(asst.services, "finance_snapshot", snap)
    ctx = tools.ToolContext(profile=_profile(73), text="")
    out = _run(tools.run("set_debt_deadline", {"person": "Асельбек", "due_date": "2026-10-05"}, ctx))
    assert out["matched_person"] == "Асилбек" and out["deadline"]["side"] == "lent" and out["amount_now"] == 1_000_000
    assert fdb.deadlines[0]["person"] == "Асилбек"
    out = _run(tools.run("clear_debt_deadline", {"person": "асил"}, ctx))
    assert out["cleared"][0]["person"] == "Асилбек"


# ------------------------------------------------------------------ voice
def test_speakable_strips_html_emoji_and_limits():
    text = "<b>Удалил</b> такси 25 000 🔥\n• пункт один\n\n\nвторой — абзац. " + "Очень длинное предложение. " * 60
    plain = voice.speakable(text)
    assert plain.startswith("Удалил такси 25 000")
    assert "<b>" not in plain and "🔥" not in plain and "•" not in plain
    assert len(plain) <= voice.MAX_CHARS and plain.endswith(".")

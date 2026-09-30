"""30.09: цели — «следующий шаг», прогресс без ручного ввода (этапы, отметки из плана), идеи целей вместо пустого экрана."""
import asyncio
from datetime import date, datetime, timedelta, timezone

from bot import goal_ideas, goal_steps, goals as goals_mod, plan
from bot.profile import Profile

TODAY = date(2026, 9, 30)


def _profile(uid: int = 1) -> Profile:
    return Profile(telegram_id=uid, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Тест", username="t")


def _status(goal: dict, **data) -> dict:
    return goals_mod.status(goal, goals_mod.GoalData(today=TODAY, **data))


def test_next_step_for_every_kind():
    save = _status({"id": 1, "title": "Ноутбук", "kind": "save", "target_amount": 10_000_000, "saved_amount": 2_000_000,
                    "deadline": "2027-01-01", "created_at": "2026-09-01T00:00:00+00:00"})
    s = goal_steps.step(save)
    assert s and s["amount"] >= 1000 and "отложите" in s["text"] and s["amount"] % 1000 == 0
    cap = _status({"id": 2, "title": "Кафе", "kind": "spend_cap", "target_amount": 700_000, "params": {"category": "food_cafe"}})
    assert goal_steps.step(cap)["text"].startswith("сегодня — до") or goal_steps.step(cap)["text"] == "сегодня без трат"
    habit = _status({"id": 3, "title": "ПДД", "kind": "habit", "target_amount": 4}, checkins=[])
    assert "сделайте и отметьте (0/4)" in goal_steps.step(habit)["text"]
    checked = _status({"id": 3, "title": "ПДД", "kind": "habit", "target_amount": 4}, checkins=[{"goal_id": 3, "day": TODAY.isoformat()}])
    assert goal_steps.step(checked) is None                                        # сегодня уже отмечено — шага нет
    done = _status({"id": 1, "title": "x", "kind": "save", "target_amount": 100, "saved_amount": 100})
    assert goal_steps.step(done) is None


def test_custom_goal_progress_is_computed_from_milestones():
    goal = {"id": 9, "title": "Выучить 500 слов", "kind": "custom", "target_amount": 100, "saved_amount": 0, "created_at": "2026-09-01T00:00:00+00:00",
            "params": {"steps": [{"id": "s1", "text": "Выучить первые 100", "done": True}, {"id": "s2", "text": "Выучить вторые 100", "done": False},
                                 {"id": "s3", "text": "Повторить всё", "done": False}, {"id": "s4", "text": "Сдать тест", "done": False}]}}
    st = _status(goal)
    assert st["progress_pct"] == 25 and st["steps_done"] == 1 and st["steps_total"] == 4 and st["next_step_text"] == "Выучить вторые 100"
    text = "\n".join(goals_mod.lines(st, "ru"))
    assert "🪜 Этапы: 1/4" in text and "➡️ <b>Шаг сегодня:</b> Выучить вторые 100" in text
    goal["params"]["steps"][1]["done"] = goal["params"]["steps"][2]["done"] = goal["params"]["steps"][3]["done"] = True
    assert _status(goal)["done"] is True                                          # все этапы — цель выполнена сама


def test_goal_lines_have_step_and_summary_for_the_model():
    st = _status({"id": 2, "title": "Кафе", "kind": "spend_cap", "target_amount": 700_000})
    assert any(line.startswith("➡️ <b>Шаг сегодня:</b>") for line in goals_mod.lines(st, "ru"))
    assert "ШАГ СЕГОДНЯ:" in goals_mod.prompt_summary([st])


class FakeDb:
    def __init__(self, goal):  # noqa: ANN001
        self.goal, self.checkins, self.updates = goal, [], []

    def available(self, name):  # noqa: ANN001, ANN202
        return True

    async def upsert_checkin(self, uid, *, goal_id, day, **k):  # noqa: ANN001, ANN003, ANN202
        self.checkins.append((goal_id, day))

    async def delete_checkin(self, uid, *, goal_id, day):  # noqa: ANN001, ANN202
        self.checkins.remove((goal_id, day))

    async def update_goal(self, uid, goal_id, fields):  # noqa: ANN001, ANN202
        self.goal.update(fields)
        self.updates.append(fields)


def _apply_setup(monkeypatch, goal):  # noqa: ANN001, ANN202
    from bot import services

    fake = FakeDb(goal)

    async def goals(uid):  # noqa: ANN001, ANN202
        return [goal]

    async def statuses_for(profile, goals=None):  # noqa: ANN001, ANN202
        return [goals_mod.status(goal, goals_mod.GoalData(today=TODAY))], None

    monkeypatch.setattr(goal_steps, "db", fake)
    monkeypatch.setattr(services, "goals", goals)
    monkeypatch.setattr(goals_mod, "statuses_for", statuses_for)
    monkeypatch.setattr(goal_steps.cache, "invalidate", lambda *a, **k: None)
    monkeypatch.setattr(Profile, "today", property(lambda self: TODAY))
    return fake


def test_checkmark_in_plan_moves_the_goal_by_itself(monkeypatch):
    # привычка: ✅ → отметка «сегодня», снятие ✅ — отметка убирается
    habit = {"id": 3, "title": "ПДД", "kind": "habit", "target_amount": 4}
    fake = _apply_setup(monkeypatch, habit)
    asyncio.run(goal_steps.apply(_profile(), "3", True))
    assert fake.checkins == [(3, "2026-09-30")]
    asyncio.run(goal_steps.apply(_profile(), "3", False))
    assert fake.checkins == []
    # накопления: ✅ → сумма шага прибавляется к «отложено», отмена — вычитается ровно столько же
    save = {"id": 1, "title": "Ноутбук", "kind": "save", "target_amount": 10_000_000, "saved_amount": 2_000_000, "deadline": "2027-01-01",
            "created_at": "2026-09-01T00:00:00+00:00"}
    fake = _apply_setup(monkeypatch, save)
    effect = asyncio.run(goal_steps.apply(_profile(), "1", True))
    amount = effect["amount"]
    assert amount > 0 and save["saved_amount"] == 2_000_000 + amount
    asyncio.run(goal_steps.apply(_profile(), "1", False, effect))
    assert save["saved_amount"] == 2_000_000
    # свободная цель: ✅ → очередной этап выполнен; снятие — тот же этап снова не выполнен
    custom = {"id": 9, "title": "Слова", "kind": "custom", "target_amount": 100, "created_at": "2026-09-01T00:00:00+00:00",
              "params": {"steps": [{"id": "s1", "text": "A", "done": False}, {"id": "s2", "text": "B", "done": False}]}}
    fake = _apply_setup(monkeypatch, custom)
    effect = asyncio.run(goal_steps.apply(_profile(), "9", True))
    assert effect == {"kind": "custom", "step": "s1"} and custom["params"]["steps"][0]["done"] is True
    asyncio.run(goal_steps.apply(_profile(), "9", False, effect))
    assert custom["params"]["steps"][0]["done"] is False


def test_plan_checkmark_on_goal_step_calls_goal_steps(monkeypatch):
    calls: list = []

    async def apply(profile, goal_id, done, effect=None):  # noqa: ANN001, ANN202
        calls.append((goal_id, done, effect))
        return {"kind": "habit"} if done else None

    monkeypatch.setattr(goal_steps, "apply", apply)
    it = {"id": "i1", "text": "Урок ПДД", "ref": {"type": "goal", "id": "3"}, "done": True}
    asyncio.run(plan._apply_done(_profile(), it))
    assert calls == [("3", True, None)] and it["effect"] == {"kind": "habit"}
    it["done"] = False
    asyncio.run(plan._apply_done(_profile(), it))
    assert calls[-1] == ("3", False, {"kind": "habit"}) and it["effect"] is None


def test_milestones_are_generated_once_for_custom_goals(monkeypatch):
    from bot import ai as ai_mod

    goal = {"id": 9, "title": "Выучить 500 слов", "kind": "custom", "target_amount": 100, "params": {}}
    fake = FakeDb(goal)
    asked: list = []

    async def generate(parts, **k):  # noqa: ANN001, ANN003, ANN202
        asked.append(parts[0]["text"])
        return '{"goals": [{"id": "9", "steps": ["Собрать список слов", "Выучить 250", "Выучить 500"]}]}'

    monkeypatch.setattr(goal_steps, "db", fake)
    monkeypatch.setattr(goal_steps.ai, "generate", generate)
    monkeypatch.setattr(goal_steps.cache, "invalidate", lambda *a, **k: None)
    monkeypatch.setattr(ai_mod, "extract_json", ai_mod.extract_json)
    assert asyncio.run(goal_steps.ensure_steps(_profile(), [goal])) == 1
    assert [s["text"] for s in goal["params"]["steps"]] == ["Собрать список слов", "Выучить 250", "Выучить 500"]
    assert asyncio.run(goal_steps.ensure_steps(_profile(), [goal])) == 0 and len(asked) == 1     # второй раз — этапы уже есть


def test_ideas_come_from_his_data_and_are_ready_to_add(monkeypatch):
    from bot import services

    async def nprofile(uid):  # noqa: ANN001, ANN202
        return {"mode": "gain", "weight": 62.0, "daily_calories": 2550}

    async def entries(uid):  # noqa: ANN001, ANN202
        rows = []
        for i in range(30):
            d = (TODAY - timedelta(days=i)).isoformat()
            rows.append({"entry_date": d, "amount": 25_000, "entry_type": "expense", "category": "food_cafe", "note": "кафе"})
            rows.append({"entry_date": d, "amount": 12_000, "entry_type": "expense", "category": "utilities", "note": "коммуналка"})
        return rows

    monkeypatch.setattr(services, "nutrition_profile", nprofile)
    monkeypatch.setattr(services, "finance_entries", entries)
    monkeypatch.setattr(goal_ideas.lessons, "items", lambda uid: [{"title": "ПДД курс", "position": 700, "duration": 3600, "at": datetime.now(timezone.utc).timestamp()}])
    monkeypatch.setattr(Profile, "today", property(lambda self: TODAY))
    found = asyncio.run(goal_ideas.ideas(_profile(), []))
    keys = [i["key"] for i in found]
    assert "weight" in keys and any(k.startswith("cap:") for k in keys) and "lesson" in keys
    weight = next(i for i in found if i["key"] == "weight")
    assert weight["args"]["kind"] == "weight" and weight["args"]["target_amount"] == 65.0 and weight["args"]["current_weight"] == 62.0
    cap = next(i for i in found if i["key"].startswith("cap:"))
    assert cap["args"]["kind"] == "spend_cap" and cap["args"]["target_amount"] % 1000 == 0 and cap["args"]["target_amount"] < 25_000 * 30
    # уже есть цель этого вида — идею не повторяем
    have = [{"id": 1, "title": "Вес", "kind": "weight"}, {"id": 2, "title": "Кафе", "kind": "spend_cap", "params": {"category": cap["args"]["category"]}}]
    again = [i["key"] for i in asyncio.run(goal_ideas.ideas(_profile(), have))]
    assert "weight" not in again and cap["key"] not in again


def test_goals_keyboard_shows_ideas_and_habits_card_is_gone():
    from bot import habits
    from bot.keyboards import goals_keyboard

    ideas = [{"key": "weight", "icon": "⚖️", "title": "Набрать до 65 кг"}, {"key": "cap:food_cafe", "icon": "💸", "title": "Кафе: не больше 640 000"}]
    data = [b.callback_data for row in goals_keyboard([], "ru", ideas=ideas).inline_keyboard for b in row]
    assert data[:2] == ["goal:idea:weight", "goal:idea:cap:food_cafe"] and "goal:add" in data
    with_goals = [b.callback_data for row in goals_keyboard([{"id": 1, "title": "x", "kind": "save"}], "ru").inline_keyboard for b in row]
    assert "goal:ideas" in with_goals                                             # цели есть, идей не показано — кнопка «💡 Идеи целей»
    assert not hasattr(habits, "habits_card")                                    # блок «Что я о тебе знаю» убран

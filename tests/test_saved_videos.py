"""30.09: бот помнит только видео и плейлисты, которые он прислал ссылкой (save_video); цель = ежедневное дело, задача — задача;
голая ссылка — вопрос кнопками; совет «продолжим?» — только про видео с целью/задачей."""
from __future__ import annotations

import asyncio

from bot import lessons
from bot import agent_tools as tools
from bot import agent_tools_daily as atd
from bot import daily_tasks, undo
from bot.profile import Profile

LINK = "https://www.youtube.com/playlist?list=PLabcdefghij"


def _run(coro):
    return asyncio.run(coro)


def _profile(uid: int = 31) -> Profile:
    return Profile(telegram_id=uid, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Тест", username="t")


class FakeDB:
    def __init__(self):
        self.tasks: list[dict] = []

    async def ensure_available(self, name):  # noqa: ANN001
        return True

    async def add_task(self, uid, *, text, due_date, due_time, ref_key=None):  # noqa: ANN001
        row = {"id": len(self.tasks) + 1, "text": text, "due_date": due_date, "due_time": due_time}
        self.tasks.append(row)
        return row


def _wire(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    fdb = FakeDB()
    monkeypatch.setattr(atd, "db", fdb)

    async def info(list_id):  # noqa: ANN001
        return {"title": "Курс английского", "videos": [{"id": f"vid{i:08d}", "title": f"Урок {i}"} for i in range(1, 6)]}

    async def oembed(video_id):  # noqa: ANN001
        return {"title": "Разбор долгов и кредитов простыми словами", "channel": "Канал"}

    monkeypatch.setattr(lessons, "playlist_info", info)
    monkeypatch.setattr(lessons, "_oembed", oembed)
    return fdb


def test_bare_link_is_saved_and_asks_with_buttons(monkeypatch, tmp_path):
    fdb = _wire(monkeypatch, tmp_path)
    ctx = tools.ToolContext(profile=_profile(), text=LINK)
    out = _run(tools.run("save_video", {"link": LINK}, ctx))
    assert out["saved"] == "Курс английского" and out["videos"] == 5 and out["asked"]
    assert ctx.ask["options"] == ["🎯 Цель — каждый день", "📝 Задача", "Просто помнить"]
    assert [s["title"] for s in lessons.saved(31)] == ["Курс английского"] and fdb.tasks == [] and daily_tasks.all_items(31) == []


def test_goal_asks_time_then_creates_daily_goal_with_progress(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    ctx = tools.ToolContext(profile=_profile(), text="")
    out = _run(tools.run("save_video", {"link": LINK, "purpose": "goal"}, ctx))
    assert out["asked"] and ctx.ask["options"][-1] == "Без напоминания" and daily_tasks.all_items(31) == []
    ctx = tools.ToolContext(profile=_profile(), text="")
    out = _run(tools.run("save_video", {"purpose": "goal", "time": "20:00"}, ctx))          # ссылку не повторил — берём последнюю присланную
    assert out["ok"] and out["goal"]["time"] == "20:00" and ctx.ask is None
    (habit,) = daily_tasks.all_items(31)
    assert habit["title"] == "Курс английского" and habit["total"] == 5 and habit["link"] == LINK
    assert lessons.saved(31)[0]["why"] == "goal"


def test_goal_without_reminder(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    ctx = tools.ToolContext(profile=_profile(), text="")
    out = _run(tools.run("save_video", {"link": LINK, "purpose": "goal", "no_time": True}, ctx))
    assert out["ok"] and daily_tasks.all_items(31)[0]["time"] is None


def test_task_and_keep(monkeypatch, tmp_path):
    fdb = _wire(monkeypatch, tmp_path)
    undo.begin_turn(31)
    ctx = tools.ToolContext(profile=_profile(), text="")
    out = _run(tools.run("save_video", {"link": "https://youtu.be/abcdefghijk", "purpose": "task", "due_date": "2026-10-03"}, ctx))
    assert out["ok"] and fdb.tasks[0]["due_date"] == "2026-10-03" and "Разбор долгов" in fdb.tasks[0]["text"] and "abcdefghijk" in fdb.tasks[0]["text"]
    assert lessons.saved(31)[0]["why"] == "task"
    out = _run(tools.run("save_video", {"link": LINK, "purpose": "keep"}, ctx))
    assert out["ok"] and lessons.saved(31)[0]["why"] is None and daily_tasks.all_items(31) == []


def test_not_a_youtube_link_and_no_link(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    ctx = tools.ToolContext(profile=_profile(), text="")
    assert "error" in _run(tools.run("save_video", {"link": "https://example.com/x"}, ctx))
    assert "error" in _run(tools.run("save_video", {}, ctx))


def test_list_and_forget(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    ctx = tools.ToolContext(profile=_profile(), text="")
    _run(tools.run("save_video", {"link": LINK, "purpose": "keep"}, ctx))
    listed = _run(tools.run("list_saved_videos", {}, ctx))
    assert listed["saved"][0]["title"] == "Курс английского" and listed["saved"][0]["as"] == "просто помню"
    assert _run(tools.run("forget_video", {"query": "английского"}, ctx))["forgot"] == "Курс английского"
    assert lessons.saved(31) == [] and "error" in _run(tools.run("forget_video", {"query": "английского"}, ctx))


def test_add_daily_with_link_registers_it_so_progress_is_tracked(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    ctx = tools.ToolContext(profile=_profile(), text="")
    out = _run(tools.run("add_daily", {"title": "Урок английского", "time": "20:00", "link": LINK, "total": 40}, ctx))
    assert out["ok"] and [(s["key"], s["why"]) for s in lessons.saved(31)] == [("p:PLabcdefghij", "goal")]
    assert lessons.note(31, {"title": "Урок 3", "position_s": 100, "duration_s": 900}) is not None


def test_phone_watching_unsent_video_stores_nothing(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    for title in ("Какой-то клип", "Ещё один ролик из рекомендаций"):
        assert lessons.note(31, {"title": title, "position_s": 400, "duration_s": 900, "state": "paused"}) is None
    assert lessons.items(31) == [] and lessons.saved(31) == []
    assert not (tmp_path / "media" / "31.json").exists()


def test_nudge_only_for_goal_or_task_videos(monkeypatch, tmp_path):
    import time as _t

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    lessons.register(31, key="v:keepvideo01", kind="video", video_id="keepvideo01", title="Просто запомненное видео про Python", why=None)
    lessons.register(31, key="v:goalvideo01", kind="video", video_id="goalvideo01", title="Видео-цель про английский язык", why="goal")
    for title in ("Просто запомненное видео про Python", "Видео-цель про английский язык"):
        lessons.note(31, {"title": title, "position_s": 300, "duration_s": 1500, "state": "paused"})
    rows = lessons.items(31)
    for r in rows:   # смотрел вчера, а не сегодня
        r["at"] = _t.time() - 86400
    lessons._save(31, rows)
    tracked = [r["title"] for r in lessons.items(31) if lessons.tracked(31, r)]
    assert tracked == ["Видео-цель про английский язык"]

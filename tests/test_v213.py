"""29.09: каждый день (дело + цель), «продолжи урок» YouTube, напоминания по месту, заблокировавшие бота, мгновенные команды."""
import asyncio
from datetime import date, datetime, timedelta

from bot import blocked, daily_tasks, geo, instant, lessons


def test_daily_task_goal_streak_and_due(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    item = daily_tasks.add(1, "Урок английского", at="20:00", link="https://www.youtube.com/playlist?list=PLabcdefghij", total=40)
    assert item["time"] == "20:00" and item["days"] == daily_tasks.ALL_DAYS
    day = date(2026, 9, 29)
    assert [h["title"] for h in daily_tasks.due(1, datetime(2026, 9, 29, 20, 5))] == ["Урок английского"]
    assert daily_tasks.due(1, datetime(2026, 9, 29, 19, 59)) == []
    daily_tasks.mark_reminded(1, item, day)
    assert daily_tasks.due(1, datetime(2026, 9, 29, 20, 30)) == []           # уже напомнили сегодня
    item = daily_tasks.done(1, daily_tasks.find(1, "урок английского"), day)
    item = daily_tasks.done(1, item, day)                                     # второй раз за день — не считается
    assert item["done"] == 1 and item["streak"] == 1
    item = daily_tasks.done(1, item, day + timedelta(days=1))
    assert item["streak"] == 2 and item["done"] == 2
    text = daily_tasks.progress(item, day + timedelta(days=1))
    assert "серия 2" in text and "2/40 (5%)" in text and "закончите" in text
    assert daily_tasks.today_lines(1, day + timedelta(days=1))[0].startswith("✅ 20:00 Урок английского")


def test_lessons_note_find_and_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    lessons.note(1, {"title": "English Lesson 5 — Past Simple", "channel": "EngTeacher", "position_s": 754, "duration_s": 1500, "state": "paused"})
    lessons.note(1, {"title": "Какой-то клип", "channel": "Music", "position_s": 30, "duration_s": 200, "state": "closed"})
    assert lessons.find(1, "english")["title"].startswith("English Lesson 5")
    assert lessons.find(1)["title"] == "Какой-то клип"                        # без слов — последний

    async def search(q, limit=5):  # noqa: ANN001
        return [{"id": "abcdefghijk", "title": "English Lesson 5 — Past Simple", "channel": "EngTeacher"}]

    from bot import media

    monkeypatch.setattr(media, "youtube_search", search)
    res = asyncio.run(lessons.resume(1, "english"))
    assert res["video_id"] == "abcdefghijk" and res["start"] == 751 and res["position"] == "12:34"
    assert lessons.url(res) == "https://www.youtube.com/watch?v=abcdefghijk&t=751s"
    assert lessons.playlist_id("https://www.youtube.com/playlist?list=PLabcdefghij") == "PLabcdefghij"
    assert lessons.video_id_of("https://youtu.be/abcdefghijk?t=5") == "abcdefghijk"


def test_lessons_playlist_next_after_finished(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    lessons.note(1, {"title": "Урок 1", "position_s": 590, "duration_s": 600})  # досмотрел

    async def playlist(list_id):  # noqa: ANN001
        return [{"id": "aaaaaaaaaaa", "title": "Урок 1"}, {"id": "bbbbbbbbbbb", "title": "Урок 2"}]

    monkeypatch.setattr(lessons, "playlist", playlist)
    res = asyncio.run(lessons.resume(1, link="https://www.youtube.com/playlist?list=PLxxxxxxxxxx"))
    assert res["video_id"] == "bbbbbbbbbbb" and res["start"] == 0 and res["next"]


def test_geo_places_and_reminders(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert geo.add_reminder(1, "дом", "купить хлеб")["need_place"] == "дом"
    geo.save_place(1, "мой дом", 40.78, 72.34)
    r = geo.add_reminder(1, "домой", "купить хлеб")
    all_zones = geo.for_phone(1)["zones"]
    assert [z["id"] for z in all_zones if z["when"] == "track"] == ["place:дом"]   # 29.09: приходы/уходы в память дел
    zones = [z for z in all_zones if z["when"] != "track"]
    assert zones[0]["id"] == r["id"] and zones[0]["lat"] == 40.78 and zones[0]["when"] == "arrive"
    assert geo.fired(1, r["id"], entering=False) is None                    # ушёл — а просили «когда приду»
    assert geo.fired(1, r["id"], entering=True)["text"] == "купить хлеб"
    assert geo.reminders(1) == []                                           # одноразовое


def test_blocked_users_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    blocked.reset_cache()
    from bot import access

    monkeypatch.setattr(access, "_members", {111, 222})
    blocked.mark(222, "test")
    assert 222 not in access.user_ids() and 111 in access.user_ids()
    blocked.unmark(222)
    assert 222 in access.user_ids()
    blocked.reset_cache()


def test_instant_resume_and_place():
    assert instant.parse("продолжи урок").tool == "resume_video"
    assert instant.parse("продолжи уроки английского").args == {"query": "английского"}
    assert instant.parse("запомни здесь мой дом").args == {"name": "дом"}

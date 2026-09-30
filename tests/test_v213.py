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


def _saved_video(uid: int = 1, title: str = "English Lesson 5 — Past Simple", vid: str = "abcdefghijk", why: str | None = "goal") -> dict:
    return lessons.register(uid, key=f"v:{vid}", kind="video", video_id=vid, title=title, channel="EngTeacher", why=why)


def test_lessons_note_find_and_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _saved_video()
    assert lessons.note(1, {"title": "English Lesson 5 — Past Simple", "channel": "EngTeacher", "position_s": 754, "duration_s": 1500, "state": "paused"})
    # 30.09: всё, что он смотрит сам (не присланное ссылкой), не запоминается
    assert lessons.note(1, {"title": "Какой-то клип", "channel": "Music", "position_s": 30, "duration_s": 200, "state": "closed"}) is None
    assert [r["title"] for r in lessons.items(1)] == ["English Lesson 5 — Past Simple"]
    assert lessons.find(1, "english")["title"].startswith("English Lesson 5")
    assert lessons.find(1)["title"].startswith("English Lesson 5")            # без слов — последний
    assert lessons.find(1, "клип") is None

    res = asyncio.run(lessons.resume(1, "english"))
    assert res["video_id"] == "abcdefghijk" and res["start"] == 751 and res["position"] == "12:34"
    assert lessons.url(res) == "https://www.youtube.com/watch?v=abcdefghijk&t=751s"
    assert lessons.playlist_id("https://www.youtube.com/playlist?list=PLabcdefghij") == "PLabcdefghij"
    assert lessons.video_id_of("https://youtu.be/abcdefghijk?t=5") == "abcdefghijk"


def test_lessons_old_auto_collected_rows_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "1.json").write_text('[{"title": "Старый клип", "position": 300, "duration": 900, "at": 1}]', encoding="utf-8")
    assert lessons.items(1) == [] and lessons.find(1) is None
    assert asyncio.run(lessons.resume(1))["error"] == lessons.NOTHING_SAVED


def test_saved_but_not_started_video_opens_from_the_start(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _saved_video(title="Плейлист про Python", vid="pythonvid01", why=None)
    res = asyncio.run(lessons.resume(1, "python"))
    assert res["video_id"] == "pythonvid01" and res["start"] == 0


def test_parse_link_and_find_links():
    assert lessons.parse_link("https://youtu.be/abcdefghijk?t=5") == {"key": "v:abcdefghijk", "kind": "video", "id": "abcdefghijk", "list_id": None}
    assert lessons.parse_link("https://www.youtube.com/playlist?list=PLabcdefghij")["key"] == "p:PLabcdefghij"
    assert lessons.parse_link("https://www.youtube.com/watch?v=abcdefghijk&list=PLabcdefghij")["kind"] == "playlist"   # ролик из плейлиста
    assert lessons.parse_link("https://www.youtube.com/watch?v=abcdefghijk&list=RDabcdefghijk")["kind"] == "video"    # «микс» — не плейлист
    assert lessons.parse_link("https://example.com/watch?v=abcdefghijk") is None
    assert lessons.find_links("смотри https://youtu.be/abcdefghijk, круто") == ["https://youtu.be/abcdefghijk"]
    assert lessons.find_links("обычный текст 12345") == []


def test_save_registers_video_and_playlist_once(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    calls = []

    async def oembed(video_id):  # noqa: ANN001
        calls.append(video_id)
        return {"title": "Урок 1", "channel": "Канал"}

    async def info(list_id):  # noqa: ANN001
        calls.append(list_id)
        return {"title": "Курс английского", "videos": [{"id": "aaaaaaaaaaa", "title": "Урок 1"}, {"id": "bbbbbbbbbbb", "title": "Урок 2"}]}

    monkeypatch.setattr(lessons, "_oembed", oembed)
    monkeypatch.setattr(lessons, "playlist_info", info)
    v = asyncio.run(lessons.save(1, "https://youtu.be/ccccccccccc"))
    p = asyncio.run(lessons.save(1, "https://www.youtube.com/playlist?list=PLabcdefghij", why="goal"))
    asyncio.run(lessons.save(1, "https://youtu.be/ccccccccccc"))       # повторно — страницу заново не открываем
    assert calls == ["ccccccccccc", "PLabcdefghij"]
    assert v["title"] == "Урок 1" and p["title"] == "Курс английского" and len(p["videos"]) == 2 and p["why"] == "goal"
    assert len(lessons.saved(1)) == 2
    # телефон прислал урок из плейлиста — запоминаем, id берём из плейлиста
    item = lessons.note(1, {"title": "Урок 2", "position_s": 100, "duration_s": 900})
    assert item["src"] == "p:PLabcdefghij" and item["id"] == "bbbbbbbbbbb"
    assert lessons.tracked(1, item) is True
    assert lessons.forget(1, "курс")["title"] == "Курс английского" and lessons.items(1) == []


def test_tracked_only_for_goal_or_task(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _saved_video(vid="keepvideo01", title="Просто ролик про что-то интересное", why=None)
    item = lessons.note(1, {"title": "Просто ролик про что-то интересное", "position_s": 200, "duration_s": 900})
    assert item is not None and lessons.tracked(1, item) is False       # советов «продолжим?» про него не будет
    lessons.set_why(1, "v:keepvideo01", "task")
    assert lessons.tracked(1, item) is True


def test_adopt_daily_links_registers_playlist_of_habit(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    async def info(list_id):  # noqa: ANN001
        return {"title": "Курс", "videos": [{"id": "aaaaaaaaaaa", "title": "Урок 1"}]}

    monkeypatch.setattr(lessons, "playlist_info", info)
    daily_tasks.add(1, "Урок английского", at="20:00", link="https://www.youtube.com/playlist?list=PLabcdefghij", total=40)
    asyncio.run(lessons.adopt_daily_links(1))
    assert [(s["key"], s["why"]) for s in lessons.saved(1)] == [("p:PLabcdefghij", "goal")]
    assert lessons.note(1, {"title": "Урок 1", "position_s": 10, "duration_s": 600}) is not None


def test_lessons_playlist_next_after_finished(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    lessons.register(1, key="p:PLxxxxxxxxxx", kind="playlist", list_id="PLxxxxxxxxxx", title="Курс",
                     videos=[{"id": "aaaaaaaaaaa", "title": "Урок 1"}, {"id": "bbbbbbbbbbb", "title": "Урок 2"}])
    assert lessons.note(1, {"title": "Урок 1", "position_s": 590, "duration_s": 600})  # досмотрел

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

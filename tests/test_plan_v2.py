"""30.09: план цитатами, пункты с галочками, правка ответом, закрепление, суры с транслитерацией, умный перенос, планёрка."""
import asyncio
import re
from datetime import date

from bot import carry, plan, quran
from bot.persona import Persona
from bot.profile import Profile

TIMES = {"Fajr": "04:52", "Sunrise": "06:22", "Dhuhr": "12:22", "Asr": "15:43", "Maghrib": "18:17", "Isha": "19:41"}


def _profile(uid: int = 1) -> Profile:
    return Profile(telegram_id=uid, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Тест", username="t")


class FakeMsg:
    message_id = 501


class FakeBot:
    def __init__(self):
        self.sent, self.edited, self.pinned, self.unpinned = [], [], [], []
        self.n = 500

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None, link_preview_options=None, **k):  # noqa: ANN001, ANN003, ANN202
        self.n += 1
        self.sent.append({"text": text, "kb": reply_markup, "parse_mode": parse_mode, "id": self.n})
        m = FakeMsg()
        m.message_id = self.n
        return m

    async def edit_message_text(self, text, chat_id=None, message_id=None, reply_markup=None, parse_mode=None, **k):  # noqa: ANN001, ANN003, ANN202
        self.edited.append({"text": text, "id": message_id, "kb": reply_markup, "parse_mode": parse_mode})

    async def pin_chat_message(self, chat_id, message_id, disable_notification=None):  # noqa: ANN001, ANN202
        self.pinned.append(message_id)

    async def unpin_chat_message(self, chat_id, message_id=None):  # noqa: ANN001, ANN202
        self.unpinned.append(message_id)


def _setup(monkeypatch, tmp_path, *, raw, open_tasks=None):  # noqa: ANN001, ANN202
    from bot import services

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    tasks = list(open_tasks or [])
    added, updated = [], []

    async def gather(profile, *, day, place=None):  # noqa: ANN001, ANN202
        return {"text": "ДАННЫЕ", "pray": {"times": TIMES}, "wins": plan.windows(profile, TIMES), "open": tasks, "habits": [], "goals": [], "day": day}

    async def persona(uid):  # noqa: ANN001, ANN202
        return Persona(lang="ru")

    async def ask(prompt, **k):  # noqa: ANN001, ANN003, ANN202
        return raw() if callable(raw) else raw

    async def add_task(uid, **k):  # noqa: ANN001, ANN003, ANN202
        row = {"id": 100 + len(added), **k}
        added.append(row)
        return row

    async def update_task(uid, task_id, fields):  # noqa: ANN001, ANN202
        updated.append((task_id, fields))

    async def list_tasks(uid, include_done=False):  # noqa: ANN001, ANN202
        return tasks + [{"id": r["id"], "text": r["text"], "done": True} for r in added if False]

    monkeypatch.setattr(plan, "gather", gather)
    monkeypatch.setattr(plan, "_ask_json", ask)
    monkeypatch.setattr(services, "persona", persona)
    monkeypatch.setattr(plan.db, "add_task", add_task)
    monkeypatch.setattr(plan.db, "update_task", update_task)
    monkeypatch.setattr(plan.db, "list_tasks", list_tasks)
    monkeypatch.setattr(plan.db, "available", lambda name: True)
    monkeypatch.setattr(services, "invalidate", lambda *a, **k: None)
    from bot import carry as carry_mod

    async def rollover(bot, profile):  # noqa: ANN001, ANN202
        return 0, 0

    monkeypatch.setattr(carry_mod, "rollover", rollover)
    return added, updated


RAW = {"intro": "Доброе утро, сэр!", "food": "Осталось 1800 ккал — плов на обед.", "money": "Завтра платёж Uzum, тратьте аккуратно.",
       "closing": "Пусть день будет с баракой.",
       "items": [{"text": "Позвонить Алишеру", "kind": "main", "window": "dhuhr", "ref": {"type": "task", "id": "7"}},
                 {"text": "Написать должникам", "kind": "main", "window": "morning", "ref": None},
                 {"text": "Английский", "kind": "task", "window": "asr", "ref": None},
                 {"text": "Прогулка", "kind": "opt", "window": "isha", "ref": None}],
       "surahs": [{"surah": 112, "from": 1, "to": 0}]}


def test_plan_is_rendered_with_quotes_windows_and_checkboxes(monkeypatch, tmp_path):
    added, _ = _setup(monkeypatch, tmp_path, raw=RAW, open_tasks=[{"id": 7, "text": "Позвонить Алишеру", "due_date": "2026-09-30"}])
    bot = FakeBot()
    monkeypatch.setattr(plan.services, "user_settings", lambda uid: asyncio.sleep(0, result={}))

    async def run():  # noqa: ANN202
        async def no_quran(*a, **k):  # noqa: ANN002, ANN003, ANN202
            return 0

        monkeypatch.setattr(plan, "send_quran", no_quran)
        return await plan.send(bot, _profile(), "morning")

    assert asyncio.run(run())
    msg = bot.sent[0]
    text = msg["text"]
    assert msg["parse_mode"] == "HTML"                                   # разметка задана явно — не зависит от настроек бота
    assert text.startswith("🗓 <b>План · ") and "<i>Доброе утро, сэр!</i>" in text
    assert "☀️ <b>До пешина</b> · 06:22–12:22\n<blockquote>☐ ⭐ Написать должникам</blockquote>" in text
    assert "🕛 <b>Между пешином и асром</b> · 12:22–15:43\n<blockquote>☐ ⭐ Позвонить Алишеру</blockquote>" in text
    assert "✨ <b>Если успеете</b>\n<blockquote>☐ Прогулка</blockquote>" in text
    assert "🍽 <b>Еда</b>" in text and "💰 <b>Деньги</b>" in text and "📊 Выполнено: <b>0</b> из 4" in text
    assert "<i>Ответьте на этот план" in text
    assert bot.pinned == [msg["id"]]                                      # закреплён
    # «Главное» без задачи стало задачей само; существующая — привязана, не задублирована
    assert [r["text"] for r in added] == ["Написать должникам"]
    st = plan.load(1)
    assert st["msg_id"] == msg["id"] and {it["text"]: (it["ref"] or {}).get("id") for it in st["items"]}["Позвонить Алишеру"] == "7"
    assert next(it for it in st["items"] if it["text"] == "Написать должникам")["auto"] is True
    # кнопки: пункты с галочками + изменить/заново/на завтра/сура
    data = [b.callback_data for row in msg["kb"].inline_keyboard for b in row]
    assert {"pl:d:0", "pl:d:1", "pl:d:2", "pl:d:3"} <= set(data)
    assert {"pl:e", "pl:r", "pl:t", "pl:q"} <= set(data)


def test_checkbox_marks_task_and_message_is_edited_in_place(monkeypatch, tmp_path):
    _, updated = _setup(monkeypatch, tmp_path, raw=RAW, open_tasks=[{"id": 7, "text": "Позвонить Алишеру", "due_date": "2026-09-30"}])
    bot = FakeBot()

    async def run():  # noqa: ANN202
        async def no_quran(*a, **k):  # noqa: ANN002, ANN003, ANN202
            return 0

        monkeypatch.setattr(plan, "send_quran", no_quran)
        await plan.send(bot, _profile(), "morning")
        st = plan.load(1)
        idx = next(i for i, it in enumerate(st["items"]) if it["text"] == "Позвонить Алишеру")
        return await plan.toggle(bot, _profile(), idx)

    assert asyncio.run(run()) == "Позвонить Алишеру"
    assert updated and updated[-1][0] == "7" and updated[-1][1]["done"] is True            # задача отмечена
    edited = bot.edited[-1]["text"]
    assert edited.count("✅") >= 1 and "<s>Позвонить Алишеру</s>" in edited and "Выполнено: <b>1</b> из 4" in edited
    assert bot.edited[-1]["id"] == bot.sent[0]["id"]                                         # то же сообщение, не новое


def test_reply_edit_keeps_done_and_removes_auto_task(monkeypatch, tmp_path):
    added, _ = _setup(monkeypatch, tmp_path, raw=RAW, open_tasks=[{"id": 7, "text": "Позвонить Алишеру", "due_date": "2026-09-30"}])
    bot = FakeBot()
    deleted: list = []

    async def delete_tasks(uid, ids):  # noqa: ANN001, ANN202
        deleted.extend(ids)

    monkeypatch.setattr(plan.db, "delete_tasks", delete_tasks)
    state = {"raw": RAW}

    async def run():  # noqa: ANN202
        async def no_quran(*a, **k):  # noqa: ANN002, ANN003, ANN202
            return 0

        monkeypatch.setattr(plan, "send_quran", no_quran)
        await plan.send(bot, _profile(), "morning")
        st = plan.load(1)
        keep = [it for it in st["items"] if it["text"] != "Написать должникам"]        # он просит убрать «должников»
        state["raw"] = {**RAW, "items": [{"id": it["id"], "text": it["text"], "kind": it["kind"], "window": it["window"], "ref": it["ref"], "done": it["done"]}
                                        for it in keep] + [{"text": "Оплатить интернет", "kind": "task", "window": "asr", "ref": None}]}
        return await plan.edit(bot, _profile(), "убери должников, добавь оплату интернета после асра")

    monkeypatch.setattr(plan, "_ask_json", lambda prompt, **k: asyncio.sleep(0, result=state["raw"]))
    new = asyncio.run(run())
    assert [it["text"] for it in new["items"]] == ["Позвонить Алишеру", "Английский", "Прогулка", "Оплатить интернет"]
    assert deleted == ["100"] or [str(x) for x in deleted] == ["100"]                # задача, созданная планом, удалена вместе с пунктом
    assert bot.edited and "Оплатить интернет" in bot.edited[-1]["text"]              # сообщение обновлено на месте
    assert len(bot.sent) == 1 and plan.is_plan_message(1, bot.sent[0]["id"])


def test_mark_done_by_words(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, raw=RAW, open_tasks=[{"id": 7, "text": "Позвонить Алишеру", "due_date": "2026-09-30"}])
    bot = FakeBot()

    async def run():  # noqa: ANN202
        async def no_quran(*a, **k):  # noqa: ANN002, ANN003, ANN202
            return 0

        monkeypatch.setattr(plan, "send_quran", no_quran)
        await plan.send(bot, _profile(), "morning")
        return (await plan.mark_done(bot, _profile(), "сделал, позвонил Алишеру"), await plan.mark_done(bot, _profile(), "полетел на Марс"))

    ok, missing = asyncio.run(run())
    assert ok and ok["text"] == "Позвонить Алишеру" and ok["done"] and ok["left"] == 3 and missing is None


def test_model_down_still_gives_a_plan(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, raw=lambda: None, open_tasks=[{"id": 7, "text": "Позвонить Алишеру", "due_date": "2026-09-30"}])
    bot = FakeBot()
    monkeypatch.setattr(plan, "_ask_json", lambda prompt, **k: asyncio.sleep(0, result=None))
    assert asyncio.run(plan.send(bot, _profile(), "morning"))
    assert "Позвонить Алишеру" in bot.sent[0]["text"]


# ------------------------------------------------------------------ суры
def test_surah_text_comes_from_quran_api_not_from_model(monkeypatch, tmp_path):
    import httpx

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    body = {"data": [
        {"edition": {"identifier": "quran-uthmani"}, "englishName": "Al-Ikhlaas", "name": "سُورَةُ الإِخۡلَاصِ", "ayahs": [
            {"numberInSurah": 1, "text": "﻿بِسْمِ ٱللَّهِ ٱلرَّحْمَٰنِ ٱلرَّحِيمِ قُلْ هُوَ ٱللَّهُ أَحَدٌ"}, {"numberInSurah": 2, "text": "ٱللَّهُ ٱلصَّمَدُ"}]},
        {"edition": {"identifier": "en.transliteration"}, "ayahs": [{"numberInSurah": 1, "text": "Qul huwal laahu ahad"}, {"numberInSurah": 2, "text": "Allah hus-samad"}]}]}
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=body)

    real = httpx.AsyncClient
    monkeypatch.setattr(quran.httpx, "AsyncClient", lambda **k: real(transport=httpx.MockTransport(handler), **k))
    msgs = asyncio.run(quran.messages(112, 1, 2))
    assert len(msgs) == 1 and "Al-Ikhlaas" in msgs[0] and "сура 112" in msgs[0] and "аяты 1–2" in msgs[0]
    assert "قُلْ هُوَ ٱللَّهُ أَحَدٌ ﴿1﴾" in msgs[0] and "بِسْمِ" not in msgs[0]                # басмала не вклеена в первый аят
    assert "1. Qul huwal laahu ahad" in msgs[0] and "<blockquote>" in msgs[0] and "Латиницей" in msgs[0]
    asyncio.run(quran.messages(112))
    assert len(calls) == 1                                                                   # второй раз — с диска
    assert quran.wants("выучить суру Ихлас") and quran.wants("аяты Бакары") and not quran.wants("купить хлеб")


def test_plan_sends_quran_when_plan_mentions_it(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, raw=RAW)
    bot = FakeBot()

    async def messages(n, a=None, b=None, lang="ru"):  # noqa: ANN001, ANN202
        return [f"📖 сура {n}"]

    monkeypatch.setattr(quran, "messages", messages)
    asyncio.run(plan.send(bot, _profile(), "morning"))
    assert [m["text"] for m in bot.sent][-1] == "📖 сура 112" and plan.load(1)["quran_sent"] == plan.load(1)["day"]


# ------------------------------------------------------------------ умный перенос
def test_carry_counts_once_a_day_and_asks_on_third(monkeypatch, tmp_path):
    from bot import services

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    updated: list = []
    rows = [{"id": 5, "text": "Купить лампочки", "due_date": "2026-09-27"}]

    async def update_task(uid, task_id, fields):  # noqa: ANN001, ANN202
        updated.append((task_id, fields))

    async def list_tasks(uid, include_done=False):  # noqa: ANN001, ANN202
        return rows

    monkeypatch.setattr(carry.db, "update_task", update_task)
    monkeypatch.setattr(carry.db, "list_tasks", list_tasks)
    monkeypatch.setattr(carry.db, "available", lambda name: True)
    monkeypatch.setattr(services, "invalidate", lambda *a, **k: None)
    bot = FakeBot()
    p = _profile()
    monkeypatch.setattr(Profile, "today", property(lambda self: date(2026, 9, 30)))
    assert asyncio.run(carry.rollover(bot, p)) == (1, 0) and carry.count(1, 5) == 1 and updated[-1][1]["due_date"] == "2026-09-30"
    assert asyncio.run(carry.rollover(bot, p)) == (1, 0) and carry.count(1, 5) == 1          # в тот же день счёт не растёт
    monkeypatch.setattr(Profile, "today", property(lambda self: date(2026, 10, 1)))
    assert asyncio.run(carry.rollover(bot, p)) == (1, 0) and carry.count(1, 5) == 2
    monkeypatch.setattr(Profile, "today", property(lambda self: date(2026, 10, 2)))
    n_updates = len(updated)
    assert asyncio.run(carry.rollover(bot, p)) == (0, 1) and carry.count(1, 5) == 3           # третий раз — не двигаем, а спрашиваем
    assert len(updated) == n_updates
    ask = bot.sent[-1]
    assert "уже 3 раз" in ask["text"] and "Купить лампочки" in ask["text"] and ask["parse_mode"] == "HTML"
    assert [b.callback_data for row in ask["kb"].inline_keyboard for b in row] == ["carry:s:5", "carry:w:5", "carry:d:5"]


# ------------------------------------------------------------------ вечерний разбор и планёрка
def test_review_is_built_by_code_with_quotes(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    text = plan.render_review(_profile(), ["План: выполнено 3 из 5", "Еда: 1450 из 2600 ккал"],
                              {"done": ["Позвонили Алишеру", "Урок ПДД"], "left": ["Оплата интернета"], "tip": "Начните с оплаты", "closing": "Спокойной ночи"},
                              date(2026, 9, 30))
    assert text.startswith("🌙 <b>Разбор дня · среда, 30 сентября</b>")
    assert "📊 <b>Цифры</b>\n<blockquote>План: выполнено 3 из 5\nЕда: 1450 из 2600 ккал</blockquote>" in text
    assert "✅ <b>Получилось</b>\n<blockquote>• Позвонили Алишеру\n• Урок ПДД</blockquote>" in text
    assert "➡️ <b>Не успели</b>" in text and "💡 <b>На завтра</b>" in text and "<i>Спокойной ночи</i>" in text
    assert not re.search(r"&lt;|\*\*", text)


def test_week_plan_saved_and_used_by_morning_plan(monkeypatch, tmp_path):
    from bot import services

    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    async def persona(uid):  # noqa: ANN001, ANN202
        return Persona(lang="ru")

    async def ask(prompt, **k):  # noqa: ANN001, ANN003, ANN202
        return {"intro": "Новая неделя", "focus": "Долги и английский", "closing": "Барака",
                "goals": [{"text": "Вернуть долги", "days": ["пн", "ср"]}, {"text": "Английский 5 раз", "days": ["вт", "чт"]}, {"text": "Копить", "days": ["пт"]}]}

    async def tasks(uid):  # noqa: ANN001, ANN202
        return []

    async def snap(profile):  # noqa: ANN001, ANN202
        return "срез"

    from bot import agent_tools, weekly

    async def wfacts(profile):  # noqa: ANN001, ANN202
        return {"start": date(2026, 9, 21), "end": date(2026, 9, 27)}

    monkeypatch.setattr(services, "persona", persona)
    monkeypatch.setattr(services, "tasks", tasks)
    monkeypatch.setattr(agent_tools, "snapshot", snap)
    monkeypatch.setattr(weekly, "facts", wfacts)
    monkeypatch.setattr(weekly, "text", lambda profile, f: "итоги")
    monkeypatch.setattr(plan, "_ask_json", ask)
    monkeypatch.setattr(Profile, "today", property(lambda self: date(2026, 10, 4)))            # воскресенье
    bot = FakeBot()
    assert asyncio.run(plan.send(bot, _profile(), "week"))
    text = bot.sent[0]["text"]
    assert "🎯 <b>Цели недели</b>\n<blockquote>1. Вернуть долги · <i>пн, ср</i>" in text and "🔦 <b>Фокус</b>" in text
    assert plan._week_line(1, date(2026, 10, 6)).startswith("ЦЕЛИ НЕДЕЛИ") and not plan._week_line(1, date(2026, 10, 13))

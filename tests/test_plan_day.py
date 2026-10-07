"""30.09: план дня, выгрузка дел голосом, мечети/намаз в другом городе, советы днём, умная модель, без «босса»."""
import asyncio
from datetime import date, datetime, timedelta, timezone

from bot import advice, capture, prayer
from bot import ai as ai_mod
from bot.persona import Persona
from bot.profile import Profile


def _profile(uid: int = 1) -> Profile:
    return Profile(telegram_id=uid, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Тест", username="t")


# ------------------------------------------------------------------ без «босса»
def test_no_boss_anywhere_in_addressing():
    from bot import live_call, persona, phone_live

    p = Persona(lang="ru", honorific="mix")
    assert "«Босс» — никогда" in persona.honorific_rule(p) or "никогда" in persona.honorific_rule(p)
    assert all("босс" not in t.lower() for t in phone_live.greeting_texts("ru", "mix"))
    assert all("босс" not in t.lower() for t, _ in phone_live.greeting_items(Persona(lang="ru", honorific="mix", mirror=True)))
    assert "boss" not in " ".join(t.lower() for t, _ in phone_live.greeting_items(Persona(lang="ru", honorific="mix", mirror=True)))
    assert "sir" in phone_live._HON["mix"][0]
    wake = live_call.system_instruction(_profile(), p, mode="wake", wake={"takbir": "05:00"})
    assert "«Доброе утро, Сэр!»" in wake


# ------------------------------------------------------------------ выгрузка дел голосом
def test_capture_recognizes_task_dumps_but_not_questions_or_commands():
    dump = ("Надо позвонить Алишеру завтра, купить лампочки, хочу накопить на ноутбук к январю и каждый день заниматься английским "
            "по часу, ещё не забыть оплатить интернет")
    assert capture.eligible(dump)
    assert not capture.eligible("Сколько я потратил на еду за этот месяц и что надо купить?")     # вопрос
    assert not capture.eligible("позвони маме")                                                   # короткая команда
    assert capture.eligible("Сейчас надиктую дела на неделю")                                     # прямая просьба
    text = capture.combine(["первое", "второе"])
    assert text.startswith("[ВЫГРУЗКА ДЕЛ") and "day_plan" in text and "(1) первое\n(2) второе" in text and "«Отменить»" in text


def test_capture_batches_several_voice_messages_into_one_agent_turn(monkeypatch):
    from bot.handlers import agent, common

    calls: list = []

    async def handle(message, state, profile, text, voice=False, **k):  # noqa: ANN001, ANN003, ANN202
        calls.append((message, text, voice))
        return True

    async def progress(message, text):  # noqa: ANN001, ANN202
        return None

    monkeypatch.setattr(agent, "handle_command", handle)
    monkeypatch.setattr(common, "show_progress", progress)
    monkeypatch.setattr(capture, "BATCH_WAIT_S", 0.05)

    async def run():  # noqa: ANN202
        p = _profile(7)
        await capture.push("m1", None, p, "надо позвонить Алишеру и купить хлеб")
        assert capture.open_for(7)
        await capture.push("m2", None, p, "и ещё выучить двадцать слов каждый день")
        await asyncio.sleep(0.2)

    asyncio.run(run())
    assert len(calls) == 1 and calls[0][0] == "m2" and calls[0][2] is True
    assert "(1) надо позвонить" in calls[0][1] and "(2) и ещё выучить" in calls[0][1] and not capture.open_for(7)


# ------------------------------------------------------------------ умная модель
def test_smart_model_is_used_for_owner_chat_when_free_key_is_off(monkeypatch):
    from bot import access, gcloud
    from bot.handlers import agent

    seen: list = []

    async def fake_run(profile, text, history, **kw):  # noqa: ANN001, ANN003, ANN202
        seen.append(ai_mod._smart_turn.get())
        return "ok"

    monkeypatch.setattr(agent, "_run_agent", fake_run)
    monkeypatch.setattr(access, "is_owner", lambda uid: True)
    monkeypatch.setattr(gcloud, "active", lambda: True)              # кредит Vertex: бесплатный ключ не нужен
    asyncio.run(agent.run_agent(_profile(), "привет", [], snapshot=""))
    asyncio.run(agent.run_agent(_profile(), "привет", [], snapshot="", free=False))   # голос — остаётся быстрая
    assert seen == [ai_mod.smart_model(), None] and ai_mod._smart_turn.get() is None


# ------------------------------------------------------------------ план: см. tests/test_plan_v2.py


def test_mosque_search_dedups_and_sorts(monkeypatch):
    import httpx

    body = {"elements": [
        {"type": "node", "lat": 40.7830, "lon": 72.3450, "tags": {"name": "Gʻishtli jome' masjidi"}},
        {"type": "way", "center": {"lat": 40.7830, "lon": 72.3450}, "tags": {"name": "Gʻishtli jome' masjidi"}},   # тот же — контуром
        {"type": "node", "lat": 40.7900, "lon": 72.3500, "tags": {}},
        {"type": "node", "lat": 40.7825, "lon": 72.3446, "tags": {"name:ru": "Ближняя мечеть", "name": "Yaqin"}}]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    real = httpx.AsyncClient
    monkeypatch.setattr(prayer.httpx, "AsyncClient", lambda **k: real(transport=httpx.MockTransport(handler), **k))
    prayer._mosque_cache.clear()
    items = asyncio.run(prayer.mosques_near(40.7821, 72.3442))
    assert [m["name"] for m in items] == ["Ближняя мечеть", "Gʻishtli jome' masjidi", "Мечеть"]
    assert items[0]["distance_m"] < items[1]["distance_m"] < items[2]["distance_m"]


# ------------------------------------------------------------------ советы днём
def test_advice_rules_limit_and_gap(monkeypatch, tmp_path):
    from bot import services, wake_runner

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    now = datetime(2026, 9, 30, 15, 45, tzinfo=timezone(timedelta(hours=5)))
    profile = _profile()
    monkeypatch.setattr(Profile, "now", property(lambda self: now))
    monkeypatch.setattr(Profile, "today", property(lambda self: now.date()))

    async def plan_for(profile, day=None):  # noqa: ANN001, ANN202
        class S:
            latitude, longitude, calc_method = 40.78, 72.34, 3
        return S(), None

    async def timings(day, **k):  # noqa: ANN001, ANN003, ANN202
        return {"Fajr": "04:52", "Sunrise": "06:22", "Dhuhr": "12:22", "Asr": "16:10", "Maghrib": "18:17", "Isha": "19:41"}

    async def tasks(uid):  # noqa: ANN001, ANN202
        return [{"text": "позвонить Алишеру", "due_date": "2026-09-30", "due_time": None}]

    async def nprofile(uid):  # noqa: ANN001, ANN202
        return {"daily_calories": 2000}

    async def logs(profile):  # noqa: ANN001, ANN202
        return [{"calories": 300, "protein": 10, "fat": 5, "carbs": 40}]

    monkeypatch.setattr(wake_runner, "plan_for", plan_for)
    monkeypatch.setattr(prayer, "timings", timings)
    monkeypatch.setattr(services, "tasks", tasks)
    monkeypatch.setattr(services, "nutrition_profile", nprofile)
    monkeypatch.setattr(services, "today_calorie_logs", logs)
    tips = asyncio.run(advice.candidates(profile))
    keys = [t.key for t in tips]
    assert "prayer:16:10" in keys and "food:low" in keys                       # до асра 25 мин + съедено 300 из 2000
    assert "успеете: позвонить Алишеру" in tips[0].fallback

    sent: list = []

    class FakeBot:
        async def send_message(self, uid, text, reply_markup=None, parse_mode=None):  # noqa: ANN001, ANN202
            sent.append(text)

    async def dead(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("нет модели")

    async def persona(uid):  # noqa: ANN001, ANN202
        return Persona(lang="ru")

    monkeypatch.setattr(services, "persona", persona)
    monkeypatch.setattr(advice.ai, "generate", dead)
    assert asyncio.run(advice.maybe_send(FakeBot(), profile)) is True and sent[0].startswith("💡 До намаза")
    assert asyncio.run(advice.maybe_send(FakeBot(), profile)) is False          # минимум 2.5 часа между советами
    st = advice._state(1, "2026-09-30")
    st["last_at"] = 0
    advice._save(1, st)
    assert asyncio.run(advice.maybe_send(FakeBot(), profile)) is False          # 07.10: в день — один совет (он удалял их, не читая)
    for _ in range(3):
        st = advice._state(1, "2026-09-30")
        st["last_at"] = 0
        advice._save(1, st)
        asyncio.run(advice.maybe_send(FakeBot(), profile))
    assert len(advice._state(1, "2026-09-30")["keys"]) <= advice.MAX_PER_DAY == 1  # не больше одного в день
    night = datetime(2026, 9, 30, 23, 0, tzinfo=timezone(timedelta(hours=5)))
    monkeypatch.setattr(Profile, "now", property(lambda self: night))
    assert asyncio.run(advice.maybe_send(FakeBot(), profile)) is False           # ночью не тревожим


def test_tools_registered_for_voice_and_chat():
    from bot import agent_tools, live_call

    for name in ("day_plan", "mosques_near", "brainstorm", "prayer_times_city", "where_am_i", "recall_deeds"):
        assert name in agent_tools.TOOLS, name
    # голос (телефон и звонок) достаёт их через bot_task — общий реестр без пропуска; в Live-набор не добавляем (токены)
    phone_names = {d["name"] for d in live_call.tool_declarations("phone", full=True)}
    assert "bot_task" in phone_names and not {"day_plan", "brainstorm", "mosques_near"} & live_call._DELEGATE_SKIP
    assert "where_am_i" in phone_names
    _ = date  # noqa

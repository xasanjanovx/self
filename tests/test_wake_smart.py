"""27.09: будильник (звонок не обрывается, пока звук соединяется; «проснулся» — только ответом голосом), «Джес» умнее
(его голос + кривое прочтение имени, обучение), команды сразу из проверки имени (без Live), бесплатный ключ Gemini."""
import asyncio
import json

import httpx

from bot import ai as ai_mod
from bot import caller, phone, phone_api, wakeword
from bot.profile import Profile


def _profile(uid: int = 1) -> Profile:
    return Profile(telegram_id=uid, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Тест", username="t")


# ------------------------------------------------------------------ «Джес» в кривом прочтении, но голос точно его
def test_lenient_name_when_voice_is_his(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    ok = lambda text, confident=False: wakeword.lenient(text, strong=True, confident=confident, uid=5)  # noqa: E731
    assert ok("джой включи фонарик")[:2] == (True, "включи фонарик")
    assert ok("джесси открой камера")[:2] == (True, "открой камера")
    assert ok("дж позвони мам")[:2] == (True, "позвони мам")
    # имя обрезалось — сразу команда, но только если и телефон уверен, что слышал «Джес» (28.09: видео)
    assert ok("позвони маме", confident=True)[:2] == (True, "позвони маме")
    assert ok("сейчас позвони маме", confident=True)[:2] == (True, "позвони маме")
    assert not ok("позвони маме")[0]
    assert not ok("с")[0] and not ok("эс")[0]                          # 28.09: одиночное «с» — звуки из видео, не имя
    assert not ok("прогноз")[0] and not ok("вот так")[0]
    assert not ok("какая погода")[0] and ok("какая погода", confident=True)[0]
    # голос не «точно его» — ничего мягкого (28.09: иначе будили звуки из видео)
    assert not wakeword.lenient("дж позвони мам", strong=False, confident=True, uid=5)[0]
    assert not wakeword.lenient("с", strong=False, confident=True, uid=5)[0]


def test_learns_how_recognizer_hears_his_name(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    wakeword.note_reject(7, "бжес включи фонарик", strong=True)  # отказ: «бжес» не похоже на имя
    wakeword.note_accept(7, "включи фонарик")                    # повторил — прошло
    assert "бжес" in wakeword.variants(7)
    assert wakeword.lenient("бжес какая погода", strong=True, confident=False, uid=7)[:2] == (True, "какая погода")
    # отказ чужим голосом и «сейчас» — не учимся
    wakeword.note_reject(8, "сейчас включи фонарик", strong=True)
    wakeword.note_accept(8, "включи фонарик")
    wakeword.note_reject(9, "ржес включи фонарик", strong=False)
    wakeword.note_accept(9, "включи фонарик")
    assert wakeword.variants(8) == set() and wakeword.variants(9) == set()


# ------------------------------------------------------------------ звонок: «не в звонке» сразу после ответа ≠ положил трубку
class NotInCall(Exception):
    pass


def test_call_is_not_dropped_while_audio_connects(monkeypatch):
    import sys
    import types

    fake = types.ModuleType("pytgcalls")
    fake_types = types.ModuleType("pytgcalls.types")
    fake_types.Device = types.SimpleNamespace(MICROPHONE=1)
    monkeypatch.setitem(sys.modules, "pytgcalls", fake)
    monkeypatch.setitem(sys.modules, "pytgcalls.types", fake_types)

    class Calls:
        async def send_frame(self, *a):  # noqa: ANN002
            raise NotInCall()

    monkeypatch.setattr(caller, "_calls", Calls())
    ended = asyncio.Event()
    monkeypatch.setitem(caller._ended, 3, ended)
    monkeypatch.setitem(caller._answered_at, 3, caller.time.monotonic())
    assert asyncio.run(caller.send_audio(3, b"\0" * 480)) is None and not ended.is_set()   # соединяется — ждём
    caller._answered_at[3] = caller.time.monotonic() - caller.CONNECT_GRACE - 1
    assert asyncio.run(caller.send_audio(3, b"\0" * 480)) is False and ended.is_set()      # давно — правда положил


# ------------------------------------------------------------------ команда сразу из проверки имени — без Gemini Live
def test_instant_command_from_wake_check(monkeypatch):
    from bot import agent_tools_extra, phone_live, services
    from bot.handlers import common

    async def prof(uid):  # noqa: ANN001
        return _profile(uid)

    async def nothing(*a, **k):  # noqa: ANN002, ANN003
        return None

    monkeypatch.setattr(common, "profile_by_id", prof)
    monkeypatch.setattr(agent_tools_extra, "remember_exchange", nothing)
    monkeypatch.setattr(services, "log_agent", nothing)
    discarded: list = []
    monkeypatch.setattr(phone_live, "discard", lambda uid: discarded.append(uid))

    async def run():  # noqa: ANN202
        done = await phone_api._instant_command(1, "джес включи фонарик", "включи фонарик", {})
        none = await phone_api._instant_command(1, "джес какая погода", "какая погода", {})
        locked = await phone_api._instant_command(1, "джес открой ютуб", "открой ютуб", {"locked": True})
        await asyncio.sleep(0)
        return done, none, locked

    done, none, locked = asyncio.run(run())
    assert done["done"] and done["tool"] == "flashlight" and done["actions"] == [{"type": "flashlight", "on": True}]
    assert none is None and locked is None  # вопрос — Gemini; заблокирован и нужно открыть приложение — Gemini
    assert discarded == [1]


# ------------------------------------------------------------------ бесплатный ключ: сначала он, лимит — платный
def test_free_key_first_then_paid(monkeypatch):
    from bot import billing
    from bot.context import ai

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("x-goog-api-key")
        seen.append(key)
        if key == "FREE":
            return httpx.Response(429, json={"error": {"message": "quota"}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "{}"}]}}], "usageMetadata": {}})

    monkeypatch.setattr(ai_mod, "FREE_API_KEY", "FREE")
    monkeypatch.setattr(ai_mod, "_free_paused_until", 0.0)
    monkeypatch.setattr(billing, "record", lambda *a, **k: 0.0)
    monkeypatch.setattr(ai, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"x-goog-api-key": "PAID"}))

    async def run():  # noqa: ANN202
        await ai._post("m", {"contents": []})             # без «бесплатного режима» — сразу платный
        token = ai_mod.use_free()
        try:
            await ai._post("m", {"contents": []})         # бесплатный → 429 → платный
            await ai._post("m", {"contents": []})         # час бесплатный на паузе — сразу платный
        finally:
            ai_mod.reset_free(token)

    asyncio.run(run())
    assert seen == ["PAID", "FREE", "PAID", "PAID"]


# ------------------------------------------------------------------ утро: «Проснулся» — только после ответа голосом
def test_morning_awake_only_after_he_answers(monkeypatch):
    from bot import persona as persona_mod
    from bot import phone_live, wake_runner
    from bot import context

    marked: list = []

    async def mark(bot, profile, *, source, notify=True):  # noqa: ANN001
        marked.append(source)
        return {}

    monkeypatch.setattr(wake_runner, "mark_awake", mark)
    monkeypatch.setattr(context, "bot_instance", lambda: None)

    class Phone:
        closed = False

        def __init__(self) -> None:
            self.sent: list = []

        async def send_str(self, s: str) -> None:
            self.sent.append(json.loads(s))

        async def send_bytes(self, b: bytes) -> None:
            pass

    async def run():  # noqa: ANN202
        sess = phone_live.PhoneLive(_profile(), persona_mod.Persona(), system="", phone_ws=Phone(), device={})
        sess.morning_proof = True
        sess._in_text = ["м"]
        sess._flush_transcript()                           # мычание — не считается
        await asyncio.sleep(0.05)
        assert not marked and sess.morning_proof
        sess._in_text = ["Аят ", "аль-Курси"]
        sess._flush_transcript()                           # ответил на вопрос дня
        await asyncio.sleep(0.05)
        return sess

    sess = asyncio.run(run())
    assert marked == ["app"] and not sess.morning_proof
    assert {"type": "awake"} in sess.phone_ws.sent


def test_free_key_answers_with_smart_model_and_keeps_it_for_the_whole_turn(monkeypatch):
    """28.09: его чат — Flash через бесплатный ключ; лимит кончился посреди хода — доделываем той же моделью (платно),
    следующий ход — сразу платный Flash-Lite, как раньше."""
    from bot import billing
    from bot.context import ai

    seen: list[tuple[str, str]] = []
    free_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("x-goog-api-key")
        model = request.url.path.rsplit("/", 1)[-1].split(":")[0]
        seen.append((key, model))
        if key == "FREE":
            free_calls["n"] += 1
            if free_calls["n"] > 1:
                return httpx.Response(429, json={"error": {"message": "quota"}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "{}"}]}}], "usageMetadata": {}})

    monkeypatch.setattr(ai_mod, "FREE_API_KEY", "FREE")
    monkeypatch.setattr(ai_mod, "_free_paused_until", 0.0)
    monkeypatch.setattr(billing, "record", lambda *a, **k: 0.0)
    monkeypatch.setattr(billing, "record_free", lambda *a, **k: None)
    monkeypatch.setattr(ai, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"x-goog-api-key": "PAID"}))

    async def run():  # noqa: ANN202
        token = ai_mod.use_free("smart")
        try:
            await ai._post("cheap", {"contents": []})   # шаг 1: бесплатно, умной
            await ai._post("cheap", {"contents": []})   # шаг 2: бесплатный лимит → платно, но той же умной
        finally:
            ai_mod.reset_free(token)
        token = ai_mod.use_free("smart")                 # новый ход: бесплатный на паузе — платный дешёвый
        try:
            await ai._post("cheap", {"contents": []})
        finally:
            ai_mod.reset_free(token)

    asyncio.run(run())
    assert seen == [("FREE", "smart"), ("FREE", "smart"), ("PAID", "smart"), ("PAID", "cheap")]


def test_smart_model_missing_on_free_tier_falls_back_to_free_cheap(monkeypatch):
    """Умной модели на бесплатном уровне нет (404) — бесплатно дешёвой, а не сразу платно."""
    from bot import billing
    from bot.context import ai

    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("x-goog-api-key")
        model = request.url.path.rsplit("/", 1)[-1].split(":")[0]
        seen.append((key, model))
        if key == "FREE" and model == "smart":
            return httpx.Response(404, json={"error": {"message": "model not found"}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "{}"}]}}], "usageMetadata": {}})

    monkeypatch.setattr(ai_mod, "FREE_API_KEY", "FREE")
    monkeypatch.setattr(ai_mod, "_free_paused_until", 0.0)
    monkeypatch.setattr(ai_mod, "_free_smart_blocked_until", 0.0)
    monkeypatch.setattr(billing, "record", lambda *a, **k: 0.0)
    monkeypatch.setattr(billing, "record_free", lambda *a, **k: None)
    monkeypatch.setattr(ai, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"x-goog-api-key": "PAID"}))

    async def run():  # noqa: ANN202
        for _ in range(2):
            token = ai_mod.use_free("smart")
            try:
                await ai._post("cheap", {"contents": []})
            finally:
                ai_mod.reset_free(token)

    asyncio.run(run())
    # 1-й ход: умная 404 → бесплатно дешёвая; 2-й ход: умную уже не пробуем
    assert seen == [("FREE", "smart"), ("FREE", "cheap"), ("FREE", "cheap")]


def test_thinking_level_is_adapted_and_learned(monkeypatch):
    """3.8 Flash не принимает «minimal» — шлём «low»; незнакомая модель ответила так же — запоминаем и сразу повторяем."""
    import json as _json

    from bot import billing
    from bot.context import ai

    assert ai_mod._adapt("gemini-3.8-flash", {"generationConfig": {"thinkingConfig": {"thinkingLevel": "minimal"}}}) == \
        {"generationConfig": {"thinkingConfig": {"thinkingLevel": "low"}}}
    assert ai_mod._adapt("gemini-3.5-flash-lite", {"generationConfig": {"thinkingConfig": {"thinkingLevel": "minimal"}}}) == \
        {"generationConfig": {"thinkingConfig": {"thinkingLevel": "minimal"}}}

    levels: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        level = _json.loads(request.content)["generationConfig"]["thinkingConfig"]["thinkingLevel"]
        levels.append(level)
        if level == "minimal":
            return httpx.Response(400, json={"error": {"message": "Thinking level MINIMAL is not supported for this model."}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ок"}]}}], "usageMetadata": {}})

    monkeypatch.setattr(ai_mod, "FREE_API_KEY", "FREE")
    monkeypatch.setattr(ai_mod, "_free_paused_until", 0.0)
    monkeypatch.setattr(ai_mod, "_NO_MINIMAL", set())
    monkeypatch.setattr(billing, "record_free", lambda *a, **k: None)
    monkeypatch.setattr(ai, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"x-goog-api-key": "PAID"}))

    async def run():  # noqa: ANN202
        token = ai_mod.use_free("new-flash")
        try:
            await ai._post("new-flash", {"contents": [], "generationConfig": {"thinkingConfig": {"thinkingLevel": "minimal"}}})
        finally:
            ai_mod.reset_free(token)

    asyncio.run(run())
    assert levels == ["minimal", "low"] and "new-flash" in ai_mod._NO_MINIMAL


def test_tts_goes_free_first_then_paid(monkeypatch):
    from bot import billing
    from bot.context import ai

    seen: list[str] = []
    audio = "data: " + '{"candidates":[{"content":{"parts":[{"inlineData":{"data":"AAAA"}}]}}]}' + "\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("x-goog-api-key")
        seen.append(key)
        if key == "FREE":
            return httpx.Response(429, json={"error": {"message": "quota"}})
        return httpx.Response(200, text=audio, headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(ai_mod, "FREE_API_KEY", "FREE")
    monkeypatch.setattr(ai_mod, "_free_tts_paused_until", 0.0)
    monkeypatch.setattr(billing, "record", lambda *a, **k: 0.0)
    monkeypatch.setattr(billing, "record_free", lambda *a, **k: None)
    monkeypatch.setattr(ai, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"x-goog-api-key": "PAID"}))

    async def run():  # noqa: ANN202
        return [chunk async for chunk in ai.speak_stream("Готово")]

    chunks = asyncio.run(run())
    assert seen == ["FREE", "PAID"] and chunks == [b"\x00\x00\x00"]


def test_free_pause_follows_google_retry_delay():
    minute = ('{"error": {"code": 429, "message": "Quota exceeded ... Please retry in 41.2s.", "details": '
              '[{"violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]}, {"retryDelay": "41s"}]}}')
    day = '{"error": {"code": 429, "details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}'
    assert ai_mod._free_pause(minute) == 42.0
    assert ai_mod._free_pause(day) == ai_mod.FREE_PAUSE_S
    assert ai_mod._free_pause("quota") == 60.0


def test_strong_voice_is_strict_on_single_short_words():
    # одно короткое слово: только сходство голоса — «банк» у звуков из видео был 0.97
    assert not wakeword.strong_voice("с", {"score": 0.43, "bank": 0.969, "z": 3.48})
    assert not wakeword.strong_voice("джесап", {"score": 0.477, "bank": 0.983, "z": 2.21})
    assert wakeword.strong_voice("джой", {"score": 0.69, "bank": 0.5, "z": 1.0})
    # фраза: и по банку, и по сходству
    assert wakeword.strong_voice("позвони маме", {"score": 0.39, "bank": 0.985, "z": 1.87})
    assert wakeword.strong_voice("дж позвони мам", {"score": 0.822, "bank": 0.778, "z": 2.69})
    assert not wakeword.strong_voice("сейчас позвони", {"score": 0.459, "bank": 0.707, "z": 1.08})


def test_learned_variant_must_sound_like_the_name(tmp_path, monkeypatch):
    """28.09: выучилось «не» — и JES просыпался на фразы из видео («а не дома на шапку…»)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "wake_variants.json").write_text(json.dumps({"3": ["не", "бжес"]}), encoding="utf-8")
    assert wakeword.variants(3) == {"бжес"}                        # старое «не» отброшено
    assert not wakeword.lenient("а не дома на шапку", strong=True, confident=False, uid=3)[0]
    assert not wakeword.lenient("не на разрешение", strong=True, confident=True, uid=3)[0]
    assert wakeword.lenient("бжес позвони маме", strong=True, confident=False, uid=3)[:2] == (True, "позвони маме")
    assert not wakeword.lenient("вот бжес позвони", strong=True, confident=False, uid=3)[0]  # выученное — только первым
    for word in ("не", "дома", "там", "шапку"):
        assert not wakeword.name_like(word), word
    for word in ("джесси", "жес", "бжес", "чес", "дес"):
        assert wakeword.name_like(word), word
    wakeword.note_reject(4, "не включи фонарик", strong=True)
    wakeword.note_accept(4, "включи фонарик")
    assert wakeword.variants(4) == set()


def test_media_playing_only_clear_name(tmp_path, monkeypatch):
    """На телефоне играет видео — никаких поблажек: только чётко расслышанное «Джес» (строгое совпадение — до lenient)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert not wakeword.lenient("дж позвони мам", strong=True, confident=True, uid=5, media=True)[0]
    assert not wakeword.lenient("позвони маме", strong=True, confident=True, uid=5, media=True)[0]
    assert wakeword.match("джес позвони маме") == (True, "позвони маме")

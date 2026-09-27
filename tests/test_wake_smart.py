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
    assert ok("позвони маме")[:2] == (True, "позвони маме")            # имя обрезалось — сразу команда
    assert ok("сейчас позвони маме")[:2] == (True, "позвони маме")
    assert ok("с")[0] and ok("эс")[0]                                   # хвост «Джес»
    assert not ok("прогноз")[0] and not ok("вот так")[0]
    assert not ok("какая погода")[0] and ok("какая погода", confident=True)[0]
    # голос не его (или не уверен) — строго, как раньше
    assert not wakeword.lenient("джой включи фонарик", strong=False, confident=True, uid=5)[0]


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

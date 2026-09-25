"""Джарвис 1.6: быстрая проверка «Джарвис» без Gemini, заготовка разговора, разговор без автозакрытия."""
from __future__ import annotations

import asyncio
import base64
import json

import pytest

from bot import live_call
from bot import phone_api
from bot import phone_live
from bot import voiceprint
from bot import wakeword


@pytest.mark.parametrize("text, after", [
    ("джарвис", ""),
    ("эй джарвис", ""),
    ("джервис позвони маме", "позвони маме"),
    ("эйджарвис", ""),
    ("жарвис какая погода завтра", "какая погода завтра"),
    ("джарвиса позови", "позови"),
    ("jarvis open", "open"),
])
def test_name_found_in_recognized_text(text, after):
    assert wakeword.match(text) == (True, after)


@pytest.mark.parametrize("text", [
    "жавахер иди сюда", "джавахир иди сюда", "дарвин был ученым", "жарко сегодня", "первое что нужно сделать",
    "второе что нам нужно", "бугун хаво джуда яхщи", "сервис не работает", "",
])
def test_similar_words_are_not_the_name(text):
    assert wakeword.match(text)[0] is False


class _Req:
    def __init__(self, body: dict) -> None:
        self._body = body

    async def json(self) -> dict:
        return self._body


@pytest.fixture()
def wake(monkeypatch):
    calls = {"prewarm": 0, "discard": 0, "gemini": 0}
    monkeypatch.setattr(phone_api, "owner_id", lambda: 7)
    monkeypatch.setattr(phone_live, "prewarm", lambda uid: calls.__setitem__("prewarm", calls["prewarm"] + 1))
    monkeypatch.setattr(phone_live, "discard", lambda uid: calls.__setitem__("discard", calls["discard"] + 1))

    async def gemini(*a, **k):
        calls["gemini"] += 1
        return json.dumps({"name": True, "text": "джарвис", "after": ""})

    monkeypatch.setattr(phone_api.ai, "generate", gemini)

    def setup(voice_ok=True, heard=None):
        async def verify(uid, audio):
            return {"ok": voice_ok, "score": 0.6 if voice_ok else 0.1, "z": 1.0}

        async def check(audio):
            return heard

        monkeypatch.setattr(voiceprint, "verify", verify)
        monkeypatch.setattr(wakeword, "check", check)
    return calls, setup


def _check(confident=False) -> dict:
    body = {"audio": base64.b64encode(b"RIFF....").decode(), "confident": confident}
    resp = asyncio.run(phone_api.wake_check(_Req(body)))
    return json.loads(resp.text)


def test_wake_check_answers_locally_without_gemini(wake):
    calls, setup = wake
    setup(heard={"text": "джарвис позвони маме", "name": True, "after": "позвони маме", "ms": 40})
    out = _check()
    assert out["ok"] is True and out["command"] is True and out["fast"] is True
    assert calls == {"prewarm": 1, "discard": 0, "gemini": 0}


def test_wake_check_rejects_other_words_and_drops_prewarm(wake):
    calls, setup = wake
    setup(heard={"text": "первое что нужно", "name": False, "after": "", "ms": 30})
    out = _check()
    assert out["ok"] is False
    assert calls["discard"] == 1 and calls["gemini"] == 0


def test_wake_check_foreign_voice_is_rejected_first(wake):
    calls, setup = wake
    setup(voice_ok=False, heard={"text": "джарвис", "name": True, "after": "", "ms": 30})
    out = _check()
    assert out == {"ok": False, "reason": "voice", "score": 0.1}
    assert calls["discard"] == 1


def test_wake_check_asks_gemini_only_when_nothing_heard(wake):
    calls, setup = wake
    setup(heard={"text": "", "name": False, "after": "", "ms": 20})
    assert _check()["ok"] is True
    assert calls["gemini"] == 1
    setup(heard=None)  # распознавателя нет
    _check()
    assert calls["gemini"] == 2


def test_wake_check_confident_detector_and_silence_passes(wake):
    calls, setup = wake
    setup(heard={"text": "", "name": False, "after": "", "ms": 20})
    assert _check(confident=True)["ok"] is True
    assert calls["gemini"] == 0


class _Closable:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _Sess:
    def __init__(self, device) -> None:
        self.turn = type("T", (), {"device": dict(device)})()


def test_prewarm_is_taken_by_the_phone_and_expires_otherwise(monkeypatch):
    built: list[tuple] = []

    async def fake_build(uid, device):
        await asyncio.sleep(0.01)
        item = (_Sess(device), _Closable(), _Closable(), 0.1)
        built.append(item)
        return item

    monkeypatch.setattr(phone_live, "_build", fake_build)
    monkeypatch.setattr(phone_live, "_warm", {})
    monkeypatch.setattr(phone_live, "_last_device", {})
    monkeypatch.setattr(phone_live, "_modes", {})
    monkeypatch.setattr(phone_live.billing, "live_allowed", lambda mode="phone": True)

    async def scenario():
        phone_live.prewarm(5)  # телефон ещё ни разу не подключался — заготовки нет
        assert 5 not in phone_live._warm
        phone_live._last_device[5] = {"duplex": True}
        phone_live._modes[5] = "economy"
        phone_live.prewarm(5)  # экономный режим начинается без Live — заготовка не нужна
        assert 5 not in phone_live._warm
        phone_live._modes[5] = "live"  # по умолчанию — облегчённый Live
        phone_live.prewarm(5)
        taken = await phone_live._take(5, {"duplex": True})
        assert taken == built[0] and not taken[2].closed
        # не «Джарвис» — заготовку закрываем
        phone_live.prewarm(5)
        await asyncio.sleep(0.05)
        phone_live.discard(5)
        await asyncio.sleep(0.01)
        assert built[1][2].closed and built[1][1].closed
        # поменялась настройка эхоподавления — заготовка не подходит
        phone_live.prewarm(5)
        assert await phone_live._take(5, {"duplex": False}) is None
        assert built[2][2].closed

    asyncio.run(scenario())


def test_phone_conversation_stays_open_until_goodbye():
    assert "НЕ закрывается сам" in live_call.PHONE_RULES
    assert "обращённую не к тебе" in live_call.PHONE_RULES
    assert "end_call" in live_call.PHONE_RULES


# ------------------------------------------------------------------ 1.6.1: слышит каждое слово, «вы», язык вопроса, «Да, сэр»
from bot import persona as persona_mod  # noqa: E402


def test_greetings_are_short_on_wake():
    # он просил: на вызов — только коротко, без «Да, слышу вас, …»
    assert phone_live.greeting_texts("ru", "mix") == ["Да, сэр.", "Да, шеф.", "Да, босс."]
    assert phone_live.greeting_texts("ru", "ser") == ["Да, сэр."]
    assert all(len(t.split()) <= 2 for t in phone_live.greeting_texts("ru", "none"))
    assert phone_live._same_words("Да, сэр.", "да сэр")
    assert not phone_live._same_words("Да, сэр, я здесь", "Да, сэр.")


def test_short_greetings_reuse_already_recorded_clips(tmp_path, monkeypatch):
    """Баланс Gemini на нуле — короткие фразы берутся из уже записанных, без нового синтеза."""
    from bot import services

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    old = {"key": "v3", "clips": [{"text": t, "wav": "UklGRg=="} for t in
                                  ("Да, сэр.", "Да, шеф.", "Да, босс.", "Да, слышу вас, сэр.")]}
    (tmp_path / "greetings_v3_Sulafat_ru_mix.json").write_text(json.dumps(old), encoding="utf-8")

    async def persona(uid):
        return persona_mod.Persona(lang="ru", honorific="mix")

    async def no_live(*a, **k):
        raise AssertionError("не должен синтезировать заново")

    monkeypatch.setattr(services, "persona", persona)
    monkeypatch.setattr(phone_live, "_live_say", no_live)
    out = asyncio.run(phone_live.greetings(1))
    assert [c["text"] for c in out["clips"]] == ["Да, сэр.", "Да, шеф.", "Да, босс."]
    assert out["key"].startswith("v4_")


def test_depleted_prepay_is_a_billing_error():
    from bot import billing

    assert billing.is_billing_error(None, "gemini-3.8-live: Your prepayment credits are depleted. Please go to AI Studio")


def test_greeting_clip_is_trimmed_without_tail():
    import numpy as np

    rate = 24000
    voice = (np.sin(np.arange(rate) / 5) * 8000).astype(np.int16)
    pcm = np.concatenate([np.zeros(rate // 5, np.int16), voice, (np.random.randn(rate // 10) * 40).astype(np.int16),
                          np.zeros(rate // 5, np.int16)]).tobytes()
    out = np.frombuffer(phone_live.trim_clip(pcm, rate), dtype=np.int16)
    assert rate <= len(out) <= rate + rate // 10  # тишина и шорох по краям срезаны
    assert abs(int(out[0])) < 50 and abs(int(out[-1])) < 50  # мягкое начало и конец — без щелчка


def test_formal_address_in_every_language():
    p = persona_mod.Persona(address="siz", lang="ru")
    style = persona_mod.style_rules(p, spoken=True)
    assert "ТОЛЬКО на «вы»" in style and "никогда «ты»" in style and "«siz»" in style
    assert "ну вы даёте" in persona_mod.human_rules(p) and "ну ты даёшь" not in persona_mod.human_rules(p)


def test_answers_in_the_language_of_the_question():
    p = persona_mod.Persona(lang="ru", mirror=True, address="siz")
    rule = persona_mod.lang_rule(p)
    assert "на том языке, на котором он сейчас" in rule and "по-узбекски, по-русски и по-английски" in rule
    assert "Не расслышала, повтори" not in rule and "пойми по смыслу" in rule
    sess = live_call._Session(_profile_stub(), p, mode="phone", system="x")
    speech = sess.setup_payload("m", rich=True)["setup"]["generationConfig"]["speechConfig"]
    assert "languageCode" not in speech  # язык речи не фиксируем
    fixed = live_call._Session(_profile_stub(), persona_mod.Persona(lang="ru"), mode="phone", system="x")
    assert fixed.setup_payload("m", rich=True)["setup"]["generationConfig"]["speechConfig"]["languageCode"] == "ru-RU"


def test_every_language_rule_knows_his_three_languages():
    for lang in ("ru", "uz"):
        rule = persona_mod.lang_rule(persona_mod.Persona(lang=lang))
        assert "испанский" in rule and "Не расслышала, повтори" not in rule


def test_mirror_is_read_from_data_dir(tmp_path, monkeypatch):
    from bot import services

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "persona_9.json").write_text('{"mirror": true}', encoding="utf-8")
    assert services.persona_overrides(9, persona_mod.Persona()).mirror is True
    assert services.persona_overrides(10, persona_mod.Persona()).mirror is False


def test_phone_waits_one_second_of_silence():
    assert phone_live.VAD_SILENCE_MS == 1000


def _profile_stub():
    from bot.profile import Profile

    return Profile(telegram_id=1, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Тест", username="t")


# ------------------------------------------------------------------ 1.6.2: «Live, но экономно», лимит $0.5 в день
import numpy as np  # noqa: E402

from bot import billing  # noqa: E402


def _pcm(level: float, ms: int = 80) -> bytes:
    n = int(16000 * ms / 1000)
    return (np.sin(np.arange(n) / 3) * level).astype(np.int16).tobytes() if level else bytes(n * 2)


def test_silence_is_not_sent_to_gemini_but_speech_is_with_preroll():
    gate = phone_live.SpeechGate()
    sent = []
    for _ in range(40):                       # 3.2 с тишины и тихого фона — ничего
        sent += gate.feed(_pcm(40))
    assert sent == [] and not gate.active
    sent += gate.feed(_pcm(4000))             # заговорил — уходит и полсекунды «до»
    assert gate.active and 6 <= len(sent) <= 8
    for _ in range(10):                       # пауза посреди фразы (0.8 с) — продолжаем слать
        sent += gate.feed(_pcm(40))
    assert gate.active
    for _ in range(25):                       # 2 с тишины — окно закрылось
        gate.feed(_pcm(40))
    assert not gate.active
    assert gate.feed(bytes(2560)) == []       # нули, пока Джарвис говорит, — тоже не шлём


class _Gem:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_str(self, s: str) -> None:
        self.sent.append(json.loads(s))


class _Phone:
    closed = False

    def __init__(self) -> None:
        self.sent: list = []

    async def send_str(self, s: str) -> None:
        self.sent.append(json.loads(s))

    async def send_bytes(self, b: bytes) -> None:
        self.sent.append(b)


def _live() -> phone_live.PhoneLive:
    return phone_live.PhoneLive(_profile_stub(), persona_mod.Persona(lang="ru"), system="", phone_ws=_Phone(), device={})


def test_screen_frames_go_one_at_a_time(monkeypatch):
    monkeypatch.setattr(billing, "over_limit", lambda: False)
    sess, gem = _live(), _Gem()
    frames = lambda: [m for m in gem.sent if "video" in m.get("realtimeInput", {})]  # noqa: E731

    async def scenario():
        sess._streams.add("screen")
        sess._stream_on_at = time.monotonic()
        await sess._on_frame(gem, {"data": "a", "mimeType": "image/jpeg"})   # первый кадр — сразу
        assert len(frames()) == 1
        for _ in range(3):                                                    # молчит — новые кадры не уходят
            await asyncio.sleep(0.45)
            await sess._on_frame(gem, {"data": "b", "mimeType": "image/jpeg"})
        assert len(frames()) == 1
        await sess._send_pending_frame(gem, min_gap=1.0)                      # заговорил — свежий кадр
        assert len(frames()) == 2 and frames()[-1]["realtimeInput"]["video"]["data"] == "b"
        sess._streams.clear()                                                 # галерея — все фото пачкой
        for i in range(3):
            await sess._on_frame(gem, {"data": f"g{i}", "mimeType": "image/jpeg"})
        assert len(frames()) == 5

    import time
    asyncio.run(scenario())


def test_conversation_ends_after_15_seconds_of_silence(monkeypatch):
    monkeypatch.setattr(billing, "over_limit", lambda: False)
    sess = _live()
    assert sess.idle_limit == 15.0

    async def scenario():
        await sess._maybe_end_idle()
        assert not sess.phone_ws.sent
        sess.gate.last_voice -= 16
        sess._last_model_audio -= 16
        await sess._maybe_end_idle()
        await sess._maybe_end_idle()
        assert sess.phone_ws.sent == [{"type": "end"}]

    asyncio.run(scenario())


def test_daily_limit_alerts_once_and_turns_on_economy(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(billing, "_state", None)
    monkeypatch.setattr(billing, "_maybe_alert", lambda: None)
    sent: list[str] = []
    monkeypatch.setattr(billing, "_notify", lambda text: sent.append(text))
    usage = {"promptTokenCount": 100000, "responseTokenCount": 10000,
             "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 100000}],
             "responseTokensDetails": [{"modality": "AUDIO", "tokenCount": 10000}]}
    assert not billing.over_limit()
    for _ in range(3):
        billing.record("gemini-3.8-live", usage, kind="live")  # ~$0.2 за раз
    assert billing.over_limit() and billing.spent_today() >= billing.DAILY_LIMIT_USD == 0.5
    assert len(sent) == 1 and "экономном режиме" in sent[0]
    assert phone_live.PhoneLive(_profile_stub(), persona_mod.Persona(), system="", phone_ws=_Phone(), device={}).idle_limit == 8.0
    assert "ЭКОНОМНЫЙ РЕЖИМ" in live_call.system_instruction(_profile_stub(), persona_mod.Persona(lang="ru"), mode="phone")


def test_phone_prompt_and_tools_are_short(monkeypatch):
    monkeypatch.setattr(billing, "over_limit", lambda: False)
    text = live_call.system_instruction(_profile_stub(), persona_mod.Persona(lang="ru", address="siz"), mode="phone", snapshot="x" * 3000)
    decls = live_call.tool_declarations("phone")
    size = len(json.dumps(decls, ensure_ascii=False))
    assert len(text) < 7500 and "x" * 100 not in text and "О СЕБЕ" not in text     # было ~14.5 тысяч знаков
    assert size < 10000                                                           # было ~25, потом ~18 тысяч
    assert "КОМАНДЫ — МОЛЧА" in text and "{year}" not in text
    names = {d["name"] for d in decls}
    assert {"phone_call", "bot_task", "screen_look", "phone_task"} <= names and not {"expect_photo", "complete_tasks", "ai_status"} & names
    assert "undo_last" in {d["name"] for d in live_call.phone_task_declarations()}
    assert all(len(d["description"]) <= 172 for d in decls)

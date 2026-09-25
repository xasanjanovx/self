"""Экономия (сентябрь 2026): учёт по видам и цена разговора, кэш, жёсткий лимит, сжатие Live, экономный режим телефона."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import numpy as np
import pytest

from bot import billing, live_call, phone, phone_cheap, phone_live
from bot.ai import AgentStep
from bot.persona import Persona
from bot.profile import Profile


def _profile(uid: int = 77) -> Profile:
    return Profile(telegram_id=uid, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Тест", username="t")


@pytest.fixture()
def fresh_billing(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(billing, "_state", None)
    monkeypatch.setattr(billing, "_last_check", 0.0)
    monkeypatch.setattr(billing, "_notify", lambda text: None)


# ------------------------------------------------------------------ billing
def test_cached_input_costs_ten_percent():
    usage = {"promptTokenCount": 10000, "cachedContentTokenCount": 8000, "candidatesTokenCount": 100}
    parts, tokens = billing.breakdown("gemini-3.5-flash-lite", usage)
    assert tokens["cached"] == 8000 and tokens["text_in"] == 2000
    assert parts["cached"] == pytest.approx(8000 * 0.3 * 0.1 / 1e6)
    assert parts["text_in"] == pytest.approx(2000 * 0.3 / 1e6)
    assert billing.cost("gemini-3.5-flash-lite", usage) == pytest.approx(sum(parts.values()))
    # без кэша — как раньше
    assert billing.cost("gemini-3.5-flash-lite", {"promptTokenCount": 10000}) == pytest.approx(10000 * 0.3 / 1e6)


def test_live_breakdown_by_modality():
    usage = {"promptTokenCount": 3000, "candidatesTokenCount": 500, "thoughtsTokenCount": 40,
             "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 2000}, {"modality": "AUDIO", "tokenCount": 800},
                                     {"modality": "IMAGE", "tokenCount": 200}],
             "candidatesTokensDetails": [{"modality": "AUDIO", "tokenCount": 500}]}
    parts, tokens = billing.breakdown("gemini-3.8-live", usage)
    assert tokens == {"text_in": 2000, "audio_in": 800, "image_in": 200, "cached": 0, "text_out": 0, "thoughts": 40, "audio_out": 500}
    assert parts["audio_out"] == pytest.approx(500 * 12 / 1e6)
    assert parts["thoughts"] == pytest.approx(40 * 4.5 / 1e6)


def test_day_detail_and_conversation_cost(fresh_billing):
    async def scenario():
        meter = billing.start_session("phone", "economy")
        billing.record("gemini-3.5-flash-lite", {"promptTokenCount": 7000, "cachedContentTokenCount": 6000, "candidatesTokenCount": 20},
                       kind="agent")
        billing.record("gemini-3.8-live", {"promptTokenCount": 50, "candidatesTokenCount": 150,
                                           "candidatesTokensDetails": [{"modality": "AUDIO", "tokenCount": 150}]}, kind="voice")
        billing.end_session(meter, said="позвони маме")
        billing.record("gemini-3.5-flash-lite", {"promptTokenCount": 1000}, kind="stt")  # уже вне разговора
        return meter

    meter = asyncio.run(scenario())
    assert set(meter.parts) == {"agent", "voice"}
    s = billing.status()
    assert s["detail_today_usd"]["agent"]["cached"] > 0 and s["detail_today_usd"]["voice"]["audio_out"] > 0
    conv = s["conversations_today"]["phone/economy"]
    assert conv["count"] == 1 and conv["usd"] == pytest.approx(round(meter.usd, 3), abs=1e-3)
    assert s["last_conversations"][-1]["said"] == "позвони маме"
    tokens = billing._load()["days"][billing._today()]["tokens"]
    assert tokens["agent"]["cached"] == 6000 and tokens["stt"]["text_in"] == 1000


def test_hard_daily_limit_turns_live_off_except_wake(fresh_billing, monkeypatch):
    assert billing.live_allowed("phone")
    monkeypatch.setattr(billing, "over_limit", lambda: True)
    assert not billing.live_allowed("phone") and not billing.live_allowed("assistant")
    assert billing.live_allowed("wake")  # подъём на фаджр — всегда
    assert "выключен" in billing.status()["live_voice_today"]


def test_telegram_call_refused_after_limit(monkeypatch):
    monkeypatch.setattr(billing, "over_limit", lambda: True)
    res = asyncio.run(live_call.run(_profile(), mode="assistant"))
    assert res.error == "daily_limit" and not res.answered


# ------------------------------------------------------------------ Live: сжатие памяти, «размышления», промпт звонка
def test_live_setup_has_compression_and_no_thinking(monkeypatch):
    monkeypatch.setattr(live_call, "_extras_level", {})
    sess = live_call._Session(_profile(), Persona(lang="ru"), mode="phone", system="x" * 3000)
    setup = sess.setup_payload("gemini-3.8-live", rich=False)["setup"]
    assert setup["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}
    cw = setup["contextWindowCompression"]
    assert cw["triggerTokens"] > cw["slidingWindow"]["targetTokens"] > 1000
    live_call._extras_level["gemini-3.8-live"] = 1
    setup = sess.setup_payload("gemini-3.8-live", rich=False)["setup"]
    assert "thinkingConfig" not in setup["generationConfig"] and "contextWindowCompression" in setup
    live_call._extras_level["gemini-3.8-live"] = 0
    setup = sess.setup_payload("gemini-3.8-live", rich=False)["setup"]
    assert "thinkingConfig" not in setup["generationConfig"] and "contextWindowCompression" not in setup
    assert "contextWindowCompression" not in sess.setup_payload("qwen3.8-omni-flash-realtime", rich=False)["setup"]


class _GemWS:
    """Gemini Live: отказывает в настройке, если в ней есть поле `reject`."""

    def __init__(self, reject: str | None) -> None:
        self.reject = reject
        self.closed = False
        self.setup: dict = {}

    async def send_str(self, s: str) -> None:
        self.setup = json.loads(s)["setup"]

    async def receive(self):
        bad = self.reject and (self.reject in self.setup or self.reject in self.setup.get("generationConfig", {}))
        if bad:
            return SimpleNamespace(type=SimpleNamespace(name="CLOSE"), data=None, extra=f'Unknown name "{self.reject}" at setup')
        return SimpleNamespace(type=SimpleNamespace(name="TEXT"), data=json.dumps({"setupComplete": {}}))

    async def close(self) -> None:
        self.closed = True


def test_live_connect_drops_extras_the_model_rejects(monkeypatch):
    monkeypatch.setattr(live_call, "_extras_level", {})
    sockets: list[_GemWS] = []

    class Http:
        async def ws_connect(self, url, **kw):
            sockets.append(_GemWS("thinkingConfig"))
            return sockets[-1]

    sess = live_call._Session(_profile(), Persona(lang="ru", mirror=True), mode="phone", system="x")
    ws = asyncio.run(sess.connect(Http()))
    assert ws is sockets[-1] and len(sockets) == 2
    assert "contextWindowCompression" in ws.setup and "thinkingConfig" not in ws.setup["generationConfig"]
    assert live_call._extras_level[live_call.MODELS[0]] == 1


def test_telegram_call_prompt_has_no_data_snapshot(monkeypatch):
    async def persona(uid):
        return Persona(lang="ru")

    async def memory(uid):
        return "ПАМЯТЬ"

    monkeypatch.setattr("bot.services.persona", persona)
    monkeypatch.setattr("bot.agent_tools_extra.memory_prompt", memory)
    monkeypatch.setattr("bot.agent_tools.snapshot", lambda profile: (_ for _ in ()).throw(AssertionError("snapshot в звонке")))
    p, snapshot, mem = asyncio.run(live_call._prompt_parts(_profile(), "assistant"))
    assert snapshot == "" and mem == "ПАМЯТЬ"


# ------------------------------------------------------------------ экономный режим: инструменты и промпт
def test_cheap_tools_have_live_mode_instead_of_frames():
    decls = phone_cheap.declarations()
    names = [d["name"] for d in decls]
    assert len(names) == len(set(names))
    assert "live_mode" in names and not phone_cheap.LIVE_TOOLS & set(names)
    assert {"phone_call", "telegram_send", "confirm_send", "add_finance_entries", "bot_task", "end_call"} <= set(names)


def test_cheap_prompt_is_stable_between_minutes():
    """Инструкция не меняется от минуты к минуте — Gemini берёт её из кэша; время — в реплике."""
    a = phone_cheap.system_prompt(_profile(), Persona(lang="ru"), "память")
    now = live_call.now_line(_profile())
    assert "ЭКОНОМНЫЙ РЕЖИМ" in a and "live_mode" in a and now not in a and "время — в его репликах" in a
    assert now in live_call.system_instruction(_profile(), Persona(lang="ru"), mode="phone")


def test_clean_reply_silence_marks():
    assert phone_cheap.clean_reply("-") == "" and phone_cheap.clean_reply(" … ") == "" and phone_cheap.clean_reply("") == ""
    assert phone_cheap.clean_reply("Готово, **сэр**.").startswith("Готово")


# ------------------------------------------------------------------ фразы из потока микрофона
RATE = phone_live.INPUT_RATE


def _chunk(loud: bool, ms: int = 80) -> bytes:
    n = RATE * ms // 1000
    x = (np.sin(np.arange(n) * 0.3) * 6000) if loud else (np.random.default_rng(0).normal(0, 30, n))
    return x.astype(np.int16).tobytes()


def test_segmenter_cuts_phrase_after_pause():
    seg = phone_cheap.Segmenter()
    for _ in range(10):
        assert seg.feed(_chunk(False)) == (False, None, False)
    started, _, _ = seg.feed(_chunk(True))
    assert started
    for _ in range(9):
        assert seg.feed(_chunk(True))[1] is None
    out = None
    for _ in range(20):
        _, out, _ = seg.feed(_chunk(False))
        if out:
            break
    assert out is not None
    seconds = len(out) / 2 / RATE
    # ~0.3 с до речи + 0.8 с речи + ~0.3 с хвоста (остальная тишина не уходит)
    assert 1.2 <= seconds <= 1.6


def test_segmenter_drops_clicks():
    seg = phone_cheap.Segmenter()
    seg.feed(_chunk(False))
    seg.feed(_chunk(True))
    results = [seg.feed(_chunk(False)) for _ in range(15)]
    assert any(r[2] for r in results) and not any(r[1] for r in results)


# ------------------------------------------------------------------ «только мой голос» в Live
def test_owner_gate_drops_other_voices(monkeypatch):
    verdicts = iter([{"other": True}, {"other": False}])

    async def is_other(uid, wav):
        return next(verdicts)

    monkeypatch.setattr("bot.voiceprint.is_other", is_other)
    gate = phone_live.OwnerGate(1, enabled=True)

    async def phrase() -> list[bytes]:
        sent: list[bytes] = []
        for i in range(20):  # 1.6 с речи, конец фразы
            sent += await gate.filter([_chunk(True)], active=i < 19)
        return sent

    assert asyncio.run(phrase()) == []            # телевизор — ничего в Gemini
    sent = asyncio.run(phrase())                  # он — вся фраза целиком
    assert sum(len(c) for c in sent) == 20 * len(_chunk(True))
    off = phone_live.OwnerGate(1, enabled=False)
    assert asyncio.run(off.filter([b"ab"], True)) == [b"ab"]


# ------------------------------------------------------------------ экономный разговор целиком (без сети)
class _PhoneWS:
    def __init__(self) -> None:
        self.sent: list = []
        self.closed = False

    async def send_str(self, s: str) -> None:
        self.sent.append(json.loads(s))

    async def send_bytes(self, b: bytes) -> None:
        self.sent.append(b)


def _cheap(monkeypatch, steps: list[AgentStep]) -> tuple[phone_cheap.PhoneCheap, _PhoneWS, list[str]]:
    monkeypatch.setattr("bot.voiceprint.enrolled", lambda uid: False)
    ws = _PhoneWS()
    sess = phone_cheap.PhoneCheap(_profile(), Persona(lang="ru"), ws, {"battery": 80}, "")
    queue = list(steps)

    async def agent_step(contents, **kw):
        assert kw["system"] == sess.system and kw["thinking_budget"] == 0
        return queue.pop(0)

    spoken: list[str] = []

    async def say(text):
        spoken.append(text)
        return True

    monkeypatch.setattr(phone_cheap.ai, "agent_step", agent_step)
    monkeypatch.setattr(sess.speaker, "say", say)
    return sess, ws, spoken


def _call(name: str, **args) -> AgentStep:
    return AgentStep(parts=[{"functionCall": {"name": name, "args": args}}], text="", calls=[(name, args)], finish="STOP")


def _text(text: str) -> AgentStep:
    return AgentStep(parts=[{"text": text}], text=text, calls=[], finish="STOP")


@pytest.fixture
def contacts(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    phone._contacts.clear()
    phone.save_contacts(77, [{"n": "Ойижон", "p": ["+998901111111"]}])


def test_cheap_command_runs_silently(monkeypatch, contacts):
    sess, ws, spoken = _cheap(monkeypatch, [_call("phone_call", who="мама")])
    asyncio.run(sess._on_text("позвони маме", visible=True))
    assert {"type": "action", "action": {"type": "call", "number": "+998901111111", "name": "Ойижон"}} in ws.sent
    assert spoken == [] and ws.sent[-1] == {"type": "turn_complete"}
    assert sess.contents[0]["parts"][0]["text"].startswith("[Сейчас ")  # время — в реплике


def test_cheap_answer_is_spoken(monkeypatch):
    sess, ws, spoken = _cheap(monkeypatch, [_text("Завтра до двадцати семи градусов, без дождя.")])
    asyncio.run(sess._on_text("какая завтра погода?", visible=True))
    assert spoken == ["Завтра до двадцати семи градусов, без дождя."]
    assert {"type": "jarvis", "text": spoken[0]} in ws.sent and ws.sent[-1] == {"type": "turn_complete"}


def test_cheap_says_nothing_to_noise(monkeypatch):
    sess, ws, spoken = _cheap(monkeypatch, [_text("-")])
    asyncio.run(sess._on_text("(разговор рядом)", visible=False))
    assert spoken == [] and ws.sent == [{"type": "turn_complete"}]


def test_cheap_switches_to_live_for_camera(monkeypatch):
    monkeypatch.setattr(billing, "live_allowed", lambda mode="phone": True)
    sess, ws, spoken = _cheap(monkeypatch, [_text("Поставила будильник."), _call("live_mode", request="посмотри, что у меня в руке")])
    asyncio.run(sess._on_text("разбуди в семь", visible=True))
    asyncio.run(sess._on_text("посмотри, что у меня в руке", visible=True))
    assert sess.upgrade is not None and sess.upgrade.request == "посмотри, что у меня в руке"
    assert "разбуди в семь" in sess.upgrade.context and "посмотри" not in sess.upgrade.context
    assert ws.sent[-1] == {"type": "status", "text": "Подключаю живой режим…"}  # отвечать будет Live — без turn_complete


def test_cheap_live_refused_after_limit(monkeypatch):
    monkeypatch.setattr(billing, "live_allowed", lambda mode="phone": False)
    sess, ws, spoken = _cheap(monkeypatch, [_call("live_mode", request="давай поговорим"), _text("Сегодня живой режим уже выключен.")])
    asyncio.run(sess._on_text("давай поговорим", visible=True))
    assert sess.upgrade is None and spoken == ["Сегодня живой режим уже выключен."]
    resp = sess.contents[-2]["parts"][0]["functionResponse"]["response"]
    assert "дневной лимит" in resp["error"]


def test_cheap_message_confirmation_is_spoken(monkeypatch, contacts):
    async def telegram_send(turn, ctx, a):
        return {"status": "awaiting_confirmation", "ask_exactly": "Отправить Ойижон: «буду в семь»?"}

    real = phone.PHONE_TOOLS["telegram_send"]
    monkeypatch.setitem(phone.PHONE_TOOLS, "telegram_send", SimpleNamespace(handler=telegram_send, declaration=real.declaration))
    sess, ws, spoken = _cheap(monkeypatch, [_call("telegram_send", who="мама", text="буду в семь")])
    asyncio.run(sess._on_text("напиши маме, что буду в семь", visible=True))
    assert spoken == ["Отправить Ойижон: «буду в семь»?"]


# ------------------------------------------------------------------ голос ответа: потоковый TTS, запасной — Live
def test_speaker_streams_tts_and_stops_on_barge_in(monkeypatch):
    sent: list[bytes] = []

    async def send(b: bytes) -> None:
        sent.append(b)
        if len(sent) == 2:
            speaker.interrupt()  # перебил голосом посреди ответа

    async def stream(text, *, voice="Kore", model=""):
        for i in range(5):
            yield bytes([i]) * 10

    speaker = phone_cheap.Speaker(_profile(), Persona(lang="ru"), send)
    monkeypatch.setattr(phone_cheap.ai, "speak_stream", stream)
    assert asyncio.run(speaker.say("Готово.")) is True
    assert len(sent) == 2 and not speaker.speaking


def test_speaker_falls_back_to_live_voice(monkeypatch):
    async def send(b: bytes) -> None:
        pass

    async def broken(text, *, voice="Kore", model=""):
        raise RuntimeError("TTS 429")
        yield b""  # noqa: unreachable — это генератор

    speaker = phone_cheap.Speaker(_profile(), Persona(lang="ru"), send)
    monkeypatch.setattr(phone_cheap.ai, "speak_stream", broken)
    live_said: list[str] = []

    async def ready(timeout: float = 6.0) -> bool:
        return True

    async def say_live(text: str) -> bool:
        live_said.append(text)
        return True

    monkeypatch.setattr(speaker, "_ready", ready)
    monkeypatch.setattr(speaker, "_say_live", say_live)
    assert asyncio.run(speaker.say("Готово.")) is True and live_said == ["Готово."]


def test_voice_agent_turn_is_billed_as_agent():
    from bot.ai import _usage_kind

    audio = {"contents": [{"role": "user", "parts": [{"inline_data": {"mime_type": "audio/wav", "data": ""}}]}]}
    assert _usage_kind("gemini-3.5-flash-lite", audio) == "stt"
    assert _usage_kind("gemini-3.5-flash-lite", {**audio, "tools": [{}]}) == "agent"
    assert _usage_kind("gemini-3.8-flash-lite-tts", audio) == "tts"

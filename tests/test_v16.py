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

    async def scenario():
        phone_live.prewarm(5)  # телефон ещё ни разу не подключался — заготовки нет
        assert 5 not in phone_live._warm
        phone_live._last_device[5] = {"duplex": True}
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

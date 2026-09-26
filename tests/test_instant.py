"""Мгновенные команды JES без Gemini: разбор фраз и поток в phone_live (26.09.2026)."""
from __future__ import annotations

import asyncio
import time

import pytest

from bot import instant, phone, phone_live, wakeword
from bot.persona import Persona
from tests.test_phone_live import FakeWS, _profile


@pytest.mark.parametrize("text, tool, args", [
    ("позвони маме", "phone_call", {"who": "маме", "variants": []}),
    ("набери сирожбек акам пожалуйста", "phone_call", {"who": "сирожбек акам", "variants": []}),
    ("открой ютуб", "open_app", {"name": "ютуб", "variants": []}),
    ("включи фонарик", "flashlight", {"on": True}),
    ("фонарик", "flashlight", {"on": True}),
    ("выключи фонарик", "flashlight", {"on": False}),
    ("пауза", "media", {"command": "pause"}),
    ("следующая песня", "media", {"command": "next"}),
    ("сделай громче", "set_volume", {"direction": "up"}),
    ("тише", "set_volume", {"direction": "down"}),
    ("поставь будильник на шесть тридцать", "set_alarm", {"time": "06:30"}),
    ("разбуди меня в семь утра", "set_alarm", {"time": "07:00"}),
    ("будильник на восемь сорок пять вечера", "set_alarm", {"time": "20:45"}),
    ("таймер на пять минут", "set_timer", {"seconds": 300}),
    ("засеки двадцать пять секунд", "set_timer", {"seconds": 25}),
    ("таймер на полчаса", "set_timer", {"seconds": 1800}),
])
def test_commands(text, tool, args):
    cmd = instant.parse(text)
    assert cmd is not None and cmd.tool == tool and cmd.args == args


@pytest.mark.parametrize("text", [
    "позвони мне", "вызови такси", "какая погода завтра", "сколько я потратил", "да", "нет",
    "будильник на завтра", "открой мне пожалуйста то что я вчера смотрел в ютубе", "onamga qongiroq qil", "",
])
def test_not_commands_go_to_gemini(text):
    assert instant.parse(text) is None


def test_numbers():
    assert instant.numbers("семь тридцать пять".split()) == [7, 35]
    assert instant.numbers("двадцать".split()) == [20]


def _speech(seconds: float) -> bytes:
    return b"\x10\x27" * int(16000 * seconds)  # громко (10000)


def test_instant_call_skips_gemini(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    ws, gem = FakeWS(), FakeWS()
    sess = phone_live.PhoneLive(_profile(), Persona(lang="ru"), system="", phone_ws=ws, device={})
    phone.save_contacts(sess.uid, [{"n": "ONAJONIM", "p": ["+998901112233"]}])

    async def heard(wav):  # noqa: ANN001
        return {"text": "джес позвони маме", "name": True, "after": "позвони маме", "ms": 40}

    monkeypatch.setattr(wakeword, "check", heard)

    async def go():
        sess.gate.active, sess.gate.last_voice = True, time.monotonic()
        out = await sess._instant(gem, [_speech(1.0)])
        assert out == []  # фраза ещё идёт — держим
        sess.gate.last_voice = time.monotonic() - 1.0
        return await sess._instant(gem, [])

    assert asyncio.run(go()) == []  # в Gemini ничего не ушло
    actions = [m["action"] for m in ws.sent if isinstance(m, dict) and m.get("type") == "action"]
    assert actions and actions[0]["type"] == "call" and actions[0]["name"] == "ONAJONIM"
    assert not [m for m in gem.sent if "realtimeInput" in m] and sess.instant_done == 1


def test_not_a_command_goes_to_gemini_whole(monkeypatch):
    ws, gem = FakeWS(), FakeWS()
    sess = phone_live.PhoneLive(_profile(), Persona(lang="ru"), system="", phone_ws=ws, device={})

    async def heard(wav):  # noqa: ANN001
        return {"text": "какая погода завтра", "name": False, "after": "", "ms": 40}

    monkeypatch.setattr(wakeword, "check", heard)
    audio = _speech(1.0)

    async def go():
        sess.gate.active, sess.gate.last_voice = True, time.monotonic()
        assert await sess._instant(gem, [audio]) == []
        sess.gate.last_voice = time.monotonic() - 1.0
        return await sess._instant(gem, [])

    assert asyncio.run(go()) == [audio]

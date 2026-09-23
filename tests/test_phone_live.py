"""Живой Джарвис на телефоне: инструменты режима phone, промпт, пересылка действий, управление экраном."""
from __future__ import annotations

import asyncio
import json

import pytest

from bot import live_call, phone, phone_live
from bot.ai import AgentStep
from bot.persona import Persona
from bot.profile import Profile


def _profile() -> Profile:
    return Profile(telegram_id=77, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Тест", username="t")


class FakeWS:
    """И телефон, и Gemini: складывает всё отправленное."""

    def __init__(self) -> None:
        self.sent: list = []
        self.closed = False

    async def send_str(self, s: str) -> None:
        self.sent.append(json.loads(s))

    async def send_bytes(self, b: bytes) -> None:
        self.sent.append(b)


def _session() -> tuple[phone_live.PhoneLive, FakeWS]:
    ws = FakeWS()
    sess = phone_live.PhoneLive(_profile(), Persona(lang="ru"), system="", phone_ws=ws, device={})
    return sess, ws


def test_phone_tool_declarations():
    decls = live_call.tool_declarations("phone")
    names = [d["name"] for d in decls]
    assert len(names) == len(set(names))
    assert {"control_phone", "phone_call", "telegram_send", "end_call", "send_to_chat", "add_finance_entries"} <= set(names)
    assert not {"call_me", "ask_user", "open_screen", "hand_off"} & set(names)
    for d in decls:
        assert d["parameters"]["type"] == "OBJECT"
        for req in d["parameters"].get("required", []):
            assert req in d["parameters"]["properties"]
    for d in phone_live.SCREEN_TOOLS:
        for req in d["parameters"].get("required", []):
            assert req in d["parameters"]["properties"]


def test_phone_prompt():
    text = live_call.system_instruction(_profile(), Persona(lang="ru"), mode="phone")
    assert "ГОЛОСОВОЙ АССИСТЕНТ НА ТЕЛЕФОНЕ" in text and "control_phone" in text
    assert "звонок в Telegram" not in text
    assert "звонок в Telegram" in live_call.system_instruction(_profile(), Persona(lang="ru"), mode="assistant")


def test_strip_old_images_keeps_only_latest():
    img = {"inline_data": {"mime_type": "image/jpeg", "data": "x"}}
    history = [{"role": "user", "parts": [{"text": "1"}, img]}, {"role": "model", "parts": [{"text": "a"}]},
               {"role": "user", "parts": [{"text": "2"}, img]}]
    out = phone_live.strip_old_images(history)
    assert "inline_data" not in out[0]["parts"][1] and "inline_data" in out[2]["parts"][1]
    assert "inline_data" in history[0]["parts"][1]  # оригинал не трогаем


@pytest.fixture
def uid(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    phone._contacts.clear()
    return 77


def test_phone_tool_forwards_action_to_phone(uid):
    phone.save_contacts(uid, [{"n": "Ойижон", "p": ["+998901111111"]}])
    sess, phone_ws = _session()
    gem = FakeWS()
    asyncio.run(sess._run_tools(gem, [{"id": "1", "name": "phone_call", "args": {"who": "мама"}}]))
    assert {"type": "action", "action": {"type": "call", "number": "+998901111111", "name": "Ойижон"}} in phone_ws.sent
    resp = gem.sent[-1]["toolResponse"]["functionResponses"][0]
    assert resp["name"] == "phone_call" and resp["response"]["ok"]


def test_end_call_tells_phone(uid):
    sess, phone_ws = _session()
    gem = FakeWS()
    asyncio.run(sess._run_tools(gem, [{"id": "1", "name": "end_call", "args": {}}]))
    assert {"type": "end"} in phone_ws.sent


def _scripted(*steps: AgentStep):
    queue = list(steps)

    async def step(history):
        return queue.pop(0)

    return step


def _call(name: str, **args) -> AgentStep:
    return AgentStep(parts=[{"functionCall": {"name": name, "args": args}}], text="", calls=[(name, args)], finish="STOP")


def _fake_phone(sess: phone_live.PhoneLive, ops: list):
    async def request(payload, timeout=15.0):
        ops.append(payload)
        if payload["type"] == "screen":
            return {"package": "com.whatsapp", "width": 1080, "height": 2400, "tree": "[0] EditText «» {edit} @540,2200", "shot": "AAAA"}
        return {"ok": True}

    sess.phone_request = request


def test_control_taps_then_done(uid, monkeypatch):
    sess, _ = _session()
    ops: list = []
    _fake_phone(sess, ops)
    monkeypatch.setattr(phone_live, "screen_step", _scripted(_call("type_text", index=0, text="привет"), _call("done", summary="Написала")))
    out = asyncio.run(sess.control("напиши привет", False))
    assert out == {"ok": True, "result": "Написала"}
    assert [o["type"] for o in ops] == ["screen", "ui", "screen"]
    assert ops[1]["op"] == "type_text" and ops[1]["args"]["text"] == "привет"


def test_control_asks_then_continues_after_yes(uid, monkeypatch):
    sess, _ = _session()
    ops: list = []
    _fake_phone(sess, ops)
    seen: list = []

    async def step(history):
        seen.append(list(history))  # копия: история дальше дополняется
        return [_call("ask_user", question="Отправить?"), _call("tap", index=0), _call("done", summary="Отправила")][len(seen) - 1]

    monkeypatch.setattr(phone_live, "screen_step", step)
    first = asyncio.run(sess.control("отправь привет Алишеру", False))
    assert first == {"need_confirmation": "Отправить?"}
    second = asyncio.run(sess.control("отправь привет Алишеру", True))
    assert second == {"ok": True, "result": "Отправила"}
    # после «да» модель получила ответ на свой ask_user и пометку ПОДТВЕРЖДЕНО
    resumed = seen[1][-1]["parts"]
    assert resumed[0]["functionResponse"]["name"] == "ask_user"
    assert "ПОДТВЕРЖДЕНО" in resumed[1]["text"]


def test_control_stops_on_stop_word(uid, monkeypatch):
    sess, _ = _session()
    ops: list = []
    _fake_phone(sess, ops)

    async def step(history):
        sess._control_cancel = True  # он сказал «стоп», пока модель думала
        return _call("scroll", direction="down")

    monkeypatch.setattr(phone_live, "screen_step", step)
    out = asyncio.run(sess.control("найди видео", False))
    assert out.get("stopped")


def test_pcm_to_wav_header():
    wav = phone_live.pcm_to_wav(b"\x00\x00" * 10)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE" and len(wav) == 44 + 20

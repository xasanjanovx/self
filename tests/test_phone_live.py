"""Живой Джарвис на телефоне: инструменты режима phone, промпт, пересылка действий на телефон."""
from __future__ import annotations

import asyncio
import json

import pytest

from bot import live_call, phone, phone_live
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
    assert {"phone_call", "telegram_send", "end_call", "send_to_chat", "add_finance_entries", "set_alarm"} <= set(names)
    assert not {"call_me", "ask_user", "open_screen", "hand_off", "control_phone"} & set(names)
    for d in decls:
        assert d["parameters"]["type"] == "OBJECT"
        for req in d["parameters"].get("required", []):
            assert req in d["parameters"]["properties"]


def test_phone_prompt():
    text = live_call.system_instruction(_profile(), Persona(lang="ru"), mode="phone")
    assert "ГОЛОСОВОЙ АССИСТЕНТ НА ТЕЛЕФОНЕ" in text and "control_phone" not in text
    assert "звонок в Telegram" not in text
    assert "звонок в Telegram" in live_call.system_instruction(_profile(), Persona(lang="ru"), mode="assistant")


def test_vad_setup_and_fallback_flag():
    sess, _ = _session()
    setup = sess.setup_payload("gemini-3.8-live", rich=True)["setup"]
    assert setup["realtimeInputConfig"]["automaticActivityDetection"]["silenceDurationMs"] == phone_live.VAD_SILENCE_MS
    sess.vad_tuned = False
    assert "realtimeInputConfig" not in sess.setup_payload("gemini-3.8-live", rich=True)["setup"]


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


def test_missing_contacts_are_requested_from_phone(uid):
    sess, phone_ws = _session()
    asyncio.run(sess._run_tools(FakeWS(), [{"id": "1", "name": "phone_call", "args": {"who": "мама"}}]))
    assert {"type": "need_contacts"} in phone_ws.sent


def test_end_call_tells_phone(uid):
    sess, phone_ws = _session()
    asyncio.run(sess._run_tools(FakeWS(), [{"id": "1", "name": "end_call", "args": {}}]))
    assert {"type": "end"} in phone_ws.sent


def test_pcm_to_wav_header():
    wav = phone_live.pcm_to_wav(b"\x00\x00" * 10)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE" and len(wav) == 44 + 20


# ------------------------------------------------------------------ блокировка экрана, карточки, проверка имени
def test_locked_phone_asks_to_unlock_before_calling(uid):
    phone.save_contacts(uid, [{"n": "Ойижон", "p": ["+998901111111"]}])
    sess, phone_ws = _session()
    sess.turn.device["locked"] = True
    gem = FakeWS()
    asyncio.run(sess._run_tools(gem, [{"id": "1", "name": "phone_call", "args": {"who": "мама"}}]))
    assert {"type": "unlock"} in phone_ws.sent
    assert not any(isinstance(m, dict) and m.get("type") == "action" for m in phone_ws.sent)
    assert gem.sent[-1]["toolResponse"]["functionResponses"][0]["response"]["need_unlock"]


def test_locked_phone_still_sets_timer(uid):
    sess, phone_ws = _session()
    sess.turn.device["locked"] = True
    asyncio.run(sess._run_tools(FakeWS(), [{"id": "1", "name": "set_timer", "args": {"seconds": 60}}]))
    assert any(isinstance(m, dict) and m.get("type") == "action" for m in phone_ws.sent)


def test_result_cards():
    weather = {"place": "Андижан, Узбекистан", "now": {"temp": 23.4, "feels": 22.1, "sky": "Ясно"}}
    card = phone_live.result_card("weather", {}, weather)
    assert card == {"icon": "☀️", "title": "23° · Ясно", "subtitle": "Андижан, Узбекистан · ощущается 22°"}
    pending = phone_live.result_card("telegram_send", {}, {"status": "awaiting_confirmation", "ask_exactly": "Отправить?"})
    assert pending["accent"] == "confirm"
    assert phone_live.result_card("confirm_send", {}, {"ok": True, "to": "Ойижон"})["subtitle"] == "Ойижон"
    assert phone_live.result_card("add_task", {"text": "купить хлеб"}, {"ok": True})["subtitle"] == "купить хлеб"
    assert phone_live.result_card("list_finance_entries", {}, {"entries": []}) is None


def test_prompt_does_not_answer_bare_name():
    text = live_call.system_instruction(_profile(), Persona(lang="ru"), mode="phone")
    assert "НИЧЕГО не отвечай" in text and "need_unlock" in text


@pytest.mark.parametrize("mode", ["assistant", "wake", "phone"])
def test_no_duplicate_tools_in_any_call_mode(mode):
    # Gemini Live закрывает сессию на повторном имени инструмента — звонок обрывается сразу после «алло»
    names = [d["name"] for d in live_call.tool_declarations(mode)]
    assert len(names) == len(set(names)), [n for n in names if names.count(n) > 1]

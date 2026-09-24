"""Qwen3.8-Omni-Realtime (Alibaba) как замена Gemini Live: мост протоколов, учёт расходов, выбор в настройках."""
from __future__ import annotations

import asyncio
import base64
import json

import aiohttp
import pytest

from bot import billing
from bot import live_call
from bot import qwen_live
from bot.persona import Persona
from bot.profile import Profile


def _profile() -> Profile:
    return Profile(telegram_id=1, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Тест", username="t")


class FakeQwenWS:
    closed = False

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_str(self, s: str) -> None:
        self.sent.append(json.loads(s))

    async def close(self) -> None:
        self.closed = True


def _bridge() -> tuple[qwen_live.QwenBridge, FakeQwenWS]:
    ws = FakeQwenWS()
    return qwen_live.QwenBridge(None, ws), ws  # type: ignore[arg-type]


def _setup() -> dict:
    sess = live_call._Session(_profile(), Persona(lang="ru"), mode="phone", system="ИНСТРУКЦИЯ")
    return sess.setup_payload(qwen_live.MODEL, rich=False)["setup"]


def test_setup_becomes_session_update():
    setup = _setup()
    setup["realtimeInputConfig"] = {"automaticActivityDetection": {"silenceDurationMs": 1000}}
    s = qwen_live.session_from_setup(setup, "Katerina")
    assert s["instructions"] == "ИНСТРУКЦИЯ" and s["voice"] == "Katerina"
    assert s["turn_detection"] == {"type": "server_vad", "silence_duration_ms": 1000}
    assert s["input_audio_format"] == "pcm" and s["modalities"] == ["text", "audio"]
    names = {t["name"] for t in s["tools"]}
    assert {"phone_call", "end_call", "bot_task"} <= names
    call = next(t for t in s["tools"] if t["name"] == "phone_call")
    assert call["type"] == "function" and call["parameters"]["type"] == "object"
    assert call["parameters"]["properties"]["variants"]["type"] == "array"
    assert qwen_live.session_from_setup(setup, "Nobody")["voice"] == qwen_live.DEFAULT_VOICE


def test_gemini_messages_are_translated_for_qwen():
    bridge, ws = _bridge()

    async def scenario():
        pcm16 = bytes(3200)  # 100 мс, 16 кГц
        await bridge.send_str(json.dumps({"realtimeInput": {"audio": {"data": base64.b64encode(pcm16).decode(), "mimeType": "audio/pcm;rate=16000"}}}))
        pcm24 = bytes(4800)  # 100 мс, 24 кГц (звонок в Telegram) — пересэмплируем в 16 кГц
        await bridge.send_str(json.dumps({"realtimeInput": {"audio": {"data": base64.b64encode(pcm24).decode(), "mimeType": "audio/pcm;rate=24000"}}}))
        await bridge.send_str(json.dumps({"realtimeInput": {"video": {"data": "SlBFRw==", "mimeType": "image/jpeg"}}}))
        await bridge.send_str(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{"text": "[заметка]"}]}], "turnComplete": False}}))
        await bridge.send_str(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{"text": "Привет"}]}], "turnComplete": True}}))
        await bridge.send_str(json.dumps({"toolResponse": {"functionResponses": [{"id": "c1", "name": "phone_call", "response": {"ok": True}}]}}))

    asyncio.run(scenario())
    kinds = [e["type"] for e in ws.sent]
    assert kinds == ["input_audio_buffer.append", "input_audio_buffer.append", "input_image_buffer.append",
                     "conversation.item.create", "conversation.item.create", "response.create",
                     "conversation.item.create", "response.create"]
    assert len(base64.b64decode(ws.sent[0]["audio"])) == 3200
    assert len(base64.b64decode(ws.sent[1]["audio"])) == 3200
    assert ws.sent[3]["item"]["content"][0] == {"type": "input_text", "text": "[заметка]"}
    out = ws.sent[6]["item"]
    assert out["type"] == "function_call_output" and out["call_id"] == "c1" and json.loads(out["output"]) == {"ok": True}


def test_qwen_events_look_like_gemini_to_the_session():
    bridge, _ = _bridge()
    events = [
        {"type": "response.created"},
        {"type": "response.audio.delta", "delta": "AAAA"},
        {"type": "response.audio_transcript.delta", "delta": "Звоню"},
        {"type": "input_audio_buffer.speech_started"},
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "позвони маме"},
        {"type": "response.function_call_arguments.done", "call_id": "c1", "name": "phone_call", "arguments": '{"who": "мама"}'},
        {"type": "response.done", "response": {"usage": {"input_tokens": 5000, "output_tokens": 100,
                                                         "input_tokens_details": {"text_tokens": 4800, "audio_tokens": 200},
                                                         "output_tokens_details": {"text_tokens": 20, "audio_tokens": 80}}}},
        {"type": "response.created"},
        {"type": "response.done", "response": {}},
    ]
    for ev in events:
        bridge.translate(ev)
    got = []
    while not bridge._queue.empty():
        msg = bridge._queue.get_nowait()
        got.append(live_call._decode(msg))
    assert got[0]["serverContent"]["modelTurn"]["parts"][0]["inlineData"] == {"mimeType": "audio/pcm;rate=24000", "data": "AAAA"}
    assert got[1] == {"serverContent": {"outputTranscription": {"text": "Звоню"}}}
    assert got[2] == {"serverContent": {"interrupted": True}}
    assert got[3] == {"serverContent": {"inputTranscription": {"text": "позвони маме"}}}
    assert got[4]["usageMetadata"]["promptTokenCount"] == 5000
    assert got[5] == {"toolCall": {"functionCalls": [{"id": "c1", "name": "phone_call", "args": {"who": "мама"}}]}}
    assert got[6] == {"serverContent": {"turnComplete": True}}


def test_qwen_spend_is_counted_separately_from_gemini(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(billing, "_state", None)
    usage = qwen_live.usage_to_gemini({"input_tokens": 1_000_000, "output_tokens": 1_000_000,
                                       "input_tokens_details": {"text_tokens": 1_000_000},
                                       "output_tokens_details": {"audio_tokens": 1_000_000}})
    usd = billing.record(qwen_live.MODEL, usage, kind="live")
    assert usd == pytest.approx(0.23 + 1.87)
    st = billing.status()
    assert st["spent_today_usd"] == 0 and st["qwen_today_usd"] == pytest.approx(2.1)
    assert not billing.over_limit()  # лимит $0.5 — про предоплату Gemini


def test_session_falls_back_to_gemini_when_qwen_fails(monkeypatch):
    tried = []

    async def broken(setup, *, voice):
        tried.append(voice)
        raise qwen_live.QwenError("InvalidApiKey")

    monkeypatch.setattr(qwen_live, "available", lambda: True)
    monkeypatch.setattr(qwen_live, "connect", broken)
    monkeypatch.setattr(qwen_live, "report_failure", lambda reason: tried.append(reason))

    class GeminiWS:
        closed = False

        async def send_str(self, s):
            pass

        async def receive(self):
            return qwen_live.Msg(aiohttp.WSMsgType.TEXT, json.dumps({"setupComplete": {}}))

        async def close(self):
            pass

    class Http:
        async def ws_connect(self, *a, **k):
            return GeminiWS()

    p = Persona(lang="ru")
    p.voice_model = "qwen"
    p.qwen_voice = "Maia"
    sess = live_call._Session(_profile(), p, mode="phone", system="x")
    ws = asyncio.run(sess.connect(Http()))
    assert isinstance(ws, GeminiWS) and sess.result.model == live_call.MODELS[0]
    assert tried == ["Maia", "InvalidApiKey"]
    # подъём на фаджр Qwen не трогает — там точные арабские дуа
    tried.clear()
    wake = live_call._Session(_profile(), p, mode="wake", system="x")
    asyncio.run(wake.connect(Http()))
    assert tried == []


def test_voice_model_choice_is_saved_in_data_dir(tmp_path, monkeypatch):
    from bot import services

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    services.save_persona_extra(5, {"voice_model": "qwen", "qwen_voice": "Katerina"})
    p = services.persona_overrides(5, Persona())
    assert p.voice_model == "qwen" and p.qwen_voice == "Katerina"
    services.save_persona_extra(5, {"voice_model": "gemini"})
    assert services.persona_overrides(5, Persona()).voice_model == "gemini"


def test_settings_keyboard_offers_both_models():
    from bot.keyboards import jarvis_settings_keyboard

    kb = jarvis_settings_keyboard("ru", voice="Sulafat", call_lang="ru", address="siz", tone="friendly", verbosity="short",
                                  voice_model="qwen", qwen_voice="Tina")
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "jarvis:vmodel:gemini" in data and "jarvis:vmodel:qwen" in data
    assert {"jarvis:qvoice:Tina", "jarvis:qvoice:Katerina"} <= set(data)
    plain = jarvis_settings_keyboard("ru", voice="Sulafat", call_lang="ru", address="siz", tone="friendly", verbosity="short")
    assert not any(str(b.callback_data).startswith("jarvis:qvoice") for row in plain.inline_keyboard for b in row)


def test_key_is_kept_in_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.delenv("QWEN_WS_URL", raising=False)
    assert not qwen_live.available()
    qwen_live.save_key("sk-test", "ws-123")
    assert qwen_live.available() and qwen_live.api_key() == "sk-test"
    assert qwen_live.ws_url() == "wss://ws-123.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime"


def test_free_quota_exhausted_switches_to_gemini_for_an_hour(tmp_path, monkeypatch):
    """«Stop-on-Exhaust» включён, квота кончилась: 403 AllocationQuota.FreeTierOnly — час без Qwen, запись в чат."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    monkeypatch.setattr(qwen_live, "_blocked_until", 0.0)
    reports = []
    monkeypatch.setattr(qwen_live, "report_failure", lambda reason: reports.append(reason))
    assert qwen_live.available()
    bridge, ws = _bridge()

    async def scenario():
        bridge.translate({"type": "error", "error": {"code": "AllocationQuota.FreeTierOnly", "message": "free tier exhausted"}})
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert not qwen_live.available() and ws.closed
    assert reports and "FreeTierOnly" in reports[0]
    bridge.translate({"type": "error", "error": {"code": "InvalidParameter", "message": "bad voice"}})  # не смертельно
    qwen_live.save_key("sk-new")  # новый ключ — снова пробуем
    assert qwen_live.available()

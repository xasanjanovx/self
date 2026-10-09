"""2.16 (29.09): меньше ложных проверок «Джес» — пауза после серии отказов, сжатый звук (μ-law) без тишины."""
import asyncio
import base64
import json

import numpy as np

from bot import phone_api


def ulaw_encode(pcm: np.ndarray) -> bytes:
    """Тот же алгоритм, что в телефоне (Audio.kt, G.711 μ-law), — чтобы сервер понимал его запись."""
    out = bytearray()
    for s in pcm.astype(np.int32):
        sample = int(s)
        sign = 0x80 if sample < 0 else 0
        sample = min(-sample if sample < 0 else sample, 32635) + 0x84
        exponent, mask = 7, 0x4000
        while exponent > 0 and not (sample & mask):
            exponent -= 1
            mask >>= 1
        mantissa = (sample >> (exponent + 3)) & 0x0F
        out.append(~(sign | (exponent << 4) | mantissa) & 0xFF)
    return bytes(out)


def test_ulaw_roundtrip_keeps_speech():
    t = np.arange(1600) / 16000
    pcm = (np.sin(2 * np.pi * 220 * t) * 12000).astype(np.int16)
    back = np.frombuffer(phone_api.ulaw_to_pcm16(ulaw_encode(pcm)), dtype=np.int16).astype(np.float64)
    err = np.abs(back - pcm) / 12000
    assert err.max() < 0.04 and len(back) == len(pcm)                 # μ-law: ошибка ≤ ~3% — речь не страдает


def test_cooldown_after_series_of_rejects(monkeypatch):
    phone_api._rejects.clear()
    assert phone_api._cooldown(1, False) == 0 and phone_api._cooldown(1, False) == 0
    assert phone_api._cooldown(1, False) == phone_api.COOLDOWN_S           # третий отказ за минуту — пауза 20 с
    assert phone_api._cooldown(1, True) == 0                                 # уверенное «Джес» не наказываем
    assert phone_api._cooldown(None, False) == 0


def test_wake_check_accepts_ulaw_and_asks_for_pause(monkeypatch):
    from bot import journal, phone_live, voiceprint, wakeword

    phone_api._rejects.clear()
    t = np.arange(8000) / 16000
    pcm = (np.sin(2 * np.pi * 180 * t) * 9000).astype(np.int16)
    seen: list = []

    async def verify(uid, wav):  # noqa: ANN001, ANN202
        seen.append(len(wav))
        return {"ok": True, "score": 0.2, "bank": 0.5, "z": 1.0}      # голос не точно его — отказы идут в серию

    async def check(wav):  # noqa: ANN001, ANN202
        return {"text": "с", "name": False, "after": "", "pos": -1, "ms": 60}

    body = {"audio_ulaw": base64.b64encode(ulaw_encode(pcm)).decode(), "rate": 16000, "confident": False}

    async def fake_json(request):  # noqa: ANN001, ANN202
        return body

    monkeypatch.setattr(phone_api, "_json", fake_json)
    monkeypatch.setattr(phone_api, "owner_id", lambda: 5)
    monkeypatch.setattr(voiceprint, "verify", verify)
    monkeypatch.setattr(wakeword, "check", check)
    monkeypatch.setattr(phone_live, "prewarm", lambda uid: None)
    monkeypatch.setattr(phone_live, "discard", lambda uid: None)
    monkeypatch.setattr(journal, "miss", lambda *a, **k: None)
    answers = [json.loads(asyncio.run(phone_api.wake_check(None)).text) for _ in range(3)]
    assert seen and seen[0] == 44 + 16000                                   # 0.5 с μ-law → WAV 16 бит
    assert [a["ok"] for a in answers] == [False, False, False]
    assert [a.get("cooldown") for a in answers] == [0, 0, phone_api.COOLDOWN_S]


# ------------------------------------------------------------------ 09.10: его голос + «потерянное» слово → второе мнение, пауза не от него
def _wake_env(monkeypatch, voice, heard_text, opinion=None, tmp=None):
    from bot import journal, phone_live, voiceprint, wakeword

    phone_api._rejects.clear()
    phone_api._second_state.update(at=0.0, day="", n=0)
    t = np.arange(8000) / 16000
    pcm = (np.sin(2 * np.pi * 180 * t) * 9000).astype(np.int16)
    body = {"audio_ulaw": base64.b64encode(ulaw_encode(pcm)).decode(), "rate": 16000, "confident": False}
    asked: list = []

    async def verify(uid, wav):  # noqa: ANN001, ANN202
        return dict(voice)

    async def check(wav):  # noqa: ANN001, ANN202
        return {"text": heard_text, "name": False, "after": "", "pos": -1, "ms": 60}

    async def second(audio):  # noqa: ANN001, ANN202
        asked.append(len(audio))
        return opinion

    async def fake_json(request):  # noqa: ANN001, ANN202
        return body

    monkeypatch.setattr(phone_api, "_json", fake_json)
    monkeypatch.setattr(phone_api, "owner_id", lambda: 5)
    monkeypatch.setattr(voiceprint, "verify", verify)
    monkeypatch.setattr(wakeword, "check", check)
    monkeypatch.setattr(phone_api, "second_opinion", second)
    monkeypatch.setattr(phone_api, "_keep_miss", lambda *a, **k: asked.append("miss"))
    monkeypatch.setattr(phone_live, "prewarm", lambda uid: None)
    monkeypatch.setattr(phone_live, "discard", lambda uid: None)
    monkeypatch.setattr(journal, "miss", lambda *a, **k: None)
    return asked


def test_his_voice_rules():
    assert phone_api.his_voice({"score": 0.65})
    assert phone_api.his_voice({"score": 0.2, "bank": 0.95})
    assert phone_api.his_voice({"score": 0.45, "z": 4.2, "bank": 0.7})
    assert not phone_api.his_voice({"score": 0.3, "z": 5.0, "bank": 0.5})
    assert not phone_api.his_voice({"score": 0.1, "z": 0.5})


def test_his_voice_with_lost_word_gets_second_opinion_and_wakes(monkeypatch):
    asked = _wake_env(monkeypatch, {"ok": True, "score": 0.5, "z": 4.5, "bank": 0.8}, "с",
                      opinion={"ok": True, "text": "Джес, который час", "after": "который час"})
    out = json.loads(asyncio.run(phone_api.wake_check(None)).text)
    assert asked == [44 + 16000]                      # Gemini слушал запись
    assert out["ok"] is True and out["text"] == "Джес, который час"


def test_second_opinion_no_name_keeps_reject_and_saves_the_clip(monkeypatch):
    asked = _wake_env(monkeypatch, {"ok": True, "score": 0.5, "z": 4.5, "bank": 0.8}, "в",
                      opinion={"ok": False, "text": "", "after": ""})
    out = json.loads(asyncio.run(phone_api.wake_check(None)).text)
    assert out["ok"] is False and "miss" in asked
    # отказ с его голосом паузу не включает — ни после трёх, ни после десяти
    answers = [json.loads(asyncio.run(phone_api.wake_check(None)).text) for _ in range(5)]
    assert all(a.get("cooldown") == 0 for a in answers)


def test_other_voice_does_not_get_second_opinion(monkeypatch):
    asked = _wake_env(monkeypatch, {"ok": True, "score": 0.2, "z": 1.0, "bank": 0.4}, "с")
    answers = [json.loads(asyncio.run(phone_api.wake_check(None)).text) for _ in range(3)]
    assert asked == []                                # слабый голос — Gemini не тревожим
    assert answers[-1]["cooldown"] == phone_api.COOLDOWN_S


def test_long_phrase_is_not_a_lost_name(monkeypatch):
    asked = _wake_env(monkeypatch, {"ok": True, "score": 0.5, "z": 4.5, "bank": 0.8}, "завтра с утра поеду в банк оформлять")
    out = json.loads(asyncio.run(phone_api.wake_check(None)).text)
    assert out["ok"] is False and asked == ["miss"]   # обычная речь — не зовём Gemini, запись сохраняем


def test_second_opinion_rejects_invented_name(monkeypatch):
    from bot import wakeword

    async def fake_generate(parts, **kw):  # noqa: ANN001, ANN202
        return json.dumps({"name": True, "text": "алло привет", "after": ""})

    phone_api._second_state.update(at=0.0, day="", n=0)
    monkeypatch.setattr(phone_api.ai, "generate", fake_generate, raising=False)
    out = asyncio.run(phone_api.second_opinion(b"RIFF"))
    assert out is not None and out["ok"] is False      # в расшифровке нет ничего похожего на «Джес»

    async def fake_ok(parts, **kw):  # noqa: ANN001, ANN202
        return json.dumps({"name": True, "text": "Джес позвони маме", "after": "позвони маме"})

    phone_api._second_state.update(at=0.0, day="", n=0)
    monkeypatch.setattr(phone_api.ai, "generate", fake_ok, raising=False)
    out = asyncio.run(phone_api.second_opinion(b"RIFF"))
    assert out == {"ok": True, "text": "Джес позвони маме", "after": "позвони маме"}
    assert wakeword.match("Джес позвони маме")[0]


def test_second_opinion_is_rate_limited(monkeypatch):
    calls: list = []

    async def fake_generate(parts, **kw):  # noqa: ANN001, ANN202
        calls.append(1)
        return json.dumps({"name": False, "text": "", "after": ""})

    phone_api._second_state.update(at=0.0, day="", n=0)
    monkeypatch.setattr(phone_api.ai, "generate", fake_generate, raising=False)
    assert asyncio.run(phone_api.second_opinion(b"x")) is not None
    assert asyncio.run(phone_api.second_opinion(b"x")) is None      # не чаще раза в SECOND_GAP_S
    assert len(calls) == 1

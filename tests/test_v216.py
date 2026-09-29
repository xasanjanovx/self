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
        return {"ok": True, "score": 0.7, "bank": 0.7, "z": 3.0}

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

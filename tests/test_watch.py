"""Часы Amazfit (04.10.2026): кто отвечает, канал на телефон, ответ часам (текст + MP3, ночью без голоса), «Джес» с часов."""
from __future__ import annotations

import asyncio
import base64
import json
import time

import numpy as np
import pytest

from bot import phone_api
from bot import phone_link
from bot import phone_live
from bot import watch


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    watch._watches.clear()
    for d in (phone_link._actions, phone_link._events, phone_link._seen, phone_link._state, phone_link._asleep):
        d.clear()
    yield
    watch._watches.clear()


def _worn_watch(uid=7, mode="idle"):
    w = watch.get(uid)
    w.touch({"sid": "s1", "wear": 1, "bat": 80, "sleeping": 0, "st": mode})
    return w


# ------------------------------------------------------------------ кто отвечает
def test_watch_answers_when_worn_listening_and_phone_locked():
    _worn_watch()
    assert watch.should_answer(7, phone_locked=True) is True


def test_phone_answers_when_unlocked_or_watch_off_or_stale_or_paused():
    w = _worn_watch()
    assert watch.should_answer(7, phone_locked=False) is False      # телефон в руках — отвечает телефон
    w.wear = 0
    assert watch.should_answer(7, phone_locked=True) is False       # часы сняты
    w.wear = 1
    w.pauses = {"namaz"}
    assert watch.should_answer(7, phone_locked=True) is False       # на паузе (намаз)
    w.pauses = set()
    w.seen = time.monotonic() - 120
    assert watch.should_answer(7, phone_locked=True) is False       # часы давно молчат
    assert watch.should_answer(99, phone_locked=True) is False      # часов нет вовсе


class _Req:
    def __init__(self, body: dict) -> None:
        self._body = body

    async def json(self) -> dict:
        return self._body


def test_wake_check_hands_over_to_the_watch(monkeypatch):
    monkeypatch.setattr(phone_api, "owner_id", lambda: 7)
    monkeypatch.setattr(phone_live, "prewarm", lambda uid: None)

    async def judge(uid, audio, **kw):
        return {"ok": True, "text": "джес позвони маме", "after": "позвони маме", "voice": 0.7, "fast": True}

    taken = []
    monkeypatch.setattr(phone_api, "judge_wake", judge)
    monkeypatch.setattr(watch, "take_over", lambda uid, heard, wav: taken.append((uid, heard, bool(wav))))
    _worn_watch()
    body = {"audio": base64.b64encode(b"RIFF" + b"\0" * 100).decode(), "act": True, "device": {"locked": True}}
    out = json.loads(asyncio.run(phone_api.wake_check(_Req(body))).text)
    assert out == {"ok": False, "reason": "watch", "text": "джес позвони маме"}
    assert taken == [(7, "джес позвони маме", True)]


def test_wake_check_keeps_phone_when_unlocked(monkeypatch):
    monkeypatch.setattr(phone_api, "owner_id", lambda: 7)
    monkeypatch.setattr(phone_live, "prewarm", lambda uid: None)

    async def judge(uid, audio, **kw):
        return {"ok": True, "text": "джес", "after": "", "voice": 0.7, "fast": True}

    monkeypatch.setattr(phone_api, "judge_wake", judge)
    _worn_watch()
    body = {"audio": base64.b64encode(b"RIFF" + b"\0" * 100).decode(), "device": {"locked": False}}
    out = json.loads(asyncio.run(phone_api.wake_check(_Req(body))).text)
    assert out["ok"] is True and out["text"] == "джес"


# ------------------------------------------------------------------ канал на телефон
def test_phone_link_delivers_actions_and_hand_state():
    async def run():
        phone_link._seen[7] = time.monotonic()
        assert phone_link.push(7, {"type": "call", "number": "+998"}) is True
        got = await phone_link.pull(7, wait=0.1)
        assert got["actions"] == [{"type": "call", "number": "+998"}]
        empty = await phone_link.pull(7, wait=0.05)
        assert empty["actions"] == []

    asyncio.run(run())
    phone_link.note_state(7, screen=True, locked=False)
    assert phone_link.in_hand(7) is True
    phone_link.note_state(7, screen=False, locked=None)
    assert phone_link.in_hand(7) is False


def test_pull_wakes_up_when_watch_says_asleep():
    async def run():
        task = asyncio.create_task(phone_link.pull(7, wait=5))
        await asyncio.sleep(0.05)
        phone_link.set_asleep(7, True)
        got = await asyncio.wait_for(task, 1)
        assert got["asleep"] is True and got["actions"] == []

    asyncio.run(run())


# ------------------------------------------------------------------ ответ часам
def _conv(monkeypatch, quiet=False):
    w = _worn_watch()
    monkeypatch.setattr(watch.Watch, "quiet", property(lambda self: quiet))

    async def mp3(pcm, rate=24000):
        return b"ID3" + b"x" * 10

    monkeypatch.setattr(watch, "pcm_to_mp3", mp3)
    return w, watch.Conversation(w, "button")


def test_answer_text_comes_first_then_voice_in_parts(monkeypatch):
    w, conv = _conv(monkeypatch)

    async def run():
        await conv.link.send_str(json.dumps({"type": "user", "text": "сколько время"}))
        await conv.link.send_str(json.dumps({"type": "jarvis", "text": "Сейчас 14:20, сэр."}))
        events_before_voice = w.take()
        await conv.link.send_bytes(b"\1\0" * 2400)
        await conv.link.send_str(json.dumps({"type": "turn_complete"}))
        return events_before_voice

    first = asyncio.run(run())
    assert first[0] == {"t": "heard", "text": "сколько время"}
    say = first[1]
    assert say["t"] == "say" and say["text"] == "Сейчас 14:20, сэр." and say["listen"] is True and say["audio"] == "expect"
    rest = w.take()
    assert [e["t"] for e in rest] == ["audio", "audio_end"]
    assert base64.b64decode(rest[0]["b"]).startswith(b"ID3") and rest[0]["i"] == 1


def test_long_voice_is_cut_into_parts_at_pauses(monkeypatch):
    w, conv = _conv(monkeypatch)
    speech = (np.sin(2 * np.pi * 200 * np.arange(int(24000 * 1.8)) / 24000) * 6000).astype(np.int16).tobytes()
    pause = b"\0\0" * 5000   # ~0,2 с тишины

    async def run():
        await conv.link.send_str(json.dumps({"type": "jarvis", "text": "Длинный ответ."}))
        await conv.link.send_bytes(speech)
        await conv.link.send_bytes(pause)       # накопилось >1,6 с и тишина — первый кусок уходит сразу
        parts_now = [e for e in w.take() if e["t"] == "audio"]
        await conv.link.send_bytes(speech)
        await conv.link.send_str(json.dumps({"type": "turn_complete"}))
        return parts_now

    now = asyncio.run(run())
    assert len(now) == 1
    tail = w.take()
    assert [e["t"] for e in tail] == ["audio", "audio_end"] and tail[0]["i"] == 2


def test_night_or_namaz_answer_is_text_only(monkeypatch):
    w, conv = _conv(monkeypatch, quiet=True)

    async def run():
        await conv.link.send_str(json.dumps({"type": "jarvis", "text": "Готово."}))
        await conv.link.send_bytes(b"\1\0" * 2400)
        await conv.link.send_str(json.dumps({"type": "turn_complete"}))

    asyncio.run(run())
    events = w.take()
    assert events[0]["text"] == "Готово." and events[0]["audio"] == "none"
    assert [e["t"] for e in events] == ["say", "audio_end"]


def test_silent_command_from_watch_goes_to_phone_and_shows_text(monkeypatch):
    w, conv = _conv(monkeypatch)
    phone_link._seen[7] = time.monotonic()

    async def run():
        await conv.link.send_str(json.dumps({"type": "action", "action": {"type": "call", "number": "+99890", "name": "Мама"}}))
        await conv.link.send_str(json.dumps({"type": "turn_complete"}))

    asyncio.run(run())
    assert phone_link._actions[7][0]["type"] == "call"
    events = w.take()
    assert events[0]["text"] == "Звоню: Мама" and events[0]["audio"] == "none" and events[-1]["t"] == "audio_end"


def test_phone_offline_is_said_on_the_watch(monkeypatch):
    w, conv = _conv(monkeypatch)

    async def run():
        await conv.link.send_str(json.dumps({"type": "action", "action": {"type": "ring_phone"}}))
        await conv.link.send_str(json.dumps({"type": "turn_complete"}))

    asyncio.run(run())
    assert "не на связи" in w.take()[0]["text"]


def test_silent_turn_sends_nothing(monkeypatch):
    w, conv = _conv(monkeypatch)
    asyncio.run(conv.link.send_str(json.dumps({"type": "turn_complete"})))
    assert w.take() == []


def test_one_recording_is_decoded_by_one_decoder(monkeypatch):
    """Непрерывная запись часов: декодер живёт между кусками; новый файл (new) — новый декодер (без щелчков на стыках)."""
    created = []

    class Fake:
        def __init__(self):
            created.append(self)
            self.closed = False

        def decode(self, packets):
            return b"\1\0" * 320 * len(packets)

        def close(self):
            self.closed = True

    monkeypatch.setattr(watch, "OpusStream", Fake)
    w = _worn_watch()
    chunk = _framed(*[bytes([0x48]) + bytes(20)] * 3)
    assert len(asyncio.run(w.decode_chunk(chunk, True))) == 3 * 640
    asyncio.run(w.decode_chunk(chunk, False))
    assert len(created) == 1                      # продолжение той же записи
    asyncio.run(w.decode_chunk(chunk, True))
    assert len(created) == 2 and created[0].closed  # новый файл — новый декодер


# ------------------------------------------------------------------ «Джес» с микрофона часов
def _tone(seconds, amp=4000, rate=16000):
    t = np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * np.pi * 220 * t) * amp).astype(np.int16).tobytes()


def test_watch_hears_its_name_and_opens_a_conversation(monkeypatch):
    w = _worn_watch()
    heard = []

    async def judge(uid, wav, **kw):
        heard.append((kw.get("source"), len(wav)))
        return {"ok": True, "text": "джес", "after": "", "fast": True}

    started = []
    monkeypatch.setattr(phone_api, "judge_wake", judge)
    monkeypatch.setattr(watch.Watch, "start", lambda self, by, heard="", first=None: started.append((by, heard)))

    async def run():
        await w.feed(b"\0" * 32000, {"f": 1})          # тишина — пол шума
        await w.feed(_tone(0.6) + b"\0" * 6400, {"f": 2})  # «Джес»
        await w.feed(b"\0" * 32000, {"f": 3})          # пауза — фраза кончилась
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert heard and heard[0][0] == "часы"
    assert started == [("wake", "джес")]


def test_skipped_quiet_chunks_end_the_phrase(monkeypatch):
    w = _worn_watch()
    checked = []

    async def judge(uid, wav, **kw):
        checked.append(len(wav))
        return {"ok": False, "text": "", "fast": True}

    monkeypatch.setattr(phone_api, "judge_wake", judge)

    async def run():
        await w.feed(b"\0" * 32000, {"f": 1})
        await w.feed(_tone(1.0), {"f": 2})
        await w.feed(_tone(0.2), {"f": 7})   # часы пропустили тихие куски 3–6 — прошлая фраза кончилась
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert checked


def test_namaz_pause_and_resume(monkeypatch):
    w = _worn_watch()
    w._prayers = [{"k": "asr", "n": "Аср", "to": "ДО АСРА", "m": 15 * 60}]
    w._prayer_day = "fixed"
    monkeypatch.setattr(watch.Watch, "prayers", lambda self: _async(self._prayers))
    monkeypatch.setattr(watch.Watch, "_now_min", lambda self: 15 * 60 + 5)
    asyncio.run(w.tick())
    assert w.take() == [{"t": "pause", "reason": "namaz"}]
    monkeypatch.setattr(watch.Watch, "_now_min", lambda self: 15 * 60 + watch.NAMAZ_PAUSE_MIN + 1)
    asyncio.run(w.tick())
    assert w.take() == [{"t": "resume"}]


async def _async(value):
    return value


# ------------------------------------------------------------------ здоровье
def test_health_is_stored_and_described():
    asyncio.run(watch.health(7, {"sid": "s1", "hr": 72, "hr_rest": 58, "steps": 3400, "stress": 35, "spo2": 97, "bat": 64, "wear": 1,
                                 "sleeping": 0, "sleep": {"score": 78, "total": 370, "deep": 80, "start": 1400, "end": 330}}))
    line = watch.health_line(7)
    assert "пульс 72 (покоя 58)" in line and "шагов 3 400" in line and "сон 6 ч 10 мин (оценка 78)" in line and "заряд часов 64%" in line
    assert watch.sleep_line(7) == "⌚ Сон по часам: 6 ч 10 мин, глубокий 1 ч 20 мин, оценка 78"


def test_sleep_event_puts_phone_microphone_to_sleep():
    asyncio.run(watch.event(7, {"sid": "s1", "kind": "sys", "list": [{"t": 1, "ev": "health.sleep_status", "sleeping": 1}]}))
    assert phone_link.asleep(7) is True
    asyncio.run(watch.event(7, {"sid": "s1", "kind": "state", "wear": 1, "bat": 70, "sleeping": 0}))
    assert phone_link.asleep(7) is False


def test_new_watch_screen_resets_old_events():
    w = _worn_watch()
    w.push({"t": "say", "text": "старое"})
    w.touch({"sid": "s2"})
    assert w.take() == []


# ------------------------------------------------------------------ запись часов: «сырые» пакеты Opus (opus_demo)
def _framed(*packets: bytes) -> bytes:
    return b"".join(len(p).to_bytes(4, "big") + (0x00DD8220).to_bytes(4, "big") + p for p in packets)


def test_parse_frames_reads_opus_demo_records():
    packets = [bytes([0x48, 0x00, 0xB3, 0xAF]) + bytes(10), bytes([0x48, 0x80, 0x0A]) + bytes(25), b""]
    assert watch.parse_frames(_framed(*packets)) == packets


def test_parse_frames_keeps_whole_packets_of_a_cut_record():
    whole = _framed(bytes([0x48]) + bytes(20), bytes([0x48]) + bytes(30))
    assert len(watch.parse_frames(whole[:-5])) == 1          # хвост оборван — берём целый первый пакет
    assert watch.parse_frames(b"\x00\x00\x10\x00" + bytes(40)) == []   # длина > 1275 — это не наш формат


def test_audio_decodes_raw_opus_chunks(monkeypatch):
    got = {}

    def fake_decode(packets):
        got["n"] = len(packets)
        return b"\x01\x00" * 320 * len(packets)

    monkeypatch.setattr(watch, "decode_packets", fake_decode)
    pcm = asyncio.run(watch.opus_to_pcm(_framed(*[bytes([0x48]) + bytes(20)] * 5)))
    assert got["n"] == 5 and len(pcm) == 5 * 640
    with pytest.raises(watch.DecodeError):
        asyncio.run(watch.opus_to_pcm(b"garbage-not-opus"))


def _tone_pcm(seconds, amp):
    t = np.arange(int(seconds * 16000)) / 16000
    return (np.sin(2 * np.pi * 220 * t) * amp).astype(np.int16).tobytes()


def test_boost_lifts_quiet_speech_but_leaves_loud_alone():
    quiet = _tone_pcm(0.5, 2000)
    _, peak_q = watch.pcm_levels(watch.boost(quiet))
    assert 19000 < peak_q <= 20500                       # до пика 20000
    _, peak_vq = watch.pcm_levels(watch.boost(_tone_pcm(0.5, 600)))
    assert 7000 < peak_vq < 7400                         # очень тихо — не больше ×12
    loud = _tone_pcm(0.5, 18000)
    assert watch.boost(loud) == loud                      # уже громко — как есть


def test_agc_raises_a_quiet_stream_so_speech_is_still_found(monkeypatch):
    w = _worn_watch()
    quiet = _tone_pcm(0.6, 1200)
    out = w._agc(quiet)
    assert watch.pcm_levels(out)[1] > watch.pcm_levels(quiet)[1] * 3

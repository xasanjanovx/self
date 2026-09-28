"""28.09 (вечер): быстрый экономный режим (мгновенные команды), «включи лайв режим», строгий «Джес» на видео, чистый банк голоса,
«поставь музыку» больше не падает."""
import asyncio
import json

from bot import instant, phone, voiceprint, wakeword


def test_instant_live_mode_and_music():
    assert instant.parse("включи лайв режим").tool == "live_mode" and instant.parse("включи лайв режим").args == {"on": True}
    assert instant.parse("включи быстрый режим").args == {"on": True}
    assert instant.parse("выключи лайв режим").args == {"on": False}
    cmd = instant.parse("поставь музыку беном")
    assert cmd.tool == "play_media" and cmd.args == {"query": "беном", "kind": "music"}
    assert instant.parse("включи видео про котов").args["kind"] == "video"
    assert instant.parse("включи музыку").tool == "media"                  # продолжить — как раньше
    assert instant.parse("поставь будильник на семь").tool == "set_alarm"  # не музыка
    assert instant.parse("включи режим не беспокоить") is None and instant.parse("включи камеру") is None


def test_play_media_action_has_kind_field():
    """28.09: _action(turn, "play", kind=…) падал TypeError — 12 раз за вечер, каждая просьба 10 с."""
    turn = phone.PhoneTurn(uid=1, device={})
    res = phone._action(turn, "play", query="Benom", kind="music", video_id="abc")
    assert res["ok"] and turn.actions == [{"type": "play", "query": "Benom", "kind": "music", "video_id": "abc"}]


def test_zhest_counts_as_name_only_first():
    """«…мышки жёст за…» из видео прошло как «Джес» и заказало такси."""
    assert wakeword.match_at("тележаеват мышки жёст за марсему") == (False, "", -1)
    assert wakeword.match_at("жес позвони маме") == (True, "позвони маме", 0)
    assert wakeword.match_at("а джес позвони маме") == (True, "позвони маме", 1)


def test_clean_bank_drops_foreign_voices(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import numpy as np

    vp = np.zeros(8, dtype=np.float32)
    vp[0] = 1.0
    his, other = [0.9, 0.43, 0, 0, 0, 0, 0, 0], [0.3, 0.95, 0, 0, 0, 0, 0, 0]
    voiceprint._file(5).write_text(json.dumps({"vp": vp.tolist(), "threshold": 0.3, "bank": [his, other]}), encoding="utf-8")
    assert voiceprint.clean_bank(5) == (2, 1)
    assert json.loads(voiceprint._file(5).read_text(encoding="utf-8"))["bank"] == [his]


def test_cheap_phone_instant_command_skips_model(monkeypatch):
    """Экономный режим: «позвони маме» — без модели, через локальный распознаватель (~0.1 с)."""
    from bot import phone_cheap
    from bot.context import ai

    async def check(wav):  # noqa: ANN001
        return {"text": "джес позвони маме", "name": True, "after": "позвони маме", "pos": 0, "ms": 90}

    async def exec_tool(sess, name, args):  # noqa: ANN001
        sess.done = (name, args)
        return {"ok": True}

    async def agent_step(*a, **kw):  # noqa: ANN002, ANN003
        raise AssertionError("модель не нужна")

    sent = []

    class Fake:
        profile = type("P", (), {"now": __import__("datetime").datetime(2026, 9, 28, 22, 0), "telegram_id": 1})()
        turn = phone.PhoneTurn(uid=1, device={})
        user_lines, log, contents, jarvis_lines = [], [], [], []
        turns = 0
        upgrade = None

        async def to_phone(self, payload):  # noqa: ANN001
            sent.append(payload)

        def _stamp(self):
            return "[t]"

    monkeypatch.setattr(wakeword, "check", check)
    monkeypatch.setattr(phone_cheap, "exec_tool", exec_tool)
    monkeypatch.setattr(ai, "agent_step", agent_step)
    fake = Fake()
    ok = asyncio.run(phone_cheap.PhoneCheap._try_instant(fake, b"RIFF"))
    assert ok and fake.done == ("phone_call", {"who": "маме", "variants": []})
    assert {"type": "turn_complete"} in sent

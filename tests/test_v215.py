"""29.09 (2.15): звонок — записанные фразы без модели, «Секунду, сэр» перед поиском, «перебил» только по звучащему голосу,
JES знает, на какой модели работает."""
import asyncio

from bot import cheap_voice, live_call, persona, wakeword
from bot.ai import AgentStep
from bot.profile import Profile


def _profile() -> Profile:
    return Profile(telegram_id=1, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Тест", username="t")


def test_quick_phrases_match_whole_reply_only():
    assert cheap_voice.quick_kind("Алло") == "hear"
    assert cheap_voice.quick_kind("алло алло") == "hear"
    assert cheap_voice.quick_kind("ты меня слышишь?") == "hear"
    assert cheap_voice.quick_kind("Спасибо большое!") == "thanks"
    assert cheap_voice.quick_kind("рахмат") == "thanks"
    assert cheap_voice.quick_kind("ну пока") == "bye"
    assert cheap_voice.quick_kind("пока не надо") is None          # не прощание
    assert cheap_voice.quick_kind("алло позвони маме") is None
    assert cheap_voice.quick_kind("") is None


def test_phrasebook_has_sir_and_chief_and_uzbek_when_mirror():
    texts = cheap_voice.phrase_texts(persona.Persona(lang="ru", mirror=True))
    assert "Да, сэр, слышу вас хорошо." in texts and "Секунду, шеф." in texts and "Ha, ser, eshitaman." in texts
    assert len(texts) == len(set(texts))


def _session(monkeypatch, *, steps=None, heard="алло"):  # noqa: ANN001, ANN202
    from bot.context import ai

    models: list = []

    async def agent_step(contents, **kw):  # noqa: ANN001, ANN003
        models.append(contents[-1])
        return steps.pop(0)

    async def speak(text, voice="Kore"):  # noqa: ANN001
        yield b"\x01\x00" * 2400

    async def transcribe(data, mime, prompt=None):  # noqa: ANN001
        return "какая завтра погода"

    async def check(wav):  # noqa: ANN001
        return {"text": heard, "name": False, "after": heard, "pos": -1, "ms": 60}

    monkeypatch.setattr(ai, "agent_step", agent_step)
    monkeypatch.setattr(ai, "speak_stream", speak)
    monkeypatch.setattr(ai, "transcribe_audio", transcribe)
    monkeypatch.setattr(wakeword, "check", check)
    sess = cheap_voice.CheapSession(_profile(), persona.Persona(voice="Sulafat"), mode="assistant", system="x",
                                    decls=live_call.tool_declarations("assistant"))
    return sess, models


def test_hello_is_answered_by_recorded_phrase_without_model(monkeypatch):
    sess, models = _session(monkeypatch, steps=[])
    asyncio.run(sess.on_phrase(b"\x00\x10" * 12000))
    assert models == [] and len(sess.out) == 4800                 # ответили записанной фразой, модель не звали
    assert sess.result.transcript[0] == "он: алло" and sess.result.transcript[1].startswith("я: ")
    assert sess.contents[-1]["role"] == "model"                   # модель потом знает, что уже ответили
    assert not sess.hang_after_turn


def test_goodbye_hangs_up_after_recorded_phrase(monkeypatch):
    sess, models = _session(monkeypatch, steps=[], heard="пока")
    asyncio.run(sess.on_phrase(b"\x00\x10" * 12000))
    assert models == [] and sess.hang_after_turn


def test_long_phrase_goes_to_model_and_slow_tool_gets_filler(monkeypatch):
    steps = [AgentStep(parts=[{"functionCall": {"name": "weather", "args": {}}}], text="", calls=[("weather", {})], finish="STOP"),
             AgentStep(parts=[{"text": "Завтра тепло."}], text="Завтра тепло.", calls=[], finish="STOP")]
    sess, models = _session(monkeypatch, steps=steps, heard="какая завтра погода")

    async def run_one(call):  # noqa: ANN001
        assert bytes(sess.out[:4]) == b"\x05\x00\x05\x00"         # «Секунду, сэр» уже звучит, пока инструмент работает
        return {"response": {"ok": True}}

    monkeypatch.setattr(sess, "_run_one", run_one)
    monkeypatch.setattr(sess, "recorded", lambda kind: b"\x05\x00" * 100 if kind == "wait" else None)
    asyncio.run(sess.on_phrase(b"\x00\x10" * 60000))               # 2.5 с — не короткая фраза
    assert len(models) == 2 and len(sess.out) == 200 + 4800


def test_barge_in_only_while_jes_is_audible(monkeypatch):
    sess, _ = _session(monkeypatch, steps=[])
    interrupts: list[int] = []
    monkeypatch.setattr(sess, "interrupt", lambda: interrupts.append(1))
    loud = b"\x00\x30" * 2400                                      # 0.1 с громкой речи

    async def feed(audible: bool) -> None:
        q: asyncio.Queue = asyncio.Queue()
        for _ in range(15):
            q.put_nowait(loud)
        sess.speaking = True                                       # ответ ещё озвучивается…
        if audible:
            sess.out.extend(b"\x01\x00" * 48000)                   # …или уже звучит в трубке
        task = asyncio.create_task(sess.listen(q))
        await asyncio.sleep(0.2)
        sess.stop.set()
        await task
        sess.stop.clear()

    asyncio.run(feed(False))
    assert interrupts == []                                        # «алло?» в тишину ответ не отменяет
    asyncio.run(feed(True))
    assert interrupts                                              # говорит поверх звучащего ответа — перебил


def test_engine_line_tells_model_and_money():
    phone = live_call.system_instruction(_profile(), persona.Persona(lang="ru"), mode="phone")
    assert "ТВОЙ РЕЖИМ СЕЙЧАС" in phone and "Gemini 3.8 Live" in phone and "БЕЗ поиска" in phone
    cheap = live_call.system_instruction(_profile(), persona.Persona(lang="ru"), mode="assistant", engine="cheap")
    assert "экономный режим" in cheap and "flash-lite-tts" in cheap and "Gemini 3.8 Live" not in cheap


def test_settings_show_who_answers_now(monkeypatch):
    from bot import billing
    from bot.handlers.settings import engine_lines

    monkeypatch.setattr(billing, "spent_today", lambda: 0.33)
    monkeypatch.setattr(billing, "live_allowed", lambda mode="phone": True)
    p = persona.Persona(voice="Sulafat")
    p.call_mode = "economy"
    text = "\n".join(engine_lines(_profile(), p))
    assert "⚡ Gemini 3.8 Live" in text and "🌿" in text and "Sulafat" in text and "$0.33" in text
    monkeypatch.setattr(billing, "live_allowed", lambda mode="phone": False)   # лимит дня — телефон тоже экономно
    assert "включи лайв режим" in "\n".join(engine_lines(_profile(), p))


def test_repeat_while_thinking_is_not_answered_twice(monkeypatch):
    assert cheap_voice.is_repeat("сколько сегодня потрачено на ИИ", "Сколько потрачено на ИИ сегодня?")
    assert not cheap_voice.is_repeat("а какая погода завтра", "Сколько потрачено на ИИ сегодня?")
    steps = [AgentStep(parts=[{"text": "Сегодня $0.33, сэр."}], text="Сегодня $0.33, сэр.", calls=[], finish="STOP")]
    sess, models = _session(monkeypatch, steps=steps, heard="-")
    sess.last_heard = "Сколько потрачено на ИИ сегодня?"

    async def transcribe(data, mime, prompt=None):  # noqa: ANN001
        return "сколько сегодня потрачено на ИИ"

    from bot.context import ai

    monkeypatch.setattr(ai, "transcribe_audio", transcribe)
    asyncio.run(sess.on_phrase(b"\x00\x10" * 60000, overlapped=True))   # начал, пока JES думала
    assert models == [] and not sess.out and "он (повтор)" in sess.result.transcript[-1]


def test_phone_and_call_repeat_guard():
    g = live_call.RepeatGuard()
    assert not g.repeat("сколько денег потрачено на ИИ сегодня")
    assert g.repeat("сколько потрачено на ИИ сегодня и сколько из этого потратила Джес")   # 29.09 из лога
    assert not g.repeat("поставь будильник на шесть")


def test_fast_mode_by_voice_really_switches(monkeypatch, tmp_path):
    from bot import agent_tools, services
    from bot.agent_tools import ToolContext

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    saved: list = []
    monkeypatch.setattr(services, "save_persona_extra", lambda uid, fields: saved.append(fields))
    res = asyncio.run(agent_tools.run("update_settings", {"jes_mode": "live"}, ToolContext(profile=_profile(), text="")))
    assert saved == [{"voice_mode": "live", "call_mode": "live"}] and res.get("changed", res).get("jes_mode", "live") == "live"

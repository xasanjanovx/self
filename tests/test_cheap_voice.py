"""28.09: экономный голос в звонке Telegram (будильник — всегда, звонки — по выбору) и облегчённый Live."""
import asyncio

from bot import cheap_voice, live_call, persona
from bot.ai import AgentStep
from bot.profile import Profile


def _profile() -> Profile:
    return Profile(telegram_id=1, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Тест", username="t")


def test_wanted_modes():
    p = persona.Persona()
    assert cheap_voice.wanted("wake", p)                       # будильник — всегда бесплатно
    assert not cheap_voice.wanted("assistant", p)              # звонок — облегчённый Live по умолчанию
    p.call_mode = "economy"
    assert cheap_voice.wanted("assistant", p)
    assert not cheap_voice.wanted("phone", p)


def test_echo_of_own_answer_is_not_a_reply():
    assert cheap_voice.is_echo("доброе утро шеф проснулись", "Доброе утро, шеф! Проснулись?")
    assert not cheap_voice.is_echo("да встал уже", "Доброе утро, шеф! Проснулись?")
    assert not cheap_voice.is_echo("", "что-то")


def test_turn_speaks_runs_tools_and_hangs_up_after_goodbye(monkeypatch):
    from bot import ai as ai_mod
    from bot.context import ai

    steps = [AgentStep(parts=[{"functionCall": {"name": "confirm_awake", "args": {}}}], text="", calls=[("confirm_awake", {})], finish="STOP"),
             AgentStep(parts=[{"functionCall": {"name": "end_call", "args": {}}}], text="", calls=[("end_call", {})], finish="STOP"),
             AgentStep(parts=[{"text": "Отлично, шеф! Хорошего дня."}], text="Отлично, шеф! Хорошего дня.", calls=[], finish="STOP")]
    seen_free: list = []

    async def agent_step(contents, **kw):  # noqa: ANN001, ANN003
        seen_free.append(ai_mod._free_mode.get())
        return steps.pop(0)

    async def speak(text, voice="Kore"):  # noqa: ANN001
        yield b"\x01\x00" * 2400

    async def transcribe(data, mime, prompt=None):  # noqa: ANN001
        return "да встал уже"

    monkeypatch.setattr(ai, "agent_step", agent_step)
    monkeypatch.setattr(ai, "speak_stream", speak)
    monkeypatch.setattr(ai, "transcribe_audio", transcribe)
    monkeypatch.setattr(ai_mod, "free_tts_ready", lambda: True)  # квота бесплатной озвучки есть — голос Google

    async def run():
        sess = cheap_voice.CheapSession(_profile(), persona.Persona(), mode="wake", system="x",
                                        decls=live_call.tool_declarations("wake"))
        await sess.on_phrase(b"\x00\x10" * 24000)
        return sess

    sess = asyncio.run(run())
    assert sess.result.confirmed                                 # confirm_awake
    assert sess.hang_after_turn and not sess.hangup_after_speech  # трубку — когда договорит прощание, а не сразу
    assert len(sess.out) == 4800                                 # прощание озвучено
    assert "он: да встал уже" in sess.result.transcript and "я: Отлично, шеф! Хорошего дня." in sess.result.transcript
    assert seen_free and all(m is None for m in seen_free)       # 29.09 «быстро»: ходы — сразу платным ключом
    # в историю — расшифровка, а не звук
    assert all("inline_data" not in p for m in sess.contents for p in m["parts"])


def test_no_reply_to_noise_or_echo(monkeypatch):
    from bot.context import ai

    async def agent_step(contents, **kw):  # noqa: ANN001, ANN003
        return AgentStep(parts=[{"text": "Слушаю!"}], text="Слушаю!", calls=[], finish="STOP")

    async def transcribe(data, mime, prompt=None):  # noqa: ANN001
        return ""

    monkeypatch.setattr(ai, "agent_step", agent_step)
    monkeypatch.setattr(ai, "transcribe_audio", transcribe)

    async def run():
        sess = cheap_voice.CheapSession(_profile(), persona.Persona(), mode="assistant", system="x", decls=[])
        await sess.on_phrase(b"\x00\x10" * 12000)
        return sess

    sess = asyncio.run(run())
    assert not sess.out and not any(t.startswith("я:") for t in sess.result.transcript)


def test_light_live_call_prompt_is_small():
    text = live_call.system_instruction(_profile(), persona.Persona(), mode="assistant", memory="ПАМЯТЬ: брат Алишер\n\nНЕДАВНИЕ РЕПЛИКИ\nон: привет")
    assert "О СЕБЕ" not in text and "НЕДАВНИЕ РЕПЛИКИ" not in text and "брат Алишер" in text
    assert len(text) < 5000
    import json

    tools = json.dumps(live_call.tool_declarations("assistant"), ensure_ascii=False)
    assert len(tools) < 3500


def test_voice_is_always_jes_google_microsoft_only_if_google_fails(monkeypatch):
    """29.09: квота бесплатной озвучки кончилась — ответил голос Microsoft («мужик какой-то»). Теперь всегда голос JES."""
    from bot import free_voice
    from bot.context import ai

    used = []
    google_ok = {"on": True}

    async def speak(text, voice="Kore"):  # noqa: ANN001
        used.append(("google", voice))
        if google_ok["on"]:
            yield bytes(20)

    async def ms(text, lang=None):  # noqa: ANN001
        used.append(("microsoft", None))
        return bytes(20)

    monkeypatch.setattr(ai, "speak_stream", speak)
    monkeypatch.setattr(free_voice, "synthesize", ms)
    monkeypatch.setattr(free_voice, "available", lambda: True)
    sess = cheap_voice.CheapSession(_profile(), persona.Persona(voice="Sulafat"), mode="assistant", system="x", decls=[])
    asyncio.run(sess._speak("Готово, сэр."))
    assert used == [("google", "Sulafat")]
    used.clear()
    google_ok["on"] = False                     # Google не ответил вовсе — тогда Microsoft, чтобы не молчать
    asyncio.run(sess._speak("Готово, сэр."))
    assert used == [("google", "Sulafat"), ("microsoft", None)]


def test_echo_over_own_answer_never_reaches_the_model(monkeypatch):
    """Фраза началась поверх ответа JES: «Записала сорок тысяч» эхом из трубки не должно записаться второй раз."""
    from bot.context import ai

    calls = []

    async def agent_step(contents, **kw):  # noqa: ANN001, ANN003
        calls.append(contents[-1])
        return AgentStep(parts=[{"text": "Хорошо."}], text="Хорошо.", calls=[], finish="STOP")

    async def transcribe(data, mime, prompt=None):  # noqa: ANN001
        return "записала сорок тысяч на обед"

    async def speak(text, voice="Kore"):  # noqa: ANN001
        yield bytes(20)

    monkeypatch.setattr(ai, "agent_step", agent_step)
    monkeypatch.setattr(ai, "transcribe_audio", transcribe)
    monkeypatch.setattr(ai, "speak_stream", speak)

    async def run():
        sess = cheap_voice.CheapSession(_profile(), persona.Persona(), mode="assistant", system="x", decls=[])
        sess.last_said = "Записала сорок тысяч на обед."
        await sess.on_phrase(bytes(4800), overlapped=True)
        return sess

    sess = asyncio.run(run())
    assert calls == [] and not sess.out


def test_wake_call_never_answers_his_words_with_silence(monkeypatch):
    """03.10 тест будильника: он что-то сказал, модель ответила «-», в трубке тишина. На подъёме — мягкий переспрос (не чаще раза в 10 с)."""
    from bot.context import ai

    async def agent_step(contents, **kw):  # noqa: ANN001, ANN003
        return AgentStep(parts=[{"text": "-"}], text="-", calls=[], finish="STOP")

    async def transcribe(data, mime, prompt=None):  # noqa: ANN001
        return ""

    async def speak(text, voice="Kore"):  # noqa: ANN001
        yield b"\x01\x00" * 2400

    monkeypatch.setattr(ai, "agent_step", agent_step)
    monkeypatch.setattr(ai, "transcribe_audio", transcribe)
    monkeypatch.setattr(ai, "speak_stream", speak)

    async def run(mode):  # noqa: ANN001, ANN202
        sess = cheap_voice.CheapSession(_profile(), persona.Persona(lang="ru"), mode=mode, system="x", decls=[])
        await sess.on_phrase(b"\x00\x10" * 12000)
        first = list(sess.result.transcript)
        sess.out.clear()
        await sess.on_phrase(b"\x00\x10" * 12000)   # сразу ещё раз — повторно не дёргаем
        return first, list(sess.result.transcript)

    first, both = asyncio.run(run("wake"))
    assert len(first) == 1 and first[0].startswith("я: ") and ("Не расслышала" in first[0] or "слышите" in first[0])
    assert both == first                                    # второй раз в пределах 10 с — молчим
    first, _ = asyncio.run(run("assistant"))
    assert first == []                                      # в обычном звонке шум по-прежнему игнорируется


def test_wake_reask_is_silent_for_echo_and_after_confirmation():
    sess = cheap_voice.CheapSession(_profile(), persona.Persona(lang="ru"), mode="wake", system="x", decls=[])
    sess.last_said = "Доброе утро, шеф! Проснулись?"
    assert sess.wake_reask("доброе утро шеф проснулись") == ""     # эхо её же слов
    assert sess.wake_reask("") != ""                                # не расслышала — переспросить
    sess2 = cheap_voice.CheapSession(_profile(), persona.Persona(lang="uz"), mode="wake", system="x", decls=[])
    said = sess2.wake_reask("")
    assert any(w in said for w in ("Eshitolmadim", "eshityapsizmi"))
    sess3 = cheap_voice.CheapSession(_profile(), persona.Persona(), mode="wake", system="x", decls=[])
    sess3.result.confirmed = True
    assert sess3.wake_reask("") == ""                                # подъём уже подтверждён


def test_wake_prompt_does_not_invite_weather_chatter():
    text = live_call.system_instruction(_profile(), persona.Persona(lang="ru"), mode="wake",
                                        wake={"takbir": "05:18", "minutes_left": 8, "quiz": "ВОПРОС 1: …", "today": ""})
    assert "инструмент weather" not in text                 # 03.10: «Погода в Андижане сегодня отличная» — выдумка вместо подъёма
    assert "Не расслышала" in text and "Молчать в ответ на его слова нельзя" in text

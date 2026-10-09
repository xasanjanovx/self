"""06.10: подъём в звонке — настоящий CheapSession с ходом из bot/wake_dialog.py, поддельные только звук (STT/TTS) и оценщик ответа.
Болтливая модель на подъёме больше не говорит: agent_step здесь падает, если его позвать."""
from __future__ import annotations

import asyncio
from datetime import date

from bot import cheap_voice, islam_quiz, persona, wake_dialog
from bot.profile import Profile


def _profile() -> Profile:
    return Profile(telegram_id=1, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Тест", username="t")


class _Wake:
    def __init__(self, monkeypatch, tmp_path, *, verdicts=(), heard=(), lang="ru", done=0, minutes_left=25):  # noqa: ANN001
        from bot import wakeword
        from bot.context import ai

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        self.qs = islam_quiz.for_day_set(1, date(2026, 10, 6))
        self.verdicts, self.heard, self.prompts = list(verdicts), list(heard), []
        self.quick = {"text": ""}

        async def generate_json(prompt, **kw):  # noqa: ANN001, ANN003
            self.prompts.append(prompt)
            v = self.verdicts.pop(0)
            if isinstance(v, Exception):
                raise v
            return {"verdict": v}

        async def agent_step(*a, **k):  # noqa: ANN002, ANN003
            raise AssertionError("на подъёме болтливая модель не участвует")

        async def transcribe(data, mime, prompt=None):  # noqa: ANN001
            return self.heard.pop(0) if self.heard else ""

        async def speak(text, voice="Kore"):  # noqa: ANN001
            yield b"\x01\x00" * 2400

        async def check(wav):  # noqa: ANN001
            return self.quick

        monkeypatch.setattr(ai, "generate_json", generate_json)
        monkeypatch.setattr(ai, "agent_step", agent_step)
        monkeypatch.setattr(ai, "transcribe_audio", transcribe)
        monkeypatch.setattr(ai, "speak_stream", speak)
        monkeypatch.setattr(wakeword, "check", check)
        self.sess = cheap_voice.CheapSession(_profile(), persona.Persona(lang=lang), mode="wake", system="x", decls=[])
        if done:
            islam_quiz.set_progress(1, date(2026, 10, 6), done)
        # тот же сборщик, что в боевом звонке: оценки и пройденное пишутся в islam_quiz
        self.sess.flow = cheap_voice.wake_flow(_profile(), persona.Persona(lang=lang), {"day": date(2026, 10, 6), "quiz_list": self.qs,
                                                                                       "minutes_left": minutes_left, "attempt": 1})
        assert isinstance(self.sess.flow, wake_dialog.WakeFlow)

    async def say(self, *, overlapped: bool = False) -> str:
        """Он что-то говорит (1 с звука) — вернуть, что в ответ прозвучало."""
        before = len(self.sess.result.transcript)
        self.sess.out.clear()
        await self.sess.on_phrase(b"\x00\x10" * 24000, overlapped=overlapped)
        return " ".join(t for t in self.sess.result.transcript[before:] if t.startswith("я: "))


def test_noise_gets_no_answer_and_no_hear_question(monkeypatch, tmp_path):
    """06.10: на шум и вздох JES больше не переспрашивает «вы меня слышите?» — тишиной занимается лестница подъёма."""
    w = _Wake(monkeypatch, tmp_path, heard=[""])
    assert asyncio.run(w.say()) == "" and not w.sess.out and not w.prompts


def test_three_questions_one_by_one_then_confirm_without_any_chat_model(monkeypatch, tmp_path):
    w = _Wake(monkeypatch, tmp_path, verdicts=["correct", "wrong", "correct", "up"],
              heard=["Алло, да, проснулся", "Пять: шахада, намаз, закят, пост и хадж", "Четыре ракаата", "Абу Бакр", "да, встал"])

    async def run():
        return [await w.say() for _ in range(5)]

    first, second, third, fourth, last = asyncio.run(run())
    assert "Первый вопрос" in first and w.qs[0].q in first and first.startswith(("я: Хорошо", "я: Отлично"))
    assert second.startswith(("я: Верно", "я: Правильно", "я: Молодец")) and "Второй вопрос" in second and w.qs[1].q in second
    assert "Не совсем" in third and w.qs[1].a in third and "Последний вопрос" in third and w.qs[2].q in third
    assert "встали?" in fourth and w.sess.flow.stage == "done"
    assert w.sess.result.confirmed and w.sess.hang_after_turn and "Отлично, сэр!" in last and "примет ваш намаз" in last
    assert len(w.prompts) == 4                                                    # оценщик — только на вопросах и на «встали ли»
    assert w.qs[0].q in w.prompts[0] and "Пять: шахада" in w.prompts[0] and "Четыре ракаата" in w.prompts[1]
    assert islam_quiz.progress(1, date(2026, 10, 6)) == 3 and islam_quiz._load(1)["missed"] == [w.qs[1].id]


def test_hurry_is_decided_without_the_grader(monkeypatch, tmp_path):
    w = _Wake(monkeypatch, tmp_path, heard=["да", "хватит, я встал, давай всё", "ну всё, встал, пока"])

    async def run():
        await w.say()
        first = await w.say()
        assert not w.sess.result.confirmed and w.sess.flow.idx == 0 and w.qs[0].q in first and "Ещё минуту" in first
        return await w.say()

    last = asyncio.run(run())
    assert w.sess.result.confirmed and w.sess.hang_after_turn and "примет ваш намаз" in last and not w.prompts


def test_hello_gets_hear_answer_and_the_question_without_the_model(monkeypatch, tmp_path):
    w = _Wake(monkeypatch, tmp_path)
    w.quick = {"text": "алло"}
    spoken = asyncio.run(w.say())
    assert "слышу вас" in spoken and "Первый вопрос" in spoken and not w.sess.flow.hear_used and not w.prompts


def test_time_question_is_answered_by_the_clock_without_the_model(monkeypatch, tmp_path):
    w = _Wake(monkeypatch, tmp_path, heard=["да", "который час?"])

    async def run():
        await w.say()
        return await w.say()

    said = asyncio.run(run())
    assert said.startswith("я: Сейчас ") and w.qs[0].q in said and w.sess.flow.idx == 0 and not w.prompts


def test_silence_asks_questions_and_hear_only_once(monkeypatch, tmp_path):
    w = _Wake(monkeypatch, tmp_path)

    async def run():
        out = []
        for _ in range(12):
            before = len(w.sess.result.transcript)
            await w.sess.nudge_once()
            out.append(" ".join(w.sess.result.transcript[before:]))
        return out

    said = asyncio.run(run())
    assert "Первый вопрос" in said[0] and w.qs[0].q in said[0]
    assert sum("слышите" in t for t in said) == 1
    assert all(t.startswith("я: ") for t in said)


def test_asking_to_sleep_is_refused_once_then_snoozed(monkeypatch, tmp_path):
    w = _Wake(monkeypatch, tmp_path, heard=["да", "дай поспать ещё минутку", "ещё 10 минут пожалуйста"])

    async def run():
        await w.say()
        first = await w.say()
        assert "намаз лучше сна" in first and not w.sess.hang_after_turn
        return await w.say()

    last = asyncio.run(run())
    assert w.sess.result.snooze_minutes == 5 and w.sess.hang_after_turn and not w.sess.result.confirmed and "позвоню через 5 минут" in last


def test_grader_down_still_teaches_and_final_needs_a_clear_word(monkeypatch, tmp_path):
    w = _Wake(monkeypatch, tmp_path, verdicts=[RuntimeError("503"), RuntimeError("503"), RuntimeError("503"), "other", RuntimeError("503")],
              heard=["да", "ответ", "ответ", "ответ", "угу", "я уже встал"], minutes_left=25)

    async def run():
        await w.say()
        second = await w.say()
        await w.say()
        fourth = await w.say()
        assert "встали?" in fourth and w.sess.flow.stage == "final"
        not_clear = await w.say()                                                  # оценщик ответил «other» на «угу» — переспрашиваем
        assert not w.sess.result.confirmed and "скажите «встал»" in not_clear
        return second, await w.say()                                               # оценщик упал, но «я уже встал» — ясное слово

    second, last = asyncio.run(run())
    assert "Принято." in second and w.qs[0].a in second and islam_quiz._load(1).get("missed", []) == []     # ошибкой не засчитано
    assert w.sess.result.confirmed and w.sess.hang_after_turn


def test_short_answer_repeating_the_question_words_is_not_echo(monkeypatch, tmp_path):
    w = _Wake(monkeypatch, tmp_path)
    w.sess.last_said = "Второй вопрос, сэр: Какой пророк построил ковчег?"
    assert not w.sess._is_echo("Нух построил ковчег")                           # ответ с теми же словами — не эхо
    assert w.sess._is_echo("второй вопрос сэр какой пророк построил ковчег")   # а вот дословный возврат голоса — эхо


def test_repeat_of_the_same_answer_while_thinking_is_not_graded_twice(monkeypatch, tmp_path):
    w = _Wake(monkeypatch, tmp_path, verdicts=["correct"], heard=["да", "Абу Бакр ас-Сиддик", "Абу Бакр ас-Сиддик"])

    async def run():
        await w.say()
        await w.say()
        before = len(w.prompts)
        again = await w.say(overlapped=True)                                       # повторил ответ, пока JES думала
        return before, again

    before, again = asyncio.run(run())
    assert again == "" and len(w.prompts) == before == 1 and w.sess.flow.idx == 1


def test_uzbek_wake_speaks_uzbek_around_the_russian_question(monkeypatch, tmp_path):
    w = _Wake(monkeypatch, tmp_path, lang="uz", verdicts=["correct"], heard=["ha", "besh"])

    async def run():
        first = await w.say()
        return first, await w.say()

    first, second = asyncio.run(run())
    assert "Birinchi savol" in first and "Ikkinchi savol" in second and w.qs[1].q in second


def test_quiet_speech_is_rescued_by_the_second_soft_transcription(monkeypatch, tmp_path):
    """07.10 05:12: «Вы встали?» — он что-то сказал, расшифровка вернула пусто, JES промолчал 13 секунд, и он повесил трубку."""
    w = _Wake(monkeypatch, tmp_path, verdicts=["correct"], heard=["да", "", "Абу Бакр"], done=2)

    async def run():
        await w.say()
        return await w.say()                                                       # первая расшифровка пуста, вторая (мягкая) услышала

    said = asyncio.run(run())
    assert w.prompts and "Абу Бакр" in w.prompts[0] and "встали?" in said and w.sess.flow.stage == "final"


def test_unreadable_speech_asks_to_repeat_once_and_keeps_the_recording(monkeypatch, tmp_path):
    w = _Wake(monkeypatch, tmp_path, heard=["да", "", "", "", ""])

    async def run():
        await w.say()
        first = await w.say()                                                      # речь 1 с, слов нет даже со второго раза
        second = await w.say()                                                     # сразу же ещё раз — не засыпаем «не разобрала»
        return first, second

    first, second = asyncio.run(run())
    assert first.startswith("я: Не разобрала.") and w.qs[0].q in first and "слышите" not in first
    assert second == "" and not w.prompts
    dumps = list((tmp_path / "wake_dump").glob("*.wav"))
    assert dumps                                                                   # запись сохранена — можно послушать, что он говорил
    w.sess._unheard_at = -1e9                                                      # прошло 12 с — можно ещё раз
    third = asyncio.run(w.say())
    # 09.10: второй раз подряд слов не разобрать — не зацикливаемся на одном вопросе: принимаем, называем ответ, идём дальше
    assert third.startswith("я: Принято.") and w.qs[0].a in third and "Второй вопрос" in third


def test_short_click_is_not_speech(monkeypatch, tmp_path):
    w = _Wake(monkeypatch, tmp_path, heard=["да", ""])

    async def run():
        await w.say()
        before = len(w.sess.result.transcript)
        await w.sess.on_phrase(b"\x00\x10" * 4000)                                 # 0.17 с — щелчок
        return w.sess.result.transcript[before:]

    assert asyncio.run(run()) == []


# ------------------------------------------------------------------ 09.10: модель слушает запись и сразу оценивает
def _listen(monkeypatch, w, answers):  # noqa: ANN001
    """ai.generate (модель слушает звук) вернёт по очереди answers — словари {"text", "verdict"} или исключение."""
    from bot.context import ai

    seen: list = []

    async def generate(parts, **kw):  # noqa: ANN001, ANN003
        import json

        seen.append(parts)
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return json.dumps(a, ensure_ascii=False)

    monkeypatch.setattr(ai, "generate", generate)
    return seen


def test_answer_is_heard_and_graded_in_one_call_with_the_expected_answer(monkeypatch, tmp_path):
    w = _Wake(monkeypatch, tmp_path, heard=["да, проснулся"])      # приветствие — обычная расшифровка
    seen = _listen(monkeypatch, w, [{"text": "Abu Tolib", "verdict": "correct"}])

    async def run():
        await w.say()                          # приветствие → первый вопрос
        return await w.say()                   # ответ на первый вопрос — слушает модель

    second = asyncio.run(run())
    assert second.startswith(("я: Верно", "я: Правильно", "я: Молодец"))
    assert not w.prompts                                       # отдельная оценка по тексту не понадобилась
    prompt = seen[0][0]["text"]
    assert w.qs[0].q in prompt and w.qs[0].a in prompt and "Abu Tolib" in prompt    # модель знает, какой ответ ждём
    assert seen[0][1]["inline_data"]["mime_type"] == "audio/wav"
    assert "он: Abu Tolib" in w.sess.result.transcript


def test_unsure_listener_means_repeat_not_mistake_and_two_in_a_row_move_on(monkeypatch, tmp_path):
    w = _Wake(monkeypatch, tmp_path, heard=["да"])
    _listen(monkeypatch, w, [{"text": "obuvnoy", "verdict": "other"}, {"text": "pe sho", "verdict": "other"}])

    async def run():
        await w.say()
        first = await w.say()
        second = await w.say()
        return first, second

    first, second = asyncio.run(run())
    assert "Не разобрала" in first and w.qs[0].q in first and w.sess.flow.idx == 1 or "Не разобрала" in first
    assert "Принято" in second and w.qs[0].a in second and "Второй вопрос" in second    # дважды не разобрали — идём дальше, ошибкой не считаем
    assert islam_quiz._load(1).get("missed", []) == []


def test_listener_down_falls_back_to_transcription_and_text_grade(monkeypatch, tmp_path):
    w = _Wake(monkeypatch, tmp_path, verdicts=["correct"], heard=["да", "Абу Талиб"])
    _listen(monkeypatch, w, [RuntimeError("503")])

    async def run():
        await w.say()
        return await w.say()

    second = asyncio.run(run())
    assert second.startswith(("я: Верно", "я: Правильно", "я: Молодец")) and len(w.prompts) == 1


def test_wake_call_listens_more_sensitively_than_a_normal_call():
    from bot.phone_cheap import Segmenter

    def tone(rms: int) -> bytes:
        return b"".join(int(rms * (1 if i % 2 else -1)).to_bytes(2, "little", signed=True) for i in range(480))   # 20 мс @ 24 кГц

    normal = Segmenter(24000, silence_ms=700)
    wake = Segmenter(24000, silence_ms=700, min_rms=110.0, factor=3.0, floor=80.0, floor_min=25.0, floor_max=120.0)
    for seg in (normal, wake):
        for _ in range(200):                        # 4 с тихой комнаты: шум ~20 — порог подъёма опускается к минимуму
            seg.feed(tone(20))
        for _ in range(25):                         # сонный шёпот ~150 rms
            seg.feed(tone(150))
    assert not normal.active                        # обычный звонок такой шёпот пропускает (порог 350)
    assert wake.active and wake.peak_rms > 140      # подъём его слышит
    assert "пик" in wake.stats()

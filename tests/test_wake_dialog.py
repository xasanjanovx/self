"""06.10: утренний звонок ведёт программа (bot/wake_dialog.py): три вопроса по одному, тишина — вопрос, а не «вы меня слышите?»;
модель только оценивает ответ."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot import islam_quiz, wake_dialog

HEAR = "слышите"
TZ = ZoneInfo("Asia/Tashkent")


def _quiz(tmp_path, monkeypatch, uid: int = 7, day: date = date(2026, 10, 6)) -> list[islam_quiz.Q]:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return islam_quiz.for_day_set(uid, day)


def _flow(qs, **kw) -> wake_dialog.WakeFlow:  # noqa: ANN003
    return wake_dialog.build(lang=kw.pop("lang", "ru"), title=kw.pop("title", "сэр"), day_quiz=qs, minutes_left=kw.pop("minutes_left", 25), **kw)


def _talk(flow: wake_dialog.WakeFlow, heard: str, verdict: str = "correct") -> wake_dialog.Move:
    """Он сказал heard; оценка ответа — verdict (как её вернула бы модель)."""
    move = flow.route(heard)
    if move.grade == "quiz":
        return flow.answer(verdict)
    if move.grade == "final":
        return flow.final_answer(verdict, heard)
    return move


# ------------------------------------------------------------------ сколько вопросов
def test_always_three_questions_whatever_the_time_left():
    """07.10: «будильник не дал 3 вопроса» — до такбира было ~9 минут, и вопросов было два. Теперь всегда три."""
    for left in (None, 25, 12, 11, 9, 6, 5, 0, -4):
        assert wake_dialog.questions_for(left) == 3, left


def test_minutes_and_questions_declension():
    assert [wake_dialog.minutes_text(n, "ru") for n in (1, 2, 5, 11, 21, 12)] == ["1 минута", "2 минуты", "5 минут", "11 минут", "21 минута", "12 минут"]
    assert wake_dialog.minutes_text(7, "uz") == "7 daqiqa"
    assert [wake_dialog.questions_text(n, "ru") for n in (1, 2, 3, 5, 11, 21)] == ["1 вопрос", "2 вопроса", "3 вопроса", "5 вопросов", "11 вопросов", "21 вопрос"]
    assert wake_dialog.questions_text(2, "uz") == "2 ta savol"


# ------------------------------------------------------------------ три вопроса по порядку
def test_three_questions_in_order_then_are_you_up(tmp_path, monkeypatch):
    qs = _quiz(tmp_path, monkeypatch)
    results, progress = [], []
    flow = _flow(qs, on_result=lambda qid, ok: results.append((qid, ok)), on_progress=progress.append)
    assert flow.stage == "greet" and flow.greeting() == "Доброе утро, сэр! Проснулись?"
    first = _talk(flow, "Алло, да, проснулся").say
    assert "Первый вопрос" in first and qs[0].q in first and flow.stage == "quiz"
    second = _talk(flow, "Пять столпов", "correct").say
    assert "Второй вопрос" in second and qs[1].q in second and second.startswith(("Верно", "Правильно", "Молодец"))
    third = _talk(flow, "Четыре", "wrong").say
    assert third.startswith("Не совсем, сэр. Правильный ответ: " + qs[1].a) and "Последний вопрос" in third and qs[2].q in third
    last = _talk(flow, "Абу Бакр", "correct").say
    assert "встали?" in last and flow.stage == "final" and flow.current() is None
    assert results == [(qs[0].id, True), (qs[1].id, False), (qs[2].id, True)] and progress == [1, 2, 3]
    done = _talk(flow, "да, встал", "up")
    assert done.confirm and "Отлично, сэр!" in done.say and "примет ваш намаз" in done.say and flow.stage == "done"


def test_unheard_speech_asks_to_repeat_the_current_question(tmp_path, monkeypatch):
    qs = _quiz(tmp_path, monkeypatch)
    flow = _flow(qs)
    assert flow.unheard() == ""                                 # на приветствии — ничего, тишину ведёт лестница
    _talk(flow, "да")
    said = flow.unheard()
    assert said.startswith("Не разобрала.") and qs[0].q in said and flow.idx == 0 and HEAR not in said
    final = _flow(qs, done=3)
    _talk(final, "да")
    assert "встали?" in final.unheard()


def test_unknown_answer_gives_the_right_answer_and_arabic(tmp_path, monkeypatch):
    dua = next(q for q in islam_quiz.BANK if q.ar)
    flow = _flow([dua])
    _talk(flow, "да")
    said = _talk(flow, "не помню", "unknown").say
    assert said.startswith("Ничего страшного, сэр.") and dua.a in said and dua.ar in said and "встали?" in said


def test_not_an_answer_repeats_the_question_and_does_not_advance(tmp_path, monkeypatch):
    qs = _quiz(tmp_path, monkeypatch)
    results = []
    flow = _flow(qs, on_result=lambda qid, ok: results.append(qid))
    _talk(flow, "да")
    said = _talk(flow, "мне надо в туалет", "other").say
    assert "Не разобрала" in said and qs[0].q in said and HEAR not in said and flow.idx == 0 and results == []


def test_grader_failure_still_teaches_but_never_counts_a_miss(tmp_path, monkeypatch):
    qs = _quiz(tmp_path, monkeypatch)
    results = []
    flow = _flow(qs, on_result=lambda qid, ok: results.append(qid))
    _talk(flow, "да")
    said = _talk(flow, "что-то", "").say
    assert "Принято." in said and qs[0].a in said and qs[1].q in said and flow.idx == 1 and results == []


def test_little_time_still_three_questions_and_the_time_is_named(tmp_path, monkeypatch):
    qs = _quiz(tmp_path, monkeypatch)
    now = datetime(2026, 10, 6, 0, 10, tzinfo=timezone.utc)
    flow = _flow(qs, minutes_left=8, takbir_at=now + timedelta(minutes=9))
    flow.now = lambda: now
    assert len(flow.quiz) == 3 and len(_flow(qs, minutes_left=3).quiz) == 3
    _talk(flow, "да")
    _talk(flow, "x")
    _talk(flow, "y")
    last = _talk(flow, "z").say
    assert "встали?" in last and "До такбира 9 минут." in last               # последний вопрос: «встали?» и сколько осталось
    far = _flow(qs, done=3, takbir_at=now + timedelta(minutes=40))
    far.now = lambda: now
    assert "До такбира" not in _talk(far, "да").say                           # времени много — не торопим


def test_second_call_resumes_where_the_first_stopped(tmp_path, monkeypatch):
    qs = _quiz(tmp_path, monkeypatch)
    follow = _talk(_flow(qs, done=2), "да").say
    assert "Последний вопрос" in follow and qs[2].q in follow and qs[0].q not in follow
    done = _flow(qs, done=3)
    assert "встали?" in _talk(done, "да").say and done.stage == "final"


# ------------------------------------------------------------------ его слова: разбор без модели
def test_hello_is_answered_and_returns_to_the_question_without_using_our_hear_limit(tmp_path, monkeypatch):
    qs = _quiz(tmp_path, monkeypatch)
    flow = _flow(qs)
    for said in ("алло", "вы меня слышите?", "Алло!"):
        move = flow.route(said)
        assert "слышу вас" in move.say and qs[0].q in move.say and not move.grade
    assert not flow.hear_used


def test_time_question_is_answered_by_the_clock(tmp_path, monkeypatch):
    qs = _quiz(tmp_path, monkeypatch)
    now = datetime(2026, 10, 6, 0, 10, tzinfo=timezone.utc)                 # 05:10 в Ташкенте
    flow = _flow(qs, takbir_at=(now + timedelta(minutes=17)).astimezone(TZ), tz=TZ)
    flow.now = lambda: now
    _talk(flow, "да")
    said = flow.route("а сколько до такбира?").say
    assert said.startswith("Сейчас 05:10, до такбира 17 минут.") and qs[0].q in said and flow.idx == 0
    assert flow.route("который час").say.startswith("Сейчас 05:10")


def test_hurry_first_asks_for_a_minute_then_lets_go_even_if_model_would_grade(tmp_path, monkeypatch):
    qs = _quiz(tmp_path, monkeypatch)
    flow = _flow(qs)
    _talk(flow, "да")
    first = flow.route("хватит, я встал, давай всё")
    assert not first.confirm and "Ещё минуту, сэр, осталось 3 вопроса" in first.say and qs[0].q in first.say and not first.grade and flow.idx == 0
    second = flow.route("ну всё, встал, пока")
    assert second.confirm and "примет ваш намаз" in second.say and flow.stage == "done"
    on_greet = _flow(qs).route("встал, отключайся")                           # торопит прямо на приветствии — вопрос сразу
    assert qs[0].q in on_greet.say and not on_greet.confirm


def test_hurry_needs_a_clear_up_and_a_hurry_word():
    for yes in ("встал, отключайся", "хватит, я встал, давай всё", "я проснулся, без вопросов", "не сплю, некогда", "turdim, bo'ldi"):
        assert wake_dialog.is_hurry(yes), yes
    for no in ("хватит, дай поспать", "да-да, хватит", "отстань", "ещё пять минут", "не встал, хватит", "Абу Бакр", "встал", "да"):
        assert not wake_dialog.is_hurry(no), no          # «хватит» сонного — не подъём


def test_sleepy_greeting_gets_prayer_is_better_and_the_question(tmp_path, monkeypatch):
    qs = _quiz(tmp_path, monkeypatch)
    flow = _flow(qs)
    said = flow.route("нет, ещё сплю").say
    assert "намаз лучше сна" in said and qs[0].q in said and flow.stage == "quiz"


def test_asking_to_sleep_is_refused_once_then_snoozed_at_most_five_minutes(tmp_path, monkeypatch):
    qs = _quiz(tmp_path, monkeypatch)
    flow = _flow(qs)
    _talk(flow, "да")
    first = flow.route("дай поспать ещё минутку")
    assert "намаз лучше сна" in first.say and qs[0].q in first.say and not first.snooze
    second = flow.route("ещё 15 минут пожалуйста")
    assert second.snooze == 5 and "позвоню через 5 минут" in second.say
    again = _flow(qs)
    _talk(again, "да")
    assert again.route("потом Умар").grade == "quiz"                          # «потом Умар» — ответ на вопрос, не просьба поспать
    third = _flow(qs)
    _talk(third, "да")
    third.route("поспать")
    assert third.route("ещё 3 минуты").snooze == 3


def test_final_answer_confirms_only_on_a_clear_up(tmp_path, monkeypatch):
    qs = _quiz(tmp_path, monkeypatch)
    flow = _flow(qs, done=3)
    _talk(flow, "да")
    assert flow.stage == "final"
    not_yet = _talk(flow, "угу", "not_yet")
    assert not not_yet.confirm and "скажите «встал»" in not_yet.say and flow.stage == "final"
    assert not _talk(flow, "что?", "other").confirm
    unsure_no = flow.final_answer("", "угу")                                      # оценщик недоступен: одно «угу» — не подъём
    assert not unsure_no.confirm
    assert flow.final_answer("", "я уже встал").confirm and flow.stage == "done"


# ------------------------------------------------------------------ тишина
def test_silence_goes_straight_to_the_first_question(tmp_path, monkeypatch):
    qs = _quiz(tmp_path, monkeypatch)
    flow = _flow(qs)
    assert flow.silence_limit() == 6.0                         # сонный, взял трубку и молчит — вопрос через 6 с
    said = flow.nudge()
    assert qs[0].q in said and "Первый вопрос" in said and HEAR not in said
    assert flow.stage == "quiz" and flow.silence_limit() == 9.0


def test_hear_question_at_most_once_per_call(tmp_path, monkeypatch):
    qs = _quiz(tmp_path, monkeypatch)
    flow = _flow(qs)
    said = [flow.nudge() for _ in range(14)]
    assert sum(HEAR in t for t in said) == 1                   # было: «Шеф, вы меня слышите?» каждые 8 секунд
    assert not any(HEAR in t for t in said[:2])                # сначала — вопрос и его повтор
    assert qs[0].q in said[1]                                  # повтор вопроса


def test_nudges_mention_minutes_to_takbir_and_motivation(tmp_path, monkeypatch):
    qs = _quiz(tmp_path, monkeypatch)
    now = datetime(2026, 10, 6, 0, 10, tzinfo=timezone.utc)
    flow = _flow(qs, takbir_at=now + timedelta(minutes=17))
    flow.now = lambda: now
    texts = [flow.nudge() for _ in range(6)]
    assert any("До такбира 17 минут" in t for t in texts)
    assert flow.farewell().startswith("До такбира 17 минут") and "примет ваш намаз" in flow.farewell()
    flow.now = lambda: now + timedelta(minutes=30)             # такбир прошёл — без «до такбира -13»
    assert "До такбира" not in flow.farewell() and "примет ваш намаз" in flow.farewell()


def test_silence_limit_is_longer_after_he_started_talking(tmp_path, monkeypatch):
    qs = _quiz(tmp_path, monkeypatch)
    flow = _flow(qs)
    _talk(flow, "да")
    assert flow.silence_limit() == 9.0 * 1.4                   # думает над ответом — не торопим
    flow.nudge()
    assert flow.silence_limit() == 11.0 * 1.4


def test_third_call_of_the_morning_goes_straight_to_the_time(tmp_path, monkeypatch):
    qs = _quiz(tmp_path, monkeypatch)
    now = datetime(2026, 10, 6, 0, 10, tzinfo=timezone.utc)
    flow = _flow(qs, takbir_at=now + timedelta(minutes=9), attempt=3)
    flow.now = lambda: now
    flow.nudge()
    assert flow.nudge().startswith("До такбира 9 минут.")


def test_final_stage_silence_asks_again_without_new_questions(tmp_path, monkeypatch):
    qs = _quiz(tmp_path, monkeypatch)
    flow = _flow(qs, done=3)
    _talk(flow, "да")
    said = [flow.nudge() for _ in range(4)]
    assert "встали?" in said[0] and "скажите «встал»" in said[1]


def test_review_question_is_announced(tmp_path, monkeypatch):
    qs = _quiz(tmp_path, monkeypatch)
    flow = _flow(qs, review_ids=frozenset({qs[0].id}))
    assert flow.nudge().count("Повторим то, что не получилось") == 1


def test_uzbek_flow_speaks_uzbek_around_the_question(tmp_path, monkeypatch):
    qs = _quiz(tmp_path, monkeypatch)
    flow = _flow(qs, lang="uz", title="ser")
    assert flow.greeting() == "Xayrli tong, ser! Uyg'ondingizmi?"
    said = _talk(flow, "ha").say
    assert "Birinchi savol" in said and qs[0].q in said
    assert flow.farewell().endswith("Alloh namozingizni qabul qilsin!")


# ------------------------------------------------------------------ оценщик: подсказка и разбор
def test_grader_prompts_carry_the_question_the_answer_and_his_words(tmp_path, monkeypatch):
    dua = next(q for q in islam_quiz.BANK if q.ar)
    prompt = wake_dialog.quiz_prompt(dua, "аль хамду лиллях")
    assert dua.q in prompt and dua.a in prompt and dua.ar in prompt and "аль хамду лиллях" in prompt and "correct" in prompt and "other" in prompt
    assert "Вы встали?" in wake_dialog.final_prompt("встал") and "«встал»" in wake_dialog.final_prompt("встал")


def test_parse_verdict_accepts_only_known_words():
    assert wake_dialog.parse_verdict({"verdict": "Correct "}, wake_dialog.QUIZ_VERDICTS) == "correct"
    assert wake_dialog.parse_verdict({"verdict": "maybe"}, wake_dialog.QUIZ_VERDICTS) == ""
    assert wake_dialog.parse_verdict(["x"], wake_dialog.QUIZ_VERDICTS) == ""
    assert wake_dialog.parse_verdict({"verdict": "up"}, wake_dialog.FINAL_VERDICTS) == "up"
    assert wake_dialog.parse_verdict({"verdict": "up"}, wake_dialog.QUIZ_VERDICTS) == ""


def test_heard_means_up_is_strict():
    for yes in ("Проснулся, уже на ногах", "не сплю", "turdim", "uyg'ondim", "я встал"):
        assert wake_dialog.heard_means_up(yes), yes
    for no in ("да", "ага", "угу", "не встал", "ещё сплю", "нет", "ещё пять минут", "пока лежу", "потом", ""):
        assert not wake_dialog.heard_means_up(no), no


# ------------------------------------------------------------------ квиз: прогресс за день и работа над ошибками
def test_quiz_progress_is_per_day(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    d = date(2026, 10, 6)
    islam_quiz.for_day_set(7, d)
    assert islam_quiz.progress(7, d) == 0
    islam_quiz.set_progress(7, d, 2)
    islam_quiz.set_progress(7, d, 1)                           # меньше — не откатываем
    assert islam_quiz.progress(7, d) == 2 and islam_quiz.progress(7, d + timedelta(days=1)) == 0
    assert islam_quiz.for_day_set(7, d)                        # набор дня и пройденное не мешают друг другу


def test_missed_question_returns_next_day_until_answered(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    d = date(2026, 10, 6)
    day1 = islam_quiz.for_day_set(7, d)
    islam_quiz.note_result(7, day1[1].id, False)               # ошибся
    islam_quiz.note_result(7, day1[0].id, True)
    day2 = islam_quiz.for_day_set(7, d + timedelta(days=1))
    assert day2[0].id == day1[1].id and islam_quiz.is_review(7, day2[0]) and len(day2) == 3
    assert not islam_quiz.is_review(7, day2[1])
    assert len({q.id for q in day2}) == 3
    islam_quiz.note_result(7, day2[0].id, True)                # ответил верно — вопрос снят с повторения
    day3 = islam_quiz.for_day_set(7, d + timedelta(days=2))
    assert day1[1].id not in {q.id for q in day3} and not islam_quiz.is_review(7, day3[0])
    assert islam_quiz.for_day_set(7, d + timedelta(days=2)) == day3     # повторные звонки дня — тот же набор


# ------------------------------------------------------------------ склейка: раннер → звонок → поток
def _profile():
    from bot.profile import Profile

    return Profile(telegram_id=7, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Тест", username="t")


def test_flow_for_the_call_resumes_progress_and_records_results(tmp_path, monkeypatch):
    from bot import cheap_voice, persona

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    day = date(2026, 10, 6)
    qs = islam_quiz.for_day_set(7, day)
    islam_quiz.set_progress(7, day, 1)                          # в прошлом звонке этого утра он прошёл один вопрос
    takbir = datetime.now(timezone.utc) + timedelta(minutes=20)
    flow = cheap_voice.wake_flow(_profile(), persona.Persona(lang="ru", honorific="shef"),
                                 {"day": day, "quiz_list": qs, "minutes_left": 20, "takbir_at": takbir, "attempt": 2})
    assert flow.idx == 1 and flow.title == "шеф" and flow.attempt == 2 and len(flow.quiz) == 3
    assert flow.minutes_left() in (19, 20)
    assert qs[1].q in _talk(flow, "да").say                       # продолжили со второго вопроса
    _talk(flow, "не знаю", "unknown")                             # на втором не знал
    assert islam_quiz.progress(7, day) == 2
    assert islam_quiz._load(7)["missed"] == [qs[1].id]            # вернётся в следующие дни


def test_runner_hands_the_flow_what_it_needs_and_trims_questions_by_time(tmp_path, monkeypatch):
    from bot import live_call, wake as wake_mod, wake_runner

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seen: dict = {}

    async def run(profile, **kw):  # noqa: ANN001, ANN003
        seen.update(kw)
        return live_call.LiveResult(answered=True, dialed=True, model="m")

    async def today(profile):  # noqa: ANN001
        return ""

    monkeypatch.setattr(live_call, "run", run)
    monkeypatch.setattr(wake_runner, "_today_line", today)
    takbir = datetime.now(timezone.utc) + timedelta(minutes=8)
    plan = wake_mod.DayPlan(day=date(2026, 10, 6), active=True, takbir="05:20", takbir_at=takbir)
    result = asyncio.run(wake_runner._dialog_call(_profile(), wake_mod.WakeSettings(), plan, 8, 3))
    wake = seen["wake"]
    assert wake["attempt"] == 3 and wake["takbir_at"] == takbir and wake["day"] == plan.day and len(wake["quiz_list"]) == 3
    assert len(result["quiz"]) == 3                              # до такбира 8 минут — всё равно три вопроса (07.10)


def test_pause_before_the_next_call_counts_from_the_end_of_this_one(monkeypatch):
    """06.10: после 58-секундного разговора перезвонили через 2 секунды («занято») — пауза считалась от начала звонка."""
    from types import SimpleNamespace

    from bot import caller, services, wake as wake_mod, wake_runner

    async def dialog(profile, s, plan, minutes_left, attempt=1):  # noqa: ANN001
        await asyncio.sleep(0.15)
        return {"answered": True, "error": None, "state": SimpleNamespace(confirmed=False, transcript=[])}

    async def save(uid, day, fields):  # noqa: ANN001
        return {}

    class Bot:
        async def send_message(self, *a, **k):  # noqa: ANN002, ANN003
            return SimpleNamespace(message_id=1)

        async def delete_message(self, *a, **k):  # noqa: ANN002, ANN003
            return True

    monkeypatch.setattr(caller, "available", lambda: True)
    monkeypatch.setattr(wake_runner, "_dialog_call", dialog)
    monkeypatch.setattr(services, "save_wake_log", save)
    now = datetime.now(timezone.utc)
    plan = wake_mod.DayPlan(day=date(2026, 10, 6), active=True, takbir="05:20", takbir_at=now + timedelta(minutes=20), wake_at=now)
    wake_runner._active.pop(7, None)
    asyncio.run(wake_runner.run_attempt(Bot(), _profile(), wake_mod.WakeSettings(), plan, None))
    last = wake_runner._active[7]["last"]
    assert (last - now).total_seconds() >= 0.12                  # конец звонка, а не его начало
    wake_runner._active.pop(7, None)


def test_test_call_asks_the_questions_but_remembers_nothing(tmp_path, monkeypatch):
    """«Проверить будильник» — звонок как утром (с вопросами дня), но прогресс и ошибки не пишутся: утром вопросы начнутся с первого."""
    from bot import call_assistant, live_call, persona, wake as wake_mod, wake_runner

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seen: dict = {}
    now = datetime.now(timezone.utc)
    plan = wake_mod.DayPlan(day=date(2026, 10, 7), active=True, takbir="05:20", takbir_at=now + timedelta(hours=7))

    async def plan_for(profile, day=None):  # noqa: ANN001
        return wake_mod.WakeSettings(enabled=True), plan

    async def run(profile, **kw):  # noqa: ANN001, ANN003
        seen.update(kw)
        return live_call.LiveResult(answered=True, dialed=True, confirmed=True)

    async def show_home(profile, text):  # noqa: ANN001
        seen["home"] = text

    monkeypatch.setattr(wake_runner, "plan_for", plan_for)
    monkeypatch.setattr(live_call, "run", run)
    monkeypatch.setattr(call_assistant, "show_home", show_home)
    profile = _profile()
    call_assistant._running.discard(profile.telegram_id)
    async def go():
        await call_assistant.wake_test_in_background(profile)

    asyncio.run(go())
    wake = seen["wake"]
    assert wake["test"] is True and len(wake["quiz_list"]) == 3 and wake["day"] == plan.day

    from bot import cheap_voice

    islam_quiz.set_progress(7, plan.day, 2)                              # даже если утренний прогресс уже есть, тест идёт с первого вопроса
    flow = cheap_voice.wake_flow(profile, persona.Persona(lang="ru"), wake)
    assert flow.idx == 0 and flow.on_result is None and flow.on_progress is None
    _talk(flow, "да")
    _talk(flow, "не знаю", "unknown")
    assert islam_quiz.progress(7, plan.day) == 2 and not islam_quiz._load(7).get("missed")


# ------------------------------------------------------------------ 09.10: громкий сигнал приложения, если не взял трубку; проверка слуха
def _loud_env(monkeypatch, *, dialog_result):  # noqa: ANN001
    from types import SimpleNamespace

    from bot import caller, phone_link, services, wake as wake_mod, wake_runner

    pushed: list = []
    notes: list = []

    async def dialog(profile, s, plan, minutes_left, attempt=1):  # noqa: ANN001
        return dialog_result

    async def save(uid, day, fields):  # noqa: ANN001
        return {}

    async def note(bot, uid, text, **kw):  # noqa: ANN001, ANN003
        notes.append(text)

    class Bot:
        async def send_message(self, *a, **k):  # noqa: ANN002, ANN003
            return SimpleNamespace(message_id=1)

        async def delete_message(self, *a, **k):  # noqa: ANN002, ANN003
            return True

    monkeypatch.setattr(caller, "available", lambda: True)
    monkeypatch.setattr(wake_runner, "_dialog_call", dialog)
    monkeypatch.setattr(services, "save_wake_log", save)
    monkeypatch.setattr(wake_runner.screen_mod_, "send_note", note)
    monkeypatch.setattr(phone_link, "push", lambda uid, action: pushed.append((uid, action)) or True)
    from bot import call_assistant

    async def intro(profile):  # noqa: ANN001
        return None

    monkeypatch.setattr(call_assistant, "helper_intro", intro)
    now = datetime.now(timezone.utc)
    plan = wake_mod.DayPlan(day=date(2026, 10, 9), active=True, takbir="05:20", takbir_at=now + timedelta(minutes=20), wake_at=now)
    wake_runner._active.pop(7, None)
    asyncio.run(wake_runner.run_attempt(Bot(), _profile(), wake_mod.WakeSettings(), plan, None))
    wake_runner._active.pop(7, None)
    return pushed, notes


def test_unanswered_call_rings_the_phone_loudly(monkeypatch):
    from types import SimpleNamespace

    pushed, notes = _loud_env(monkeypatch, dialog_result={"answered": False, "error": "no_answer", "state": SimpleNamespace(confirmed=False, transcript=[])})
    assert pushed == [(7, {"type": "ring_phone", "seconds": 60})] and notes == []


def test_picked_up_but_silent_rings_and_tells_what_it_heard(monkeypatch):
    from types import SimpleNamespace

    pushed, notes = _loud_env(monkeypatch, dialog_result={"answered": True, "error": None, "no_reply": True, "heard_peak": 12,
                                                          "state": SimpleNamespace(confirmed=False, transcript=[])})
    assert pushed == [(7, {"type": "ring_phone", "seconds": 60})]
    assert len(notes) == 1 and "12" in notes[0] and "не слышу" in notes[0].lower()


def test_normal_answered_call_does_not_ring(monkeypatch):
    from types import SimpleNamespace

    pushed, notes = _loud_env(monkeypatch, dialog_result={"answered": True, "error": None, "no_reply": False,
                                                          "state": SimpleNamespace(confirmed=False, transcript=["он: да"])})
    assert pushed == [] and notes == []


def test_confirmed_wake_or_too_many_attempts_never_ring(monkeypatch):
    from bot import phone_link, wake_runner

    pushed: list = []
    monkeypatch.setattr(phone_link, "push", lambda uid, action: pushed.append(action) or True)
    profile = _profile()
    asyncio.run(wake_runner._loud_backup(None, profile, 1, call_error="no_answer", answered=False, confirmed=True, result={}))
    asyncio.run(wake_runner._loud_backup(None, profile, wake_runner.LOUD_RING_ATTEMPTS + 1, call_error="no_answer", answered=False,
                                         confirmed=False, result={}))
    assert pushed == []                                          # подтвердил подъём / слишком много неудачных попыток — сигнал не нужен
    asyncio.run(wake_runner._loud_backup(None, profile, 2, call_error="no_answer", answered=False, confirmed=False, result={}))
    assert pushed == [{"type": "ring_phone", "seconds": wake_runner.LOUD_RING_S}]


def test_phone_link_hurry_flag_goes_to_the_app():
    from bot import phone_link

    phone_link._hurry_until.clear()
    assert phone_link.hurrying(7) is False
    phone_link.hurry(7, 60)
    assert phone_link.hurrying(7) is True
    out = asyncio.run(phone_link.pull(7, wait=0.01))
    assert out["hurry"] is True
    phone_link._hurry_until.clear()


def test_deaf_hint_is_said_once_when_the_call_has_no_sound(tmp_path, monkeypatch):
    from datetime import date

    from bot import islam_quiz
    from tests.test_wake_session import _Wake  # noqa: PLC0415 — готовая сессия подъёма

    w = _Wake(monkeypatch, tmp_path)
    from bot.phone_cheap import Segmenter

    w.sess._seg = Segmenter(24000, silence_ms=700, min_rms=110.0, factor=3.0, floor=80.0, floor_min=25.0, floor_max=120.0)
    w.sess.flow.total_nudges = 1                                  # одна реплика в тишину уже была
    said: list = []

    async def say(text):  # noqa: ANN001
        said.append(text)

    w.sess.say = say
    asyncio.run(w.sess.nudge_once())
    assert said and "почти не слышу" in said[0] and w.sess.flow.deaf_used
    asyncio.run(w.sess.nudge_once())
    assert "почти не слышу" not in said[1]                        # второй раз — обычная лестница подъёма
    assert islam_quiz and date


def test_no_deaf_hint_when_the_line_is_alive(tmp_path, monkeypatch):
    from tests.test_wake_session import _Wake  # noqa: PLC0415

    w = _Wake(monkeypatch, tmp_path)
    from bot.phone_cheap import Segmenter

    seg = Segmenter(24000, silence_ms=700, min_rms=110.0, factor=3.0, floor=80.0, floor_min=25.0, floor_max=120.0)
    seg.peak_all = 400.0                                          # звук из трубки есть — он просто молчит/спит
    w.sess._seg = seg
    w.sess.flow.total_nudges = 1
    said: list = []

    async def say(text):  # noqa: ANN001
        said.append(text)

    w.sess.say = say
    asyncio.run(w.sess.nudge_once())
    assert said and "почти не слышу" not in said[0]

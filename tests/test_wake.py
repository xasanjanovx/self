"""Подъём на фаджр: план дня, повторные звонки, мотивация вместо заданий, подтверждение."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from bot import prayer, wake

TZ = ZoneInfo("Asia/Tashkent")
DAY = date(2026, 9, 23)  # среда
TIMINGS = {"Fajr": "04:27", "Sunrise": "05:59", "Dhuhr": "12:03", "Asr": "15:29", "Maghrib": "18:07", "Isha": "19:33"}


def _s(**kw) -> wake.WakeSettings:
    return wake.WakeSettings(**kw)


# ------------------------------------------------------------------ план дня
def test_plan_wakes_before_takbir():
    plan = wake.plan_for_day(_s(offset_min=25, takbir_offset_min=20), DAY, tz=TZ, timings=TIMINGS)
    assert plan.active and plan.takbir == "04:47"
    assert plan.wake_at == datetime(2026, 9, 23, 4, 22, tzinfo=TZ)
    assert plan.takbir_at == datetime(2026, 9, 23, 4, 47, tzinfo=TZ)


def test_plan_respects_off_days_skip_and_fixed_time():
    off = wake.plan_for_day(_s(enabled=False), DAY, tz=TZ, timings=TIMINGS)
    assert not off.active and off.reason == "off"
    weekend_only = wake.plan_for_day(_s(days_of_week=(6, 7)), DAY, tz=TZ, timings=TIMINGS)
    assert not weekend_only.active and weekend_only.reason == "day_off"
    skipped = wake.plan_for_day(_s(skip_until=date(2026, 9, 25)), DAY, tz=TZ, timings=TIMINGS)
    assert not skipped.active and skipped.reason == "skip"
    fixed = wake.plan_for_day(_s(mode="fixed", fixed_time="06:30"), DAY, tz=TZ, timings=TIMINGS)
    assert fixed.active and fixed.wake_at == datetime(2026, 9, 23, 6, 30, tzinfo=TZ) and fixed.takbir == "04:47"
    no_times = wake.plan_for_day(_s(), DAY, tz=TZ, timings={})
    assert not no_times.active and no_times.reason == "no_times"


# ------------------------------------------------------------------ звонки
def test_should_call_until_confirmed():
    s = _s(retry_seconds=45, max_attempts=3)
    plan = wake.plan_for_day(s, DAY, tz=TZ, timings=TIMINGS)
    before = datetime(2026, 9, 23, 4, 10, tzinfo=TZ)
    assert wake.should_call(plan, None, before, s) == (False, "too_early")
    at_time = datetime(2026, 9, 23, 4, 22, tzinfo=TZ)
    assert wake.should_call(plan, None, at_time, s) == (True, "call")
    # только что звонили — ждём retry_seconds
    log = {"attempts": 1, "last_attempt_at": at_time}
    assert wake.should_call(plan, log, at_time + timedelta(seconds=20), s) == (False, "cooldown")
    assert wake.should_call(plan, log, at_time + timedelta(seconds=50), s) == (True, "call")
    # подтвердил подъём — больше не звоним
    assert wake.should_call(plan, {**log, "woke_at": "2026-09-23T04:25:00+00:00"}, at_time + timedelta(minutes=5), s)[1] == "already_awake"
    # лимит попыток и «слишком поздно»
    assert wake.should_call(plan, {"attempts": 3}, at_time + timedelta(minutes=5), s)[1] == "max_attempts"
    assert wake.should_call(plan, None, at_time + timedelta(hours=3), s)[1] == "too_late"


# ------------------------------------------------------------------ «проснулся» / «ещё 10 минут»
def test_looks_awake_and_snooze():
    assert wake.looks_awake("проснулся")
    assert wake.looks_awake("Uyg'ondim, rahmat")
    assert wake.looks_awake("я уже встал")
    assert not wake.looks_awake("такси 25000")
    assert wake.snooze_minutes("ещё 10 минут") == 10
    assert wake.snooze_minutes("yana 5 daqiqa") == 5
    assert wake.snooze_minutes("ещё 60 минут") is None
    assert wake.snooze_minutes("проснулся") is None


# ------------------------------------------------------------------ задания
def test_motivation_instead_of_tasks():
    """Никаких упражнений и заданий — только слова, чтобы встать на намаз; каждый день/попытка — другая фраза."""
    assert not hasattr(wake, "make_task") and not hasattr(wake, "check_answer")
    for lang in ("ru", "uz", "en"):
        phrases = wake.MOTIVATION[lang]
        assert len(phrases) >= 6
        assert wake.motivation(DAY, 1, lang) in phrases
        assert wake.motivation(DAY, 1, lang) != wake.motivation(DAY, 2, lang)
        assert wake.motivation(DAY, 1, lang) == wake.motivation(DAY, 1, lang)  # без случайности между перезапусками
    ru = " ".join(wake.MOTIVATION["ru"]).lower()
    assert "мунафик" in ru and "доволен вами" in ru and "рай" in ru and "намаз лучше сна" in ru
    for word in ("присед", "отжим", "стакан", "×"):
        assert word not in ru


# ------------------------------------------------------------------ тексты и статистика
def test_texts_mention_takbir_and_motivation():
    s = _s()
    plan = wake.plan_for_day(s, DAY, tz=TZ, timings=TIMINGS)
    msg = wake.wake_message(name="Хасан", plan=plan, attempt=2, lang="ru")
    assert "04:47" in msg and wake.motivation(DAY, 2, "ru") in msg and "Попытка 2" in msg
    assert "Задание" not in msg
    script = wake.call_script(name="Hasan", takbir=plan.takbir, minutes_left=25, motivation_text=wake.motivation(DAY, 1, "uz"), lang="uz")
    assert "04:47" in script and "25 daqiqa" in script and "Assalomu alaykum" in script and wake.motivation(DAY, 1, "uz") in script
    done = wake.done_message(plan=plan, now=datetime(2026, 9, 23, 4, 30, tzinfo=TZ), lang="ru", streak=3)
    assert "17 мин" in done and "3" in done


def test_streak_and_stats():
    rows = [{"day": (DAY - timedelta(days=i)).isoformat(), "woke_at": "x", "before_takbir": True, "attempts": 2} for i in range(3)]
    rows.append({"day": (DAY - timedelta(days=3)).isoformat(), "woke_at": "x", "before_takbir": False, "attempts": 5})
    assert wake.streak_days(rows, DAY) == 3
    line = wake.stats_line(rows, "ru")
    assert "4 из 4" in line and "до такбира — 3" in line


# ------------------------------------------------------------------ namoz
def test_prayer_helpers():
    assert prayer.takbir_time("04:27", 20).strftime("%H:%M") == "04:47"
    assert prayer.offset_from_takbir("04:27", "05:20") == 53
    assert prayer.offset_from_takbir("04:27", "03:00") is None
    text = prayer.summary(TIMINGS, "uz", takbir="04:47")
    assert "Bomdod 04:27 (takbir 04:47)" in text and "Xufton 19:33" in text
    now = datetime(2026, 9, 23, 13, 0, tzinfo=TZ)
    nxt = prayer.next_prayer(TIMINGS, now, "ru")
    assert nxt[0] == "Аср" and nxt[2] == 149


# ------------------------------------------------------------------ разговор в трубке
def test_voice_buffer_cuts_phrase_on_silence():
    from bot import call_dialog as cd

    loud = bytes.fromhex("0040") * 2400   # ~100 мс громкого звука при 24 кГц
    quiet = bytes(4800)                   # ~100 мс тишины
    buf = cd.VoiceBuffer()
    assert buf.feed(quiet) is None          # тишина до речи ничего не копит
    for _ in range(5):                      # 500 мс речи
        assert buf.feed(loud) is None
    for _ in range(8):                      # 800 мс тишины — ещё мало
        out = buf.feed(quiet)
    assert out is None
    out = buf.feed(quiet)                   # 900 мс — фраза закончилась
    assert out is not None and cd.ms_of(out) >= 1000
    assert buf.speech_ms == 0               # буфер очистился


def test_dialog_decides_awake_or_nudge():
    from bot import call_dialog as cd

    state = cd.DialogState(lang="ru", name="Хасан", takbir="04:47", minutes_left=25, task_text="выпей стакан воды")
    assert "04:47" in cd.greeting(state) and "Хасан" in cd.greeting(state)
    silent = cd.decide(state, "")
    assert silent["action"] == "nudge" and silent["say"]
    still = cd.decide(state, "сплю ещё пять минут")
    assert still["action"] == "reply" and state.confirmed is False
    ok = cd.decide(state, "да я встал уже")
    assert ok["action"] == "confirm" and state.confirmed is True
    assert "воды" in ok["say"]
    assert "04:47" in cd.farewell(state)
    assert cd.sounds_awake("uyg'ondim") and not cd.sounds_awake("uxlayapman")
    assert cd.sounds_awake("да, сейчас иду умываться")   # связная фраза = проснулся
    assert not cd.sounds_awake("мм")


def test_dialog_prompt_language():
    from bot import call_dialog as cd

    uz = cd.system_prompt(cd.DialogState(lang="uz", name="Hasan", takbir="04:47"))
    assert "o'zbek" in uz and "04:47" in uz
    ru = cd.system_prompt(cd.DialogState(lang="ru", name="Хасан"))
    assert "по-русски" in ru


# ------------------------------------------------------------------ звонок по требованию
def test_assistant_call_helpers():
    from bot import call_assistant as ca

    assert ca.is_goodbye("всё, спасибо") and ca.is_goodbye("rahmat") and ca.is_goodbye("пока")
    assert not ca.is_goodbye("сколько я потратил")
    assert "Слушаю" in ca.greeting(_profile_stub("ru"), "ru")
    assert "Tinglayapman" in ca.greeting(_profile_stub("uz"), "uz")
    # ответ агента режется до произносимой длины, без html и эмодзи
    long_answer = "<b>Сегодня</b> потратил 250 000 сум 🎯. " + "Ещё подробности. " * 40
    said = ca.voice_reply(long_answer)
    assert "<b>" not in said and "🎯" not in said and len(said) <= 400
    assert said.endswith(".")


def _profile_stub(lang: str):
    from bot.profile import Profile

    return Profile(telegram_id=1, lang=lang, tz_name="Asia/Tashkent", currency="UZS", first_name="Хасан", username="x")

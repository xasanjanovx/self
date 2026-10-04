"""Будильник как сервис (26.09.2026): место и часовой пояс, будильник в приложении, бесплатное приветствие, главное меню."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot import app_alarm, emoji as pe, live_call, places
from bot.keyboards import main_menu_keyboard
from bot.persona import Persona


def test_nearest_city():
    assert places.nearest_city(40.78, 72.34)[0] == "andijan"
    assert places.nearest_city(41.31, 69.28)[0] == "tashkent"
    assert places.nearest_city(55.75, 37.62) is None  # Москва — не в списке: подпись координатами, пояс — от Aladhan


def test_telegram_call_never_waits_for_the_app_alarm(monkeypatch, tmp_path):
    """04.10: «он должен будить по звонку Telegram ВСЕГДА». Будильник приложения — дополнительный слой, звонок его не ждёт."""
    import inspect

    from bot import workers

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert not hasattr(app_alarm, "holds_telegram")          # условия «подождать приложение» больше нет
    assert "holds_telegram" not in inspect.getsource(workers._wake_tick)
    assert app_alarm.app_enabled(1) is False                  # по умолчанию будильник приложения выключен
    wake_at = datetime(2026, 10, 5, 0, 10, tzinfo=timezone.utc)
    app_alarm.scheduled(1, "2026-10-05", int(wake_at.timestamp() * 1000))   # старый будильник в телефоне ещё стоит и даже звонил
    app_alarm.rang(1, "2026-10-05")
    assert app_alarm.tomorrow_ready(1, "2026-10-05")


def test_app_alarm_is_an_optional_extra_switched_per_user(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    app_alarm.set_app(1, True)
    assert app_alarm.app_enabled(1) is True and app_alarm.app_enabled(2) is False
    app_alarm.scheduled(1, "2026-10-05", 1791159000000)       # состояние «поставлен» не стирает выбор
    assert app_alarm.app_enabled(1) is True
    app_alarm.set_app(1, False)
    assert app_alarm.app_enabled(1) is False
    app_alarm.set_app(1, True)
    monkeypatch.setattr(app_alarm, "ENABLED", False)           # APP_ALARM=0 на сервере — нельзя включить вовсе
    assert app_alarm.app_enabled(1) is False


def test_ringing_app_alarm_goes_quiet_when_he_picks_up_the_call(monkeypatch):
    monkeypatch.setattr(app_alarm, "_call_answered", {})
    assert not app_alarm.in_call(1)
    app_alarm.call_answered(1)
    assert app_alarm.in_call(1) and not app_alarm.in_call(2)
    app_alarm._call_answered[1] -= app_alarm.CALL_ANSWERED_TTL + 1   # звонок давно кончился — будильник снова может звонить
    assert not app_alarm.in_call(1)


def test_next_plan_says_telegram_calls_even_when_app_alarm_is_off(monkeypatch, tmp_path):
    """Приложение не должно писать «Выключен» (из-за этого он трижды нажал переключатель): подъём включён, звонит Telegram."""
    import asyncio
    from datetime import date
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    from bot import places, services, wake as wake_mod, wake_runner

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    tz = ZoneInfo("Asia/Tashkent")
    soon = datetime.now(timezone.utc) + timedelta(hours=6)
    plan = wake_mod.DayPlan(day=date.today(), active=True, wake_at=soon, takbir="05:19", fajr="04:39", window=("04:09", "05:55"))

    async def plan_for(profile, day=None):  # noqa: ANN001
        return wake_mod.WakeSettings(enabled=True), plan

    async def wake_log(uid, day):  # noqa: ANN001
        return None

    monkeypatch.setattr(wake_runner, "plan_for", plan_for)
    monkeypatch.setattr(services, "wake_log", wake_log)
    monkeypatch.setattr(places, "label", lambda uid, lang: "Андижан")
    profile = SimpleNamespace(telegram_id=1, lang="ru", today=date.today(), tz=tz)
    off = asyncio.run(app_alarm.next_plan(profile))
    assert off["enabled"] is False and off["on"] is True and off["app"] is False      # приложение свой будильник не ставит…
    assert off["wake_at"] == soon.strftime("%H:%M") and off["takbir"] == "05:19"        # …но показывает время подъёма
    app_alarm.set_app(1, True)
    on = asyncio.run(app_alarm.next_plan(profile))
    assert on["enabled"] is True and on["app"] is True and on["at_ms"] == int(soon.timestamp() * 1000)


def test_alarm_voice_phrases_follow_persona():
    from bot import alarm_voice

    ru = alarm_voice.texts(Persona(lang="ru", honorific="mix", mirror=False))
    assert ru and {lang for _, lang in ru} == {"ru"}
    assert ru[0][0].startswith("Доброе утро, сэр!") and "фаджр" in ru[0][0]
    both = alarm_voice.texts(Persona(lang="ru", honorific="mix", mirror=True))
    assert [lang for _, lang in both][:4] == ["ru", "uz", "ru", "uz"]   # два языка — через один
    assert len({t for t, _ in both}) == len(both)                          # фразы не повторяются
    assert all("{" not in t for t, _ in both)


def test_alarm_voice_gain_is_capped():
    import numpy as np

    from bot import alarm_voice

    quiet = (np.sin(np.linspace(0, 200, 24000)) * 4000).astype(np.int16)
    out = np.frombuffer(alarm_voice.loud(quiet.tobytes()), dtype=np.int16)
    assert 11500 < int(np.abs(out).max()) < 12100     # ×3 — предел усиления
    loud_ = (np.sin(np.linspace(0, 200, 24000)) * 30000).astype(np.int16)
    out2 = np.frombuffer(alarm_voice.loud(loud_.tobytes()), dtype=np.int16)
    assert int(np.abs(out2).max()) <= int(0.93 * 32767)  # громкое не раздувается до щелчков
    silence = np.zeros(2400, dtype=np.int16).tobytes()
    assert alarm_voice.loud(silence) == silence          # шум тишины не усиливаем


def test_wake_clip_text():
    assert live_call.wake_clip_text(Persona(lang="ru", honorific="shef"), "Тест") == "Доброе утро, шеф! Проснулись?"
    # 28.09 его выбор: «mix» — чаще «сэр»
    assert live_call.wake_clip_text(Persona(lang="uz", honorific="mix"), "Test") == "Xayrli tong, ser! Uyg'ondingizmi?"


def test_main_menu_alarm_instead_of_refresh():
    kb = main_menu_keyboard("ru")
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "settings:wake" in data and "menu:dashboard" in data and "menu:open" not in data
    # иконки главного меню — только из его паков (TgAndroidIcons / SoLo_HaMiD / adaptiveqp_by_emsetbot)
    allowed = {pe.ID_NUTRITION, pe.ID_FINANCE, pe.ID_TASKS, pe.ID_GOAL, pe.ID_JARVIS, pe.ID_ALARM, pe.ID_SETTINGS, pe.ID_ANALYTICS}
    assert {b.icon_custom_emoji_id for row in kb.inline_keyboard for b in row} <= allowed
    assert pe.ID_ANALYTICS == "5877485980901971030" and pe.ID_SETTINGS == "5877260593903177342" and pe.ID_GOAL == "5961051261204696786"


def test_app_taps_never_cancel_the_telegram_wake_call(monkeypatch, tmp_path):
    """04.10 05:10: «Проснулся» в приложении через 8 секунд → сервер засчитал подъём → Telegram не позвонил. Теперь тап и «ещё 5 минут» ничего не меняют."""
    import asyncio
    import json
    from datetime import date
    from types import SimpleNamespace

    from bot import phone_api, wake_runner
    from bot.handlers import common

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_OWNER_ID", "7")
    calls: list[str] = []

    async def mark_awake(*a, **kw):  # noqa: ANN002, ANN003
        calls.append("mark_awake")

    async def snooze(*a, **kw):  # noqa: ANN002, ANN003
        calls.append("snooze")

    async def profile_by_id(uid):  # noqa: ANN001
        return SimpleNamespace(telegram_id=uid, today=date(2026, 10, 5))

    monkeypatch.setattr(wake_runner, "mark_awake", mark_awake)
    monkeypatch.setattr(wake_runner, "snooze", snooze)
    monkeypatch.setattr(common, "profile_by_id", profile_by_id)

    def post(body):  # noqa: ANN001, ANN202
        async def js():  # noqa: ANN202
            return body
        return json.loads(asyncio.run(phone_api.wake_event(SimpleNamespace(json=js))).text)

    assert post({"event": "awake"}).get("ignored") is True
    assert post({"event": "snooze", "minutes": 5}).get("ignored") is True
    assert calls == []                                                   # ни подъём, ни пауза звонков
    assert post({"event": "scheduled", "day": "2026-10-05", "at_ms": 1}).get("ignored") is True   # будильник приложения выключен
    app_alarm.set_app(7, True)
    assert post({"event": "scheduled", "day": "2026-10-05", "at_ms": 1}) == {"ok": True}
    assert post({"event": "ring"}) == {"ok": True} and app_alarm.tomorrow_ready(7, "2026-10-05")
    assert post({"event": "awake"}).get("ignored") is True and calls == []

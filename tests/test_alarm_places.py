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


def test_app_alarm_holds_telegram_while_alarm_is_expected_to_ring(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    wake_at = datetime(2026, 9, 27, 4, 30, tzinfo=timezone.utc)
    assert not app_alarm.holds_telegram(1, "2026-09-27", wake_at, wake_at)  # приложение будильник не ставило — Telegram сразу
    app_alarm.scheduled(1, "2026-09-27", int(wake_at.timestamp() * 1000))
    assert app_alarm.holds_telegram(1, "2026-09-27", wake_at, wake_at + timedelta(seconds=60))   # ждём «звоню» от приложения
    assert not app_alarm.holds_telegram(1, "2026-09-27", wake_at, wake_at + timedelta(seconds=100))  # не зазвонил — Telegram
    assert not app_alarm.holds_telegram(1, "2026-09-28", wake_at, wake_at)  # другой день


def test_app_alarm_rang_then_telegram_after_grace(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    wake_at = datetime.now(timezone.utc).replace(microsecond=0)
    app_alarm.scheduled(1, "2026-09-27", int(wake_at.timestamp() * 1000))
    app_alarm.rang(1, "2026-09-27")   # приложение: «звоню»
    assert app_alarm.holds_telegram(1, "2026-09-27", wake_at, wake_at + timedelta(minutes=2))
    assert not app_alarm.holds_telegram(1, "2026-09-27", wake_at, wake_at + timedelta(minutes=4))  # не встал — звонит и Telegram
    app_alarm.scheduled(1, "2026-09-27", int(wake_at.timestamp() * 1000))   # переустановка не стирает «уже звонил»
    assert not app_alarm.holds_telegram(1, "2026-09-27", wake_at, wake_at + timedelta(minutes=4))


def test_app_alarm_stale_time_in_phone_does_not_hold_telegram(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    wake_at = datetime(2026, 9, 27, 4, 30, tzinfo=timezone.utc)
    # в боте время поменяли на 4:10, а в телефоне ещё стоит 4:30 — ждать нечего, звонит Telegram
    app_alarm.scheduled(1, "2026-09-27", int((wake_at + timedelta(minutes=20)).timestamp() * 1000))
    assert not app_alarm.holds_telegram(1, "2026-09-27", wake_at, wake_at + timedelta(seconds=10))


def test_app_alarm_is_on_by_default_and_can_be_switched_off(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert app_alarm.ENABLED is True   # 03.10: он проспал, пока подъём был только звонком Telegram
    monkeypatch.setattr(app_alarm, "ENABLED", False)
    wake_at = datetime(2026, 9, 27, 4, 30, tzinfo=timezone.utc)
    app_alarm.scheduled(1, "2026-09-27", int(wake_at.timestamp() * 1000))
    assert not app_alarm.holds_telegram(1, "2026-09-27", wake_at, wake_at + timedelta(seconds=1))  # APP_ALARM=0: Telegram звонит вовремя


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

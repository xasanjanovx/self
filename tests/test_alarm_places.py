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


def test_app_alarm_holds_telegram_only_until_grace(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    wake_at = datetime(2026, 9, 27, 4, 30, tzinfo=timezone.utc)
    assert not app_alarm.holds_telegram(1, "2026-09-27", wake_at, wake_at)  # приложение будильник не ставило
    app_alarm.scheduled(1, "2026-09-27", int(wake_at.timestamp() * 1000))
    assert app_alarm.holds_telegram(1, "2026-09-27", wake_at, wake_at + timedelta(minutes=4))
    assert not app_alarm.holds_telegram(1, "2026-09-27", wake_at, wake_at + timedelta(minutes=6))  # не встал — звонит Telegram
    assert not app_alarm.holds_telegram(1, "2026-09-28", wake_at, wake_at)  # другой день


def test_wake_clip_text():
    assert live_call.wake_clip_text(Persona(lang="ru", honorific="shef"), "Тест") == "Доброе утро, шеф! Проснулись?"
    assert live_call.wake_clip_text(Persona(lang="uz", honorific="mix"), "Test") == "Xayrli tong, shef! Uyg'ondingizmi?"


def test_main_menu_alarm_instead_of_refresh():
    kb = main_menu_keyboard("ru")
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "settings:wake" in data and "menu:dashboard" in data and "menu:open" not in data
    # иконки главного меню — только из его паков (TgAndroidIcons / SoLo_HaMiD / adaptiveqp_by_emsetbot)
    allowed = {pe.ID_NUTRITION, pe.ID_FINANCE, pe.ID_TASKS, pe.ID_GOAL, pe.ID_JARVIS, pe.ID_ALARM, pe.ID_SETTINGS, pe.ID_ANALYTICS}
    assert {b.icon_custom_emoji_id for row in kb.inline_keyboard for b in row} <= allowed
    assert pe.ID_ANALYTICS == "5877485980901971030" and pe.ID_SETTINGS == "5877260593903177342" and pe.ID_GOAL == "5961051261204696786"

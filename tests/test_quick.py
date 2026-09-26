"""Без Live (26.09): простые вопросы, команды бота, «Звонит брат», общая память."""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from bot import instant, live_call, phone, phone_live


@pytest.fixture
def uid(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    phone._contacts.clear()
    return 77


@pytest.mark.parametrize("text, kind", [
    ("сколько времени", "time"), ("который час", "time"), ("какое сегодня число", "date"), ("какой сегодня день", "date"),
    ("сколько заряда", "battery"), ("на сколько у меня будильник", "alarm"), ("какая погода", "ask"),
    ("сколько я потратил сегодня", "ask"), ("запиши обед сорок тысяч", "do"), ("напомни через час позвонить маме", "do"),
    ("добавь задачу купить хлеб", "do"),
])
def test_quick_kinds(text, kind):
    assert instant.quick(text) == kind


@pytest.mark.parametrize("text", ["расскажи анекдот", "как дела", "что ты умеешь", "позвони мне", ""])
def test_conversation_stays_in_live(text):
    assert instant.quick(text) is None


def test_local_answers():
    now = datetime(2026, 9, 26, 17, 5)
    assert instant.local_answer("time", now, {}) == "Сейчас 17:05."
    assert instant.local_answer("date", now, {}) == "Сегодня суббота, 26 сентября."
    assert instant.local_answer("battery", now, {"battery": 64, "charging": True}) == "Заряд 64 процентов, заряжается."
    assert instant.local_answer("alarm", now, {}, {"enabled": True, "day": "2026-09-27", "wake_at": "05:00", "takbir": "05:11"}) == \
        "Будильник завтра в 05:00, такбир в 05:11."


def test_kin_announce_without_model(uid, monkeypatch):
    phone.save_contacts(uid, [{"n": "SIROJBEK AKAM", "p": ["1"]}])
    phone.learn_alias(uid, "phone", "брат", "SIROJBEK AKAM")
    assert phone.kin_of(uid, "SIROJBEK AKAM") == "брат"

    from bot import services
    from bot.context import ai
    from bot.persona import Persona

    async def persona(u):
        return Persona(lang="ru")

    async def no_llm(*a, **k):
        raise AssertionError("своих называем без модели")

    async def tts(text, voice=None):
        return b"\x00\x00" * 2400

    monkeypatch.setattr(services, "persona", persona)
    monkeypatch.setattr(ai, "generate", no_llm)
    monkeypatch.setattr(ai, "synthesize", tts)
    assert asyncio.run(phone_live.announce(uid, "SIROJBEK AKAM", ""))["text"] == "Звонит брат"
    assert asyncio.run(phone_live.announce(uid, "+998 90 123 45 67", "Telegram"))["text"] == "Звонит незнакомый номер в Telegram"


def test_recent_lines_in_phone_prompt():
    memory = "ПАМЯТЬ О ПОЛЬЗОВАТЕЛЕ:\n• любит чай\n\nНЕДАВНИЕ РЕПЛИКИ (прошлые дни):\n26.09 10:00 · я: купи хлеб\n26.09 11:00 · я: 📱 позвони брату"
    assert "📱 позвони брату" in live_call.recent_lines(memory) and "любит чай" not in live_call.recent_lines(memory)

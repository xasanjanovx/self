"""Звонок на Gemini Live и характер Джарвиса — промпты, инструменты, настройки (без сети)."""
from __future__ import annotations

from bot import live_call, persona
from bot.profile import Profile


def _profile(lang: str = "ru") -> Profile:
    return Profile(telegram_id=7, lang=lang, tz_name="Asia/Tashkent", currency="UZS", first_name="Хасан", username="xasan")


def test_persona_from_row_sanitizes_values():
    p = persona.Persona.from_row({"voice": "NoSuchVoice", "lang": "ru", "address": "siz", "tone": "weird", "verbosity": "detailed", "call_name": "  Hasanjon "})
    assert p.voice == persona.DEFAULT_VOICE and p.lang == "ru" and p.address == "siz"
    assert p.tone == "friendly" and p.verbosity == "detailed"
    assert p.name_for("Хасан") == "Hasanjon"
    assert persona.Persona.from_row(None).name_for("Хасан") == "Хасан"


def test_style_rules_follow_settings():
    spoken = persona.style_rules(persona.Persona(address="siz", tone="strict", verbosity="short"), spoken=True)
    assert "«вы»" in spoken and "требовательный" in spoken and "одним-двумя" in spoken
    chat = persona.style_rules(persona.Persona(verbosity="detailed"))
    assert "развёрнуто" in chat
    assert "по-русски" in persona.lang_rule(persona.Persona(lang="ru"))
    assert "узбекски" in persona.lang_rule(persona.Persona(lang="uz"))


def test_wake_instruction_has_takbir_task_and_confirm_rule():
    text = live_call.system_instruction(_profile(), persona.Persona(), mode="wake",
                                        wake={"takbir": "05:07", "minutes_left": 25, "task": "выпей стакан воды"})
    assert "05:07" in text and "25 минут" in text and "стакан воды" in text
    assert "confirm_awake" in text and "НЕ подтверждение" in text
    assert "ДАННЫЕ" not in text  # подъёму данные не нужны


def test_assistant_instruction_has_data_topic_and_tools_rule():
    text = live_call.system_instruction(_profile(), persona.Persona(voice="Kore"), mode="assistant",
                                        snapshot="Балансы: карта 100", memory="ПАМЯТЬ: брат Алишер", topic="разберём траты")
    assert "разберём траты" in text and "Балансы: карта 100" in text and "брат Алишер" in text
    assert "add_calorie_logs" in text and "end_call" in text
    assert "ПО ТЕЛЕФОНУ" in text


def test_tool_declarations_per_mode():
    wake_names = {d["name"] for d in live_call.tool_declarations("wake")}
    assert {"end_call", "confirm_awake", "snooze", "prayer_times"} <= wake_names
    assert "add_finance_entries" not in wake_names  # при подъёме операций не пишем
    assistant_names = {d["name"] for d in live_call.tool_declarations("assistant")}
    assert {"end_call", "add_goal", "delete_finance_entries", "add_calorie_logs", "list_goals"} <= assistant_names
    # в голосе нет чатовых инструментов: парсеры с экранами, кнопки, звонок самому себе
    assert not {"hand_off", "open_screen", "ask_user", "call_me"} & assistant_names
    assert "confirm_awake" not in assistant_names

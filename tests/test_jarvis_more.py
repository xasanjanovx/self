"""Джарвис: обращение «Шеф/Сэр/Босс», фото для агента, звонок о важном, чистота чата (без сети)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bot import persona, screen
from bot.profile import Profile


def _profile(lang: str = "ru") -> Profile:
    return Profile(telegram_id=7, lang=lang, tz_name="Asia/Tashkent", currency="UZS", first_name="Хасан", username="xasan")


# ------------------------------------------------------------------ обращение и характер
def test_honorific_from_row_and_rule():
    p = persona.Persona.from_row({"honorific": "boss", "morning_voice": True, "alert_calls": True})
    assert p.honorific == "boss" and p.morning_voice and p.alert_calls
    assert "«Босс»" in persona.style_rules(p)
    assert persona.Persona.from_row({"honorific": "weird"}).honorific == "mix"
    mix = persona.honorific_rule(persona.Persona())
    assert "Шеф" in mix and "Сэр" in mix and "Босс" in mix and "Не в каждой фразе" in mix
    assert "без «шеф/сэр/босс»" in persona.honorific_rule(persona.Persona(honorific="none"))
    assert not persona.Persona().alert_calls and not persona.Persona().morning_voice  # по умолчанию выключено


# ------------------------------------------------------------------ фото → агент
def test_agent_sees_photo_but_history_keeps_only_a_mark():
    from bot.handlers import agent

    seen = {}

    async def step_fn(contents, **kw):  # noqa: ANN001
        seen["parts"] = contents[-1]["parts"]
        return SimpleNamespace(parts=[{"text": "Отметила 5 из 8"}], text="Отметила 5 из 8", calls=[], finish="STOP")

    result = asyncio.run(agent.run_agent(_profile(), "(прислал фото)", [], snapshot="", step_fn=step_fn,
                                         image=(b"\x89PNG", "image/png")))
    assert any("inline_data" in p for p in seen["parts"])
    compact = agent.compact_message(result.contents[0])
    assert compact["parts"] == [{"text": "(прислал фото)"}, {"text": "[фото]"}]


def test_prompts_require_expect_photo_and_honesty():
    from bot import agent_tools, live_call
    from bot.handlers.agent import system_prompt

    names = {d["name"] for d in agent_tools.declarations()}
    assert "expect_photo" in names
    chat = system_prompt(_profile(), "")
    assert "expect_photo" in chat and "ФОТО" in chat and "ЧЕСТНОСТЬ" in chat
    call = live_call.system_instruction(_profile(), persona.Persona(), mode="assistant")
    assert "expect_photo" in call
    assert "expect_photo" in {d["name"] for d in live_call.tool_declarations("assistant")}


# ------------------------------------------------------------------ звонок о важном
def test_only_important_alerts_trigger_a_call():
    from bot.proactive import Alert
    from bot.workers import important_alerts

    alerts = [Alert("debt_today:Али:lent:2026-09-23", "срок сегодня"), Alert("spike:2026-09-23", "много потратил"),
              Alert("budget_proj:food:2026-09", "лимит"), Alert("weekly:2026-W39", prompt="итог недели"),
              Alert("goal_day:5:2026-09-23", "цель отстаёт")]
    assert [a.key.split(":")[0] for a in important_alerts(alerts)] == ["debt_today", "budget_proj", "goal_day"]


# ------------------------------------------------------------------ чистота чата
class _FakeBot:
    def __init__(self) -> None:
        self.deleted: list[tuple[int, int]] = []
        self.next_id = 500

    async def send_message(self, chat_id, text, reply_markup=None):  # noqa: ANN001
        self.next_id += 1
        return SimpleNamespace(message_id=self.next_id)

    async def delete_message(self, chat_id, message_id):  # noqa: ANN001
        self.deleted.append((chat_id, message_id))


class _FakeTrash:
    def __init__(self) -> None:
        self.rows: dict[tuple[int, int], datetime] = {}

    async def add_ephemeral(self, chat_id, message_id, delete_at):  # noqa: ANN001
        self.rows[(chat_id, message_id)] = delete_at

    async def list_ephemerals(self, *, chat_id=None, due_before=None):  # noqa: ANN001
        return [{"chat_id": c, "message_id": m} for (c, m), at in self.rows.items()
                if (chat_id is None or c == chat_id) and (due_before is None or at <= due_before)]

    async def drop_ephemerals(self, chat_id, ids):  # noqa: ANN001
        for m in ids:
            self.rows.pop((chat_id, m), None)


def test_ephemeral_messages_survive_restart_and_are_swept():
    """«Не дозвонился» оставался в чату навсегда, если бот перезапускался до таймера."""
    bot, trash, chat = _FakeBot(), _FakeTrash(), 9911
    screen.configure_trash(trash)
    try:
        async def scenario():
            mid = await screen.send_ephemeral(bot, chat, "📵 не дозвонилась", keep_previous=True, ttl=180)
            await asyncio.sleep(0)  # дать фоновой записи в журнал выполниться
            assert (chat, mid) in trash.rows
            # «перезапуск»: память процесса пуста, срок вышел — воркер всё равно удалит
            screen._ephemerals.pop(chat, None)
            trash.rows[(chat, mid)] = datetime.now(timezone.utc) - timedelta(seconds=1)
            assert await screen.sweep(bot) == 1
            assert (chat, mid) in bot.deleted and not trash.rows
            # без ttl: после перезапуска подберём на первом же действии
            old = 777
            trash.rows[(chat, old)] = datetime.now(timezone.utc) + timedelta(hours=5)
            screen._trash_loaded.discard(chat)
            await screen.clear_ephemerals(bot, chat)
            await asyncio.sleep(0)
            assert (chat, old) in bot.deleted
        asyncio.run(scenario())
    finally:
        screen.configure_trash(None)
        screen._ephemerals.pop(chat, None)


def test_settings_and_jarvis_are_separate():
    from bot.keyboards import jarvis_hub_keyboard, main_menu_keyboard, settings_keyboard

    data = [b.callback_data for row in settings_keyboard("ru").inline_keyboard for b in row]
    assert "settings:jarvis" not in data and "settings:wake" not in data
    hub = [b.callback_data for row in jarvis_hub_keyboard("ru", alert_calls=True).inline_keyboard for b in row]
    for cb in ("wakeset:calltest", "settings:wake", "settings:jarvis", "jarvis:toggle:alert_calls", "jarvis:toggle:morning_voice"):
        assert cb in hub
    btn = next(b for row in main_menu_keyboard("ru").inline_keyboard for b in row if b.callback_data == "menu:jarvis")
    assert btn.icon_custom_emoji_id and "🤖" not in btn.text  # премиум-иконка вместо обычного эмодзи


def test_voice_brief_prompt_uses_call_language_and_honorific():
    from bot import voice_brief

    prompt = voice_brief.script_prompt("<b>Сегодня</b>: 3 задачи", _profile(), persona.Persona(lang="uz", honorific="shef"))
    assert "узбекском" in prompt and "«Шеф»" in prompt and "<b>" not in prompt

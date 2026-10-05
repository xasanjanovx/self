"""05.10: память текущего разговора (bot/session_memory.py), подробная справка в чат (bot/agent_tools_research.py)
и русские формулировки про Telegram в голосе."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot import agent_tools, agent_tools_extra as extra, live_call, session_memory
from bot import agent_tools_research as research
from bot.agent_tools import ToolContext


@pytest.fixture(autouse=True)
def _clean():
    session_memory._state.clear()
    yield
    session_memory._state.clear()


# ------------------------------------------------------------------ память разговора
def test_block_is_empty_without_talk_and_keeps_subject_after_pause():
    assert session_memory.block(1) == ""
    session_memory.note(1, "📱 расскажи про Тома Хэнкса", "Том Хэнкс — американский актёр, родился в 1956 году")
    session_memory.set_topic(1, "человек: Том Хэнкс")
    text = session_memory.block(1)
    assert "Предмет: человек: Том Хэнкс" in text and "расскажи про Тома Хэнкса" in text and "родился в 1956" in text
    assert "📱" not in text                                       # служебная метка источника не попадает в промпт
    assert "а сколько ему лет" in text                            # правило про короткие вопросы без предмета — в блоке


def test_block_expires_and_is_per_user(monkeypatch):
    session_memory.note(1, "про Нолана", "режиссёр")
    session_memory.note(2, "про Бекмамбетова", "тоже режиссёр")
    assert "Нолана" in session_memory.block(1) and "Нолана" not in session_memory.block(2)
    real = session_memory.time.monotonic()
    monkeypatch.setattr(session_memory.time, "monotonic", lambda: real + session_memory.TTL_S + 5)
    assert session_memory.block(1) == ""


def test_topic_expires_sooner_than_exchanges(monkeypatch):
    session_memory.note(1, "про Нолана", "режиссёр")
    session_memory.set_topic(1, "фильм: Интерстеллар")
    real = session_memory.time.monotonic()
    monkeypatch.setattr(session_memory.time, "monotonic", lambda: real + session_memory.TOPIC_TTL_S + 5)
    text = session_memory.block(1)
    assert "Нолана" in text and "Интерстеллар" not in text


def test_block_is_compact_and_masks_secrets():
    for i in range(10):
        session_memory.note(1, f"реплика номер {i} " + "слово " * 60, "ответ " * 100)
    text = session_memory.block(1)
    assert text.count("• ") == 3 and len(text) < 1500             # Live оплачивает инструкцию в каждом ответе
    session_memory.note(2, "мой пароль: abc123XYZ!", "")
    assert "abc123XYZ" not in session_memory.block(2)


def test_remember_exchange_feeds_current_talk_even_without_database():
    asyncio.run(extra.remember_exchange(7, "📱 а кто режиссёр?", "Кристофер Нолан", when="05.10 12:00"))
    assert "Кристофер Нолан" in session_memory.block(7)
    asyncio.run(extra.remember_exchange(7, "", "ничего", when="05.10 12:01"))     # пустая реплика не пишется
    assert session_memory.block(7).count("• ") == 1


def test_session_end_keeps_the_last_exchanges_separately_and_aligned_from_the_end():
    # три обмена; на второй команда выполнена молча (ответа нет): сдвиг считается от последнего
    users = ["кто такой Нолан", "включи фонарик", "а какой у него лучший фильм"]
    answers = ["режиссёр", "Начало, Интерстеллар и Помни"]
    session_memory.note_turns(3, users, answers, n=3)
    block = session_memory.block(3)
    assert block.count("• ") == 3
    assert "он: а какой у него лучший фильм → ты: Начало, Интерстеллар и Помни" in block
    assert block.index("кто такой Нолан") < block.index("а какой у него лучший фильм")        # хронология сохранена


def test_joined_session_text_loses_its_beginning_not_its_end():
    joined = " / ".join(f"реплика {i} " + "слово " * 10 for i in range(12))
    session_memory.note(4, joined, "")
    block = session_memory.block(4)
    assert "реплика 11" in block and "реплика 0 " not in block


def test_session_end_hooks_do_not_double_count_exchanges(monkeypatch):
    asyncio.run(extra.remember_exchange(8, "📱 один", "ответ", when="05.10 12:00", session=False))
    assert session_memory.block(8) == ""                     # вызывающий пишет обмены сам (note_turns)


def test_voice_prompt_carries_current_talk_but_facts_only_does_not_double_it():
    memory = "ПАМЯТЬ О ПОЛЬЗОВАТЕЛЕ:\n• брат — Сирожбек\n\n" + session_memory.BLOCK_HEAD + "\n• 1 мин назад — он: x → ты: y\n\nНЕДАВНИЕ РЕПЛИКИ (прошлые дни):\n1"
    assert live_call.facts_only(memory) == "ПАМЯТЬ О ПОЛЬЗОВАТЕЛЕ:\n• брат — Сирожбек"
    profile = SimpleNamespace(telegram_id=9)
    assert live_call.current_talk(profile) == ""
    session_memory.note(9, "кто такой Нолан", "режиссёр")
    assert "кто такой Нолан" in live_call.current_talk(profile)


# ------------------------------------------------------------------ справка в чат
def test_research_is_registered_and_in_voice_sets():
    assert "research" in agent_tools.TOOLS
    decl = next(d for d in agent_tools.declarations() if d["name"] == "research")
    assert decl["parameters"]["required"] == ["kind", "query"]
    assert set(decl["parameters"]["properties"]["kind"]["enum"]) >= {"person", "movie", "book"}
    assert "research" in live_call.PHONE_LIVE_CORE and "research" in live_call.CALL_LIVE_CORE and "research" in live_call.VOICE_CORE
    for mode in ("phone", "assistant"):
        names = [d["name"] for d in live_call.tool_declarations(mode)]
        assert names.count("research") == 1 and len(names) == len(set(names))      # дубли деклараций ломают Gemini Live


def test_html_is_safe_and_split_keeps_tags_whole():
    out = research.to_html("**Фильм** <script>x</script> & кино\n- пункт\n* ещё")
    assert "<b>Фильм</b>" in out and "&lt;script&gt;" in out and "&amp;" in out and "• пункт" in out and "• ещё" in out and "*" not in out
    text = "\n\n".join(f"<b>Раздел {i}</b>\n" + "строка " * 80 for i in range(20))
    parts = research.chunks(text, limit=1500)
    assert len(parts) > 1 and all(p.count("<b>") == p.count("</b>") for p in parts)
    assert "".join(parts).count("Раздел") == 20


def test_brief_line_is_split_off():
    brief, body = research._clean_text("КРАТКО: Том Хэнкс — американский актёр, двукратный обладатель «Оскара».\n\n👤 Кто это\nТом…")
    assert brief.startswith("Том Хэнкс") and body.startswith("👤 Кто это") and "КРАТКО" not in body


def _ctx(uid: int = 5) -> ToolContext:
    from datetime import datetime

    profile = SimpleNamespace(telegram_id=uid, lang="ru", now=datetime(2026, 10, 5, 12, 0))
    return ToolContext(profile=profile, text="")


def test_private_person_hunting_is_refused_without_a_search(monkeypatch):
    async def boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("поиск не должен запускаться")

    monkeypatch.setattr(research.ai, "research", boom)
    for q in ("домашний адрес Алишера", "номер телефона моего друга Азиза", "паспорт Бахтиёра", "where does Anvar live"):
        res = asyncio.run(research._research(_ctx(), {"kind": "person", "query": q}))
        assert res["error"] == "refused", q


def test_person_card_goes_to_chat_with_sources_and_sets_topic(monkeypatch):
    sent: list[str] = []

    async def fake_research(system, query, **kw):  # noqa: ANN001, ANN003
        assert "ПУБЛИЧНОМ ЧЕЛОВЕКЕ" in system and "по пунктам" in system        # правила о частных лицах и его формат — в инструкции
        assert "Том Хэнкс" in query
        return "КРАТКО: Американский актёр, два «Оскара».\n\n👤 **Кто это**\nТом Хэнкс, род. 1956.", [("imdb.com", "https://imdb.com/x?a=1&b=2")]

    async def fake_deliver(uid, card_html, extra_line):  # noqa: ANN001
        sent.extend([card_html, extra_line])
        return True

    monkeypatch.setattr(research.ai, "research", fake_research)
    monkeypatch.setattr(research, "_deliver", fake_deliver)
    res = asyncio.run(research._research(_ctx(5), {"kind": "person", "query": "Том Хэнкс", "format": "по пунктам"}))
    assert res["ok"] and res["sent_to_chat"] and res["brief"].startswith("Американский актёр")
    assert "по-русски" in res["note"] and "Telegram" in res["note"]
    assert "<b>Кто это</b>" in sent[0] and "КРАТКО" not in sent[0] and 'href="https://imdb.com/x?a=1&amp;b=2"' in sent[1]
    assert "Том Хэнкс" in session_memory.topic(5)                   # следующее «а сколько ему лет?» знает, о ком речь


def test_non_public_person_gets_no_card(monkeypatch):
    async def fake_research(system, query, **kw):  # noqa: ANN001, ANN003
        return "НЕ ПУБЛИЧНЫЙ: публичной информации об этом человеке нет", []

    async def no_deliver(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("карточка на частного человека не отправляется")

    monkeypatch.setattr(research.ai, "research", fake_research)
    monkeypatch.setattr(research, "_deliver", no_deliver)
    res = asyncio.run(research._research(_ctx(), {"kind": "person", "query": "Азиз из соседнего двора"}))
    assert res["ok"] is False and res["public"] is False


def test_chat_failure_is_reported_honestly(monkeypatch):
    async def fake_research(system, query, **kw):  # noqa: ANN001, ANN003
        return "КРАТКО: Фильм Нолана 2014 года.\n\n🎬 Интерстеллар", []

    async def fail(*a, **k):  # noqa: ANN002, ANN003
        return False

    monkeypatch.setattr(research.ai, "research", fake_research)
    monkeypatch.setattr(research, "_deliver", fail)
    res = asyncio.run(research._research(_ctx(), {"kind": "movie", "query": "Интерстеллар"}))
    assert res["sent_to_chat"] is False and "не получилось" in res["note"]


def test_voice_request_answers_at_once_and_builds_the_card_in_background(monkeypatch):
    from bot import deeds

    order: list[str] = []

    async def fake_research(system, query, **kw):  # noqa: ANN001, ANN003
        await asyncio.sleep(0.05)
        order.append("search")
        return "КРАТКО: Фильм Нолана.\n\n🎬 Интерстеллар", []

    async def fake_deliver(uid, card_html, extra_line):  # noqa: ANN001
        order.append("card")
        return True

    monkeypatch.setattr(research.ai, "research", fake_research)
    monkeypatch.setattr(research, "_deliver", fake_deliver)

    async def scenario():
        token = deeds.source.set("телефон")
        try:
            res = await research._research(_ctx(), {"kind": "movie", "query": "Интерстеллар"})
            order.append("answer")
        finally:
            deeds.source.reset(token)
        assert res["started"] is True and "по-русски" in res["note"].lower() and "Интерстеллар" in res["note"]
        await asyncio.gather(*research._background)

    asyncio.run(scenario())
    assert order == ["answer", "search", "card"]                     # голос не ждёт поиск


def test_voice_failure_is_explained_in_chat(monkeypatch):
    from bot import context, deeds

    messages: list[str] = []

    class FakeBot:
        async def send_message(self, uid, text, **kw):  # noqa: ANN001, ANN003
            messages.append(text)

    async def fake_research(system, query, **kw):  # noqa: ANN001, ANN003
        return "НЕ ПУБЛИЧНЫЙ: публичной информации об этом человеке нет", []

    monkeypatch.setattr(research.ai, "research", fake_research)
    monkeypatch.setattr(context, "_bot", FakeBot())

    async def scenario():
        token = deeds.source.set("звонок")
        try:
            await research._research(_ctx(), {"kind": "person", "query": "Азиз <b>"})
        finally:
            deeds.source.reset(token)
        await asyncio.gather(*research._background)

    asyncio.run(scenario())
    assert len(messages) == 1 and "не публичная личность" in messages[0] and "<b>" not in messages[0].replace("&lt;b&gt;", "")


# ------------------------------------------------------------------ только русский про Telegram
def test_telegram_mentions_are_russian_only():
    assert "ТОЛЬКО по-русски" in live_call.PHONE_RULES and "ТОЛЬКО по-русски" in live_call.CALL_RULES
    res = asyncio.run(live_call._send_to_chat(1, ""))
    assert res == {"error": "empty text"}

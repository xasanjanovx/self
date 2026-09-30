"""29.09: память дел («что я делал вчера?», «когда звонил Алишеру?») и итоги недели голосом."""
import asyncio
from datetime import date

from bot import agent_tools, deeds, weekly
from bot.agent_tools import Tool, ToolContext
from bot.persona import Persona
from bot.profile import Profile


def _profile() -> Profile:
    return Profile(telegram_id=7, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Тест", username="t")


def test_deeds_are_written_and_found_across_scripts(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    deeds.note(7, "phone_call", {"who": "Алишер"}, {"ok": True, "contact": "Alisher Aka"}, src="телефон")
    deeds.note(7, "weather", {"city": "Andijan"}, {"now": {}})                        # чтение — не дело
    deeds.note(7, "telegram_send", {"who": "мама", "text": "буду в семь"}, {"ask_exactly": "Отправить?"})  # ещё не отправлено
    deeds.note(7, "taxi", {"destination": "Chorsu"}, {"error": "нет Яндекс Go"})      # не вышло
    deeds.note(7, "incoming_call", {"who": "Onajonim"}, src="телефон", dedupe_s=180)
    deeds.note(7, "incoming_call", {"who": "Onajonim"}, src="телефон", dedupe_s=180)  # тот же звонок второй раз
    found = deeds.search(7, "Алишеру")
    assert len(found) == 1 and "Alisher Aka" in found[0]["text"] and found[0]["src"] == "телефон"
    assert [r["tool"] for r in deeds.rows(7)] == ["phone_call", "incoming_call"]
    assert deeds.search(7, "мама") == [] and len(deeds.search(7, "мама", variants=["Onajonim"])) == 1
    assert "звонок: Alisher Aka" in deeds.line(found[0])


def test_agent_tools_run_records_deed_with_source(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    async def handler(ctx, args):  # noqa: ANN001, ANN202
        return {"ok": True}

    monkeypatch.setitem(agent_tools.TOOLS, "add_reminder", Tool("add_reminder", "x", {}, (), handler))
    ctx = ToolContext(profile=_profile(), text="")

    async def run():  # noqa: ANN202
        token = deeds.source.set("звонок")
        try:
            await agent_tools.run("add_reminder", {"text": "купить хлеб", "time": "18:00"}, ctx)
        finally:
            deeds.source.reset(token)

    asyncio.run(run())
    row = deeds.rows(7)[-1]
    assert row["tool"] == "add_reminder" and row["src"] == "звонок" and "купить хлеб" in row["text"]


def test_recall_tool_by_day_and_name(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    deeds.note(7, "web_search", {"query": "курс доллара"})
    ctx = ToolContext(profile=_profile(), text="")
    res = asyncio.run(agent_tools.run("recall_deeds", {"day": "сегодня"}, ctx))
    assert res["found"] == 1 and "искала: курс доллара" in res["items"][0]
    res = asyncio.run(agent_tools.run("recall_deeds", {"query": "Алишер"}, ctx))
    assert res["found"] == 0 and res["note"]
    assert not any(r["tool"] == "recall_deeds" for r in deeds.rows(7))           # сам вопрос — не дело


def test_weekly_text_and_voice(monkeypatch):
    sent: list = []

    class FakeBot:
        async def send_voice(self, uid, voice, caption=None, parse_mode=None):  # noqa: ANN001, ANN202
            sent.append(("voice", caption))

        async def send_message(self, uid, text, parse_mode=None):  # noqa: ANN001, ANN202
            sent.append(("text", text))

    f = {"start": date(2026, 9, 21), "end": date(2026, 9, 27),
         "money": {"spent": 1250000.0, "income": 0, "prev_spent": 1000000.0, "change_pct": 25.0, "ops": 14,
                   "top": [("Еда", 600000.0), ("Такси", 200000.0)]},
         "tasks": {"done": 5, "open": 3, "overdue": 1, "done_titles": []},
         "lessons": [{"title": "English lesson 12", "at": "12:30", "of": "40:00", "finished": False}],
         "wake": "⏰ Встал 6 из 7 дней, до такбира — 5, в среднем 1.2 звонка.",
         "jes": {"всего": 31, "звонки": 9, "поиск": 4}}

    async def facts(profile):  # noqa: ANN001, ANN202
        return f

    async def gen(prompt, **kw):  # noqa: ANN001, ANN003, ANN202
        assert "1 250 000" in prompt or "1250000" in prompt.replace(" ", "")
        return "Добрый вечер, сэр! Неделя была хорошей."

    async def synth(text, voice="Kore"):  # noqa: ANN001, ANN202
        return b"\x00\x01" * 100

    async def ogg(pcm, rate=24000):  # noqa: ANN001, ANN202
        return b"OggS"

    from bot.context import ai

    monkeypatch.setattr(weekly, "facts", facts)
    monkeypatch.setattr(ai, "generate_text", gen)
    monkeypatch.setattr(ai, "synthesize", synth)
    monkeypatch.setattr(weekly.voice_mod, "available", lambda: True)
    monkeypatch.setattr(weekly.voice_mod, "pcm_to_ogg", ogg)
    assert asyncio.run(weekly.send(FakeBot(), _profile(), Persona(voice="Sulafat"))) is True
    kind, caption = sent[0]
    assert kind == "voice" and "Итоги недели" in caption and "+25%" in caption and "сделано 5" in caption
    assert "English lesson 12" in caption and "до такбира" in caption and "JES: дел 31" in caption


def test_weekly_lessons_skip_music_and_barely_opened(monkeypatch, tmp_path):
    import time as _t

    from bot import lessons

    now = _t.time()
    rows = [{"title": "UZmir - Ummon Popuri (Audio) | barcha XIT taronalari", "position": 136, "duration": 414, "at": now},
            {"title": "Chunki Bu Biz Kliplar to'plami", "position": 104, "duration": 1717, "at": now},
            {"title": "I Tested Sonnet 5.5 vs Opus 5.5", "position": 5, "duration": 1548, "at": now},
            {"title": "How to Build $10K Websites in Minutes (Claude AI)", "position": 992, "duration": 1509, "at": now}]
    monkeypatch.setattr(lessons, "items", lambda uid: rows)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    async def empty(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return []

    monkeypatch.setattr(weekly.services, "finance_entries", empty)
    monkeypatch.setattr(weekly.services, "wake_history", empty)
    monkeypatch.setattr(weekly.db, "available", lambda name: False)
    f = asyncio.run(weekly.facts(_profile()))
    assert [x["title"] for x in f["lessons"]] == ["How to Build $10K Websites in Minutes (Claude AI)"]

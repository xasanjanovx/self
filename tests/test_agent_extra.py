"""Джарвис как ассистент: ask_user с кнопками, память, калькулятор, курсы, роутинг без тупиков."""
from __future__ import annotations

import asyncio
from datetime import date

from bot import agent_tools as tools
from bot import agent_tools_extra as extra
from bot import finance as fin
from bot.ai import AgentStep
from bot.handlers import agent
from bot.profile import Profile


def _run(coro):
    return asyncio.run(coro)


def _profile() -> Profile:
    return Profile(telegram_id=5, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Тест", username="t")


# ------------------------------------------------------------------ ask_user
def test_ask_user_tool_sets_context_and_stops_loop():
    steps = [AgentStep(parts=[{"functionCall": {"name": "ask_user", "args": {"question": "Какое такси удалить?", "options": ["Вчера 25 000", "Сегодня 25 000"]}}}],
                       text="", calls=[("ask_user", {"question": "Какое такси удалить?", "options": ["Вчера 25 000", "Сегодня 25 000"]})])]

    async def step_fn(contents, **kw):
        return steps.pop(0)

    res = _run(agent.run_agent(_profile(), "удали такси", [], snapshot="—", step_fn=step_fn))
    assert res.ctx.ask == {"question": "Какое такси удалить?", "options": ["Вчера 25 000", "Сегодня 25 000"]}
    assert res.text == "Какое такси удалить?"
    # история закрыта: call → response → текст модели с вариантами, чтобы следующий ответ пользователя лёг в тот же диалог
    assert res.contents[-1]["role"] == "model" and "Вчера 25 000" in res.contents[-1]["parts"][0]["text"]
    calls = sum(1 for m in res.contents for p in m["parts"] if "functionCall" in p)
    resps = sum(1 for m in res.contents for p in m["parts"] if "functionResponse" in p)
    assert calls == resps == 1


def test_ask_user_limits_options():
    ctx = tools.ToolContext(profile=_profile(), text="")
    out = _run(tools.run("ask_user", {"question": "?", "options": ["a", "b", "c", "d", "e", ""]}, ctx))
    assert out["status"].startswith("asked") and ctx.ask["options"] == ["a", "b", "c", "d"]
    ctx2 = tools.ToolContext(profile=_profile(), text="")
    assert "error" in _run(tools.run("ask_user", {"options": ["a"]}, ctx2)) and ctx2.ask is None


def test_reply_keyboard_renders_options():
    kb = agent._reply_kb("ru", undo_available=False, options=["Вчера", "Сегодня"])
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert data == ["agent:opt:0", "agent:opt:1", "menu:open"]


def test_no_more_dead_end_text():
    steps = [AgentStep(parts=[{"text": ""}], text="", calls=[])]

    async def step_fn(contents, **kw):
        return steps.pop(0)

    res = _run(agent.run_agent(_profile(), "ыыы", [], snapshot="—", step_fn=step_fn))
    assert "Не понял" not in res.text and "уточни" in res.text


def test_system_prompt_has_memory_and_new_rules():
    text = agent.system_prompt(_profile(), "Балансы: карта 1", "ПАМЯТЬ О ПОЛЬЗОВАТЕЛЕ:\n• Асилбек — брат")
    assert "Асилбек — брат" in text and text.index("ПАМЯТЬ") < text.index("ДАННЫЕ:")
    assert "ask_user" in text and "set_debt_deadline(person как в «Долги по займам», due_date" in text
    assert "БУХГАЛТЕР" in text and "record_debt" in text and "repay TEZ" in text
    assert "remember_about_me" in text and "currency_rates" in text and "web_search" in text
    assert "НИКОГДА не отвечай «не понял»" in text
    assert agent.system_prompt(_profile(), "x") .count("ПАМЯТЬ") == 1  # без памяти — только упоминание в правиле


# ------------------------------------------------------------------ memory
def test_merge_facts_replaces_similar_and_forgets():
    cur = ["Асилбек — брат", "зарплата 5-го числа", "обедаю в Evos"]
    out = extra.merge_facts(cur, add=["Зарплата 5-го числа, ~5 млн"], forget=["evos"])
    assert out == ["Асилбек — брат", "Зарплата 5-го числа, ~5 млн"]
    many = extra.merge_facts([], add=[f"факт {i}" for i in range(50)], forget=[])
    assert len(many) == extra.MAX_FACTS and many[-1] == "факт 49"
    assert extra.parse_facts("• a\n- b\n\n c ") == ["a", "b", "c"]


# ------------------------------------------------------------------ calculator / currency
def test_safe_eval():
    assert extra.safe_eval("(5000000-1200000)/9") == 3800000 / 9
    assert extra.safe_eval("3 000 000 * 0.12") == 360000
    assert extra.safe_eval("2^10") == 1024
    for bad in ("__import__('os')", "2**1000", "a+1", ""):
        try:
            extra.safe_eval(bad)
        except (ValueError, SyntaxError):
            continue
        raise AssertionError(bad)


def test_convert():
    rates = {"USD": 12500.0, "EUR": 13500.0}
    assert extra.convert(100, "usd", "uzs", rates) == 1_250_000
    assert round(extra.convert(1_250_000, "UZS", "USD", rates), 2) == 100
    assert round(extra.convert(100, "USD", "EUR", rates), 4) == round(100 * 12500 / 13500, 4)
    assert extra.convert(1, "USD", "GBP", rates) is None


# ------------------------------------------------------------------ routing
def test_local_finance_only_for_unambiguous_phrases():
    assert fin.local_finance("такси 25000")
    assert fin.local_finance("25000")
    assert fin.local_finance("мне должен Алишер 200000")
    # раньше уходило в финансовый парсер и заканчивалось «Не понял операцию»
    assert not fin.local_finance("запиши срок долга Uzum bank до 5 октября")
    assert not fin.local_finance("дал Асилбеку 1 млн, вернёт до 5 октября")
    assert not fin.local_finance("сколько потратил на еду?")


def test_extra_tools_registered():
    names = set(tools.TOOLS)
    assert {"ask_user", "remember_about_me", "currency_rates", "calculate", "web_search", "weather"} <= names


def test_thinking_config_by_generation():
    from bot.ai import thinking_config

    assert thinking_config("gemini-2.5-flash", 0) == {"thinkingBudget": 0}
    assert thinking_config("gemini-2.5-flash", 512) == {"thinkingBudget": 512}
    assert thinking_config("gemini-3.5-flash-lite", 0) == {"thinkingLevel": "minimal"}
    assert thinking_config("gemini-3.5-flash-lite", 512) == {"thinkingLevel": "low"}
    assert thinking_config("gemini-3.6-flash", 1024) == {"thinkingLevel": "medium"}
    assert thinking_config("gemini-2.0-flash", 512) is None and thinking_config("gemini-3.6-flash", None) is None

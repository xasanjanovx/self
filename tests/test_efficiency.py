"""27.09: экономия (данные в конце запроса агента, короче Live на телефоне), промахи из логов (звонок не тому,
«позвони мне», такси), журнал промахов и расход по клиентам с лимитом."""
import asyncio
from datetime import date

from bot import access, billing, journal, live_call, names, phone
from bot.handlers import agent
from bot.profile import Profile


def _profile(uid: int = 90) -> Profile:
    return Profile(telegram_id=uid, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Тест", username="t")


USAGE = {"promptTokenCount": 100000, "responseTokenCount": 10000,
         "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 100000}],
         "responseTokensDetails": [{"modality": "AUDIO", "tokenCount": 10000}]}   # ~$0.2 в Live


def _fresh_billing(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(billing, "_state", None)
    monkeypatch.setattr(billing, "_maybe_alert", lambda: None)
    monkeypatch.setattr(billing, "_notify", lambda text: None)
    monkeypatch.setattr(access, "is_owner", lambda uid: uid == 1)


# ------------------------------------------------------------------ агент: неизменное — в инструкции, данные — к реплике
def test_agent_static_prompt_has_no_clock_or_data():
    static = agent.static_prompt(_profile())
    assert "ДАННЫЕ:\n" not in static and "Сейчас:" not in static
    assert "[КОНТЕКСТ" in static  # модель знает, где искать данные
    full = agent.system_prompt(_profile(), "Балансы: карта 1", "ПАМЯТЬ О ПОЛЬЗОВАТЕЛЕ:\n• брат — Азиз")
    assert full.startswith(static) and "Балансы: карта 1" in full and "Сейчас:" in full


def test_agent_sends_context_with_the_reply_but_does_not_store_it():
    seen: list[tuple[str, list]] = []

    async def step(contents, *, system, tools, thinking_budget):  # noqa: ANN001
        seen.append((system, contents))

        class S:
            parts = [{"text": "ок"}]
            text = "ок"
            calls: list = []

        return S()

    history = [{"role": "user", "parts": [{"text": "раньше"}]}, {"role": "model", "parts": [{"text": "да"}]}]
    res = asyncio.run(agent.run_agent(_profile(), "сколько на карте?", history, snapshot="Балансы: карта 1", step_fn=step))
    system, sent = seen[0]
    assert "Балансы" not in system and "Сейчас:" not in system
    assert sent[:2] == history  # история как есть — префикс для кэша
    assert "Балансы: карта 1" in sent[2]["parts"][0]["text"] and sent[2]["parts"][1]["text"] == "сколько на карте?"
    stored = res.contents[2]["parts"]
    assert stored == [{"text": "сколько на карте?"}]  # в историю данные не попадают


# ------------------------------------------------------------------ телефон: короче Live
def test_phone_live_tools_are_short_and_data_goes_to_bot_task():
    decls = live_call.tool_declarations("phone")
    names_ = {d["name"] for d in decls}
    assert {"add_finance_entries", "add_calorie_logs", "add_reminder", "add_task", "get_finance_stats", "weather"}.isdisjoint(names_)
    assert {"bot_task", "phone_task", "phone_call", "set_alarm", "open_app"} <= names_
    assert all(len(d["description"]) <= live_call.PHONE_DESC_LIMIT + 1 for d in decls)
    # а phone_task (Flash-Lite) умеет всё, что ушло из Live
    task = {d["name"] for d in live_call.phone_task_declarations()}
    assert {"add_finance_entries", "weather", "taxi"} <= task


def test_phone_rules_answer_in_language_of_last_phrase():
    assert "ПОСЛЕДНЕЙ фразы" in live_call.PHONE_RULES and "позвони мне" in live_call.PHONE_RULES


# ------------------------------------------------------------------ звонок не тому / «позвони мне»
def test_mentioned_person():
    assert names.mentioned(["папа"], "джес позвони папе")
    assert names.mentioned(["мама", "ойи"], "онамга позвони")  # родство на узбекском
    assert names.mentioned(["Хусан"], "позвони хусану")
    assert names.mentioned(["Alisher"], "набери алишера")
    assert not names.mentioned(["папа"], "джаз вызови такси")   # 26.09: модель позвонила папе
    assert not names.mentioned(["папа"], "джес позвони")
    assert names.mentioned(["папа"], "да Позвонить папе?")       # он ответил «да» на вопрос JES
    assert names.mentioned(["+998901234567"], "позвони")
    assert names.mentioned(["папа"], "")                          # не расслышали — не мешаем


def test_call_me_rings_via_telegram(monkeypatch):
    from bot import call_assistant, caller

    calls: list = []
    monkeypatch.setattr(caller, "available", lambda: True)
    monkeypatch.setattr(call_assistant, "call_in_background", lambda profile, **kw: calls.append(profile.telegram_id))

    class Ctx:
        profile = _profile(7)

    turn = phone.PhoneTurn(uid=7)
    res = asyncio.run(phone.PHONE_TOOLS["phone_call"].handler(turn, Ctx(), {"who": "мне"}))
    assert res.get("calling_via_telegram") and calls == [7] and not turn.actions
    assert names.is_self("меня") and names.is_self("menga") and not names.is_self("маме")


# ------------------------------------------------------------------ журнал промахов
def test_journal_report(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    journal.miss(5, "wrong_call", "модель хотела позвонить «папа»")
    journal.miss(5, "false_wake", "не «Джес»: «ку»")
    journal.check_session(5, ["открой айгра приложение"], ["Kechirasiz, Igra nomli ilovani topa olmadim"])
    journal.app_events(5, [{"t": 0, "level": "crash", "tag": "JarvisService", "msg": "NullPointerException"}])
    lines = "\n".join(journal.report_lines(date.today(), 5))
    assert "звонок не тому" in lines and "не на том языке" in lines and "сбой приложения" in lines
    assert "ложное «Джес» (отсеяно): 1" in lines
    assert journal.report_lines(date.today(), 6) == []  # чужие промахи — не ему


def test_journal_quiet_day(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    journal.miss(5, "false_wake", "x")
    assert "серьёзных нет" in "\n".join(journal.report_lines(date.today(), 5))


# ------------------------------------------------------------------ расход по клиентам и лимит
def test_spend_is_split_by_client_and_limits_apply(tmp_path, monkeypatch):
    _fresh_billing(tmp_path, monkeypatch)
    token = billing.set_user(42)
    billing.record("gemini-3.8-live", USAGE, kind="live")
    billing.reset_user(token)
    assert billing.user_spent_today(42) > 0.1 and billing.user_over_limit(42)   # лимит клиента $0.10
    assert not billing.user_over_limit(1)                                      # владелец — без клиентского лимита
    assert not billing.over_limit()  # его $0.2 не съедают лимит владельца ($0.5)
    token = billing.set_user(1)
    for _ in range(3):
        billing.record("gemini-3.8-live", USAGE, kind="live")
    billing.reset_user(token)
    assert billing.over_limit()
    rows = billing.clients_report(30)
    assert [r["uid"] for r in rows] == [42] and rows[0]["today"] == rows[0]["period"] > 0


def test_unattributed_spend_counts_as_owner(tmp_path, monkeypatch):
    _fresh_billing(tmp_path, monkeypatch)
    for _ in range(3):
        billing.record("gemini-3.8-live", USAGE, kind="live")  # фоновые задачи без клиента — это его расход
    assert billing.over_limit() and billing.clients_report() == []

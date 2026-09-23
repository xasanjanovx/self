"""Голосовой Джарвис на телефоне: поиск имён, да/нет, расшифровка, инструменты и подтверждение отправки."""
from __future__ import annotations

import asyncio

import pytest

from bot import names, phone, services, tg_user
from bot.ai import AgentStep
from bot.handlers import agent
from bot.phone_api import clean_transcript
from bot.profile import Profile

CONTACTS = [
    {"name": "Ойижон", "phones": ["+998901111111"]},
    {"name": "Dadajon", "phones": ["+998902222222"]},
    {"name": "Alisher aka", "phones": ["+998903333333"]},
    {"name": "Bobur aka", "phones": ["+998904444444"]},
    {"name": "Лола опа", "phones": ["+998905555555"]},
    {"name": "Азиз", "phones": ["+998906666666"]},
]


def _profile() -> Profile:
    return Profile(telegram_id=77, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Тест", username="t")


def _names(found: dict) -> list[str] | str | None:
    if "match" in found:
        return found["match"]["name"]
    if "candidates" in found:
        return [c["name"] for c in found["candidates"]]
    return None


# ------------------------------------------------------------------ names
@pytest.mark.parametrize("queries, expected", [
    (["мама"], "Ойижон"),
    (["папа", "ota"], "Dadajon"),
    (["Алишер"], "Alisher aka"),
    (["алишер ака"], "Alisher aka"),
    (["Бобур"], "Bobur aka"),
    (["Азизу"], "Азиз"),
    (["Лоле"], "Лола опа"),
    (["Сардор"], None),
])
def test_resolve_contacts(queries, expected):
    assert _names(names.resolve(queries, CONTACTS)) == expected


def test_resolve_ambiguous_returns_candidates():
    both = CONTACTS + [{"name": "Мама Beeline", "phones": ["+998907777777"]}]
    assert set(_names(names.resolve(["мама"], both))) == {"Ойижон", "Мама Beeline"}


def test_norm_translit_and_apostrophes():
    assert names.norm("Ғайрат О'ғли") == "gayrat ogli"
    assert names.norm("  Хасан!! ") == "xasan"


def test_phone_number_detection():
    assert names.as_phone_number("+998 90 123-45-67") == "+998901234567"
    assert names.as_phone_number("мама") is None


# ------------------------------------------------------------------ yes / no / transcript
@pytest.mark.parametrize("text", ["Да", "да, отправь", "Ha", "ok", "Давай!", "Джарвис, да"])
def test_is_yes(text):
    assert phone.is_yes(text) and not phone.is_no(text)


@pytest.mark.parametrize("text", ["нет", "Не надо", "yo'q", "отмена"])
def test_is_no(text):
    assert phone.is_no(text) and not phone.is_yes(text)


@pytest.mark.parametrize("text", ["да, но напиши что буду в восемь", "нет, напиши Алишеру", "позвони маме"])
def test_long_phrases_are_not_short_answers(text):
    assert not phone.is_yes(text) and not phone.is_no(text)


def test_clean_transcript():
    assert clean_transcript("Эй, Джарвис, позвони маме.") == "позвони маме."
    assert clean_transcript("Hey Jarvis what time") == "what time"
    assert clean_transcript("<пусто>") == ""
    assert clean_transcript("Джарвис") == ""


# ------------------------------------------------------------------ declarations
def test_phone_declarations_valid_and_without_screen_tools():
    decls = phone.declarations()
    names_ = [d["name"] for d in decls]
    assert len(names_) == len(set(names_))
    assert not set(names_) & phone.EXCLUDED_BOT_TOOLS
    assert {"phone_call", "telegram_send", "confirm_send", "set_alarm", "add_finance_entries"} <= set(names_)
    for d in decls:
        params = d["parameters"]
        assert params["type"] == "OBJECT"
        for key, prop in params["properties"].items():
            assert prop["type"] in {"STRING", "NUMBER", "INTEGER", "BOOLEAN", "ARRAY", "OBJECT"}, (d["name"], key)
            if prop["type"] == "ARRAY":
                assert "items" in prop
        for req in params.get("required", []):
            assert req in params["properties"], (d["name"], req)


# ------------------------------------------------------------------ agent loop with phone tools
def _scripted(*steps: AgentStep):
    queue = list(steps)

    async def step_fn(contents, **kwargs):
        return queue.pop(0)

    return step_fn


def _call(name: str, **args) -> AgentStep:
    return AgentStep(parts=[{"functionCall": {"name": name, "args": args}}], text="", calls=[(name, args)], finish="STOP")


def _say(text: str) -> AgentStep:
    return AgentStep(parts=[{"text": text}], text=text, calls=[], finish="STOP")


def _run(turn, text, *steps):
    return asyncio.run(agent.run_agent(_profile(), text, [], snapshot="", step_fn=_scripted(*steps),
                                       run_tool=phone.make_runner(turn), decls=phone.declarations(),
                                       system_extra=phone.system_extra(turn, None)))


@pytest.fixture
def uid(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    phone._contacts.clear()
    phone.set_pending(77, None)
    return 77


def test_call_mom_adds_call_action(uid):
    phone.save_contacts(uid, [{"n": c["name"], "p": c["phones"]} for c in CONTACTS])
    turn = phone.PhoneTurn(uid=uid)
    result = _run(turn, "позвони маме", _call("phone_call", who="мама", variants=["ойи", "ona"]), _say("Звоню маме."))
    assert turn.actions == [{"type": "call", "number": "+998901111111", "name": "Ойижон"}]
    assert result.text == "Звоню маме."


def test_call_without_contacts_asks_phone_to_upload(uid):
    turn = phone.PhoneTurn(uid=uid)
    _run(turn, "позвони маме", _call("phone_call", who="мама"), _say("Секунду, контакты ещё не загрузились."))
    assert turn.actions == [] and turn.need_contacts


def test_alarm_and_bad_alarm(uid):
    turn = phone.PhoneTurn(uid=uid)
    _run(turn, "разбуди в 6:30 по будням", _call("set_alarm", time="6:30", days=["mon", "fri"]), _say("Поставил."))
    assert turn.actions == [{"type": "alarm", "hour": 6, "minute": 30, "label": "Джарвис", "days": [2, 6]}]
    turn2 = phone.PhoneTurn(uid=uid)
    _run(turn2, "будильник", _call("set_alarm", time="25:00"), _say("Во сколько?"))
    assert turn2.actions == []


def test_excluded_bot_tool_is_refused(uid):
    turn = phone.PhoneTurn(uid=uid)
    result = asyncio.run(phone.make_runner(turn)("open_screen", {"screen": "finance"}, agent.tools.ToolContext(profile=_profile(), text="")))
    assert "error" in result


def test_telegram_send_waits_for_yes_then_sends(uid, monkeypatch):
    chat = {"id": 501, "name": "Ойижон", "username": "", "kind": "user", "unread": 0, "muted": False}
    sent: list[tuple[int, str]] = []

    async def fake_dialogs(*, fresh=False):
        return [chat]

    async def fake_send(c, text):
        sent.append((c["id"], text))
        return True

    async def fake_log(*args, **kwargs):
        return None

    monkeypatch.setattr(tg_user, "configured", lambda: True)
    monkeypatch.setattr(tg_user, "dialogs", fake_dialogs)
    monkeypatch.setattr(tg_user, "send", fake_send)
    monkeypatch.setattr(services, "log_agent", fake_log)

    turn = phone.PhoneTurn(uid=uid)
    _run(turn, "напиши маме что буду в семь", _call("telegram_send", who="мама", text="Буду в семь"),
         _say("Отправить в Telegram — Ойижон: «Буду в семь»?"))
    assert sent == [] and turn.listen
    assert phone.get_pending(uid)["chat_id"] == 501

    out = asyncio.run(phone.handle(uid, "да", {}))
    assert sent == [(501, "Буду в семь")]
    assert "Отправил" in out["say"] and phone.get_pending(uid) is None


def test_no_cancels_pending(uid):
    phone.set_pending(uid, {"kind": "sms", "number": "+1", "name": "Азиз", "text": "ок"})
    out = asyncio.run(phone.handle(uid, "нет", {}))
    assert out["actions"] == [] and phone.get_pending(uid) is None


def test_sms_confirm_returns_sms_action(uid, monkeypatch):
    async def fake_log(*args, **kwargs):
        return None

    monkeypatch.setattr(services, "log_agent", fake_log)
    phone.set_pending(uid, {"kind": "sms", "number": "+998906666666", "name": "Азиз", "text": "Перезвоню"})
    out = asyncio.run(phone.handle(uid, "ha", {}))
    assert out["actions"] == [{"type": "sms", "number": "+998906666666", "text": "Перезвоню", "name": "Азиз"}]


# ------------------------------------------------------------------ quick replies (без второго запроса к модели)
def _run_quick(turn, text, *steps):
    return asyncio.run(agent.run_agent(_profile(), text, [], snapshot="", step_fn=phone.make_step(turn, "ru", _scripted(*steps)),
                                       run_tool=phone.make_runner(turn), decls=phone.declarations(),
                                       system_extra=phone.system_extra(turn, None)))


def test_quick_reply_for_call_skips_second_model_step(uid):
    phone.save_contacts(uid, [{"n": c["name"], "p": c["phones"]} for c in CONTACTS])
    turn = phone.PhoneTurn(uid=uid)
    result = _run_quick(turn, "позвони маме", _call("phone_call", who="мама"))  # второго шага в сценарии нет
    assert result.text == "Звоню: Ойижон." and turn.actions[0]["type"] == "call"


def test_quick_reply_for_alarm_and_timer(uid):
    turn = phone.PhoneTurn(uid=uid)
    result = _run_quick(turn, "будильник на 7 и таймер 10 минут",
                        AgentStep(parts=[], text="", calls=[("set_alarm", {"time": "07:00"}), ("set_timer", {"seconds": 600})], finish="STOP"))
    assert result.text == "Ставлю будильник на 07:00. Таймер на 10 минут."


def test_quick_reply_for_pending_message_is_the_confirmation_question(uid, monkeypatch):
    chat = {"id": 9, "name": "Азиз", "username": "", "kind": "user", "unread": 0, "muted": False}

    async def fake_dialogs(*, fresh=False):
        return [chat]

    monkeypatch.setattr(tg_user, "configured", lambda: True)
    monkeypatch.setattr(tg_user, "dialogs", fake_dialogs)
    turn = phone.PhoneTurn(uid=uid)
    result = _run_quick(turn, "напиши Азизу ок", _call("telegram_send", who="Азиз", text="Ок"))
    assert result.text == "Отправить в Telegram — Азиз: «Ок»?" and turn.listen


def test_no_quick_reply_when_contact_is_ambiguous(uid):
    phone.save_contacts(uid, [{"n": "Ойижон", "p": ["1"]}, {"n": "Мама Beeline", "p": ["2"]}])
    turn = phone.PhoneTurn(uid=uid)
    result = _run_quick(turn, "позвони маме", _call("phone_call", who="мама"), _say("Какой маме: Ойижон или Мама Beeline?"))
    assert turn.actions == [] and "Ойижон" in result.text

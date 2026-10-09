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
@pytest.mark.parametrize("text", ["Да", "да, отправь", "Ha", "ok", "Давай!", "Джес, да"])
def test_is_yes(text):
    assert phone.is_yes(text) and not phone.is_no(text)


@pytest.mark.parametrize("text", ["нет", "Не надо", "yo'q", "отмена"])
def test_is_no(text):
    assert phone.is_no(text) and not phone.is_yes(text)


@pytest.mark.parametrize("text", ["да, но напиши что буду в восемь", "нет, напиши Алишеру", "позвони маме"])
def test_long_phrases_are_not_short_answers(text):
    assert not phone.is_yes(text) and not phone.is_no(text)


def test_clean_transcript():
    assert clean_transcript("Эй, Джес, позвони маме.") == "позвони маме."
    assert clean_transcript("Hey Jes what time") == "what time"
    assert clean_transcript("<пусто>") == ""
    assert clean_transcript("Джес") == ""
    assert clean_transcript("Джарвис, позвони маме") == "Джарвис, позвони маме"  # прежнее имя — просто слово
    assert clean_transcript("Джесс, позвони маме") == "позвони маме" and clean_transcript("эй джес открой ютуб") == "открой ютуб"
    assert clean_transcript("Зеки, позвони маме") == "Зеки, позвони маме"


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
    assert turn.actions == [{"type": "alarm", "hour": 6, "minute": 30, "label": "JES", "days": [2, 6]}]
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


def test_similar_rare_contacts_ask_frequent_one_wins(uid):
    """26.09 его выбор: двое почти одинаковых — звоним тому, кому он чаще звонит; оба редкие — коротко спросить."""
    phone.save_contacts(uid, [{"n": "Ойижон", "p": ["1"]}, {"n": "Мама Beeline", "p": ["2"]}])
    found = phone.find_contact(uid, "мама", [])
    assert found["ask"] == ["Ойижон", "Мама Beeline"]
    assert phone.ask_which(found)["ask_exactly"] == "Ойижон или Мама Beeline?"
    phone.save_contacts(uid, [{"n": "Ойижон", "p": ["1"], "c": 25}, {"n": "Мама Beeline", "p": ["2"]}])
    turn = phone.PhoneTurn(uid=uid)
    _run_quick(turn, "позвони маме", _call("phone_call", who="мама"), _say("Звоню."))
    assert len(turn.actions) == 1 and turn.actions[0]["type"] == "call" and turn.actions[0]["name"] == "Ойижон"


def test_brother_is_not_any_aka(uid):
    """«брат» звонил случайному «… Aka» (у него ~180 таких контактов): «ака» — вежливость, а «акам» — брат."""
    phone.save_contacts(uid, [{"n": "Mashxurbek Aka ISH", "p": ["1"]}, {"n": "ABDULATIF AKA", "p": ["2"]},
                              {"n": "SIROJBEK AKAM", "p": ["3"]}, {"n": "Sirojiddin Aka", "p": ["4"]}])
    assert phone.find_contact(uid, "брат", ["akam"])["match"]["name"] == "SIROJBEK AKAM"
    # запомнил «брат» по корню — «брату», «akamga» и «акам» ведут туда же
    phone.learn_alias(uid, "phone", "брату", "SIROJBEK AKAM")
    for who in ("брат", "akamga", "Акам"):
        assert phone.find_contact(uid, who, [])["match"]["name"] == "SIROJBEK AKAM"
    assert "Родные: brat — SIROJBEK AKAM." in phone.people_line(uid)


def test_unclear_pick_is_not_learned(uid):
    """Неуверенный выбор не запоминаем — иначе ошибка закрепляется навсегда («srachbek aka» → ABDULATIF AKA)."""
    phone.save_contacts(uid, [{"n": "ABDULATIF AKA", "p": ["1"]}, {"n": "SIROJBEK AKAM", "p": ["2"]}])
    turn = phone.PhoneTurn(uid=uid)
    _run_quick(turn, "позвони Срачбек ака", _call("phone_call", who="Срачбек ака"), _say("Звоню."))
    assert "srachbek aka" not in phone.aliases(uid)["phone"]


def test_frequency_boost_grows_slowly():
    from bot import names

    assert names.frequency_boost(0) == 0 and names.frequency_boost(1) == 0.03
    assert names.frequency_boost(3) == 0.06 and names.frequency_boost(100) == 0.12


def test_mama_prefers_person_over_organization_and_learns(uid):
    phone.save_contacts(uid, [{"n": "Ona va bola Markazi", "p": ["+998901110000"]}, {"n": "ONAJONIM", "p": ["+998902220000"]},
                              {"n": "Mashhur bek aka", "p": ["+998903330000"]}])
    found = phone.find_contact(uid, "мама", ["ойи", "ona", "oyijon"])
    assert found["match"]["name"] == "ONAJONIM"
    phone.learn_alias(uid, "phone", "работа", "Mashhur bek aka")
    assert phone.find_contact(uid, "работа", [])["match"]["name"] == "Mashhur bek aka"


def test_call_log_breaks_ties(uid):
    phone.save_contacts(uid, [{"n": "Alisher aka", "p": ["+998901111111"]}, {"n": "Alisher Ishxona", "p": ["+998902222222"]}])
    device = {"calls": [{"number": "+998902222222", "type": "out"}] * 3}
    assert phone.find_contact(uid, "Алишер", [], device)["match"]["name"] == "Alisher Ishxona"


@pytest.mark.parametrize("n, word", [(1, "минуту"), (3, "минуты"), (5, "минут"), (11, "минут"), (21, "минуту"), (24, "минуты")])
def test_ru_plural(n, word):
    assert phone.ru_plural(n, "минуту", "минуты", "минут") == word


def test_action_phrases():
    assert phone.action_phrase({"type": "timer", "seconds": 180}) == "Таймер на 3 минуты."
    assert phone.action_phrase({"type": "timer", "seconds": 3600}) == "Таймер на 1 час."
    assert phone.action_phrase({"type": "call", "name": "Ойижон"}) == "Звоню: Ойижон."


def test_honorific_aka_does_not_decide(uid):
    """«Срачбек ака» (плохо расслышанное «Сирожбек акам») не должно уводить к «ABDULATIF AKA» по слову «aka»."""
    phone.save_contacts(uid, [{"n": "ABDULATIF AKA", "p": ["1"]}, {"n": "AKBARJON AKA", "p": ["2"]},
                              {"n": "SIROJBEK AKAM", "p": ["3"], "c": 12}, {"n": "Sirojiddin Aka", "p": ["4"]}, {"n": "SIROJIDDIN", "p": ["5"]}])
    for who in ("Срачбек ака", "Сарочубек акам", "Сирожбек"):
        found = phone.find_contact(uid, who, [])
        assert found.get("match", {}).get("name") == "SIROJBEK AKAM", (who, found)
    assert phone.find_contact(uid, "Абдулатиф ака", [])["match"]["name"] == "ABDULATIF AKA"
    assert phone.find_contact(uid, "Сирожиддин ака", [])["match"]["name"] == "Sirojiddin Aka"


# ------------------------------------------------------------------ «Избранное» (Saved Messages) — JES раньше не находил этот чат
def _fake_tg(monkeypatch, me_id=5):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    sent: list = []
    asked: list = []

    class Client:
        async def send_message(self, peer, text):
            sent.append((peer, text))

        async def iter_messages(self, peer, limit=None):
            asked.append(peer)
            yield SimpleNamespace(out=True, sender_id=me_id, sender=None, message="купить хлеб", media=None, date=datetime.now(timezone.utc))

    async def fake_client():
        return Client()

    async def no_dialogs(*, fresh=False):
        return []

    async def fake_log(*args, **kwargs):
        return None

    monkeypatch.setattr(tg_user, "configured", lambda: True)
    monkeypatch.setattr(tg_user, "client", fake_client)
    monkeypatch.setattr(tg_user, "_me", SimpleNamespace(id=me_id), raising=False)
    monkeypatch.setattr(tg_user, "dialogs", no_dialogs)
    monkeypatch.setattr(services, "log_agent", fake_log)
    return sent, asked


def test_self_chat_words_are_recognised():
    for word in ("избранное", "Избранное", "в избранное", "Saved Messages", "себе", "saqlangan xabarlar"):
        assert tg_user.is_self_query([word]), word
    for word in ("мама", "Азиз", "избранный друг Алишер", ""):
        assert not tg_user.is_self_query([word]), word


def test_jes_reads_the_saved_messages(uid, monkeypatch):
    sent, asked = _fake_tg(monkeypatch)
    turn = phone.PhoneTurn(uid=uid)
    ctx = agent.tools.ToolContext(profile=_profile(), text="")
    result = asyncio.run(phone.make_runner(turn)("telegram_read", {"who": "избранное", "limit": 1}, ctx))
    assert result["chat"] == "Избранное" and result["messages"][0]["text"] == "купить хлеб" and result["messages"][0]["from"] == "я"
    assert asked == ["me"]


def test_jes_writes_to_the_saved_messages_after_confirmation(uid, monkeypatch):
    sent, _ = _fake_tg(monkeypatch)
    turn = phone.PhoneTurn(uid=uid)
    ctx = agent.tools.ToolContext(profile=_profile(), text="")
    result = asyncio.run(phone.make_runner(turn)("telegram_send", {"who": "себе", "text": "Купить хлеб"}, ctx))
    assert result["status"] == "awaiting_confirmation" and "Избранное" in result["ask_exactly"] and sent == []
    out = asyncio.run(phone.handle(uid, "да", {}))
    assert sent == [("me", "Купить хлеб")] and "Отправил" in out["say"]


def test_telegram_tool_failures_are_reported_not_raised(uid, monkeypatch):
    _fake_tg(monkeypatch)

    async def boom(queries, alias=None):
        raise ConnectionError("net")

    monkeypatch.setattr(tg_user, "find_chat", boom)
    turn = phone.PhoneTurn(uid=uid)
    ctx = agent.tools.ToolContext(profile=_profile(), text="")
    read = asyncio.run(phone.make_runner(turn)("telegram_read", {"who": "Алишер"}, ctx))
    send = asyncio.run(phone.make_runner(turn)("telegram_send", {"who": "Алишер", "text": "привет"}, ctx))
    assert "ConnectionError" in read["error"] and "ConnectionError" in send["error"]


# ------------------------------------------------------------------ 09.10: «JES не видит сообщения в Telegram и пропущенные»
def _fake_digest(monkeypatch, *, unread=(), missed=(), latest=()):
    async def fake_unread(*a, **k):
        return list(unread)

    async def fake_missed(*a, **k):
        return list(missed)

    async def fake_latest(*a, **k):
        return list(latest)

    monkeypatch.setattr(tg_user, "configured", lambda: True)
    monkeypatch.setattr(tg_user, "unread", fake_unread)
    monkeypatch.setattr(tg_user, "missed_calls", fake_missed)
    monkeypatch.setattr(tg_user, "latest", fake_latest)


def test_what_is_new_in_telegram_gives_unread_and_missed_calls(uid, monkeypatch):
    unread = [{"chat": "Алишер", "kind": "user", "unread": 2, "messages": [{"from": "Алишер", "text": "позвони", "time": "09.10 09:56"}]}]
    missed = [{"from": "Мама", "count": 1, "last": "09.10 08:10"}]
    _fake_digest(monkeypatch, unread=unread, missed=missed)
    turn = phone.PhoneTurn(uid=uid)
    ctx = agent.tools.ToolContext(profile=_profile(), text="")
    out = asyncio.run(phone.make_runner(turn)("telegram_read", {}, ctx))
    assert out["unread_chats"] == unread and out["missed_telegram_calls"] == missed and "latest_chats" not in out


def test_nothing_unread_still_shows_the_latest_messages_instead_of_nothing(uid, monkeypatch):
    latest = [{"chat": "Алишер", "kind": "user", "messages": [{"from": "Алишер", "text": "ок", "time": "09.10 09:56"}]}]
    _fake_digest(monkeypatch, latest=latest)
    turn = phone.PhoneTurn(uid=uid)
    ctx = agent.tools.ToolContext(profile=_profile(), text="")
    out = asyncio.run(phone.make_runner(turn)("telegram_read", {}, ctx))
    assert out["unread_chats"] == [] and out["latest_chats"] == latest and "нет" in out["note"]


def test_search_for_the_word_favorites_reads_the_saved_messages(uid, monkeypatch):
    """08.10 20:39 модель искала слово «Избранное» по всем чатам вместо чтения чата с самим собой."""
    sent, asked = _fake_tg(monkeypatch)
    turn = phone.PhoneTurn(uid=uid)
    ctx = agent.tools.ToolContext(profile=_profile(), text="")
    result = asyncio.run(phone.make_runner(turn)("telegram_search", {"query": "Избранное", "limit": 1}, ctx))
    assert result["chat"] == "Избранное" and result["messages"][0]["text"] == "купить хлеб" and asked == ["me"]


def test_message_time_is_shown_in_his_time_zone_not_the_server_utc():
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    msg = SimpleNamespace(out=False, sender_id=5, sender=None, message="привет", media=None, date=datetime(2026, 10, 9, 4, 56, tzinfo=timezone.utc))
    assert tg_user._msg_view(msg, me_id=1, chat_name="Алишер", tz=ZoneInfo("Asia/Tashkent"))["time"] == "09.10 09:56"
    assert tg_user._msg_view(msg, me_id=1, chat_name="Алишер")["time"] == "09.10 09:56"       # без зоны — Ташкент по умолчанию


def test_missed_telegram_calls_skip_the_helper_account_and_outgoing(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from bot import caller

    class PhoneCallDiscardReasonMissed: ...
    class PhoneCallDiscardReasonHangup: ...
    class MessageActionPhoneCall:
        def __init__(self, reason):
            self.reason = reason

    now = datetime.now(timezone.utc)
    history = {
        1: [SimpleNamespace(date=now - timedelta(hours=2), out=False, action=MessageActionPhoneCall(PhoneCallDiscardReasonMissed())),
            SimpleNamespace(date=now - timedelta(hours=3), out=False, action=MessageActionPhoneCall(PhoneCallDiscardReasonHangup())),
            SimpleNamespace(date=now - timedelta(hours=60), out=False, action=MessageActionPhoneCall(PhoneCallDiscardReasonMissed()))],
        2: [SimpleNamespace(date=now - timedelta(hours=1), out=False, action=MessageActionPhoneCall(PhoneCallDiscardReasonMissed()))],   # помощник JES
        3: [SimpleNamespace(date=now - timedelta(hours=1), out=True, action=MessageActionPhoneCall(PhoneCallDiscardReasonMissed()))],    # он не дозвонился
    }

    class Client:
        async def iter_messages(self, peer, limit=None):
            for m in history[peer]:
                yield m

    async def fake_client():
        return Client()

    async def fake_dialogs(*, fresh=False):
        return [{"id": i, "name": n, "kind": "user", "unread": 0, "muted": False, "_peer": i} for i, n in ((1, "Мама"), (2, "Джес"), (3, "Алишер"))]

    monkeypatch.setattr(tg_user, "client", fake_client)
    monkeypatch.setattr(tg_user, "dialogs", fake_dialogs)
    monkeypatch.setattr(caller, "helper_id", 2)
    out = asyncio.run(tg_user.missed_calls(hours=48))
    assert [(r["from"], r["count"]) for r in out] == [("Мама", 1)]      # старше 48 ч, не пропущенное, помощник и исходящее — не в счёт

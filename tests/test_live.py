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
    text = live_call.system_instruction(_profile(), persona.Persona(honorific="shef"), mode="wake",
                                        wake={"takbir": "05:07", "minutes_left": 25})
    assert "05:07" in text and "25 минут" in text
    assert "confirm_awake" in text and "ещё НЕ проснулся" in text and "1–2 минуты" in text
    # мотивация вместо заданий + проверка по голосу с обращением из настроек
    assert "мунафик" in text and "Никаких упражнений" in text
    assert "убедиться, что вы уже встали с кровати, Шеф" in text
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


def test_language_is_locked_to_settings():
    uz = persona.lang_rule(persona.Persona(lang="uz"))
    assert "ТОЛЬКО по-узбекски" in uz and "Никогда не переходи" in uz
    assert "отвечай по-русски" not in uz  # раньше при русской речи переключался — теперь нет
    ru = persona.lang_rule(persona.Persona(lang="ru"))
    assert "ТОЛЬКО по-русски" in ru


def test_assistant_is_general_and_knows_itself():
    text = live_call.system_instruction(_profile(), persona.Persona(), mode="assistant")
    assert "не только финансовый" in text.lower() or "НЕ ТОЛЬКО ФИНАНСОВЫЙ" in text
    assert "О СЕБЕ" in text and "в месяц" in text
    assert "шутит" in text  # живой характер
    from bot.handlers.agent import system_prompt

    chat = system_prompt(_profile(), "")
    assert "О СЕБЕ" in chat and "не могу" in chat


def test_setup_payload_rich_and_plain():
    sess = live_call._Session(_profile(), persona.Persona(lang="uz"), mode="assistant", system="x")
    rich = sess.setup_payload("m", rich=True)["setup"]
    assert "enableAffectiveDialog" not in rich["generationConfig"]  # ломает генерацию (проверено на сервере)
    assert rich["generationConfig"]["speechConfig"]["languageCode"] == "uz-UZ"
    names = {d["name"] for d in rich["tools"][-1]["functionDeclarations"]}
    assert "send_to_chat" in names and "web_search" in names
    plain = sess.setup_payload("m", rich=False)["setup"]
    assert "languageCode" not in plain["generationConfig"]["speechConfig"] and len(plain["tools"]) == 1
    assert "web_search" in {d["name"] for d in plain["tools"][0]["functionDeclarations"]}
    wake = live_call._Session(_profile(), persona.Persona(), mode="wake", system="x").setup_payload("m", rich=True)["setup"]
    assert "web_search" not in {d["name"] for d in wake["tools"][0]["functionDeclarations"]}


class _WS:
    def __init__(self, closed: bool = False) -> None:
        self.closed = closed
        self.sent: list[str] = []

    async def send_str(self, data: str) -> None:
        if self.closed:
            raise ConnectionResetError("Cannot write to closing transport")
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


class _RingSess:
    """Сессия для _live_ready: подключение, отправка и приём — без сети."""

    def __init__(self, fresh: "_WS", out: bytes = b"", greeting: str = "") -> None:
        self.fresh = fresh
        self.connects = 0
        self.out = bytearray(out)
        self.greeting = greeting
        self.pre_answer = True

    async def connect(self, http):  # noqa: ANN001
        self.connects += 1
        return self.fresh

    async def send(self, ws, payload):  # noqa: ANN001
        import json

        try:
            await ws.send_str(json.dumps(payload, ensure_ascii=False))
            return True
        except Exception:
            return False

    async def downlink(self, ws):  # noqa: ANN001
        import asyncio

        await asyncio.sleep(3600)


def _first_turn(ws: "_WS") -> dict:
    import json

    return json.loads(ws.sent[0])["clientContent"]


def test_live_reconnects_if_session_died_while_ringing():
    """Утром трубку взяли через 32 с — сессия Gemini уже закрылась, бот падал с висящим звонком."""
    import asyncio

    async def scenario():
        dead = _WS(closed=True)

        async def early_conn():
            return dead, None

        # приветствие не успело — просим начать заново
        sess = _RingSess(_WS())
        ws, down = await live_call._live_ready(sess, None, asyncio.create_task(early_conn()), live_call.KICK)
        assert ws is sess.fresh and sess.connects == 1 and not sess.pre_answer
        turn = _first_turn(ws)
        assert "Звонок соединён" in turn["turns"][0]["parts"][0]["text"] and turn["turnComplete"] is True
        down.cancel()

        # приветствие уже накоплено — не здороваемся второй раз, только сообщаем модели, что сказала
        sess = _RingSess(_WS(), out=b"\x00" * 4800, greeting="Доброе утро, Шеф!")
        ws, down = await live_call._live_ready(sess, None, asyncio.create_task(early_conn()), live_call.KICK)
        turn = _first_turn(ws)
        assert "уже поздоровалась" in turn["turns"][0]["parts"][0]["text"] and turn["turnComplete"] is False
        assert len(sess.out) == 4800  # накопленный голос сыграем сразу
        down.cancel()

    asyncio.run(scenario())


def test_live_keeps_pregreeted_session():
    """Трубку взяли, пока сессия жива: берём её и её приём как есть — приветствие уже в очереди."""
    import asyncio

    async def scenario():
        alive = _WS()
        sess = _RingSess(_WS())
        down = asyncio.create_task(asyncio.sleep(3600))

        async def early_conn():
            return alive, down

        ws, d = await live_call._live_ready(sess, None, asyncio.create_task(early_conn()), live_call.KICK)
        assert ws is alive and d is down and sess.connects == 0 and not sess.pre_answer
        down.cancel()

    asyncio.run(scenario())


def test_call_over_flags():
    """«Положили трубку» — по флагам pytgcalls (занято приходит как DISCARDED|BUSY), текст — запасной путь."""
    from bot import caller

    assert caller.is_call_over("Status.DISCARDED_CALL|BUSY_CALL")
    assert caller.is_call_over("LEFT_CALL")
    assert not caller.is_call_over("Status.INCOMING_CALL")
    assert caller._is_incoming("Status.INCOMING_CALL") and not caller._is_incoming("Status.INCOMING_CONFERENCE_CALL")


def test_live_tools_run_in_parallel():
    """Два инструмента в одном ходе не ждут друг друга, и один ответ уходит модели."""
    import asyncio
    import json
    import time

    sess = live_call._Session(_profile(), persona.Persona(), mode="assistant", system="x")

    async def slow(call):  # noqa: ANN001
        await asyncio.sleep(0.2)
        return {"id": call["id"], "name": call["name"], "response": {"ok": True}}

    sess._run_one = slow  # type: ignore[method-assign]
    ws = _WS()

    async def scenario():
        t = time.monotonic()
        await sess._run_tools(ws, [{"id": "1", "name": "weather"}, {"id": "2", "name": "currency_rates"}])
        return time.monotonic() - t

    took = asyncio.run(scenario())
    assert took < 0.35
    sent = json.loads(ws.sent[0])["toolResponse"]["functionResponses"]
    assert [r["id"] for r in sent] == ["1", "2"]

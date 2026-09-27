"""27.09: он прислал ключ Gemini боту — агент принял набор символов за просьбу и позвонил. Ключи перехватываются до ИИ,
ключ владельца проверяется и сохраняется, в журналы и память секреты не попадают; звонок — только по просьбе."""
import asyncio

from bot import access, agent_tools_assistant, secrets_guard
from bot import ai as ai_mod

FAKE_GOOGLE = "AIza" + "x" * 35                 # заведомо ненастоящие
FAKE_NEW = "AQ." + "Ab8R" + "y" * 40
FAKE_BOT = "123456789:" + "A" * 35


def test_find_keys_but_not_bank_sms():
    assert secrets_guard.find(FAKE_GOOGLE) == ("gemini", FAKE_GOOGLE)
    assert secrets_guard.find(f"вот ключ {FAKE_NEW}")[0] == "gemini"
    assert secrets_guard.find(FAKE_BOT)[0] == "secret"
    assert secrets_guard.find("a1" * 20)[0] == "secret"          # одно сообщение = один длинный код
    # в банковской SMS длинный номер операции — это не ключ
    assert secrets_guard.find("Uzum: оплата 45 000 сум, операция " + "a1" * 20 + ", баланс 1 200 000") is None
    assert secrets_guard.find("обед 45000 картой") is None
    assert secrets_guard.find("https://example.com/" + "a1" * 20) is None


def test_mask_hides_keys():
    out = secrets_guard.mask(f"я: {FAKE_NEW} → бот: Звоню")
    assert FAKE_NEW not in out and secrets_guard.MASK in out and "Звоню" in out


class Msg:
    def __init__(self, text: str) -> None:
        self.text, self.caption = text, None
        self.deleted = False
        self.answers: list[str] = []

    async def delete(self) -> None:
        self.deleted = True

    async def answer(self, text: str) -> None:
        self.answers.append(text)


def test_owner_key_is_checked_saved_and_deleted(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(access, "is_owner", lambda uid: uid == 1)
    monkeypatch.setattr(ai_mod, "FREE_API_KEY", "")

    async def ok(key):  # noqa: ANN001
        return True, ""

    monkeypatch.setattr(secrets_guard, "validate_gemini", ok)
    msg = Msg(FAKE_NEW)
    asyncio.run(secrets_guard.handle(msg, 1))
    assert msg.deleted and "проверен и сохранён" in msg.answers[0]
    assert secrets_guard.saved_free_key() == FAKE_NEW and ai_mod.free_key() == FAKE_NEW


def test_bad_or_foreign_key_is_not_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(access, "is_owner", lambda uid: uid == 1)

    async def bad(key):  # noqa: ANN001
        return False, "Google ответил 400"

    monkeypatch.setattr(secrets_guard, "validate_gemini", bad)
    msg = Msg(FAKE_GOOGLE)
    asyncio.run(secrets_guard.handle(msg, 1))
    assert "не принял" in msg.answers[0] and secrets_guard.saved_free_key() == ""
    msg = Msg(FAKE_GOOGLE)
    asyncio.run(secrets_guard.handle(msg, 42))             # клиент: не сохраняем, только предупреждаем
    assert msg.deleted and "не сохранял" in msg.answers[0] and secrets_guard.saved_free_key() == ""


def test_call_only_when_asked():
    assert agent_tools_assistant.asked_to_call("позвони мне")
    assert agent_tools_assistant.asked_to_call("menga qo‘ng‘iroq qil")
    assert agent_tools_assistant.asked_to_call("набери меня через 5 минут")
    assert not agent_tools_assistant.asked_to_call(FAKE_NEW)
    assert not agent_tools_assistant.asked_to_call("обсудим текущие дела")


def test_call_me_refuses_without_request():
    from bot.agent_tools import ToolContext, run
    from bot.profile import Profile

    ctx = ToolContext(profile=Profile(telegram_id=1, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Т", username="t"),
                      text=FAKE_NEW)
    res = asyncio.run(run("call_me", {"topic": "обсудим текущие дела"}, ctx))
    assert "не просил звонить" in res["error"]


def test_agent_log_and_memory_never_keep_keys(monkeypatch):
    from bot import services
    from bot.context import db

    stored: dict = {}

    async def add(uid, **kw):  # noqa: ANN001, ANN003
        stored.update(kw)

    monkeypatch.setattr(db, "available", lambda name: True)
    monkeypatch.setattr(db, "add_agent_log", add)
    asyncio.run(services.log_agent(1, text=FAKE_NEW, kind="agent", reply="ок"))
    assert FAKE_NEW not in stored["text"] and secrets_guard.MASK in stored["text"]


def test_passwords_are_caught_but_not_ordinary_words():
    fake = "почта test@example.com пароль: Qwerty2002X."        # заведомо ненастоящий
    assert secrets_guard.find(fake) == ("secret", "Qwerty2002X.")
    masked = secrets_guard.mask(fake)
    assert "Qwerty2002X" not in masked and "пароль: " + secrets_guard.MASK in masked
    assert secrets_guard.find("пин 4821")[0] == "secret"
    assert secrets_guard.find("поменял пароль вчера вечером") is None
    assert secrets_guard.find("забыл пароль от wifi") is None
    assert secrets_guard.find("обед 45000, pass the salt") is None

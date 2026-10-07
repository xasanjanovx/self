"""07.10: правка поста канала от аккаунта владельца (премиум-эмодзи) — без сети, с подменой клиента Telethon."""
import asyncio
from types import SimpleNamespace

from bot import tg_user


class FakeTelethon:
    def __init__(self, error=None):
        self.error = error
        self.calls: list = []

    async def get_entity(self, channel):
        return ("entity", channel)

    async def edit_message(self, entity, message_id, html, **kwargs):
        self.calls.append((entity, message_id, html, kwargs))
        if self.error:
            raise self.error


def _connect(monkeypatch, client, premium=True):
    async def fake_client():
        return client

    monkeypatch.setattr(tg_user, "client", fake_client)
    monkeypatch.setattr(tg_user, "_me", SimpleNamespace(premium=premium))


def test_not_connected_and_not_premium_are_reported_without_touching_the_post(monkeypatch):
    _connect(monkeypatch, None)
    assert asyncio.run(tg_user.edit_post("@ch", 5, "x")) == {"ok": False, "error": "not_connected"}
    fake = FakeTelethon()
    _connect(monkeypatch, fake, premium=False)
    assert asyncio.run(tg_user.edit_post("@ch", 5, "x")) == {"ok": False, "error": "not_premium"} and fake.calls == []


def test_edit_sends_html_with_the_post_media_untouched(monkeypatch):
    fake = FakeTelethon()
    _connect(monkeypatch, fake)
    html = '<tg-emoji emoji-id="5389061359403039918">✅</tg-emoji> <b>Barista</b>'
    assert asyncio.run(tg_user.edit_post("@ch", 7, html)) == {"ok": True}
    entity, message_id, sent, kwargs = fake.calls[0]
    assert entity == ("entity", "@ch") and message_id == 7 and sent == html
    assert kwargs == {"parse_mode": "html", "link_preview": False}                  # без file= — картинка поста остаётся


def test_unchanged_text_is_fine_but_other_errors_are_reported(monkeypatch):
    class MessageNotModifiedError(Exception):
        pass

    _connect(monkeypatch, FakeTelethon(MessageNotModifiedError("same")))
    assert asyncio.run(tg_user.edit_post("@ch", 7, "x")) == {"ok": True}

    class ChatAdminRequiredError(Exception):
        pass

    _connect(monkeypatch, FakeTelethon(ChatAdminRequiredError("no rights")))
    result = asyncio.run(tg_user.edit_post("@ch", 7, "x"))
    assert result["ok"] is False and result["error"].startswith("ChatAdminRequiredError")

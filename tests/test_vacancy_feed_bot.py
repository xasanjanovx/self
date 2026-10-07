"""07.10: автоподбор вакансий — кнопки, карточки, публикация, фоновый круг (aiogram без сети)."""
import asyncio
import dataclasses
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher

from bot import access, caller, image_gen, screen
from bot import vacancy_feed as feed
from bot.ai import VacancyData
from bot.handlers import vacancy_feed as ui

OWNER = 424242
CHANNEL = "@testch"


class FakeBot:
    def __init__(self):
        self.photos: list = []
        self.messages: list = []
        self.deleted: list = []
        self._id = 100

    def _next(self) -> int:
        self._id += 1
        return self._id

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None):
        mid = self._next()
        self.photos.append({"chat": chat_id, "photo": photo, "caption": caption, "markup": reply_markup, "id": mid})
        return SimpleNamespace(message_id=mid, photo=[SimpleNamespace(file_id=f"FID{mid}")])

    async def send_message(self, chat_id, text, reply_markup=None):
        mid = self._next()
        self.messages.append({"chat": chat_id, "text": text, "markup": reply_markup, "id": mid})
        return SimpleNamespace(message_id=mid)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class FakeCb:
    def __init__(self, data, bot, uid=OWNER, premium=False):
        self.data, self.bot, self.message = data, bot, None
        self.from_user = SimpleNamespace(id=uid, is_premium=premium)
        self.answers: list = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    feed.reset_cache()
    cfg = dataclasses.replace(ui.settings, allowed_telegram_ids=frozenset({OWNER}), vacancy_channel=CHANNEL)
    monkeypatch.setattr(ui, "settings", cfg)
    monkeypatch.setattr(access, "settings", cfg)
    monkeypatch.setattr(feed, "SOURCE_PAUSE_S", 0)
    monkeypatch.setattr(feed, "SEND_FROM", 0)
    monkeypatch.setattr(feed, "SEND_TO", 24)
    ephemerals: list = []

    async def fake_ephemeral(bot, chat_id, text, reply_markup=None, **kw):
        ephemerals.append(text)
        return 1

    monkeypatch.setattr(screen, "send_ephemeral", fake_ephemeral)
    monkeypatch.setattr(ui, "_ephemerals", ephemerals, raising=False)

    async def fake_image(headline, scene=None, company=None):
        return b"JPEGDATA"

    monkeypatch.setattr(image_gen, "vacancy_image", fake_image)
    yield
    feed.reset_cache()


def _cand(cid="c1", headline="Barista kerak", long=False, telegram="@cafe_hr"):
    data = VacancyData(headline=headline, intro=None, company="Cafe", region_tag="#TOSHKENT", address="Chilonzor",
                       salary="4 000 000 so'm", schedule="9:00-18:00", requirements=[f"Talab {i}: " + "x" * 40 for i in range(30)] if long else ["18 yosh"], duties=[],
                       benefits=["Choychaqa"], phone="+998901234567", telegram=telegram)
    cand = {"id": cid, "source": "jobs_uz", "msg_id": 7, "url": "https://t.me/jobs_uz/7", "status": "new", "score": 70, "key": cid,
            "created": datetime.now(feed.TZ).isoformat(timespec="seconds"), "scene": "кафе", "regen": 0,
            "data": feed.data_to_dict(data)}
    feed.load()["queue"][cid] = cand
    return cand


def _buttons(markup) -> dict:
    return {b.text: (b.callback_data or b.url) for row in markup.inline_keyboard for b in row}


# ------------------------------------------------------------------ маршрутизация
def _update(uid, data):
    return {"update_id": 1, "callback_query": {
        "id": "1", "from": {"id": uid, "is_bot": False, "first_name": "x"}, "chat_instance": "c", "data": data,
        "message": {"message_id": 10, "date": 1700000000, "chat": {"id": uid, "type": "private"},
                    "from": {"id": 1, "is_bot": True, "first_name": "bot"}, "text": "panel"}}}


def test_only_the_owner_reaches_feed_buttons():
    bot = Bot("123456:ABCDEF")
    calls: list = []

    async def fake_request(bot_, method, timeout=None):
        calls.append(type(method).__name__)
        return True

    bot.session.make_request = fake_request
    dp = Dispatcher()
    dp.include_router(ui.router)

    async def run():
        await dp.feed_raw_update(bot, _update(7, "vf:toggle"))        # чужой — ничего не происходит
        assert feed.load()["enabled"] is True
        await dp.feed_raw_update(bot, _update(OWNER, "vf:toggle"))    # владелец — выключил
        assert feed.load()["enabled"] is False
        await dp.feed_raw_update(bot, _update(OWNER, "vf:cap"))
        assert feed.load()["cap"] == 10

    asyncio.run(run())
    assert "AnswerCallbackQuery" in calls and "EditMessageText" in calls


# ------------------------------------------------------------------ карточки
def test_short_post_goes_as_one_photo_with_buttons():
    bot, cand = FakeBot(), _cand()
    asyncio.run(ui.send_card(bot, OWNER, cand))
    assert len(bot.photos) == 1 and bot.messages == []
    photo = bot.photos[0]
    assert photo["chat"] == OWNER and "Barista kerak" in photo["caption"]
    buttons = _buttons(photo["markup"])
    assert buttons["✅ Опубликовать"] == "vf:pub:c1" and buttons["⏭ Пропустить"] == "vf:skip:c1" and buttons["🔄 Другая картинка"] == "vf:img:c1"
    assert buttons["🔗 Источник"] == "https://t.me/jobs_uz/7"
    assert cand["status"] == "carded" and cand["file_id"] == f"FID{photo['id']}" and feed.cards_today() == 1
    assert feed.image_path("c1").read_bytes() == b"JPEGDATA"


def test_long_post_goes_as_photo_then_text_with_buttons():
    bot, cand = FakeBot(), _cand(long=True)
    asyncio.run(ui.send_card(bot, OWNER, cand))
    assert len(bot.photos) == 1 and len(bot.messages) == 1
    assert "из @jobs_uz" in bot.photos[0]["caption"] and bot.photos[0]["markup"] is None
    assert "✅ Опубликовать" in _buttons(bot.messages[0]["markup"])
    assert cand["card_ids"] == [bot.photos[0]["id"], bot.messages[0]["id"]]


def test_card_without_picture_is_still_publishable(monkeypatch):
    async def broken(*a, **k):
        raise image_gen.ImageError("503")

    monkeypatch.setattr(image_gen, "vacancy_image", broken)
    bot, cand = FakeBot(), _cand()
    asyncio.run(ui.send_card(bot, OWNER, cand))
    assert bot.photos == [] and "Картинка не получилась" in bot.messages[0]["text"]
    assert {"✅ Опубликовать", "🔄 Другая картинка"} <= set(_buttons(bot.messages[0]["markup"]))


# ------------------------------------------------------------------ кнопки карточки
def test_publish_sends_the_same_photo_to_the_channel():
    bot, cand = FakeBot(), _cand()
    asyncio.run(ui.send_card(bot, OWNER, cand))
    card_id = bot.photos[0]["id"]
    cb = FakeCb("vf:pub:c1", bot)
    asyncio.run(ui.cb_publish(cb))
    posted = bot.photos[-1]
    assert posted["chat"] == CHANNEL and posted["photo"] == f"FID{card_id}" and "Barista kerak" in posted["caption"]
    assert "Aloqa" in "".join(_buttons(posted["markup"])) or posted["markup"] is not None   # кнопка связи с работодателем под постом
    assert (OWNER, card_id) in bot.deleted
    assert feed.get_candidate("c1")["status"] == "published" and "c1" in feed.load()["published"]
    asyncio.run(ui.cb_publish(FakeCb("vf:pub:c1", bot)))
    assert len(bot.photos) == 2                                                          # второй раз не публикуем


def test_long_post_in_channel_is_photo_plus_text():
    bot, cand = FakeBot(), _cand(long=True)
    asyncio.run(ui.send_card(bot, OWNER, cand))
    asyncio.run(ui.cb_publish(FakeCb("vf:pub:c1", bot)))
    assert bot.photos[-1]["chat"] == CHANNEL and bot.photos[-1]["caption"] is None
    assert bot.messages[-1]["chat"] == CHANNEL and "Barista kerak" in bot.messages[-1]["text"]


def test_publish_error_keeps_the_card():
    bot, cand = FakeBot(), _cand()
    asyncio.run(ui.send_card(bot, OWNER, cand))

    async def boom(*a, **k):
        raise RuntimeError("bot is not an administrator")

    bot.send_photo = boom
    cb = FakeCb("vf:pub:c1", bot)
    asyncio.run(ui.cb_publish(cb))
    assert cb.answers[-1][1] is True and "administrator" in cb.answers[-1][0]
    assert feed.get_candidate("c1")["status"] == "carded"


def test_skip_removes_card_and_remembers_vacancy():
    bot, cand = FakeBot(), _cand()
    asyncio.run(ui.send_card(bot, OWNER, cand))
    asyncio.run(ui.cb_skip(FakeCb("vf:skip:c1", bot)))
    assert bot.deleted and feed.get_candidate("c1")["status"] == "skipped" and "c1" in feed.load()["published"]


def test_new_picture_at_most_twice():
    bot, cand = FakeBot(), _cand()
    asyncio.run(ui.send_card(bot, OWNER, cand))
    for _ in range(2):
        asyncio.run(ui.cb_new_image(FakeCb("vf:img:c1", bot)))
    assert len(bot.photos) == 3 and cand["regen"] == 2
    third = FakeCb("vf:img:c1", bot)
    asyncio.run(ui.cb_new_image(third))
    assert len(bot.photos) == 3 and third.answers[-1][1] is True


def test_premium_flag_follows_the_owner(monkeypatch):
    bot, cand = FakeBot(), _cand()
    asyncio.run(ui.send_card(bot, OWNER, cand))
    asyncio.run(ui.cb_publish(FakeCb("vf:pub:c1", bot, premium=True)))
    assert feed.load()["premium"] is True and "tg-emoji" in bot.photos[-1]["caption"]


# ------------------------------------------------------------------ фоновый круг
class FakeAI:
    async def assess_vacancy(self, text):
        return {"is_vacancy": True, "abroad": False, "pay_upfront": False, "scam_signals": [], "salary": "amount",
                "has_conditions": True, "salary_unrealistic": False, "contact_kind": "employer", "reason": "ok"}

    async def rewrite_vacancy(self, text, *, default_region_tag="#TOSHKENT"):
        return VacancyData(headline="Barista kerak", intro=None, company="Cafe", region_tag="#TOSHKENT", address="Chilonzor",
                           salary="4 000 000 so'm", schedule="9:00-18:00", requirements=["18 yosh"], duties=[], benefits=[],
                           phone="+998901234567", telegram="@cafe_hr", image_prompt="кафе")


POST = ("Kafega barista kerak. Maosh: 4 000 000 so'm. Ish vaqti: 9:00-18:00, 6/1. Talablar: 18 yoshdan. Toshkent, Chilonzor. "
        "Aloqa: +998 90 123 45 67, @cafe_hr")


class FeedClient:
    async def get_entity(self, name):
        return SimpleNamespace(username=name)

    async def iter_messages(self, entity, limit=None, min_id=0):
        yield SimpleNamespace(id=9, message=POST, date=datetime.now(timezone.utc) - timedelta(hours=2))


def test_tick_reads_channel_and_sends_exactly_one_card(monkeypatch):
    from bot.context import ai

    monkeypatch.setattr(ai, "assess_vacancy", FakeAI().assess_vacancy)
    monkeypatch.setattr(ai, "rewrite_vacancy", FakeAI().rewrite_vacancy)
    monkeypatch.setattr(caller, "user_client", lambda: FeedClient())
    feed.add_pending("jobs_uz", {"title": "Jobs"})
    feed.set_source_status("jobs_uz", "approved")
    feed.load()["last_discovery"] = time.time()
    bot = FakeBot()
    out = asyncio.run(ui.tick(bot))
    assert out["card"] is True and out["pulled"]["queued"] == 1
    assert len(bot.photos) == 1 and "Barista kerak" in bot.photos[0]["caption"]
    out = asyncio.run(ui.tick(bot))                                                  # те же посты второй раз — ничего нового
    assert out["card"] is False and len(bot.photos) == 1


def test_tick_does_nothing_when_switched_off_or_jes_offline(monkeypatch):
    feed.add_pending("jobs_uz", {})
    feed.set_source_status("jobs_uz", "approved")
    feed.load()["last_discovery"] = time.time()
    monkeypatch.setattr(caller, "user_client", lambda: None)
    bot = FakeBot()
    assert asyncio.run(ui.tick(bot))["note"] == "аккаунт JES не в сети"
    feed.set_enabled(False)
    assert asyncio.run(ui.tick(bot))["note"] == "выключен"
    assert bot.photos == [] and bot.messages == []


def test_source_decision_buttons():
    feed.add_pending("jobs_uz", {})
    edits: list = []

    class Msg:
        async def edit_text(self, text, reply_markup=None):
            edits.append(text)

    cb = FakeCb("vf:src:ok:jobs_uz", FakeBot())
    cb.message = Msg()
    asyncio.run(ui.cb_source_decision(cb))
    assert feed.sources("approved") and "в работе" in edits[0]
    cb2 = FakeCb("vf:src:no:ghost", FakeBot())
    cb2.message = Msg()
    asyncio.run(ui.cb_source_decision(cb2))
    assert cb2.answers[-1][1] is True


def test_vacancy_panel_shows_feed_button_only_to_the_owner():
    from bot.keyboards import vacancy_panel_keyboard

    assert "vf:panel" in _buttons(vacancy_panel_keyboard("ru", feed=True)).values()
    assert "vf:panel" not in _buttons(vacancy_panel_keyboard("ru")).values()

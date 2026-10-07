"""07.10: автоподбор вакансий — кнопки, карточки, публикация, фоновый круг (aiogram без сети)."""
import asyncio
import dataclasses
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher

from bot import access, caller, image_gen, screen, tg_user
from bot import vacancy_feed as feed
from bot.ai import VacancyData
from bot.handlers import vacancy_feed as ui

OWNER = 424242
CHANNEL = "@testch"
FAVORITES_AT_IMPORT = feed.FAVORITE_SOURCES


class FakeBot:
    def __init__(self):
        self.photos: list = []
        self.messages: list = []
        self.deleted: list = []
        self.edited_markup: list = []
        self.markup_error = None
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

    async def edit_message_reply_markup(self, chat_id=None, message_id=None, reply_markup=None):
        self.edited_markup.append((chat_id, message_id, reply_markup))
        if self.markup_error:
            raise RuntimeError(self.markup_error)


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
    monkeypatch.setattr(feed, "FAVORITE_SOURCES", ())          # его каналы-«избранные» проверяются отдельным тестом
    notes: list = []

    async def fake_note(bot, chat_id, text, reply_markup=None, **kw):
        notes.append(text)
        return 1

    monkeypatch.setattr(screen, "send_note", fake_note)
    monkeypatch.setattr(ui, "_notes", notes, raising=False)

    async def fake_image(data, scene=None, *, design=None):
        return image_gen.Banner(b"JPEGDATA", None, design["id"])

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
    """текст → данные; у кнопок с премиум-иконкой эмодзи уходит из текста в иконку, поэтому добавляем и ключи «эмодзи текст»."""
    from bot import emoji as pe

    by_id: dict = {}
    for ch, ident in pe._ID_BY_EMOJI.items():
        by_id.setdefault(ident, []).append(ch)
    out: dict = {}
    for row in markup.inline_keyboard:
        for b in row:
            data = b.callback_data or b.url
            out[b.text] = data
            for ch in by_id.get(getattr(b, "icon_custom_emoji_id", None), []):
                out[f"{ch} {b.text}"] = data
                out[f"{ch}️ {b.text}"] = data
    return out


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
    assert buttons["✅ Опубликовать сейчас"] == "vf:pub:c1" and buttons["⏭ Пропустить"] == "vf:skip:c1" and buttons["🔄 Другая картинка"] == "vf:img:c1"
    assert buttons["🔗 Источник"] == "https://t.me/jobs_uz/7"
    assert cand["status"] == "carded" and cand["file_id"] == f"FID{photo['id']}" and feed.cards_today() == 1
    assert feed.image_path("c1").read_bytes() == b"JPEGDATA"


def test_long_post_is_trimmed_into_one_photo_caption():
    """Его требование: фото ВСЕГДА в одном сообщении с текстом, поэтому длинная вакансия сокращается до лимита подписи."""
    from bot import vacancy as vac

    bot, cand = FakeBot(), _cand(long=True)
    asyncio.run(ui.send_card(bot, OWNER, cand))
    assert len(bot.photos) == 1 and bot.messages == []
    photo = bot.photos[0]
    assert vac.visible_len(photo["caption"]) <= vac.CAPTION_LIMIT
    assert "Barista kerak" in photo["caption"] and "+998901234567" in photo["caption"] and "@cafe_hr" in photo["caption"]
    assert "✅ Опубликовать сейчас" in _buttons(photo["markup"]) and cand["card_ids"] == [photo["id"]]
    assert any("Текст сокращён" in note for note in ui._notes)


def test_picture_failure_is_retried_and_then_shown_without_publish(monkeypatch):
    """Без картинки публиковать нельзя: сначала молча пробуем ещё, потом карточка без «Опубликовать» (только «Нарисовать картинку»)."""
    async def broken(*a, **k):
        raise image_gen.ImageError("503")

    monkeypatch.setattr(image_gen, "vacancy_image", broken)
    bot, cand = FakeBot(), _cand()
    for tries in range(1, ui.IMAGE_TRIES):
        assert asyncio.run(ui.send_card(bot, OWNER, cand)) is False
        assert bot.photos == [] and bot.messages == [] and cand["status"] == "new" and cand["img_tries"] == tries
    assert asyncio.run(ui.send_card(bot, OWNER, cand)) is True
    buttons = _buttons(bot.messages[0]["markup"])
    assert "Картинки нет" in bot.messages[0]["text"]
    assert "✅ Опубликовать сейчас" not in buttons and buttons["🎨 Нарисовать картинку"] == "vf:img:c1"
    cb = FakeCb("vf:pub:c1", bot)                                                          # старая кнопка тоже не пропустит без картинки
    asyncio.run(ui.cb_publish(cb))
    assert cb.answers[-1][1] is True and "Без картинки" in cb.answers[-1][0]
    assert [p for p in bot.photos if p["chat"] == CHANNEL] == [] and cand["status"] == "carded"


def test_redraw_after_failure_gets_the_publish_button_back(monkeypatch):
    calls = {"n": 0}

    async def flaky(data, scene=None, *, design=None):
        calls["n"] += 1
        if calls["n"] <= ui.IMAGE_TRIES:
            raise image_gen.ImageError("503")
        return image_gen.Banner(b"JPEGDATA", None, design["id"])

    monkeypatch.setattr(image_gen, "vacancy_image", flaky)
    bot, cand = FakeBot(), _cand()
    for _ in range(ui.IMAGE_TRIES):
        asyncio.run(ui.send_card(bot, OWNER, cand))
    assert bot.photos == []
    asyncio.run(ui.cb_new_image(FakeCb("vf:img:c1", bot)))
    assert len(bot.photos) == 1 and "✅ Опубликовать сейчас" in _buttons(bot.photos[0]["markup"]) and cand["has_image"] is True


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


def test_long_post_in_channel_is_one_photo_with_the_text():
    from bot import vacancy as vac

    bot, cand = FakeBot(), _cand(long=True)
    asyncio.run(ui.send_card(bot, OWNER, cand))
    asyncio.run(ui.cb_publish(FakeCb("vf:pub:c1", bot)))
    posted = bot.photos[-1]
    assert posted["chat"] == CHANNEL and vac.visible_len(posted["caption"]) <= vac.CAPTION_LIMIT and "Barista kerak" in posted["caption"]
    assert [m for m in bot.messages if m["chat"] == CHANNEL] == []                          # отдельного текстового сообщения нет


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


def _premium_edit(monkeypatch, result=None, boom=None):
    """Подмена правки поста от аккаунта владельца: записываем вызовы."""
    calls: list = []

    async def fake_edit(channel, message_id, html):
        calls.append((channel, message_id, html))
        if boom:
            raise boom
        return result if result is not None else {"ok": True}

    monkeypatch.setattr(tg_user, "edit_post", fake_edit)
    return calls


def test_after_publishing_his_premium_account_edits_the_post_and_buttons_are_restored(monkeypatch):
    """Бот не может поставить премиум-эмодзи в канал — сразу после публикации пост правит его Premium-аккаунт."""
    calls = _premium_edit(monkeypatch)
    bot, cand = FakeBot(), _cand()
    asyncio.run(ui.send_card(bot, OWNER, cand))
    asyncio.run(ui.cb_publish(FakeCb("vf:pub:c1", bot)))
    posted = [p for p in bot.photos if p["chat"] == CHANNEL][0]
    assert calls == [(CHANNEL, posted["id"], posted["caption"])]                              # правит ту же подпись, что опубликовал бот
    assert 'emoji-id="5389061359403039918"' in calls[0][2]                                     # с тегами премиум-эмодзи
    assert [(c, m) for c, m, _ in bot.edited_markup] == [(CHANNEL, posted["id"])]              # кнопки возвращены после правки
    assert bot.edited_markup[0][2] is not None
    assert not any("⚠️" in n for n in ui._notes)                                               # всё вышло — без предупреждений


def test_premium_edit_warnings_are_short_and_do_not_break_publishing(monkeypatch):
    cases = [
        ({"ok": False, "error": "not_connected"}, None, "Telegram не подключён в JES"),
        ({"ok": False, "error": "not_premium"}, None, "нет Premium"),
        ({"ok": False, "error": "ChatAdminRequiredError: no rights"}, None, "не поставились"),
        (None, RuntimeError("boom"), "RuntimeError"),
    ]
    for i, (result, boom, expected) in enumerate(cases):
        ui._notes.clear()
        _premium_edit(monkeypatch, result, boom)
        bot, cand = FakeBot(), _cand(f"w{i}", headline=f"Vakansiya {i} kerak")
        asyncio.run(ui.send_card(bot, OWNER, cand))
        asyncio.run(ui.cb_publish(FakeCb(f"vf:pub:w{i}", bot)))
        assert cand["status"] == "published" and [p["chat"] for p in bot.photos][-1] == CHANNEL         # пост всё равно вышел
        assert any(expected in n for n in ui._notes), (expected, ui._notes)
        assert bot.edited_markup == []                                                         # раз правки нет — кнопки не трогаем


def test_buttons_after_the_edit_unchanged_is_fine_but_a_real_failure_is_reported(monkeypatch):
    _premium_edit(monkeypatch)
    bot, cand = FakeBot(), _cand()
    asyncio.run(ui.send_card(bot, OWNER, cand))
    bot.markup_error = "Bad Request: message is not modified"                                  # правка сохранила кнопки — это нормально
    asyncio.run(ui.cb_publish(FakeCb("vf:pub:c1", bot)))
    assert not any("⚠️" in n for n in ui._notes)
    ui._notes.clear()
    cand2 = _cand("c2", headline="Oshpaz kerak")
    asyncio.run(ui.send_card(bot, OWNER, cand2))
    bot.markup_error = "Bad Request: message can't be edited"
    asyncio.run(ui.cb_publish(FakeCb("vf:pub:c2", bot)))
    assert any("кнопки под постом пропали" in n for n in ui._notes)


def test_split_post_is_not_edited(monkeypatch):
    calls = _premium_edit(monkeypatch)
    assert asyncio.run(ui.upgrade_premium(FakeBot(), [1, 2], "text", None)) == "" and calls == []


def test_forward_button_sends_a_clean_post_to_him_and_nothing_to_the_channel():
    """Telegram не принимает премиум-эмодзи от бота в канал (проверено на живом канале), а в личку — принимает: второй путь — чистый пост ему."""
    bot, cand = FakeBot(), _cand()
    asyncio.run(ui.send_card(bot, OWNER, cand))
    card_id = bot.photos[0]["id"]
    assert "vf:fwd:c1" in _buttons(bot.photos[0]["markup"]).values()
    asyncio.run(ui.cb_forward(FakeCb("vf:fwd:c1", bot)))
    clean = bot.photos[-1]
    assert clean["chat"] == OWNER and clean["markup"] is None and clean["id"] != card_id          # без кнопок — так пересылают в канал
    assert 'emoji-id="5389061359403039918"' in clean["caption"] and "Barista kerak" in clean["caption"]
    assert [p for p in bot.photos if p["chat"] == CHANNEL] == [] and (OWNER, card_id) in bot.deleted
    assert feed.get_candidate("c1")["status"] == "published" and any("перешли в канал" in n for n in ui._notes)
    again = FakeCb("vf:fwd:c1", bot)
    asyncio.run(ui.cb_forward(again))
    assert again.answers[-1][1] is True and len(bot.photos) == 2                                  # второй раз не шлём


def test_forward_button_needs_a_picture(monkeypatch):
    async def broken(*a, **k):
        raise image_gen.ImageError("503")

    monkeypatch.setattr(image_gen, "vacancy_image", broken)
    bot, cand = FakeBot(), _cand()
    for _ in range(ui.IMAGE_TRIES):
        asyncio.run(ui.send_card(bot, OWNER, cand))
    cb = FakeCb("vf:fwd:c1", bot)
    asyncio.run(ui.cb_forward(cb))
    assert cb.answers[-1][1] is True and "Без картинки" in cb.answers[-1][0] and cand["status"] == "carded"


def test_posts_always_carry_the_channels_premium_emoji_tags():
    """Раньше теги премиум-эмодзи включались только после нажатия кнопки пользователем с Premium; теперь всегда (в личку они доходят)."""
    bot, cand = FakeBot(), _cand()
    asyncio.run(ui.send_card(bot, OWNER, cand))
    assert 'emoji-id="5389061359403039918"' in bot.photos[0]["caption"]                       # ✅ в заголовке — как в его постах
    asyncio.run(ui.cb_publish(FakeCb("vf:pub:c1", bot, premium=False)))
    posted = bot.photos[-1]["caption"]
    assert posted.count("<tg-emoji") >= 6 and 'emoji-id="5348418461838098123"' in posted and 'emoji-id="5897938112654348733"' in posted
    assert "premium" not in feed.load()


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


def test_tick_takes_his_named_channels_at_once_and_does_not_bring_back_removed_ones(monkeypatch):
    monkeypatch.setattr(feed, "FAVORITE_SOURCES", ("fav_one", "fav_two"))
    monkeypatch.setattr(caller, "user_client", lambda: None)
    feed.load()["last_discovery"] = time.time()
    asyncio.run(ui.tick(FakeBot()))
    assert set(feed.sources("approved")) == {"fav_one", "fav_two"} and feed.load()["last_discovery"] == 0.0   # и пора искать другие каналы
    feed.set_source_status("fav_one", "rejected")                       # он убрал канал — обратно не возвращаем
    asyncio.run(ui.tick(FakeBot()))
    assert set(feed.sources("approved")) == {"fav_two"}


def test_his_three_channels_are_the_favorites():
    assert FAVORITES_AT_IMPORT == ("vakansyuz_vacansyuz", "ishtopuz_rasmiy", "ish_keremi")      # настоящие константы, до подмены в фикстуре


def test_card_shows_source_views_and_popular_posts_go_first():
    low, high = _cand("lo"), _cand("hi", headline="Oshpaz kerak")
    low["views"], high["views"] = 20, 12400
    assert feed.next_card()["id"] == "hi"                                 # при равной оценке — та, у которой больше просмотров
    bot = FakeBot()
    asyncio.run(ui.send_card(bot, OWNER, high))
    assert "🔗 Источник · 👁 12.4к" in _buttons(bot.photos[0]["markup"])
    assert ui._views_label(0) == "" and ui._views_label(None) == "" and ui._views_label(950) == " · 👁 950"


def test_tick_does_not_ask_about_a_post_without_a_picture(monkeypatch):
    from bot.context import ai

    async def broken(*a, **k):
        raise image_gen.ImageError("503")

    monkeypatch.setattr(ai, "assess_vacancy", FakeAI().assess_vacancy)
    monkeypatch.setattr(ai, "rewrite_vacancy", FakeAI().rewrite_vacancy)
    monkeypatch.setattr(caller, "user_client", lambda: FeedClient())
    monkeypatch.setattr(image_gen, "vacancy_image", broken)
    feed.add_pending("jobs_uz", {"title": "Jobs"})
    feed.set_source_status("jobs_uz", "approved")
    feed.load()["last_discovery"] = time.time()
    bot = FakeBot()
    out = asyncio.run(ui.tick(bot))
    assert out["card"] is False and "картинка" in out["note"] and bot.photos == [] and bot.messages == []
    assert len(feed.candidates("new")) == 1                                # вакансия ждёт следующего круга, не потеряна


def test_feed_has_no_auto_mode_anymore():
    from bot.handlers import vacancy_settings as sett

    assert not hasattr(ui, "auto_publish") and not hasattr(ui, "cb_undo")
    values = {b.callback_data for row in sett.settings_keyboard().inline_keyboard for b in row}
    assert "vf:s:mode" not in values and "vf:s:imgs" not in values
    assert "В день" in sett.settings_text() and "Автоподбор" in ui.panel_text()


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


def test_unverified_banner_is_posted_with_a_warning_note(monkeypatch):
    async def doubtful(data, scene=None, *, design=None):
        return image_gen.Banner(b"JPEGDATA", "⚠️ Проверь картинку: телефон на картинке не совпал с вакансией. Если неверно — «Другая картинка».")

    monkeypatch.setattr(image_gen, "vacancy_image", doubtful)
    bot, cand = FakeBot(), _cand()
    asyncio.run(ui.send_card(bot, OWNER, cand))
    assert len(bot.photos) == 1 and any("телефон на картинке не совпал" in n for n in ui._notes)


def test_checked_banner_has_no_warning_note():
    bot, cand = FakeBot(), _cand()
    asyncio.run(ui.send_card(bot, OWNER, cand))
    assert ui._notes == []


def test_every_card_gets_a_different_design_and_new_picture_changes_it(monkeypatch):
    used: list = []

    async def pictured(data, scene=None, *, design=None):
        used.append(design["id"])
        return image_gen.Banner(b"JPEGDATA", None, design["id"])

    monkeypatch.setattr(image_gen, "vacancy_image", pictured)
    bot = FakeBot()
    cands = [_cand(f"c{i}", headline=f"Vakansiya {i} kerak") for i in range(5)]
    for cand in cands:
        asyncio.run(ui.send_card(bot, OWNER, cand))
    assert len(set(used)) == 5                                         # пять вакансий подряд — пять разных дизайнов
    assert feed.recent_designs()[:5] == used[::-1]
    before = cands[0]["design"]
    asyncio.run(ui.cb_new_image(FakeCb("vf:img:c0", bot)))
    assert cands[0]["design"] != before and len(used) == 6              # «Другая картинка» — другой дизайн, не тот же

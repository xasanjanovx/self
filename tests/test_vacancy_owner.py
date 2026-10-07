"""07.10: раздел вакансий — только у владельца; ручное оформление с баннером; защита платного поста; каждый пост спрашивается у него;
фото в одном посте с текстом; настройки (без сети)."""
import asyncio
import dataclasses
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import bot.context as ctx
from bot import access, caller, image_gen, screen
from bot import vacancy_feed as feed
from bot.ai import VacancyData
from bot.handlers import channel as channel_h
from bot.handlers import vacancy as vac_h
from bot.handlers import vacancy_feed as ui
from bot.handlers import vacancy_settings as sett
from bot.keyboards import main_menu_keyboard

OWNER = 424242
GUEST = 777
CHANNEL = "@testch"


class FakeBot:
    def __init__(self):
        self.photos: list = []
        self.messages: list = []
        self.deleted: list = []
        self._id = 500

    def _next(self):
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
    def __init__(self, data, bot, uid=OWNER, premium=False, message=None):
        self.data, self.bot = data, bot
        self.from_user = SimpleNamespace(id=uid, is_premium=premium)
        self.message = message or SimpleNamespace(chat=SimpleNamespace(id=uid), message_id=1)
        self.answers: list = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _profile(uid=OWNER):
    return SimpleNamespace(lang="ru", telegram_id=uid, tr=lambda ru, uz: ru)


class FakeAI:
    calls = 0

    async def rewrite_vacancy(self, text, *, default_region_tag="#TOSHKENT"):
        FakeAI.calls += 1
        return VacancyData(headline="Barista kerak", intro=None, company="Cafe", region_tag="#TOSHKENT", address="Chilonzor",
                           salary="4 000 000 so'm", schedule="9:00-18:00", requirements=["18 yosh"], duties=[], benefits=["Choychaqa"],
                           phone="+998901234567", telegram="@cafe_hr", image_prompt="уютное кафе, бариста")

    async def assess_vacancy(self, text):
        return {"is_vacancy": True, "abroad": False, "pay_upfront": False, "scam_signals": [], "salary": "amount", "has_conditions": True,
                "salary_unrealistic": False, "contact_kind": "employer", "reason": "ok"}


RAW = ("Kafega barista kerak. Maosh: 4 000 000 so'm. Ish vaqti: 9:00-18:00, 6/1. Talablar: 18 yoshdan. Toshkent, Chilonzor. "
       "Aloqa: +998 90 123 45 67, @cafe_hr")


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    feed.reset_cache()
    cfg = dataclasses.replace(ctx.settings, allowed_telegram_ids=frozenset({OWNER}), vacancy_channel=CHANNEL)
    for module in (ctx, ui, vac_h, access):
        monkeypatch.setattr(module, "settings", cfg, raising=False)
    monkeypatch.setattr(feed, "SOURCE_PAUSE_S", 0)
    monkeypatch.setattr(feed, "SEND_FROM", 0)
    monkeypatch.setattr(feed, "SEND_TO", 24)
    notes: list = []

    async def fake_note(bot, chat_id, text, reply_markup=None, **kw):
        notes.append((text, reply_markup))
        return 1

    async def fake_ephemeral(bot, chat_id, text, reply_markup=None, **kw):
        notes.append((text, reply_markup))
        return 1

    monkeypatch.setattr(screen, "send_note", fake_note)
    monkeypatch.setattr(screen, "send_ephemeral", fake_ephemeral)
    monkeypatch.setattr("bot.handlers.vacancy_feed.settings", cfg)
    designs: list = []

    async def fake_image(data, scene=None, *, design=None):
        designs.append(design["id"] if isinstance(design, dict) else design)
        return image_gen.Banner(b"JPEGDATA", None, design["id"] if isinstance(design, dict) else (design or ""))

    monkeypatch.setattr(image_gen, "vacancy_image", fake_image)
    monkeypatch.setattr(vac_h, "ai", FakeAI())

    async def fake_get_profile(user):
        return _profile(user.id)

    monkeypatch.setattr(vac_h, "get_profile", fake_get_profile)
    monkeypatch.setattr(ctx, "ai", FakeAI())
    monkeypatch.setattr("bot.handlers.vacancy_feed.settings", cfg)
    monkeypatch.setattr(_env, "notes", notes, raising=False)
    monkeypatch.setattr(_env, "designs", designs, raising=False)
    yield notes
    feed.reset_cache()


def _state(uid=OWNER):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def _patch_screen(monkeypatch):
    shown: list = []

    async def noop(*a, **k):
        return None

    async def show_panel(message, state, text, kb):
        shown.append((text, kb))

    monkeypatch.setattr(vac_h, "show_progress", noop)
    monkeypatch.setattr(vac_h, "safe_delete", noop)
    monkeypatch.setattr(vac_h, "show_panel", show_panel)
    return shown


def _message(bot, text, uid=OWNER, photo=None):
    return SimpleNamespace(bot=bot, chat=SimpleNamespace(id=uid), from_user=SimpleNamespace(id=uid, is_premium=False), photo=photo, text=text,
                           caption=None, voice=None, audio=None, message_id=1)


def _buttons(markup) -> dict:
    return {b.text: (b.callback_data or b.url) for row in markup.inline_keyboard for b in row}


# ------------------------------------------------------------------ только владелец
def test_vacancy_button_in_the_main_menu_only_for_the_owner():
    owner_kb = {v for v in _buttons(main_menu_keyboard("ru", owner=True)).values()}
    guest_kb = {v for v in _buttons(main_menu_keyboard("ru")).values()}
    assert "menu:vacancy" in owner_kb and "menu:vacancy" not in guest_kb


def test_guest_cannot_open_the_section_or_publish():
    bot = FakeBot()
    cb = FakeCb("menu:vacancy", bot, uid=GUEST)
    asyncio.run(vac_h.cb_open(cb, _state(GUEST)))
    assert cb.answers[-1] == ("Раздел недоступен", True)
    cb2 = FakeCb("vacancy:publish", bot, uid=GUEST)
    asyncio.run(vac_h.cb_publish(cb2, _state(GUEST)))
    assert cb2.answers[-1][1] is True and bot.photos == [] and bot.messages == []


def test_guest_vacancy_text_is_not_swallowed(monkeypatch):
    shown = _patch_screen(monkeypatch)
    asyncio.run(vac_h.process_vacancy(_message(FakeBot(), RAW, uid=GUEST), _state(GUEST), _profile(GUEST), RAW))
    assert shown == [] and FakeAI.calls == 0


def test_feed_buttons_ignore_a_guest():
    assert ui.owner_filter(FakeCb("vf:toggle", FakeBot(), uid=OWNER)) is True
    assert ui.owner_filter(FakeCb("vf:toggle", FakeBot(), uid=GUEST)) is False
    assert ui.owner_filter(FakeCb("menu:vacancy", FakeBot(), uid=OWNER)) is False


def test_agent_hand_off_to_vacancy_is_refused_for_a_guest():
    from bot import agent_tools

    ctx_guest = agent_tools.ToolContext(profile=SimpleNamespace(telegram_id=GUEST), text="x")
    result = asyncio.run(agent_tools._hand_off.__wrapped__(ctx_guest, {"module": "vacancy"})) if hasattr(agent_tools._hand_off, "__wrapped__") \
        else asyncio.run(agent_tools._hand_off(ctx_guest, {"module": "vacancy"}))
    assert "error" in result and ctx_guest.handoff is None
    ctx_owner = agent_tools.ToolContext(profile=SimpleNamespace(telegram_id=OWNER), text="x")
    result = asyncio.run(agent_tools._hand_off(ctx_owner, {"module": "vacancy", "text": "t"}))
    assert result.get("ok") and ctx_owner.handoff == ("vacancy", "t")


# ------------------------------------------------------------------ ручное оформление: баннер сразу
def test_manual_vacancy_gets_a_banner_card_immediately(monkeypatch):
    shown = _patch_screen(monkeypatch)
    bot, state = FakeBot(), _state()
    asyncio.run(vac_h.process_vacancy(_message(bot, RAW), state, _profile(), RAW))
    assert len(bot.photos) == 1 and "Barista kerak" in bot.photos[0]["caption"]
    labels = _buttons(bot.photos[0]["markup"])
    assert labels["✅ Опубликовать сейчас"] == "vacancy:publish" and labels["🔄 Другой дизайн"] == "vacancy:img"
    assert len(_env.designs) == 1 and any("баннер ниже" in text for text, _ in shown)
    data = asyncio.run(state.get_data())
    assert data["vacancy_file_id"] == f"FID{bot.photos[0]['id']}" and data["vacancy_design"] == _env.designs[0]


def test_manual_with_his_own_photo_keeps_the_old_flow_and_publishes_one_post(monkeypatch):
    shown = _patch_screen(monkeypatch)
    bot, state = FakeBot(), _state()
    asyncio.run(vac_h.process_vacancy(_message(bot, RAW, photo=[SimpleNamespace(file_id="HIS")]), state, _profile(), RAW))
    assert bot.photos == [] and _env.designs == [] and "Barista kerak" in shown[-1][0]          # его фото — генерировать не надо
    asyncio.run(vac_h.cb_publish(FakeCb("vacancy:publish", bot), state))
    posted = bot.photos[-1]
    assert posted["chat"] == CHANNEL and posted["photo"] == "HIS" and "Barista kerak" in posted["caption"] and bot.messages == []


def test_manual_forward_button_sends_a_clean_post_with_premium_emoji_to_him(monkeypatch):
    _patch_screen(monkeypatch)
    bot, state = FakeBot(), _state()
    asyncio.run(vac_h.process_vacancy(_message(bot, RAW), state, _profile(), RAW))
    card_id = bot.photos[0]["id"]
    assert "vacancy:fwd" in _buttons(bot.photos[0]["markup"]).values()
    asyncio.run(vac_h.cb_forward(FakeCb("vacancy:fwd", bot), state))
    clean = bot.photos[-1]
    assert clean["chat"] == OWNER and clean["markup"] is None and 'emoji-id="5389061359403039918"' in clean["caption"]
    assert [p for p in bot.photos if p["chat"] == CHANNEL] == [] and (OWNER, card_id) in bot.deleted
    assert any("перешли его в канал" in text for text, _ in _env.notes)
    guest = FakeCb("vacancy:fwd", bot, uid=GUEST)
    asyncio.run(vac_h.cb_forward(guest, _state(GUEST)))
    assert guest.answers[-1][1] is True and len(bot.photos) == 2


def test_manual_without_a_banner_cannot_be_published(monkeypatch):
    """Фото обязательно в одном посте с текстом: баннер не нарисовался → «Опубликовать» нет, а старая кнопка отвечает отказом."""
    _patch_screen(monkeypatch)

    async def broken(data, scene=None, *, design=None):
        raise image_gen.ImageError("503")

    monkeypatch.setattr(image_gen, "vacancy_image", broken)
    bot, state = FakeBot(), _state()
    asyncio.run(vac_h.process_vacancy(_message(bot, RAW), state, _profile(), RAW))
    assert bot.photos == [] and "Картинка не получилась" in bot.messages[0]["text"]
    labels = _buttons(bot.messages[0]["markup"])
    assert "✅ Опубликовать сейчас" not in labels and labels["🎨 Нарисовать баннер"] == "vacancy:img"
    cb = FakeCb("vacancy:publish", bot)
    asyncio.run(vac_h.cb_publish(cb, state))
    assert cb.answers[-1][1] is True and "Без картинки" in cb.answers[-1][0]
    assert [m for m in bot.messages if m["chat"] == CHANNEL] == [] and [p for p in bot.photos if p["chat"] == CHANNEL] == []


def test_manual_long_vacancy_is_one_photo_post_with_premium_emoji(monkeypatch):
    from bot import vacancy as vac

    _patch_screen(monkeypatch)

    class LongAI(FakeAI):
        async def rewrite_vacancy(self, text, *, default_region_tag="#TOSHKENT"):
            data = await super().rewrite_vacancy(text, default_region_tag=default_region_tag)
            data.requirements = [f"Talab {i}: " + "x" * 50 for i in range(25)]
            data.duties = [f"Vazifa {i}: " + "y" * 50 for i in range(25)]
            data.intro = "Katta kompaniya yangi xodimlarni ishga taklif qiladi. " * 3
            return data

    monkeypatch.setattr(vac_h, "ai", LongAI())
    bot, state = FakeBot(), _state()
    asyncio.run(vac_h.process_vacancy(_message(bot, RAW), state, _profile(), RAW))
    assert any("Текст сокращён" in text for text, _ in _env.notes)
    asyncio.run(vac_h.cb_publish(FakeCb("vacancy:publish", bot, premium=False), state))
    posted = bot.photos[-1]
    assert posted["chat"] == CHANNEL and vac.visible_len(posted["caption"]) <= vac.CAPTION_LIMIT
    assert "+998901234567" in posted["caption"] and 'emoji-id="5389061359403039918"' in posted["caption"]
    assert [m for m in bot.messages if m["chat"] == CHANNEL] == []


def test_redraw_switches_the_design_and_has_a_limit(monkeypatch):
    _patch_screen(monkeypatch)
    bot, state = FakeBot(), _state()
    asyncio.run(vac_h.process_vacancy(_message(bot, RAW), state, _profile(), RAW))
    first = _env.designs[0]
    cb = FakeCb("vacancy:img", bot)
    asyncio.run(vac_h.cb_redraw(cb, state))
    assert _env.designs[1] != first and len(bot.photos) == 2 and (OWNER, bot.photos[0]["id"]) in bot.deleted
    for _ in range(vac_h.MAX_REDRAWS):
        asyncio.run(vac_h.cb_redraw(FakeCb("vacancy:img", bot), state))
    assert len(bot.photos) == 1 + vac_h.MAX_REDRAWS                                              # дальше лимит
    assert len(_env.designs) == len(set(_env.designs))                                            # ни один дизайн не повторился


def test_publish_now_is_a_free_post_without_protection(monkeypatch):
    """Раньше любая публикация из бота считалась платной (включала защиту на 3 часа). Теперь «Опубликовать сейчас» — обычный пост."""
    _patch_screen(monkeypatch)
    bot, state = FakeBot(), _state()
    asyncio.run(vac_h.process_vacancy(_message(bot, RAW), state, _profile(), RAW))
    asyncio.run(vac_h.cb_publish(FakeCb("vacancy:publish", bot), state))
    posted = bot.photos[-1]
    assert posted["chat"] == CHANNEL and posted["photo"].startswith("FID") and "Barista kerak" in posted["caption"]
    assert feed.is_own(posted["id"])                                                              # свой пост — не «чужой»
    assert feed.hold_left() == 0                                                                  # защиту не включали
    assert not any("Лента под защитой" in text for text, _ in _env.notes)


def test_paid_button_publishes_and_starts_the_three_hour_protection(monkeypatch):
    _patch_screen(monkeypatch)
    bot, state = FakeBot(), _state()
    asyncio.run(vac_h.process_vacancy(_message(bot, RAW), state, _profile(), RAW))
    assert _buttons(bot.photos[0]["markup"])["💰 Платный пост"] == "vacancy:paid"
    asyncio.run(vac_h.cb_publish_paid(FakeCb("vacancy:paid", bot), state))
    posted = bot.photos[-1]
    assert posted["chat"] == CHANNEL and "Barista kerak" in posted["caption"] and feed.is_own(posted["id"])
    left = feed.hold_left()
    assert 2.99 * 3600 < left <= 3 * 3600 + 5                                                     # ≥ 3 часов наверху
    ok, why, _ = feed.publish_gate(respect_schedule=False)
    assert (ok, why) == (False, "hold")
    assert any("Лента под защитой" in text for text, _ in _env.notes)


def test_channel_post_has_two_blue_buttons_contact_and_ad_request(monkeypatch):
    """Под постом в канале: «Bog'lanish» (работодателю) и «E'lon joylash» (админу канала с готовым текстом) — обе синие."""
    from urllib.parse import parse_qs, unquote, urlparse

    _patch_screen(monkeypatch)
    bot, state = FakeBot(), _state()
    asyncio.run(vac_h.process_vacancy(_message(bot, RAW), state, _profile(), RAW))
    asyncio.run(vac_h.cb_publish(FakeCb("vacancy:publish", bot), state))
    row = bot.photos[-1]["markup"].inline_keyboard[0]
    assert [b.text for b in row] == ["📩 Bog'lanish", "📢 E'lon joylash"] and all(b.style == "primary" for b in row)
    assert row[0].url.startswith("tg://resolve?domain=cafe_hr&text=")
    ad = urlparse(row[1].url)
    assert ad.scheme == "tg" and parse_qs(ad.query)["domain"] == ["ishdasiz_admin"]
    text = unquote(parse_qs(ad.query)["text"][0])
    assert "e'lon joylashtirmoqchiman" in text and "narxlari" in text and "https://t.me/ishdasiz" in text      # ссылка на канал и вопрос о ценах


# ------------------------------------------------------------------ защита и отложенная публикация
def _cand(cid="c1", headline="Barista kerak"):
    data = VacancyData(headline=headline, intro=None, company="Cafe", region_tag="#TOSHKENT", address="Chilonzor", salary="4 000 000 so'm",
                       schedule="9:00-18:00", requirements=["18 yosh"], duties=[], benefits=["Choychaqa"], phone="+998901234567", telegram="@cafe_hr")
    cand = {"id": cid, "source": "jobs_uz", "msg_id": 7, "url": "https://t.me/jobs_uz/7", "status": "new", "score": 70, "key": cid,
            "created": datetime.now(feed.TZ).isoformat(timespec="seconds"), "scene": "кафе", "regen": 0, "data": feed.data_to_dict(data)}
    feed.load()["queue"][cid] = cand
    return cand


def test_publish_now_does_not_wait_for_the_paid_post_but_warns():
    """«Опубликовать сейчас» — сразу, без очереди (раньше при платном посте наверху вакансия уходила в очередь)."""
    bot = FakeBot()
    cand = _cand()
    asyncio.run(ui.send_card(bot, OWNER, cand))
    feed.note_manual_post(time.time() - 3600, 1)                                                   # платный пост час назад → ещё ~2 часа
    cb = FakeCb("vf:pub:c1", bot)
    asyncio.run(ui.cb_publish(cb))
    assert cand["status"] == "published" and cb.answers[-1] == ("Опубликовано ✅", False)
    assert [p["chat"] for p in bot.photos][-1] == CHANNEL and feed.is_own(bot.photos[-1]["id"])
    assert any("встала выше него" in text for text, _ in _env.notes)                                # предупреждение — после публикации
    assert feed.get_candidate("c1")["status"] != "scheduled"


def test_a_leftover_scheduled_vacancy_goes_out_when_protection_ends():
    """Очередь больше не создаётся, но то, что уже стояло в ней до обновления, выходит само, когда защита кончилась."""
    bot = FakeBot()
    cand = _cand()
    asyncio.run(ui.send_card(bot, OWNER, cand))
    cand.update({"status": "scheduled", "publish_at": time.time() + 7200})
    feed.note_manual_post(time.time() - 3600, 1)
    assert asyncio.run(ui.publish_due(bot)) == 0                                                   # рано
    feed.load()["hold_until"] = 0.0                                                                # защита кончилась
    cand["publish_at"] = time.time() - 1
    assert asyncio.run(ui.publish_due(bot)) == 1
    assert [p["chat"] for p in bot.photos][-1] == CHANNEL and cand["status"] == "published"
    assert feed.hold_left() == 0 and feed.is_own(bot.photos[-1]["id"])                            # свой пост защиту не включает


def test_a_leftover_scheduled_vacancy_waits_again_if_a_new_paid_post_appears():
    bot = FakeBot()
    cand = _cand()
    asyncio.run(ui.send_card(bot, OWNER, cand))
    cand.update({"status": "scheduled", "publish_at": time.time() - 1})
    feed.note_manual_post(time.time(), 2)                                                          # пока ждали, он разместил платный
    assert asyncio.run(ui.publish_due(bot)) == 0 and cand["publish_at"] > time.time() + 2.9 * 3600


# ------------------------------------------------------------------ каждый пост спрашиваем у него
class FeedClient:
    def __init__(self, text):
        self.text = text

    async def get_entity(self, name):
        return SimpleNamespace(username=name)

    async def iter_messages(self, entity, limit=None, min_id=0):
        yield SimpleNamespace(id=9, message=self.text, date=datetime.now(timezone.utc) - timedelta(hours=2))


POST = ("Kafega barista kerak. Maosh: 4 000 000 so'm. Ish vaqti: 9:00-18:00, 6/1. Talablar: 18 yoshdan. Toshkent, Chilonzor. "
        "Aloqa: +998 90 123 45 67, @cafe_hr")


def _feed_setup(monkeypatch):
    monkeypatch.setattr(caller, "user_client", lambda: FeedClient(POST))
    monkeypatch.setattr(feed, "FAVORITE_SOURCES", ())
    feed.add_pending("jobs_uz", {"title": "Jobs"})
    feed.set_source_status("jobs_uz", "approved")
    feed.load()["last_discovery"] = time.time()


def test_feed_only_asks_he_decides_every_post(monkeypatch):
    """Его требование: каждый пост спрашивать у него обязательно — сама лента в канал не публикует никогда."""
    _feed_setup(monkeypatch)
    bot = FakeBot()
    out = asyncio.run(ui.tick(bot))
    assert out["card"] is True
    assert [p["chat"] for p in bot.photos] == [OWNER] and "vf:pub:" in "".join(_buttons(bot.photos[0]["markup"]).values())
    assert feed.cards_today() == 1 and feed.load()["last_feed_post"] == 0                           # ничего не опубликовано
    feed.load()["last_discovery"] = time.time()
    for _ in range(3):                                                                             # сколько бы кругов ни прошло
        asyncio.run(ui.tick(bot))
    assert [p for p in bot.photos if p["chat"] == CHANNEL] == []


def test_unverified_banner_goes_to_him_as_a_card_with_a_warning(monkeypatch):
    _feed_setup(monkeypatch)

    async def doubtful(data, scene=None, *, design=None):
        return image_gen.Banner(b"JPEGDATA", "⚠️ Проверь картинку: телефон не совпал", design["id"])

    monkeypatch.setattr(image_gen, "vacancy_image", doubtful)
    bot = FakeBot()
    out = asyncio.run(ui.tick(bot))
    assert out["card"] is True and [p["chat"] for p in bot.photos] == [OWNER]
    assert any("телефон не совпал" in text for text, _ in _env.notes)


def test_no_new_cards_during_protection_but_next_button_and_publish_now_work(monkeypatch):
    _feed_setup(monkeypatch)
    bot = FakeBot()
    feed.note_manual_post(time.time() - 600, 1)                                                    # платный пост 10 минут назад
    out = asyncio.run(ui.tick(bot))
    assert out["card"] is False and bot.photos == [] and len(feed.candidates("new")) == 1          # платный пост на топе — сам не шлю, вакансия ждёт
    assert asyncio.run(ui.tick(bot, manual=True))["card"] is True                                   # «Следующая вакансия» — по его просьбе показываю
    cb = FakeCb("vf:pub:" + feed.candidates("carded")[0]["id"], bot)
    asyncio.run(ui.cb_publish(cb))
    assert [p["chat"] for p in bot.photos][-1] == CHANNEL                                          # а «опубликовать сейчас» — сразу


# ------------------------------------------------------------------ настройки
class ScreenCapture:
    def __init__(self, monkeypatch, module):
        self.shown: list = []

        async def safe_edit(callback, text, markup=None):
            self.shown.append((text, markup))

        monkeypatch.setattr(module, "safe_edit", safe_edit)


def test_settings_screen_cycles_values_and_protection_never_drops_below_three(monkeypatch):
    cap = ScreenCapture(monkeypatch, sett)
    bot = FakeBot()
    for key in ("cap", "wf", "wt", "gap", "prot", "sal"):
        asyncio.run(sett.cb_setting(FakeCb(f"vf:s:{key}", bot)))
    assert feed.load()["cap"] == 10 and feed.window() == (5, 15)                                         # в тесте окно 0–24: следующие по кругу значения
    assert feed.cfg("require_salary") is False and feed.cfg("card_gap_min") == 180               # по умолчанию 2 ч → следующее по кругу
    assert feed.protect_seconds() == 4 * 3600
    for _ in range(6):
        asyncio.run(sett.cb_setting(FakeCb("vf:s:prot", bot)))
        assert feed.protect_seconds() >= 3 * 3600
    text, markup = cap.shown[-1]
    assert "Защита платного поста" in text and "меньше 3 нельзя" in text
    assert "vf:ds" in _buttons(markup).values() and "vf:ads" in _buttons(markup).values()


def test_designs_screen_lists_all_and_toggles(monkeypatch):
    from bot import vacancy as v

    cap = ScreenCapture(monkeypatch, sett)
    bot = FakeBot()
    asyncio.run(sett.cb_designs(FakeCb("vf:ds", bot)))
    text, markup = cap.shown[-1]
    design_buttons = [b for row in markup.inline_keyboard for b in row if (b.callback_data or "").startswith("vf:d:") and b.callback_data != "vf:d:all"]
    assert len(design_buttons) == len(v.DESIGNS) >= 30 and f"{len(v.DESIGNS)} из {len(v.DESIGNS)}" in text
    asyncio.run(sett.cb_design_toggle(FakeCb("vf:d:gold_black", bot)))
    assert "gold_black" not in feed.allowed_designs() and "⛔" in cap.shown[-1][1].inline_keyboard[0][0].text
    asyncio.run(sett.cb_design_toggle(FakeCb("vf:d:all", bot)))
    assert "gold_black" in feed.allowed_designs()
    asyncio.run(sett.cb_design_toggle(FakeCb("vf:d:nonsense", bot)))                               # чужой id игнорируем
    assert feed.cfg("off_designs") == []


def test_banner_never_uses_a_switched_off_design(monkeypatch):
    from bot import vacancy as v

    for design in v.DESIGNS:
        if design["id"] != "neon_green":
            feed.toggle_design(design["id"])
    bot = FakeBot()
    for i in range(3):
        asyncio.run(ui.send_card(bot, OWNER, _cand(f"d{i}", headline=f"Vakansiya {i} kerak"), regenerate=True))
    assert set(_env.designs) == {"neon_green"}


def test_paid_mark_buttons_start_and_clear_the_protection(monkeypatch):
    cap = ScreenCapture(monkeypatch, sett)
    bot = FakeBot()
    assert feed.hold_left() == 0
    asyncio.run(sett.cb_paid(FakeCb("vf:paid", bot)))
    assert 2.99 * 3600 < feed.hold_left() <= 3 * 3600 + 5
    text, markup = cap.shown[-1]
    assert "Сейчас защита: ещё" in text and "vf:unpaid" in _buttons(markup).values()
    asyncio.run(sett.cb_unpaid(FakeCb("vf:unpaid", bot)))
    assert feed.hold_left() == 0 and "vf:paid" in _buttons(cap.shown[-1][1]).values()
    assert {"vf:now", "vf:paid"} <= set(_buttons(ui.panel_keyboard()).values())
    assert "▶️ Следующая вакансия" in _buttons(ui.panel_keyboard())


def test_ads_screen_toggles_and_shows_the_log(monkeypatch):
    cap = ScreenCapture(monkeypatch, sett)
    bot = FakeBot()
    feed.log_ad({"id": 1, "ts": time.time(), "topics": ["credit"], "text": "Tez kredit!", "deleted": True})
    asyncio.run(sett.cb_ads(FakeCb("vf:ads", bot)))
    text, markup = cap.shown[-1]
    assert "Удалено рекламы: 1" in text and "Кредиты и займы" in text and "Tez kredit" in text
    for key in ("on", "mode", "c:bank", "c:credit"):
        asyncio.run(sett.cb_ads_setting(FakeCb(f"vf:a:{key}", bot)))   # (это режим ФИЛЬТРА РЕКЛАМЫ: удалять/сообщать — он остаётся)
    assert feed.cfg("ads_on") is False and feed.cfg("ads_mode") == "notify"
    assert feed.cfg("ads_cats")["bank"] is True and feed.cfg("ads_cats")["credit"] is False


def test_panel_shows_ask_mode_protection_and_ads():
    feed.note_manual_post(time.time(), 1)
    text = ui.panel_text()
    assert "решаешь ты" in text and "Платный пост наверху" in text and "Реклама: фильтр включён" in text
    assert {"vf:cfg", "vf:ads"} <= set(_buttons(ui.panel_keyboard()).values())


# ------------------------------------------------------------------ посты канала в реальном времени
def test_channel_post_handler_reacts_only_to_our_channel(monkeypatch):
    monkeypatch.setattr(channel_h, "SETTLE_S", 0)
    monkeypatch.setattr(channel_h, "settings", dataclasses.replace(ctx.settings, vacancy_channel=CHANNEL))
    seen: list = []

    async def fake_handle(bot, post, **kw):
        seen.append(post["id"])
        return "ok"

    monkeypatch.setattr(channel_h.guard, "handle_post", fake_handle)

    def post(chat_username, mid):
        return SimpleNamespace(bot=FakeBot(), chat=SimpleNamespace(username=chat_username, id=-100), message_id=mid, text="x", caption=None,
                               date=datetime.now(timezone.utc))

    asyncio.run(channel_h.on_channel_post(post("testch", 1)))
    asyncio.run(channel_h.on_channel_post(post("otherch", 2)))
    assert seen == [1]

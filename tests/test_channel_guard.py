"""07.10: охрана ленты — защита платного поста (≥ 3 ч), удаление рекламы #reklama на запрещённые темы, настройки (без сети)."""
import asyncio
import dataclasses
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot import caller
from bot import channel_guard as guard
from bot import screen
from bot import vacancy_feed as feed

CHANNEL = "@testch"
OWNER = 424242

OUR_VACANCY = ("✅ Kredit menejeri kerak\n— — — —\nHudud: #TOSHKENT\nMaosh: 4 000 000 so'm\nAloqa: +998901234567\n"
               "❗️E'lonlardagi ma'lumotlar uchun kanal ma'muriyati javobgar emas.\n➡️ ISHDASIZ - Tez va oson ish toping!")
CREDIT_AD = "Tez kredit! Foizsiz nasiya, 5 daqiqada qaror. Bank karta shart emas.\n\n#reklama\nerid: 2VtzqwP7fFA"
CREDIT_AD_RU = "Займ на карту за 5 минут! Без проверок. Реклама. #reklama"
BET_AD = "Ставки на спорт, бонус 100% на первый депозит! 1xBet\n#reklama"
CRYPTO_AD = "Купи USDT на Binance без комиссии. #reklama"
INSURE_AD = "Sug'urta polisi 20% arzon! Investitsiya bilan passiv daromad. #reklama"
SHOES_AD = "Yangi kolleksiya krossovkalar chegirma bilan! Do'konimizga marhamat. #reklama"
MANUAL_POST = "Ish bor! Sotuvchi kerak, maosh 3 mln, tel +998 90 111 22 33"


class FakeBot:
    def __init__(self, fail_delete=False):
        self.deleted: list = []
        self.fail_delete = fail_delete

    async def delete_message(self, chat_id, message_id):
        if self.fail_delete:
            raise RuntimeError("Bad Request: not enough rights to delete a message")
        self.deleted.append((chat_id, message_id))


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    feed.reset_cache()
    import bot.context as ctx

    monkeypatch.setattr(ctx, "settings", dataclasses.replace(ctx.settings, allowed_telegram_ids=frozenset({OWNER}), vacancy_channel=CHANNEL))
    notes: list = []

    async def fake_note(bot, chat_id, text, reply_markup=None, **kw):
        notes.append(text)
        return 1

    monkeypatch.setattr(screen, "send_note", fake_note)
    monkeypatch.setattr(guard, "_notes", notes, raising=False)
    yield
    feed.reset_cache()


def _post(mid, text, hours_ago=0.0):
    return {"id": mid, "text": text, "ts": time.time() - hours_ago * 3600}


# ------------------------------------------------------------------ признаки
def test_reklama_marker_and_our_template():
    assert guard.is_reklama(CREDIT_AD) and guard.is_reklama(CREDIT_AD_RU) and guard.is_reklama("Реклама. #реклама")
    assert not guard.is_reklama(MANUAL_POST) and not guard.is_reklama("kredit haqida maqola")
    assert guard.is_our_template(OUR_VACANCY) and not guard.is_our_template(CREDIT_AD)


@pytest.mark.parametrize("text,topic", [
    (CREDIT_AD, "credit"), (CREDIT_AD_RU, "credit"), (BET_AD, "bets"), (CRYPTO_AD, "bets"), (INSURE_AD, "insure"),
])
def test_banned_topics_are_found(text, topic):
    assert topic in guard.ad_topics(text)


def test_harmless_ads_have_no_topics_and_banks_are_off_by_default():
    assert guard.ad_topics(SHOES_AD) == []
    bank_ad = "Bankda omonat oching! Mikromoliya xizmatlari. #reklama"
    assert guard.ad_topics(bank_ad) == ["bank"]
    assert guard.ad_topics(bank_ad, dict(feed.cfg("ads_cats"))) == []          # «Банки и МФО» он не выбрал


def test_classification():
    feed.mark_own([10])
    assert guard.classify(CREDIT_AD, 10) == "own"
    assert guard.classify(OUR_VACANCY + "\n#reklama", 11) == "manual"           # платный пост в нашем шаблоне — его, не реклама
    assert guard.classify(CREDIT_AD, 12) == "ad"
    assert guard.classify(MANUAL_POST, 13) == "manual"


# ------------------------------------------------------------------ реклама
def test_credit_ad_is_deleted_and_the_owner_gets_its_text():
    bot = FakeBot()
    assert asyncio.run(guard.handle_post(bot, _post(100, CREDIT_AD))) == "deleted"
    assert bot.deleted == [(CHANNEL, 100)]
    assert feed.load()["ads_log"][0]["deleted"] is True and "nasiya" in feed.load()["ads_log"][0]["text"]
    assert any("Удалил из канала рекламу" in n and "nasiya" in n for n in guard._notes)
    assert feed.hold_left() == 0                                                  # реклама ленту не «держит»


@pytest.mark.parametrize("text", [CREDIT_AD_RU, BET_AD, CRYPTO_AD, INSURE_AD])
def test_all_selected_topics_are_deleted(text):
    bot = FakeBot()
    assert asyncio.run(guard.handle_post(bot, _post(101, text))) == "deleted" and len(bot.deleted) == 1


def test_harmless_ad_and_bank_ad_stay():
    bot = FakeBot()
    assert asyncio.run(guard.handle_post(bot, _post(102, SHOES_AD))) == "ad_ok"
    assert asyncio.run(guard.handle_post(bot, _post(103, "Bankda omonat oching! #reklama"))) == "ad_ok"
    assert bot.deleted == []
    feed.toggle_ad_category("bank")                                               # включил «Банки и МФО» — теперь удаляется
    assert asyncio.run(guard.handle_post(bot, _post(104, "Bankda omonat oching! #reklama"))) == "deleted"


def test_his_own_vacancy_about_a_bank_is_never_deleted_even_with_reklama():
    bot = FakeBot()
    assert asyncio.run(guard.handle_post(bot, _post(105, OUR_VACANCY + "\n#reklama"))) == "manual"
    assert bot.deleted == []


def test_notify_only_mode_and_switch_off():
    bot = FakeBot()
    feed.set_cfg("ads_mode", "notify")
    assert asyncio.run(guard.handle_post(bot, _post(106, CREDIT_AD))) == "notified" and bot.deleted == []
    assert any("не удалял" in n for n in guard._notes)
    feed.set_cfg("ads_on", False)
    assert asyncio.run(guard.handle_post(bot, _post(107, CREDIT_AD))) == "ad_ok" and bot.deleted == []


def test_delete_failure_is_reported():
    bot = FakeBot(fail_delete=True)
    assert asyncio.run(guard.handle_post(bot, _post(108, CREDIT_AD))) == "notified"
    assert any("удалить не смог" in n and "право удалять" in n for n in guard._notes)


def test_every_post_is_handled_once():
    bot = FakeBot()
    assert asyncio.run(guard.handle_post(bot, _post(109, CREDIT_AD))) == "deleted"
    assert asyncio.run(guard.handle_post(bot, _post(109, CREDIT_AD))) == "seen" and len(bot.deleted) == 1


def test_llm_second_opinion_only_for_unmarked_paraphrases():
    class AI:
        calls = 0

        async def generate(self, parts, **kw):
            AI.calls += 1
            return '{"credit": true, "bank": false, "bets": false, "insure": false}'

    bot = FakeBot()
    paraphrase = "Pul kerakmi? Bugun oling, oyligingizda qaytaring. #reklama"       # слов-признаков нет
    assert asyncio.run(guard.handle_post(bot, _post(110, paraphrase), ai=AI())) == "deleted" and AI.calls == 1
    assert asyncio.run(guard.handle_post(bot, _post(111, CREDIT_AD), ai=AI())) == "deleted" and AI.calls == 1   # по словам — без нейросети
    assert asyncio.run(guard.handle_post(bot, _post(112, SHOES_AD), ai=SimpleNamespace(generate=AI().generate))) == "deleted"  # нейросеть сказала credit


# ------------------------------------------------------------------ защита платного поста
def test_a_manual_post_in_the_channel_does_not_start_the_protection():
    """Его жалоба 07.10: бот принимал за платные и бесплатные посты. Теперь чужой/ручной пост защиту НЕ включает — только его отметка."""
    bot = FakeBot()
    assert asyncio.run(guard.handle_post(bot, _post(200, MANUAL_POST, hours_ago=1.0))) == "manual"
    assert asyncio.run(guard.handle_post(bot, _post(201, OUR_VACANCY, hours_ago=0.1))) == "manual"      # даже в нашем шаблоне
    assert feed.hold_left() == 0
    assert feed.publish_gate(respect_schedule=False)[0] is True


def test_the_paid_mark_starts_the_protection_for_three_hours_and_can_be_cleared():
    assert feed.note_manual_post(time.time(), 0) is True
    left = feed.hold_left()
    assert 2.99 * 3600 < left <= 3 * 3600 + 5
    ok, why, wait = feed.publish_gate(respect_schedule=False)
    assert (ok, why) == (False, "hold") and abs(wait - left) < 5
    feed.clear_hold()
    assert feed.hold_left() == 0 and feed.load()["hold_post"] == 0


def test_hold_never_goes_below_three_hours_and_only_extends():
    feed.set_cfg("protect_hours", 1)
    assert feed.protect_seconds() == 3 * 3600
    feed.note_manual_post(time.time() - 3600)
    first = feed.load()["hold_until"]
    assert not feed.note_manual_post(time.time() - 7200)                          # более старый пост защиту не сокращает
    assert feed.load()["hold_until"] == first
    assert feed.note_manual_post(time.time())                                     # новый — продлевает
    assert feed.load()["hold_until"] > first


def test_own_posts_do_not_create_a_hold():
    feed.mark_own([300, 301])
    bot = FakeBot()
    assert asyncio.run(guard.handle_post(bot, _post(300, OUR_VACANCY))) == "own"
    assert feed.hold_left() == 0


def test_publish_gate_schedule_rules():
    noon = datetime(2026, 10, 7, 12, 0, tzinfo=feed.TZ)
    assert feed.publish_gate(noon)[0] is True
    feed.note_feed_post(noon.timestamp() - 30 * 60)                               # недавно уже публиковали: интервал 90 мин
    ok, why, wait = feed.publish_gate(noon)
    assert (ok, why) == (False, "gap") and 59 * 60 < wait <= 60 * 60
    assert feed.publish_gate(noon, respect_schedule=False)[0] is True              # его ручная кнопка расписанию не подчиняется
    feed.note_feed_post(0)
    ok, why, wait = feed.publish_gate(noon.replace(hour=3))
    assert (ok, why) == (False, "window") and wait == 5 * 3600
    for _ in range(int(feed.load()["cap"])):
        feed.note_card_sent()
    assert feed.publish_gate(noon)[:2] == (False, "cap")
    feed.set_cfg("window_from", 13)
    assert feed.in_window(noon) is False and feed.window() == (13, feed.SEND_TO)


def test_human_wait():
    assert feed.human_wait(65 * 60) == "1 ч 5 мин" and feed.human_wait(90) == "2 мин" and feed.human_wait(60) == "1 мин"


def test_design_toggles_and_allowed_set():
    from bot import vacancy as v

    assert feed.allowed_designs() == {d["id"] for d in v.DESIGNS}
    assert feed.toggle_design("gold_black") is False and "gold_black" not in feed.allowed_designs()
    assert feed.toggle_design("gold_black") is True
    for d in v.DESIGNS:                                                           # все выключил — берём все, а не падаем
        feed.toggle_design(d["id"])
    assert feed.allowed_designs() == {d["id"] for d in v.DESIGNS}
    data = SimpleNamespace(headline="Xodim kerak", company=None)
    assert v.pick_design(data, seed="s", allowed={"neon_green"})["id"] == "neon_green"


# ------------------------------------------------------------------ чтение канала аккаунтом JES
class FakeClient:
    def __init__(self, posts):
        self.posts = posts

    async def get_entity(self, name):
        return SimpleNamespace(username=name)

    async def iter_messages(self, entity, limit=None, min_id=0):
        for p in sorted(self.posts, key=lambda x: -x["id"]):
            if p["id"] > min_id:
                yield SimpleNamespace(id=p["id"], message=p["text"], date=datetime.fromtimestamp(p["ts"], timezone.utc))


def test_poll_first_run_never_deletes_history_and_starts_no_protection(monkeypatch):
    posts = [_post(1, CREDIT_AD, hours_ago=2), _post(2, MANUAL_POST, hours_ago=1), _post(3, SHOES_AD, hours_ago=0.5),
             _post(4, MANUAL_POST, hours_ago=30)]
    monkeypatch.setattr(caller, "user_client", lambda: FakeClient(posts))
    bot = FakeBot()
    result = asyncio.run(guard.poll(bot))
    assert result["manual"] == 1 and result["deleted"] == 0 and bot.deleted == []   # историю не трогаем
    assert feed.hold_left() == 0                                                     # и защиту от ручных постов не включаем
    assert feed.load()["guard_cursor"] == 4


def test_poll_then_deletes_a_new_credit_ad_and_ignores_own_posts(monkeypatch):
    posts = [_post(10, MANUAL_POST, hours_ago=5)]
    monkeypatch.setattr(caller, "user_client", lambda: FakeClient(posts))
    bot = FakeBot()
    asyncio.run(guard.poll(bot))
    feed.mark_own([12])
    posts += [_post(11, CREDIT_AD), _post(12, OUR_VACANCY), _post(13, SHOES_AD)]
    result = asyncio.run(guard.poll(bot))
    assert result["deleted"] == 1 and result["own"] == 1 and result["ad_ok"] == 1
    assert bot.deleted == [(CHANNEL, 11)] and feed.load()["guard_cursor"] == 13
    assert asyncio.run(guard.poll(bot))["posts"] == 0                              # новых нет — ничего не делаем


def test_phones_of_posts_already_in_the_channel_are_remembered_once(monkeypatch):
    """Антидубли: вакансия, которую он уже выложил сам, из чужого канала не предлагается (телефоны канала читаем один раз)."""
    posts = [_post(1, MANUAL_POST, hours_ago=40), _post(2, CREDIT_AD, hours_ago=1)]
    monkeypatch.setattr(caller, "user_client", lambda: FakeClient(posts))
    asyncio.run(guard.poll(FakeBot()))
    assert feed.in_channel("Sotuvchi kerak. Aloqa: +998 90 111 22 33")           # старый пост (дальше окна первого запуска) тоже учтён
    assert not feed.in_channel("Boshqa vakansiya, tel +998 91 000 11 22")
    assert feed.load()["phones_seeded"] is True
    posts.append(_post(3, OUR_VACANCY))                                         # новый пост канала — тоже запоминаем
    asyncio.run(guard.poll(FakeBot()))
    assert feed.in_channel("Kredit menejeri. +998 90 123 45 67")


def test_poll_without_jes_or_with_a_numeric_channel(monkeypatch):
    monkeypatch.setattr(caller, "user_client", lambda: None)
    assert asyncio.run(guard.poll(FakeBot()))["error"] == "аккаунт JES не в сети"
    monkeypatch.setattr(caller, "user_client", lambda: FakeClient([]))
    import bot.context as ctx

    monkeypatch.setattr(ctx, "settings", dataclasses.replace(ctx.settings, vacancy_channel="-1001234"))
    assert "именем" in asyncio.run(guard.poll(FakeBot()))["error"]


def test_negations_are_not_credit():
    assert guard.ad_topics("Samsung Galaxy ombordan, nasiyasiz, narxi hamyonbop #reklama") == []
    assert guard.ad_topics("Telefonlar nasiyaga! 0% foizsiz #reklama") == ["credit"]

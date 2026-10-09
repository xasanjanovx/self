"""07.10: автоподбор вакансий из чужих каналов — отбор «только надёжные», очередь, чтение каналов (без сети и без Telegram)."""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot import vacancy_feed as feed
from bot.ai import VacancyData

GOOD = ("Kafega barista kerak. Maosh: 4 000 000 so'm + choychaqa. Ish vaqti: 9:00-18:00, 6/1. Talablar: 18 yoshdan, "
        "muloqotga ochiq, tajriba shart emas. Manzil: Toshkent, Chilonzor. Aloqa: +998 90 123 45 67, @cafe_hr")
ASSESS_OK = {"is_vacancy": True, "employer_type": "company", "abroad": False, "pay_upfront": False, "scam_signals": [],
             "salary": "amount", "has_conditions": True, "salary_unrealistic": False, "contact_kind": "employer", "reason": "всё указано"}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    feed.reset_cache()
    monkeypatch.setattr(feed, "SOURCE_PAUSE_S", 0)
    monkeypatch.setattr(feed, "SEARCH_PAUSE_S", 0)
    yield
    feed.reset_cache()


# ------------------------------------------------------------------ тревожные признаки
@pytest.mark.parametrize("text", [
    "Koreyaga ishchilar kerak, viza bor, maosh 2000$. Aloqa +998901234567",
    "Требуются рабочие в Польшу. Зарплата 1500 евро. +998901234567",
    "Moskvada qurilishga yigitlar kerak, vaxta usulida",
    "Работа в Дубае для девушек, проживание и питание",
])
def test_abroad_is_flagged(text):
    assert "abroad" in feed.hard_flags(text)


@pytest.mark.parametrize("text", [
    "Для оформления нужно внести залог 500 000 сум, затем выход на работу",
    "Ro'yxatdan o'tish uchun to'lov 200 000 so'm, keyin ishga olamiz",
    "Платное обучение, после — трудоустройство",
    "Ishga kirish uchun oldindan to'lov qilish kerak",
])
def test_money_upfront_is_flagged(text):
    assert "upfront" in feed.hard_flags(text)


@pytest.mark.parametrize("text", [
    "Сетевой маркетинг, пассивный доход без вложений",
    "Kripto trading bo'yicha hamkor kerak, tez pul",
    "Сдайте банковскую карту в аренду и получайте 500 000 в месяц",
])
def test_scam_is_flagged(text):
    assert "scam" in feed.hard_flags(text)


@pytest.mark.parametrize("text", [
    GOOD,
    "Koreys restoraniga ofitsiant kerak (Toshkent). Maosh kelishiladi",   # корейская кухня в Ташкенте — не заграница
    "В корейский ресторан на Чиланзаре требуется повар. Зарплата 5 000 000",
    "Ingliz tili bilgan operator kerak, Samarqand. Maosh 4 mln. Aloqa +998901234567",
    "Знание английского и французского языка. Вахтёр в бизнес-центр, Ташкент",
    "В американскую компанию в Ташкенте нужен менеджер",
])
def test_normal_posts_have_no_flags(text):
    assert feed.hard_flags(text) == []


# ------------------------------------------------------------------ контакт, зарплата
def test_contacts_exclude_source_and_admin_handles():
    text = "Aloqa: @cafe_hr, +998 90 123 45 67. Reklama: @ish_admin. Kanal: @jobs_uz https://t.me/ishdasiz"
    phones, handles = feed.employer_contacts(text, source="jobs_uz")
    assert phones == ["+998901234567"] and handles == ["cafe_hr"]


def test_no_contact_when_only_channel_promo():
    phones, handles = feed.employer_contacts("Bizning kanal: @jobs_uz. Reklama admin: @adm_reklama", source="jobs_uz")
    assert phones == [] and handles == []


def test_salary_hint_distinguishes_amount_negotiable_none():
    assert feed.salary_hint("Maosh: 4 000 000 so'm") == "amount"
    assert feed.salary_hint("Зарплата 500$") == "amount"
    assert feed.salary_hint("Maosh kelishiladi") == "negotiable"
    assert feed.salary_hint("Оплата по собеседованию") == "negotiable"
    assert feed.salary_hint("Kafega barista kerak, 9:00-18:00") == "none"


# ------------------------------------------------------------------ решение
def test_good_post_passes():
    verdict = feed.judge(GOOD, ASSESS_OK, "jobs_uz")
    assert verdict.ok and verdict.reasons == [] and verdict.salary == "amount"


def test_salary_at_interview_is_accepted():
    text = GOOD.replace("Maosh: 4 000 000 so'm + choychaqa", "Maosh suhbat asosida")
    assert feed.judge(text, {**ASSESS_OK, "salary": "negotiable"}, "jobs_uz").ok


def test_rejections_name_the_reason():
    abroad = feed.judge(GOOD, {**ASSESS_OK, "abroad": True}, "jobs_uz")
    assert not abroad.ok and "работа за границей" in abroad.reasons
    paid = feed.judge(GOOD, {**ASSESS_OK, "pay_upfront": True}, "jobs_uz")
    assert not paid.ok and "просят деньги вперёд" in paid.reasons
    no_contact = feed.judge("Barista kerak. Maosh 4 000 000 so'm. 9:00-18:00, 6/1. Toshkent. Yozing kanal adminiga", ASSESS_OK, "jobs_uz")
    assert not no_contact.ok and "нет контакта работодателя" in no_contact.reasons
    admin = feed.judge(GOOD, {**ASSESS_OK, "contact_kind": "admin_or_ad"}, "jobs_uz")
    assert not admin.ok and "контакт админа канала, а не работодателя" in admin.reasons
    unrealistic = feed.judge(GOOD, {**ASSESS_OK, "salary_unrealistic": True}, "jobs_uz")
    assert not unrealistic.ok


def test_no_salary_or_no_conditions_is_rejected():
    text = "Barista kerak. Aloqa +998901234567"
    verdict = feed.judge(text, {**ASSESS_OK, "salary": "none", "has_conditions": False}, "jobs_uz")
    assert not verdict.ok
    assert "нет зарплаты" in verdict.reasons and "нет условий (график, обязанности, требования)" in verdict.reasons


def test_regex_overrides_llm_when_llm_misses_salary():
    verdict = feed.judge(GOOD, {**ASSESS_OK, "salary": "none"}, "jobs_uz")
    assert verdict.ok and verdict.salary == "amount"


def test_prefilter_skips_digests_and_junk():
    digest = "Vakansiyalar: " + " ".join(f"Sotuvchi kerak +99890123456{i}." for i in range(5)) + " " * 30 + "x" * 40
    assert feed.prefilter(digest) == (False, "подборка нескольких вакансий")
    assert feed.prefilter("привет")[0] is False
    assert feed.prefilter(GOOD) == (True, "")


def test_fingerprint_ignores_case_and_spacing():
    assert feed.fingerprint(GOOD) == feed.fingerprint("  " + GOOD.upper().replace(" ", "  ") + " ")
    assert feed.fingerprint(GOOD) != feed.fingerprint(GOOD + " Yangi")


# ------------------------------------------------------------------ разбор поста
class FakeAI:
    def __init__(self, assessment=None, fail_assess=False):
        self.assessment = assessment or ASSESS_OK
        self.fail_assess = fail_assess
        self.assess_calls = 0
        self.rewrite_calls = 0

    async def assess_vacancy(self, text):
        self.assess_calls += 1
        if self.fail_assess:
            raise RuntimeError("503")
        return dict(self.assessment)

    async def rewrite_vacancy(self, text, *, default_region_tag="#TOSHKENT"):
        self.rewrite_calls += 1
        return VacancyData(headline="Barista kerak", intro=None, company="Cafe", region_tag="#TOSHKENT", address="Chilonzor",
                           salary="4 000 000 so'm", schedule="9:00-18:00", requirements=["18 yosh"], duties=[], benefits=["Choychaqa"],
                           phone="+998901234567", telegram="@jobs_uz", image_prompt="уютное кафе, бариста за стойкой")


def _src(name="jobs_uz"):
    feed.add_pending(name, {"title": name, "members": 9000})
    feed.set_source_status(name, "approved")


def test_good_post_is_queued_with_fixed_contact_and_scene():
    _src()
    ai = FakeAI()
    result, cand = asyncio.run(feed.consider(GOOD, source="jobs_uz", msg_id=7, ai=ai))
    assert result == "queued" and cand["status"] == "new" and cand["url"] == "https://t.me/jobs_uz/7"
    assert cand["data"]["telegram"] == "@cafe_hr"           # ник источника из разбора заменён ником работодателя
    assert cand["scene"] == "уютное кафе, бариста за стойкой"
    assert feed.get_candidate(cand["id"]) is not None
    feed.reset_cache()                                       # переживает перезапуск
    assert feed.get_candidate(cand["id"])["data"]["phone"] == "+998901234567"


def test_same_post_twice_and_same_vacancy_from_another_channel_are_not_repeated():
    _src("jobs_uz")
    _src("ish_bor")
    ai = FakeAI()
    assert asyncio.run(feed.consider(GOOD, source="jobs_uz", msg_id=7, ai=ai))[0] == "queued"
    assert asyncio.run(feed.consider(GOOD, source="jobs_uz", msg_id=8, ai=ai)) == ("skip", "уже видели")
    other = GOOD.replace("Kafega", "Kafe-restoranga")        # текст чуть иначе — отпечаток другой, но телефон и должность те же
    assert asyncio.run(feed.consider(other, source="ish_bor", msg_id=3, ai=ai)) == ("skip", "такая вакансия уже есть")
    assert ai.assess_calls == 2 and ai.rewrite_calls == 2


def test_rejected_post_costs_no_rewrite_and_is_counted():
    _src()
    ai = FakeAI({**ASSESS_OK, "abroad": True})
    result, reasons = asyncio.run(feed.consider(GOOD, source="jobs_uz", msg_id=7, ai=ai))
    assert result == "reject" and reasons == ["работа за границей"]
    assert ai.rewrite_calls == 0
    stats = feed.sources()["jobs_uz"]["stats"]
    assert stats["rejected"] == 1 and stats["reasons"] == {"работа за границей": 1}


def test_ai_failure_is_retried_later():
    _src()
    broken = FakeAI(fail_assess=True)
    assert asyncio.run(feed.consider(GOOD, source="jobs_uz", msg_id=7, ai=broken))[0] == "error"
    assert asyncio.run(feed.consider(GOOD, source="jobs_uz", msg_id=7, ai=FakeAI()))[0] == "queued"   # отпечаток не записали


# ------------------------------------------------------------------ чтение канала
class FakeClient:
    def __init__(self, posts):
        self.posts = posts
        self.asked: list = []

    async def get_entity(self, name):
        return SimpleNamespace(username=name)

    async def iter_messages(self, entity, limit=None, min_id=0):
        self.asked.append((entity.username, limit, min_id))
        for p in sorted(self.posts, key=lambda m: -m.id):
            if p.id > min_id:
                yield p


def _post(mid, text, hours_ago=1.0):
    return SimpleNamespace(id=mid, message=text, date=datetime.now(timezone.utc) - timedelta(hours=hours_ago))


def test_pull_source_reads_only_new_and_fresh_posts():
    _src()
    client = FakeClient([_post(5, GOOD, hours_ago=100), _post(6, GOOD.replace("barista", "oshpaz"), hours_ago=2), _post(7, "Salom, kanalga obuna bo'ling", 1)])
    counts = asyncio.run(feed.pull_source(client, "jobs_uz", FakeAI()))
    assert counts["queued"] == 1 and counts["skipped"] == 2                # старый пост и болтовня
    assert feed.sources()["jobs_uz"]["last_id"] == 7
    counts = asyncio.run(feed.pull_source(client, "jobs_uz", FakeAI()))
    assert client.asked[-1] == ("jobs_uz", feed.PULL_LIMIT, 7) and counts["posts"] == 0


def test_pull_source_does_not_skip_posts_when_ai_is_down():
    _src()
    client = FakeClient([_post(6, GOOD, hours_ago=2)])
    counts = asyncio.run(feed.pull_source(client, "jobs_uz", FakeAI(fail_assess=True)))
    assert counts["errors"] == 1 and feed.sources()["jobs_uz"]["last_id"] == 0
    assert asyncio.run(feed.pull_source(client, "jobs_uz", FakeAI()))["queued"] == 1


def test_pull_all_needs_jes_online(monkeypatch):
    _src()
    from bot import caller

    monkeypatch.setattr(caller, "user_client", lambda: None)
    with pytest.raises(feed.FeedUnavailable):
        asyncio.run(feed.pull_all(FakeAI()))
    monkeypatch.setattr(caller, "user_client", lambda: FakeClient([_post(6, GOOD, 2)]))
    assert asyncio.run(feed.pull_all(FakeAI()))["queued"] == 1


def test_flood_wait_pauses_everything(monkeypatch):
    _src()
    from bot import caller

    class FloodWaitError(Exception):
        seconds = 300

    class Flooded(FakeClient):
        async def get_entity(self, name):
            raise FloodWaitError()

    monkeypatch.setattr(caller, "user_client", lambda: Flooded([]))
    with pytest.raises(feed.FeedUnavailable):
        asyncio.run(feed.pull_all(FakeAI()))
    with pytest.raises(feed.FeedUnavailable, match="подождать"):
        asyncio.run(feed.pull_all(FakeAI()))                                # второй раз даже не стучимся


# ------------------------------------------------------------------ показ карточек
def _cand(cid, score, status="new", hours_old=1):
    c = {"id": cid, "source": "jobs_uz", "msg_id": 1, "url": "u", "status": status, "score": score, "key": cid,
         "created": (datetime.now(feed.TZ) - timedelta(hours=hours_old)).isoformat(timespec="seconds"), "data": {}}
    feed.load()["queue"][cid] = c
    return c


NOON = datetime(2026, 10, 7, 12, 0, tzinfo=feed.TZ)


def test_default_is_seven_cards_a_day():
    assert feed.load()["cap"] == 7 and 7 in feed.CAPS


def test_best_candidate_first_within_daily_cap():
    _cand("a", 50)
    _cand("b", 80)
    assert feed.next_card(NOON)["id"] == "b"
    feed.set_cap(3)
    for _ in range(3):
        feed.note_card_sent()
    assert feed.next_card(NOON) is None


def test_cards_only_in_daytime_unless_forced():
    _cand("a", 50)
    night = NOON.replace(hour=2)
    assert feed.next_card(night) is None
    assert feed.next_card(night, force=True)["id"] == "a"


def test_one_card_at_a_time_and_not_too_often():
    """«Бот без остановки шлёт вакансии» → по одной и редко: интервал между карточками, одна открытая, не пока платный пост на топе."""
    assert feed.MAX_OPEN_CARDS == 1 and feed.cfg("card_gap_min") == 120
    _cand("a", 50)
    _cand("b", 40)
    ts = NOON.timestamp()
    feed.note_card_sent()
    feed.load()["last_card"] = ts - 30 * 60                                   # только что показали карточку
    assert feed.next_card(NOON) is None and 89 * 60 < feed.card_wait_seconds(ts) <= 90 * 60
    feed.load()["last_card"] = ts - 121 * 60                                  # прошло больше двух часов
    assert feed.next_card(NOON)["id"] == "a"
    _cand("open", 10, status="carded")                                         # одна карточка ещё без ответа
    assert feed.next_card(NOON) is None
    assert feed.next_card(NOON, force=True)["id"] == "a"                      # «Следующая вакансия» — всё это снимает
    feed.drop_candidate("open")
    feed.note_manual_post(ts - 600)                                            # платный пост на топе — карточек нет
    assert feed.next_card(NOON) is None and feed.next_card(NOON, force=True)["id"] == "a"
    feed.set_cfg("card_gap_min", 60)
    feed.clear_hold()
    feed.load()["last_card"] = ts - 61 * 60
    assert feed.next_card(NOON)["id"] == "a"


def test_no_pile_up_when_he_does_not_answer():
    for i in range(feed.MAX_OPEN_CARDS):
        _cand(f"o{i}", 10, status="carded")
    _cand("new", 99)
    assert feed.next_card(NOON) is None


def test_switch_off_and_cleanup():
    _cand("a", 50)
    feed.set_enabled(False)
    assert feed.next_card(NOON) is None
    _cand("old", 50, hours_old=feed.NEW_HOURS + 1)
    _cand("done", 50, status="skipped")
    feed.cleanup()
    assert set(feed.load()["queue"]) == {"a"}


def test_handled_vacancy_is_remembered_by_key():
    cand = _cand("a", 50)
    cand["key"] = "same-key"
    feed.mark_handled(cand, "skipped")
    assert "same-key" in feed.load()["published"]


# ------------------------------------------------------------------ просмотры и каналы, которые он назвал сам
def test_views_bonus_is_logarithmic_and_capped():
    assert feed.views_bonus(0) == 0 and feed.views_bonus(None) == 0 and feed.views_bonus("x") == 0
    assert feed.views_bonus(10) < feed.views_bonus(1000) < feed.views_bonus(100000) == 15


def test_with_equal_score_the_more_viewed_post_comes_first_but_quality_still_wins():
    a, b = _cand("a", 70), _cand("b", 70)
    a["views"], b["views"] = 50, 9000
    assert feed.next_card(NOON)["id"] == "b"
    c = _cand("c", 90)
    c["views"] = 0
    assert feed.next_card(NOON)["id"] == "c"                                  # 90 + 0 всё равно выше, чем 70 + 15


def test_pull_source_remembers_the_views_of_the_original():
    _src()
    post = SimpleNamespace(id=6, message=GOOD, date=datetime.now(timezone.utc) - timedelta(hours=2), views=4321)
    asyncio.run(feed.pull_source(FakeClient([post]), "jobs_uz", FakeAI()))
    assert feed.candidates("new")[0]["views"] == 4321


def test_pull_source_stops_when_the_stock_of_candidates_is_big_enough(monkeypatch):
    _src()
    monkeypatch.setattr(feed, "MAX_NEW_PER_SOURCE", 1)
    second = GOOD.replace("barista", "oshpaz").replace("+998 90 123 45 67", "+998 90 555 11 22")
    ai = FakeAI()
    counts = asyncio.run(feed.pull_source(FakeClient([_post(6, GOOD, 2), _post(7, second, 2)]), "jobs_uz", ai))
    assert counts["queued"] == 1 and ai.assess_calls == 1                       # нейросеть на лишнее не тратим
    assert feed.candidates("new")[0]["msg_id"] == 7                             # взяли самый свежий из канала, а не самый старый
    assert feed.sources()["jobs_uz"]["last_id"] == 7                            # старый пост уже не нужен


def test_ensure_favorites_adds_his_channels_once_and_respects_removal():
    assert feed.ensure_favorites() is True
    got = feed.sources("approved")
    assert set(got) == set(feed.FAVORITE_SOURCES) and all(v["favorite"] for v in got.values())
    assert feed.ensure_favorites() is False
    feed.set_source_status("ish_keremi", "rejected")                            # убрал сам — не возвращаем
    feed.ensure_favorites()
    assert "ish_keremi" not in feed.sources("approved")


def test_new_favorites_make_the_channel_search_due_soon():
    feed.load()["last_discovery"] = 12345.0
    feed.ensure_favorites()
    assert feed.load()["last_discovery"] == 0.0
    feed.load()["last_discovery"] = 777.0
    feed.ensure_favorites()                                                     # ничего нового — время поиска не трогаем
    assert feed.load()["last_discovery"] == 777.0


def test_vacancy_already_in_our_channel_is_not_offered_again():
    _src()
    feed.note_channel_phones("Aloqa: +998 90 123 45 67 | +998 93 111 22 33")
    ai = FakeAI()
    assert asyncio.run(feed.consider(GOOD, source="jobs_uz", msg_id=7, ai=ai)) == ("skip", "уже есть в нашем канале")
    assert ai.assess_calls == 0                                                 # и нейросеть на неё не тратим
    feed.load()["channel_phones"].clear()
    feed.note_channel_phones(GOOD, ts=time.time() - 30 * 86400)                 # месяц назад — уже не считается
    assert not feed.in_channel(GOOD)
    assert asyncio.run(feed.consider(GOOD, source="jobs_uz", msg_id=7, ai=ai))[0] == "queued"


def test_old_guessed_protection_is_dropped_once_after_the_update():
    """До 07.10 вечера защиту включал любой чужой пост (и бесплатный). После обновления такие «платные» отметки сбрасываются один раз."""
    import json

    (feed._file()).write_text(json.dumps({"hold_until": time.time() + 3 * 3600, "hold_post": 984}), encoding="utf-8")
    feed.reset_cache()
    assert feed.hold_left() == 0 and feed.load()["hold_v2"] is True
    feed.note_manual_post(time.time(), 5)                                       # его настоящая отметка после обновления — сохраняется
    feed.save()
    feed.reset_cache()
    assert feed.hold_left() > 2.9 * 3600


def test_search_keywords_cover_the_channels_he_named():
    assert {"ish kerak", "xodim kerak", "ish topish"} <= set(feed.KEYWORDS)


# ------------------------------------------------------------------ поиск каналов
def test_discover_rates_channels_and_adds_only_good_ones(monkeypatch):
    from bot import caller

    good_posts = [_post(i, GOOD.replace("barista", f"ish{i}") + f" #{i}", 5) for i in range(1, 9)]
    bad_posts = [_post(i, "Koreyaga ishchilar kerak, viza. Aloqa +998901234567. Maosh 2000$", 5) for i in range(1, 9)]
    chats = {
        "good_jobs": SimpleNamespace(username="good_jobs", title="Good", broadcast=True, megagroup=False, participants_count=9000),
        "scam_jobs": SimpleNamespace(username="scam_jobs", title="Scam", broadcast=True, megagroup=False, participants_count=9000),
        "tiny_jobs": SimpleNamespace(username="tiny_jobs", title="Tiny", broadcast=True, megagroup=False, participants_count=100),
        "chat_group": SimpleNamespace(username="chat_group", title="Chat", broadcast=False, megagroup=True, participants_count=9000),
        "own": SimpleNamespace(username="ishdasiz", title="Own", broadcast=True, megagroup=False, participants_count=9000),
    }

    class SearchClient(FakeClient):
        def __init__(self):
            super().__init__([])

        async def __call__(self, request):
            kind = type(request).__name__
            if kind == "SearchRequest":
                return SimpleNamespace(chats=list(chats.values()))
            chat = request.channel
            return SimpleNamespace(full_chat=SimpleNamespace(participants_count=chat.participants_count))

        async def iter_messages(self, entity, limit=None, min_id=0):
            for p in (good_posts if entity.username == "good_jobs" else bad_posts):
                yield p

    monkeypatch.setattr(caller, "user_client", lambda: SearchClient())
    added = asyncio.run(feed.discover(own_channel="@ishdasiz"))
    assert [a["name"] for a in added] == ["good_jobs"]
    assert feed.sources("pending")["good_jobs"]["passing"] >= 5
    assert asyncio.run(feed.discover(own_channel="@ishdasiz")) == []          # уже известные не предлагаем второй раз


# ------------------------------------------------------------------ только Vertex (никакого AI Studio)
def _ai_client(monkeypatch, handler):
    import httpx

    from bot.context import ai

    monkeypatch.setenv("VERTEX_API_KEY", "VKEY")
    monkeypatch.setenv("VERTEX_PROJECT", "123")
    from bot import gcloud

    gcloud.reset_cache()
    monkeypatch.setattr(ai, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"x-goog-api-key": "STUDIO"}))
    return ai


def test_feed_text_requests_never_reach_ai_studio(monkeypatch):
    import httpx

    from bot.ai import vertex_only

    hosts: list = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(500, text="boom")

    ai = _ai_client(monkeypatch, handler)
    monkeypatch.setattr("bot.ai._backoff", lambda attempt: 0.0)     # 09.10: Vertex на временных сбоях повторяется — без пауз в тесте
    with vertex_only():
        with pytest.raises(RuntimeError, match="Vertex"):
            asyncio.run(ai._post("gemini-3.5-flash-lite", {"contents": []}))
    assert hosts and set(hosts) == {"aiplatform.googleapis.com"}


def test_feed_text_goes_to_vertex_even_if_settings_say_ai_studio(monkeypatch):
    import httpx

    from bot import gcloud
    from bot.ai import vertex_only

    seen: list = []

    def handler(request):
        seen.append((request.url.host, request.headers["x-goog-api-key"]))
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "{}"}]}}], "usageMetadata": {"promptTokenCount": 3}})

    ai = _ai_client(monkeypatch, handler)
    assert gcloud.chosen() == "studio"
    with vertex_only():
        asyncio.run(ai._post("gemini-3.5-flash-lite", {"contents": []}))
    assert seen == [("aiplatform.googleapis.com", "VKEY")]
    asyncio.run(ai._post("gemini-3.5-flash-lite", {"contents": []}))          # без vertex_only всё как раньше — AI Studio
    assert seen[-1][0] == "generativelanguage.googleapis.com"


def test_no_vertex_key_stops_feed_text_requests(monkeypatch):
    import httpx

    from bot.ai import vertex_only

    ai = _ai_client(monkeypatch, lambda request: httpx.Response(200, json={}))
    monkeypatch.delenv("VERTEX_API_KEY")
    with vertex_only():
        with pytest.raises(RuntimeError, match="VERTEX_API_KEY"):
            asyncio.run(ai._post("gemini-3.5-flash-lite", {"contents": []}))


# ------------------------------------------------------------------ только свежее и лучшее, раз в N часов (08.10)
def test_fresh_and_best_goes_first_and_old_posts_are_dropped():
    now_ts = NOON.timestamp()
    old = _cand("old", 80)
    old["posted_ts"] = now_ts - 20 * 3600                                       # 20 часов назад
    new = _cand("new", 70)
    new["posted_ts"] = now_ts - 1 * 3600                                        # час назад
    assert feed.freshness_bonus(now_ts - 3600, now_ts) == 15 and feed.freshness_bonus(now_ts - 20 * 3600, now_ts) == 0
    assert feed.rank(new, now_ts) > feed.rank(old, now_ts)                      # 70+15 против 80+0
    assert feed.next_card(NOON)["id"] == "new"
    stale = _cand("stale", 99)
    stale["posted_ts"] = now_ts - (feed.MAX_AGE_H + 1) * 3600
    feed.cleanup(NOON)
    assert "stale" not in feed.load()["queue"] and "new" in feed.load()["queue"]


def test_pull_source_takes_the_freshest_posts_first_and_keeps_a_small_stock(monkeypatch):
    _src()
    monkeypatch.setattr(feed, "MAX_NEW_PER_SOURCE", 2)
    phones = ["+998 90 111 11 11", "+998 90 222 22 22", "+998 90 333 33 33", "+998 90 444 44 44"]
    titles = ["oshpaz", "haydovchi", "tikuvchi", "sotuvchi"]
    posts = [_post(10 + i, GOOD.replace("barista", titles[i]).replace("+998 90 123 45 67", phones[i]), hours_ago=8 - 2 * i) for i in range(4)]
    counts = asyncio.run(feed.pull_source(FakeClient(posts), "jobs_uz", FakeAI()))
    assert counts["queued"] == 2
    assert sorted(c["msg_id"] for c in feed.candidates("new")) == [12, 13]      # два самых свежих (2 и 0 часов назад)
    assert feed.sources()["jobs_uz"]["last_id"] == 13


def test_next_card_time_follows_the_interval_the_window_and_the_daily_cap():
    ts = NOON.timestamp()
    assert feed.next_card_at(NOON) == NOON                                      # карточек ещё не было — можно сразу
    feed.load()["last_card"] = ts - 30 * 60
    assert feed.next_card_at(NOON) == NOON + timedelta(minutes=90)              # интервал 2 ч от прошлой
    feed.load()["last_card"] = ts - 10 * 60
    feed.set_cfg("card_gap_min", 240)
    assert feed.next_card_at(NOON) == NOON + timedelta(minutes=230)
    feed.set_cfg("window_to", 14)                                               # 15:50 уже за окном → завтра с начала окна
    nxt = feed.next_card_at(NOON)
    assert nxt.date() == (NOON + timedelta(days=1)).date() and nxt.hour == feed.window()[0]
    feed.set_enabled(False)
    assert feed.next_card_at(NOON) is None

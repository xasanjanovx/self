"""07.10: автоподбор вакансий из чужих каналов — отбор «только надёжные», очередь, чтение каналов (без сети и без Telegram)."""
import asyncio
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
    assert feed.next_card(night, ignore_hours=True)["id"] == "a"


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
    with vertex_only():
        with pytest.raises(RuntimeError, match="Vertex"):
            asyncio.run(ai._post("gemini-3.5-flash-lite", {"contents": []}))
    assert hosts == ["aiplatform.googleapis.com"]


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

"""07.10: картинки к вакансиям — Nano Banana 2.1 через Vertex с запасным AI Studio, логотип снизу слева (без сети)."""
import asyncio
import base64
from io import BytesIO

import httpx
import pytest
from PIL import Image

from bot import billing, gcloud, image_gen
from bot.ai import VacancyData


def _png(color=(30, 60, 160), size=(1280, 720)) -> bytes:
    out = BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


def _ok(image: bytes) -> dict:
    return {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": base64.b64encode(image).decode()}}]}}],
            "usageMetadata": {"promptTokenCount": 60, "candidatesTokenCount": 1120, "totalTokenCount": 1180}}


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VERTEX_API_KEY", "VKEY")
    monkeypatch.setenv("VERTEX_PROJECT", "123")
    gcloud.reset_cache()
    recorded: list = []
    monkeypatch.setattr(billing, "record", lambda model, usage, **kw: recorded.append((model, kw.get("provider"), kw.get("kind"))) or 0.0)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(image_gen.asyncio, "sleep", no_sleep)

    def install(handler):
        monkeypatch.setattr(image_gen, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    return install, recorded


def _data():
    return VacancyData(headline="Barista kerak", intro=None, company="Bahor Coffee", region_tag="#TOSHKENT", address="Chilonzor",
                       salary="4 000 000 so'm", schedule="9:00-18:00", requirements=["18 yosh"], duties=[], benefits=["Bepul tushlik"],
                       phone="+998901234567", telegram="@hr_ish")


def test_prompt_is_a_full_poster_brief_with_exact_texts_and_a_free_corner_for_the_logo():
    prompt = image_gen.build_prompt(_data(), "уютная кофейня, бариста готовит кофе")
    assert "JOB VACANCY POSTER" in prompt and "photorealistic" in prompt and "уютная кофейня" in prompt
    for exact in ('"BARISTA"', '"KERAK"', '"ISHGA TAKLIF QILAMIZ!"', "\"4 000 000 so'm\"", '"9:00-18:00"', '"+998 90 123 45 67"', '"@hr_ish"',
                  '"#TOSHKENT"', '"Bepul tushlik"', '"Bahor Coffee"'):
        assert exact in prompt, exact
    assert "RESERVED ZONE" in prompt and "bottom-left" in prompt and "do not draw any logo" in prompt and "NO panel" in prompt


def test_image_request_asks_for_3_2_at_2k_and_falls_back_without_size(setup):
    install, _ = setup
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        config = json.loads(request.content)["generationConfig"]["imageConfig"]
        seen.append(config)
        if "imageSize" in config:
            return httpx.Response(400, json={"error": {"message": "Invalid value at 'generation_config.image_config.image_size'"}})
        return httpx.Response(200, json=_ok(_png()))

    install(handler)
    asyncio.run(image_gen.generate("prompt"))
    assert seen == [{"aspectRatio": "3:2", "imageSize": "2K"}, {"aspectRatio": "3:2"}]


def test_manual_prompt_for_chatgpt_is_unchanged():
    from bot.vacancy import build_full_prompt

    manual = build_full_prompt(_data(), scene="кофейня")
    assert "@ishdasiz" in manual and "КОМПОЗИЦИЯ" not in manual


def test_only_vertex_even_when_ai_studio_is_selected_in_settings(setup):
    install, recorded = setup
    assert gcloud.chosen() == "studio"                       # в настройках выбран AI Studio — картинки всё равно только Vertex
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.host, request.url.path, request.headers["x-goog-api-key"]))
        return httpx.Response(200, json=_ok(_png()))

    install(handler)
    image, model = asyncio.run(image_gen.generate("prompt"))
    assert model == "gemini-nano-banana-2.1" and image[:4] == b"\x89PNG"
    assert calls == [("aiplatform.googleapis.com", "/v1/projects/123/locations/global/publishers/google/models/gemini-nano-banana-2.1:generateContent", "VKEY")]
    assert recorded == [("gemini-nano-banana-2.1", "vertex", "image")]


def test_never_falls_back_to_ai_studio(setup):
    install, recorded = setup
    hosts: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(404, json={"error": {"message": "Publisher model was not found"}})

    install(handler)
    with pytest.raises(image_gen.ImageError):
        asyncio.run(image_gen.generate("prompt"))
    assert set(hosts) == {"aiplatform.googleapis.com"} and recorded == []     # обе модели — только Vertex, платить AI Studio не пытались


def test_no_vertex_key_means_no_request_at_all(setup, monkeypatch):
    install, _ = setup
    monkeypatch.delenv("VERTEX_API_KEY")
    install(lambda request: pytest.fail("запрос не должен уйти"))
    with pytest.raises(image_gen.ImageError, match="VERTEX_API_KEY"):
        asyncio.run(image_gen.generate("prompt"))


def test_falls_back_to_nano_banana_2_when_2_1_is_missing(setup):
    install, _ = setup
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = request.url.path.split("/")[-1].split(":")[0]
        seen.append(model)
        if model == "gemini-nano-banana-2.1":
            return httpx.Response(404, json={"error": {"message": "not found"}})
        return httpx.Response(200, json=_ok(_png()))

    install(handler)
    _, model = asyncio.run(image_gen.generate("prompt"))
    assert model == "gemini-3.1-flash-image" and seen == ["gemini-nano-banana-2.1", "gemini-3.1-flash-image"]


def test_refused_key_stops_at_once(setup):
    install, _ = setup
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(403, json={"error": {"message": "API key blocked"}})

    install(handler)
    with pytest.raises(image_gen.ImageError, match="не пускает ключ"):
        asyncio.run(image_gen.generate("prompt"))
    assert len(calls) == 1


def test_safety_block_does_not_touch_the_shared_vertex_state(setup):
    install, _ = setup
    gcloud.set_provider("vertex")
    install(lambda request: httpx.Response(400, json={"error": {"message": "The prompt was blocked for safety"}}))
    with pytest.raises(image_gen.ImageError):
        asyncio.run(image_gen.generate("prompt"))
    assert gcloud.active() and gcloud.status()["error"] is None            # текстовые запросы из-за одного промпта не страдают


def test_transient_error_is_retried_once(setup):
    install, _ = setup
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(503, text="busy") if len(attempts) == 1 else httpx.Response(200, json=_ok(_png()))

    install(handler)
    asyncio.run(image_gen.generate("prompt"))
    assert len(attempts) == 2


def test_reply_without_image_is_an_error(setup):
    install, _ = setup
    install(lambda request: httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "sorry"}]}}], "promptFeedback": {"blockReason": "SAFETY"}}))
    with pytest.raises(image_gen.ImageError, match="SAFETY"):
        asyncio.run(image_gen.generate("prompt"))


def _logo_box(width=1280, height=720):
    logo_w = max(40, int(width * image_gen.LOGO_WIDTH_SHARE))
    from PIL import Image as _I

    logo_h = round(_I.open(image_gen.LOGO_LIGHT).height * logo_w / _I.open(image_gen.LOGO_LIGHT).width)
    margin = int(width * image_gen.LOGO_MARGIN_SHARE)
    return margin, height - logo_h - margin, logo_w, logo_h


def _count(img, box, predicate):
    x0, y0, w, h = box
    return sum(1 for x in range(x0, x0 + w, 2) for y in range(y0, y0 + h, 2) if predicate(img.getpixel((x, y))))


def test_logo_on_a_dark_photo_is_the_light_one_without_the_yellow_plate():
    blue = (30, 60, 160)
    img = Image.open(BytesIO(image_gen.add_logo(_png(blue))))
    assert img.format == "JPEG" and img.size == (1280, 720)
    box = _logo_box()
    assert _count(img, box, lambda p: p[0] > 200 and p[1] > 170 and p[2] < 90) > 150      # жёлтый круг и слово есть
    x0, y0, w, h = box
    r, g, b = img.getpixel((x0 + 3, y0 + 3))                                               # угол бывшей плашки — снова фон, не жёлтый
    assert abs(r - blue[0]) < 25 and abs(g - blue[1]) < 25 and abs(b - blue[2]) < 25
    r, g, b = img.getpixel((1270, 10))                                                      # верхний правый угол как был
    assert abs(r - blue[0]) < 12 and abs(b - blue[2]) < 12
    r, g, b = img.getpixel((x0 + w + 40, y0 + h // 2))                                      # правее логотипа — исходный фон
    assert abs(b - blue[2]) < 12


def test_logo_on_a_light_photo_is_navy():
    light = (240, 242, 246)
    img = Image.open(BytesIO(image_gen.add_logo(_png(light))))
    box = _logo_box()
    assert _count(img, box, lambda p: p[2] < 110 and p[0] < 60) > 150                     # тёмно-синие буквы и круг
    assert _count(img, box, lambda p: p[0] > 200 and p[1] > 170 and p[2] < 90) == 0       # жёлтого нет


def test_vacancy_image_is_model_picture_with_logo(setup, monkeypatch):
    install, _ = setup
    install(lambda request: httpx.Response(200, json=_ok(_png())))

    async def good(raw, data):
        return True, ""

    monkeypatch.setattr(image_gen, "inspect", good)
    banner = asyncio.run(image_gen.vacancy_image(_data(), "кафе"))
    assert Image.open(BytesIO(banner.image)).format == "JPEG" and banner.warning is None


def test_missing_logo_still_gives_the_picture(setup, monkeypatch):
    install, _ = setup
    install(lambda request: httpx.Response(200, json=_ok(_png())))

    async def good(raw, data):
        return True, ""

    monkeypatch.setattr(image_gen, "inspect", good)
    monkeypatch.setattr(image_gen, "LOGO_LIGHT", image_gen.LOGO_LIGHT.with_name("nope.png"))
    monkeypatch.setattr(image_gen, "LOGO_NAVY", image_gen.LOGO_NAVY.with_name("nope.png"))
    banner = asyncio.run(image_gen.vacancy_image(_data()))
    assert banner.image[:4] == b"\x89PNG"                                  # без логотипа, но с картинкой


def test_banner_is_redrawn_until_the_check_passes(setup, monkeypatch):
    install, _ = setup
    drawn = []

    def handler(request: httpx.Request) -> httpx.Response:
        drawn.append(1)
        return httpx.Response(200, json=_ok(_png()))

    install(handler)
    verdicts = iter([(False, "в левом нижнем углу есть текст — там ляжет логотип"), (False, "телефон на картинке не совпал с вакансией"), (True, "")])

    async def check(raw, data):
        return next(verdicts)

    monkeypatch.setattr(image_gen, "inspect", check)
    banner = asyncio.run(image_gen.vacancy_image(_data()))
    assert len(drawn) == 3 and banner.warning is None


def test_after_three_failed_checks_the_last_banner_goes_out_with_a_warning(setup, monkeypatch):
    install, _ = setup
    drawn = []

    def handler(request: httpx.Request) -> httpx.Response:
        drawn.append(1)
        return httpx.Response(200, json=_ok(_png()))

    install(handler)

    async def bad(raw, data):
        return False, "телефон на картинке не совпал с вакансией"

    monkeypatch.setattr(image_gen, "inspect", bad)
    banner = asyncio.run(image_gen.vacancy_image(_data()))
    assert len(drawn) == image_gen.ATTEMPTS and banner.image and "телефон на картинке не совпал" in banner.warning


# ------------------------------------------------------------------ проверка текста на баннере
class _FakeAI:
    def __init__(self, answer=None, boom=False):
        self.answer, self.boom, self.seen_vertex_only = answer, boom, []

    async def generate(self, parts, **kw):
        from bot.ai import _vertex_only

        self.seen_vertex_only.append(_vertex_only.get())
        if self.boom:
            raise RuntimeError("503")
        assert parts[0]["inline_data"]["mime_type"] == "image/jpeg"
        import json

        return json.dumps(self.answer)


def _inspect(monkeypatch, answer=None, boom=False):
    from bot.context import ai

    fake = _FakeAI(answer, boom)
    monkeypatch.setattr(ai, "generate", fake.generate)
    return asyncio.run(image_gen.inspect(_png(), _data())), fake


def test_inspect_accepts_exact_phone_and_empty_corner(monkeypatch):
    (ok, why), fake = _inspect(monkeypatch, {"lines": ["BARISTA KERAK", "MAOSH: 4 000 000 SO'M", "Tel: +998 90 123 45 67"], "text_in_bottom_left": False})
    assert ok and why == "" and fake.seen_vertex_only == [True]          # проверка тоже только через Vertex


def test_inspect_rejects_a_wrong_digit_in_the_phone(monkeypatch):
    (ok, why), _ = _inspect(monkeypatch, {"lines": ["BARISTA KERAK", "4 000 000", "Tel: +998 90 123 45 68"], "text_in_bottom_left": False})
    assert not ok and "телефон" in why


def test_inspect_rejects_text_in_the_logo_corner(monkeypatch):
    (ok, why), _ = _inspect(monkeypatch, {"lines": ["BARISTA KERAK", "4 000 000", "Tel: +998901234567"], "text_in_bottom_left": True})
    assert not ok and "левом нижнем углу" in why


def test_inspect_rejects_a_wrong_digit_in_the_salary(monkeypatch):
    (ok, why), _ = _inspect(monkeypatch, {"lines": ["BARISTA KERAK", "MAOSH: 4 000 010 so'm", "Tel: +998 90 123 45 67"], "text_in_bottom_left": False})
    assert not ok and "зарплаты" in why
    (ok, _), _ = _inspect(monkeypatch, {"lines": ["BARISTA KERAK", "MAOSH: 4 000 000 so'm", "Tel: +998 90 123 45 67"], "text_in_bottom_left": False})
    assert ok


def test_salary_numbers_ignore_short_numbers_and_split_ranges():
    assert image_gen._salary_numbers("4 000 000 so'mdan + KPI") == ["4000000"]
    assert image_gen._salary_numbers("3 000 000 - 5 000 000 so'm") == ["3000000", "5000000"]
    assert image_gen._salary_numbers("400$ - 800$, haftada 6 kun") == []
    assert image_gen._salary_numbers(None) == []


def test_inspect_retries_when_vertex_is_busy_then_gives_up_as_unchecked(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(image_gen.asyncio, "sleep", no_sleep)
    (ok, why), fake = _inspect(monkeypatch, boom=True)
    assert ok is None and why == "" and len(fake.seen_vertex_only) == 3


def test_unchecked_banner_goes_out_with_a_warning_after_a_single_drawing(setup, monkeypatch):
    install, _ = setup
    drawn = []
    install(lambda request: drawn.append(1) or httpx.Response(200, json=_ok(_png())))

    async def unknown(raw, data):
        return None, ""

    monkeypatch.setattr(image_gen, "inspect", unknown)
    banner = asyncio.run(image_gen.vacancy_image(_data()))
    assert len(drawn) == 1 and "не удалось проверить" in banner.warning


def test_inspect_rejects_a_misspelled_headline(monkeypatch):
    (ok, why), _ = _inspect(monkeypatch, {"lines": ["BARSITA KERAK", "4 000 000", "Tel: +998 90 123 45 67"], "text_in_bottom_left": False})
    assert not ok and "заголовок" in why


def test_inspect_compares_the_short_salary_that_is_actually_drawn(monkeypatch):
    data = _data()
    data.salary = "3 000 000 - 11 000 000 so'm (o'z vaqtida)"
    data.short_salary = "3-11 mln so'm"
    from bot.context import ai

    fake = _FakeAI({"lines": ["BARISTA KERAK", "3-11 mln so'm", "Tel: +998 90 123 45 67"], "text_in_bottom_left": False})
    monkeypatch.setattr(ai, "generate", fake.generate)
    ok, why = asyncio.run(image_gen.inspect(_png(), data))
    assert ok and why == ""


def test_inspect_rejects_a_doubled_headline_word_but_not_repeated_digits(monkeypatch):
    (ok, why), _ = _inspect(monkeypatch, {"lines": ["BARISTA BARISTA KERAK", "4 000 000", "Tel: +998 90 123 45 67"], "text_in_bottom_left": False})
    assert not ok and "задвоено" in why
    (ok, _), _ = _inspect(monkeypatch, {"lines": ["BARISTA KERAK", "4 000 000", "Tel: +998 90 123 45 67"], "text_in_bottom_left": False})
    assert ok


def test_logo_variant_follows_the_background_under_the_letters_not_the_whole_corner():
    # красная волна почти под всем логотипом (белым осталась только кромка слева) → светлый (жёлтый) логотип, а не тёмно-синий
    img = Image.new("RGB", (1280, 720), (245, 245, 245))
    box = _logo_box()
    x0, y0, w, h = box
    for x in range(x0 - 10, x0 + w + 10):
        for y in range(y0 - 6, y0 + h + 6):
            if (x - x0) > w * 0.05:
                img.putpixel((x, y), (215, 25, 28))
    out = BytesIO()
    img.save(out, "PNG")
    result = Image.open(BytesIO(image_gen.add_logo(out.getvalue())))
    assert _count(result, box, lambda p: p[0] > 200 and p[1] > 170 and p[2] < 90) > 100        # жёлтое слово на красном


def test_navy_logo_gets_a_light_halo():
    # средне-светлый фон → тёмно-синий логотип; вокруг букв светлый ореол, чтобы читался и на красном, и на пёстром фото
    gray = 150
    out = BytesIO()
    Image.new("RGB", (1280, 720), (gray, gray, gray)).save(out, "PNG")
    result = Image.open(BytesIO(image_gen.add_logo(out.getvalue())))
    x0, y0, w, h = _logo_box()
    assert _count(result, (x0, y0, w, h), lambda p: p[2] < 110 and p[0] < 60) > 100          # синий логотип выбран
    assert result.getpixel((x0 + 2, y0 + h // 2))[0] > gray + 6                              # а у самого края круга — светлее фона

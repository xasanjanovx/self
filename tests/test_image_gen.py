"""07.10: картинки к вакансиям — Nano Banana 2.1 через Vertex с запасным AI Studio, логотип снизу слева (без сети)."""
import asyncio
import base64
from io import BytesIO

import httpx
import pytest
from PIL import Image

from bot import billing, gcloud, image_gen


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


def test_prompt_has_job_scene_and_no_text_rules():
    prompt = image_gen.build_prompt("Barista kerak", "уютное кафе, бариста за стойкой")
    assert "Barista kerak" in prompt and "уютное кафе" in prompt
    assert "bottom-left" in prompt and "no text" in prompt.lower() and "16:9" in prompt


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


def test_logo_lands_bottom_left_and_result_is_jpeg():
    blue = (30, 60, 160)
    out = image_gen.add_logo(_png(blue))
    img = Image.open(BytesIO(out))
    assert img.format == "JPEG" and img.size == (1280, 720)
    margin = int(1280 * image_gen.LOGO_MARGIN_SHARE)
    logo_w = int(1280 * image_gen.LOGO_WIDTH_SHARE)
    # внутри плашки логотипа (левый нижний угол) цвет изменился — основной жёлтый фон логотипа
    r, g, b = img.getpixel((margin + logo_w - 6, 720 - margin - 6))
    assert r > 200 and g > 150 and b < 120
    # верхний правый угол остался как был
    r, g, b = img.getpixel((1270, 10))
    assert abs(r - blue[0]) < 12 and abs(g - blue[1]) < 12 and abs(b - blue[2]) < 12
    # логотип не вылезает: правее и выше плашки — исходный фон
    r, g, b = img.getpixel((margin + logo_w + 30, 720 - margin - 6))
    assert abs(b - blue[2]) < 12


def test_vacancy_image_is_model_picture_with_logo(setup):
    install, _ = setup
    install(lambda request: httpx.Response(200, json=_ok(_png())))
    out = asyncio.run(image_gen.vacancy_image("Barista kerak", "кафе"))
    assert Image.open(BytesIO(out)).format == "JPEG"


def test_missing_logo_still_gives_the_picture(setup, monkeypatch):
    install, _ = setup
    install(lambda request: httpx.Response(200, json=_ok(_png())))
    monkeypatch.setattr(image_gen, "LOGO", image_gen.LOGO.with_name("nope.png"))
    out = asyncio.run(image_gen.vacancy_image("Barista kerak"))
    assert out[:4] == b"\x89PNG"                                           # без логотипа, но с картинкой

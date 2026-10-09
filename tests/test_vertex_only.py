"""09.10: «вообще не используй пока API из Google AI Studio, только Google Cloud Vertex».

Пока ALLOW_AI_STUDIO не включён (в проде его нет), ни один запрос Gemini не идёт на generativelanguage.googleapis.com:
ни платный ключ, ни «бесплатный», ни озвучка, ни живой голос, ни список моделей, ни проверка присланного ключа.
Vertex не ответил — ошибка (с повторами на временных сбоях), а не тихий откат в AI Studio.
"""
import asyncio
import base64
import json
from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from bot import access, billing, gcloud, live_call, secrets_guard
from bot import ai as ai_mod
from bot.keyboards import jarvis_settings_keyboard
from bot.persona import Persona
from bot.profile import Profile

STUDIO = "generativelanguage.googleapis.com"
VERTEX = "aiplatform.googleapis.com"
_OK = {"candidates": [{"content": {"parts": [{"text": "ок"}]}}], "usageMetadata": {"promptTokenCount": 3}}


@pytest.fixture()
def closed(monkeypatch, tmp_path):
    """Прод-режим: AI Studio закрыт, ключ Vertex и проект есть."""
    monkeypatch.delenv("ALLOW_AI_STUDIO", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VERTEX_API_KEY", "VKEY")
    monkeypatch.setenv("VERTEX_PROJECT", "123")
    gcloud.reset_cache()
    monkeypatch.setattr(ai_mod, "_backoff", lambda attempt: 0.0)
    return monkeypatch


def _client(monkeypatch, handler):
    from bot.context import ai

    recorded: list = []
    monkeypatch.setattr(billing, "record", lambda model, usage, **kw: recorded.append(kw.get("provider", "studio")) or 0.0)
    monkeypatch.setattr(ai, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"x-goog-api-key": "STUDIO"}))
    return ai, recorded


def test_default_is_closed_and_text_goes_only_to_vertex(closed):
    seen: list = []

    def handler(request):
        seen.append((request.url.host, request.headers["x-goog-api-key"]))
        return httpx.Response(200, json=_OK)

    ai, recorded = _client(closed, handler)
    assert not gcloud.studio_allowed() and gcloud.chosen() == "vertex" and gcloud.active()
    # даже если в файле настроек остался прежний выбор «AI Studio»
    (gcloud._file()).write_text(json.dumps({"provider": "studio"}), encoding="utf-8")
    gcloud.reset_cache()
    assert asyncio.run(ai._post("gemini-3.5-flash-lite", {"contents": []}))["candidates"]
    assert seen == [(VERTEX, "VKEY")] and recorded == ["vertex"]


def test_vertex_error_raises_and_never_falls_back_or_pauses(closed):
    hosts: list = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(403, json={"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "API_KEY_SERVICE_BLOCKED blocked"}})

    ai, _ = _client(closed, handler)
    with pytest.raises(RuntimeError, match="Vertex"):
        asyncio.run(ai._post("gemini-3.5-flash-lite", {"contents": []}))
    with pytest.raises(RuntimeError, match="Vertex"):                   # пауза ключа не включилась: пробуем снова, а не молчим
        asyncio.run(ai._post("gemini-3.5-flash-lite", {"contents": []}))
    assert hosts == [VERTEX, VERTEX] and gcloud.active()
    assert "Vertex AI API" in gcloud.status()["error"]


def test_transient_vertex_errors_are_retried_on_vertex(closed):
    calls: list = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(503, text="busy") if len(calls) < 3 else httpx.Response(200, json=_OK)

    ai, recorded = _client(closed, handler)
    assert asyncio.run(ai._post("gemini-3.5-flash-lite", {"contents": []}))["candidates"]
    assert calls == [VERTEX] * 3 and recorded == ["vertex"]


def test_missing_model_is_not_blacklisted_when_there_is_no_fallback(closed):
    def handler(request):
        return httpx.Response(404, json={"error": {"code": 404, "message": "Publisher Model was not found"}})

    ai, _ = _client(closed, handler)
    with pytest.raises(RuntimeError):
        asyncio.run(ai._post("gemini-3.8-flash", {"contents": []}))
    assert gcloud.use_vertex("gemini-3.8-flash")                         # к AI Studio всё равно не уйти — в списке «плохих» не держим


def test_no_vertex_key_means_error_not_ai_studio(closed):
    closed.delenv("VERTEX_API_KEY")
    gcloud.reset_cache()
    hosts: list = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(200, json=_OK)

    ai, _ = _client(closed, handler)
    with pytest.raises(RuntimeError, match="VERTEX_API_KEY"):
        asyncio.run(ai._post("gemini-3.5-flash-lite", {"contents": []}))
    assert hosts == []


def test_free_key_is_off_and_free_mode_does_not_reach_ai_studio(closed):
    closed.setattr(ai_mod, "FREE_API_KEY", "FREE")
    hosts: list = []

    def handler(request):
        hosts.append((request.url.host, request.headers["x-goog-api-key"]))
        return httpx.Response(200, json=_OK)

    ai, _ = _client(closed, handler)
    assert ai_mod.free_key() == "" and not ai_mod.free_tts_ready() and not ai_mod.free_status()["configured"]
    token = ai_mod.use_free(ai_mod.FREE_SMART_MODEL)
    try:
        asyncio.run(ai._post("gemini-3.5-flash-lite", {"contents": []}))
    finally:
        ai_mod.reset_free(token)
    assert hosts == [(VERTEX, "VKEY")]


def test_no_model_list_at_startup_and_voice_model_is_vertex_one(closed):
    hosts: list = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(200, json={"models": []})

    ai, _ = _client(closed, handler)
    asyncio.run(ai.ensure_models())
    assert hosts == [] and ai.tts_model == "gemini-3.1-flash-tts-preview"
    assert asyncio.run(ai.list_available_models()) == set()
    assert "x-goog-api-key" not in ai_mod.AIService(SimpleNamespace(
        gemini_api_key="AI-STUDIO-KEY", gemini_model="m", gemini_vision_model="m", gemini_transcribe_model="m", agent_model="m"))._client.headers


def test_trial_expiry_and_provider_switch_cannot_move_to_ai_studio(closed):
    gcloud.set_provider("studio")                                         # кнопка/старый выбор — игнорируется
    assert gcloud.chosen() == "vertex"
    closed.setattr(gcloud, "_today", lambda: date(2027, 3, 1))            # кредит по сроку кончился — всё равно только Vertex
    assert gcloud.active() and gcloud.chosen() == "vertex" and gcloud.daily_limit() == gcloud.VERTEX_DAILY_LIMIT_USD


def test_voice_streams_only_from_vertex_and_retries_then_raises(closed):
    pcm = b"\x02\x00" * 4800
    sse = "data: " + json.dumps({"candidates": [{"content": {"parts": [{"inlineData": {"data": base64.b64encode(pcm).decode()}}]}}]}) + "\n\n"
    hosts: list = []
    state = {"fail": 1}

    def handler(request):
        hosts.append(request.url.host)
        if state["fail"] > 0:
            state["fail"] -= 1
            return httpx.Response(503, text="busy")
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    ai, recorded = _client(closed, handler)

    async def say(text):  # noqa: ANN202
        return b"".join([c async for c in ai.speak_stream(text, voice="Sulafat", free=True)])

    assert asyncio.run(say("Да, сэр.")) == pcm and hosts == [VERTEX, VERTEX] and recorded == ["vertex"]
    hosts.clear()
    state["fail"] = 5
    with pytest.raises(RuntimeError, match="Vertex"):
        asyncio.run(say("Совсем другая длинная фраза, которой нет в записях на диске."))
    assert set(hosts) == {VERTEX}


def test_live_voice_connects_only_to_vertex_and_fails_loudly(closed):
    urls: list = []

    class Http:
        async def ws_connect(self, url, **kw):
            urls.append(url)
            raise OSError("down")

    sess = live_call._Session(Profile(telegram_id=77, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Т", username="t"),
                              Persona(lang="ru"), mode="phone", system="x")
    with pytest.raises(RuntimeError, match="Vertex"):
        asyncio.run(sess.connect(Http()))
    assert urls and all("aiplatform.googleapis.com" in u for u in urls) and not any(STUDIO in u for u in urls)
    closed.delenv("VERTEX_API_KEY")
    with pytest.raises(RuntimeError, match="VERTEX_API_KEY"):
        asyncio.run(sess.connect(Http()))


class _VertexLiveWS:
    """Vertex Live 09.10: отказывает в setup, где есть thinkingConfig («project is not allowlisted to customize the thinking level»)."""

    def __init__(self) -> None:
        self.setup: dict = {}
        self.closed = False

    async def send_str(self, s: str) -> None:
        self.setup = json.loads(s)["setup"]

    async def receive(self):
        if "thinkingConfig" in self.setup.get("generationConfig", {}):
            return SimpleNamespace(type=SimpleNamespace(name="CLOSE"), data=None,
                                   extra="setup: current project is not allowlisted to customize the thinking level for this model")
        return SimpleNamespace(type=SimpleNamespace(name="TEXT"), data=json.dumps({"setupComplete": {}}))

    async def close(self) -> None:
        self.closed = True


def test_vertex_live_drops_thinking_config_when_project_is_not_allowlisted(closed):
    closed.setattr(live_call, "_extras_level", {})
    sockets: list = []
    urls: list = []

    class Http:
        async def ws_connect(self, url, **kw):
            urls.append(url)
            sockets.append(_VertexLiveWS())
            return sockets[-1]

    sess = live_call._Session(Profile(telegram_id=77, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Т", username="t"),
                              Persona(lang="ru", mirror=True), mode="phone", system="x")
    ws = asyncio.run(sess.connect(Http()))
    assert ws is sockets[-1] and len(sockets) == 2 and sess.provider == "vertex"
    assert "thinkingConfig" not in ws.setup["generationConfig"] and "contextWindowCompression" in ws.setup
    assert ws.setup["model"].startswith("projects/123/locations/") and all("aiplatform.googleapis.com" in u for u in urls)
    assert live_call._extras_level[live_call.MODELS[0]] == 1                  # запомнили: следующие звонки сразу без «размышлений»
    assert gcloud.status()["error"] is None


def test_key_sent_to_the_bot_is_not_validated_against_ai_studio(closed, tmp_path):
    closed.setattr(access, "is_owner", lambda uid: uid == 1)

    async def boom(key):  # noqa: ANN001
        raise AssertionError("ключ не должен проверяться запросом в AI Studio")

    closed.setattr(secrets_guard, "validate_gemini", boom)

    class Msg:
        text, caption = "AIza" + "x" * 35, None

        def __init__(self) -> None:
            self.answers: list = []
            self.deleted = False

        async def delete(self) -> None:
            self.deleted = True

        async def answer(self, text: str) -> None:
            self.answers.append(text)

    msg = Msg()
    asyncio.run(secrets_guard.handle(msg, 1))
    assert msg.deleted and "AI Studio" in msg.answers[0] and "не сохранял" in msg.answers[0] and secrets_guard.saved_free_key() == ""


def test_settings_keyboard_has_no_ai_studio_button_when_closed(closed):
    data = [b.callback_data for row in jarvis_settings_keyboard("ru", voice="Sulafat", call_lang="ru", gai="vertex", gai_limit=3.0).inline_keyboard
            for b in row]
    assert "jarvis:gai:studio" not in data and "jarvis:gai:vertex" in data and "jarvis:glimit:3" in data
    closed.setenv("ALLOW_AI_STUDIO", "1")
    data = [b.callback_data for row in jarvis_settings_keyboard("ru", voice="Sulafat", call_lang="ru", gai="vertex").inline_keyboard for b in row]
    assert "jarvis:gai:studio" in data

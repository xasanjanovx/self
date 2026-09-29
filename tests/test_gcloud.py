"""29.09: Gemini через Vertex AI (кредит $300 Google Cloud) — с переключателем и откатом на AI Studio."""
import asyncio
from datetime import date

import httpx

from bot import billing, gcloud
from bot.keyboards import jarvis_settings_keyboard


def _setup(monkeypatch, tmp_path, handler):  # noqa: ANN001, ANN202
    from bot.context import ai

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VERTEX_API_KEY", "VKEY")
    gcloud.reset_cache()
    recorded: list = []
    monkeypatch.setattr(billing, "record", lambda model, usage, **kw: recorded.append(kw.get("provider", "studio")) or 0.0)
    monkeypatch.setattr(ai, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"x-goog-api-key": "STUDIO"}))
    return ai, recorded


_OK = {"candidates": [{"content": {"parts": [{"text": "ок"}]}}], "usageMetadata": {"promptTokenCount": 3}}


def test_studio_by_default_vertex_when_chosen(monkeypatch, tmp_path):
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("x-goog-api-key")))
        return httpx.Response(200, json=_OK)

    ai, recorded = _setup(monkeypatch, tmp_path, handler)
    asyncio.run(ai._post("gemini-3.5-flash-lite", {"contents": []}))
    assert seen == [("generativelanguage.googleapis.com", "STUDIO")] and recorded == ["studio"]
    gcloud.set_provider("vertex")
    seen.clear()
    recorded.clear()
    asyncio.run(ai._post("gemini-3.5-flash-lite", {"contents": []}))
    assert seen == [("aiplatform.googleapis.com", "VKEY")] and recorded == ["vertex"]
    gcloud.reset_cache()                                   # выбор переживает перезапуск (файл в DATA_DIR)
    assert gcloud.chosen() == "vertex"


def test_blocked_vertex_key_falls_back_and_pauses(monkeypatch, tmp_path):
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "aiplatform.googleapis.com":
            return httpx.Response(403, json={"error": {"code": 403, "status": "PERMISSION_DENIED", "details": [{"reason": "API_KEY_SERVICE_BLOCKED"}],
                                                       "message": "Requests to this API aiplatform.googleapis.com method ... are blocked."}})
        return httpx.Response(200, json=_OK)

    ai, _ = _setup(monkeypatch, tmp_path, handler)
    gcloud.set_provider("vertex")
    assert asyncio.run(ai._post("gemini-3.5-flash-lite", {"contents": []}))["candidates"]
    assert seen == ["aiplatform.googleapis.com", "generativelanguage.googleapis.com"]  # тот же запрос — сразу в AI Studio
    assert "Vertex AI API" in gcloud.status()["error"]
    seen.clear()
    asyncio.run(ai._post("gemini-3.5-flash-lite", {"contents": []}))
    assert seen == ["generativelanguage.googleapis.com"]  # ключ на паузе — лишний круг не тратим


def test_missing_model_goes_to_studio_only_for_that_model(monkeypatch, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "aiplatform.googleapis.com" and "tts" in request.url.path:
            return httpx.Response(404, json={"error": {"code": 404, "message": "Publisher Model was not found"}})
        return httpx.Response(200, json=_OK)

    ai, recorded = _setup(monkeypatch, tmp_path, handler)
    gcloud.set_provider("vertex")
    asyncio.run(ai._post("gemini-3.8-flash-lite-tts", {"contents": []}))
    asyncio.run(ai._post("gemini-3.5-flash-lite", {"contents": []}))
    assert recorded == ["studio", "vertex"] and gcloud.use_vertex("gemini-3.5-flash-lite")
    assert not gcloud.use_vertex("gemini-3.8-flash-lite-tts")


def test_trial_expiry_returns_to_studio(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VERTEX_API_KEY", "VKEY")
    gcloud.reset_cache()
    gcloud.set_provider("vertex")
    assert gcloud.active() and gcloud.daily_limit() == gcloud.VERTEX_DAILY_LIMIT_USD
    monkeypatch.setattr(gcloud, "_today", lambda: date(2027, 1, 5))
    assert not gcloud.active() and gcloud.chosen() == "studio" and gcloud.daily_limit() is None


def test_vertex_spend_does_not_eat_ai_studio_balance(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    st = {"days": {}, "anchor": {"usd": 10.0, "at": "2026-09-29"}, "spent_since": 0.0, "alerts": {}}
    monkeypatch.setattr(billing, "_load", lambda: st)
    monkeypatch.setattr(billing, "_schedule_save", lambda: None)
    monkeypatch.setattr(billing, "breakdown", lambda model, usage: ({p: (0.01 if p == billing.PARTS[0] else 0.0) for p in billing.PARTS},
                                                                    {p: 0 for p in billing.PARTS}))
    billing.record("gemini-3.8-live", {"x": 1}, kind="live", provider="vertex")
    billing.record("gemini-3.8-live", {"x": 1}, kind="live")
    assert round(st["spent_since"], 4) == 0.01 and billing.vertex_spent()["today"] == 0.01


def test_admin_switch_buttons_only_when_passed():
    plain = jarvis_settings_keyboard("ru", voice="Sulafat", call_lang="ru")
    assert not any("jarvis:gai" in (b.callback_data or "") for row in plain.inline_keyboard for b in row)
    kb = jarvis_settings_keyboard("ru", voice="Sulafat", call_lang="ru", gai="vertex", gai_limit=3.0)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "jarvis:gai:studio" in data and "jarvis:gai:vertex" in data and "jarvis:glimit:3" in data

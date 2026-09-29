"""29.09: JES знает, где он сейчас (место при вызове + приходы/уходы), и всё о токенах по моделям."""
import asyncio

from bot import billing, deeds, geo, where


def test_place_from_phone_matches_saved_place_and_is_remembered(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    geo.save_place(1, "работа", 40.7821, 72.3442)
    row = where.update(1, 40.7825, 72.3446, accuracy=20, age_s=30)       # ~55 м от «работы»
    assert row["place"] == "работа" and 30 < row["place_m"] < 80
    d = where.describe(1)
    assert d["known"] and d["fresh"] and d["place"] == "работа" and "maps.google.com" in d["map"]
    assert "ГДЕ ОН СЕЙЧАС: работа" in where.now_line(1)
    assert where.update(1, 40.9, 72.9, age_s=4000)["place"] == "работа"   # более старая точка не затирает свежую
    assert [r["text"] for r in deeds.rows(1)] == ["был: работа"]


def test_zone_arrive_leave_once_each(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    geo.save_place(1, "дом", 40.78, 72.34)
    where.arrived(1, "дом", True)
    where.arrived(1, "дом", True)          # телефон переставил зоны — Android снова «вошёл»: это не новый приход
    assert where.describe(1)["place"] == "дом"
    where.arrived(1, "дом", False)
    where.arrived(1, "дом", False)
    assert [r["text"] for r in deeds.rows(1)] == ["пришёл: дом", "ушёл: дом"]
    assert where.describe(1)["place"] is None and "дом" in where.describe(1)["left"]


def test_unknown_place_asks_for_address_in_background(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    async def reverse(lat, lon):  # noqa: ANN001, ANN202
        return "улица Навои 12, Андижан"

    monkeypatch.setattr(where, "reverse_address", reverse)

    async def run():  # noqa: ANN202
        where.update(1, 40.79, 72.35, accuracy=15)
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert where.describe(1)["address"] == "улица Навои 12, Андижан"
    assert "улица Навои 12" in where.now_line(1)
    assert deeds.rows(1)[-1]["text"] == "был: улица Навои 12, Андижан"


def test_tokens_per_model_and_provider(monkeypatch):
    st = {"days": {}, "alerts": {}}
    monkeypatch.setattr(billing, "_load", lambda: st)
    monkeypatch.setattr(billing, "_schedule_save", lambda: None)
    live = {"promptTokenCount": 1000, "candidatesTokenCount": 300,
            "promptTokensDetails": [{"modality": "AUDIO", "tokenCount": 800}, {"modality": "TEXT", "tokenCount": 200}],
            "candidatesTokensDetails": [{"modality": "AUDIO", "tokenCount": 300}]}
    billing.record("gemini-3.8-live", live, kind="live", provider="vertex")
    billing.record("gemini-3.8-live", live, kind="live")
    billing.record("gemini-3.5-flash-lite", {"promptTokenCount": 5000, "candidatesTokenCount": 50}, kind="agent")
    r = billing.tokens_report("today")
    m = {x["model"]: x for x in r["models"]}
    liv = m["gemini-3.8-live"]
    assert liv["requests"] == 2 and liv["tokens"]["audio_in"] == 1600 and liv["tokens"]["audio_out"] == 600
    assert liv["tokens"]["всего_на_вход"] == 2000 and 0 < liv["via_vertex_usd"] < liv["usd"]
    assert m["gemini-3.5-flash-lite"]["tokens"]["text_in"] == 5000 and m["gemini-3.5-flash-lite"]["via_vertex_usd"] == 0
    assert r["models"][0]["model"] == "gemini-3.8-live"                     # самая дорогая — первой
    assert "живой голос" in " ".join(r["by_purpose"]) or r["by_purpose"]

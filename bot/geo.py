"""Напоминания по месту (29.09, его выбор): «когда приду домой — напомни про хлеб», «когда уйду из офиса — позвонить маме».

Места («дом», «работа», «офис»…) — «Джес, запомни, здесь мой дом» (телефон присылает, где он сейчас) или адресом в чате.
Напоминания хранит сервер; телефон (GeoReminders, приложение 2.13) ставит на каждое «зону» Android (LocationManager
proximity alert, 150 м) и, когда он пришёл/ушёл, показывает уведомление и сообщает сюда — бот пишет в чат.
Сработавшее напоминание — одноразовое. Хранится в DATA_DIR/geo_<uid>.json.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

RADIUS_M = 150
_ALIASES = {"дома": "дом", "домой": "дом", "уй": "дом", "uy": "дом", "uyga": "дом", "home": "дом", "работа": "работа", "работу": "работа",
            "работы": "работа", "офис": "офис", "офиса": "офис", "офисе": "офис", "ish": "работа", "ishga": "работа", "ishxona": "работа"}


def _file(uid: int):  # noqa: ANN202
    from .tg_user import data_dir

    return data_dir() / f"geo_{uid}.json"


def _load(uid: int) -> dict[str, Any]:
    try:
        data = json.loads(_file(uid).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(uid: int, data: dict[str, Any]) -> None:
    data["version"] = time.time()
    _file(uid).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def place_key(name: Any) -> str:
    """«мой дом», «домой», «uyga» → «дом» (кириллица — как он говорит)."""
    import re

    n = re.sub(r"[^\w\s]", " ", str(name or "").lower().replace("ё", "е"))
    words = [w for w in n.split() if w not in {"мой", "моя", "мое", "мою", "в", "на", "из", "с", "к", "до", "mening"}]
    key = " ".join(words) or n
    return _ALIASES.get(key, key)


def save_place(uid: int, name: str, lat: float, lon: float, label: str = "") -> dict[str, Any]:
    data = _load(uid)
    key = place_key(name)
    places = data.setdefault("places", {})
    places[key] = {"name": key, "lat": round(float(lat), 6), "lon": round(float(lon), 6), "label": label[:120], "at": time.time()}
    _save(uid, data)
    logger.info("geo: место «%s» сохранено", key)
    return places[key]


def places(uid: int) -> dict[str, dict[str, Any]]:
    return dict(_load(uid).get("places") or {})


def add_reminder(uid: int, place: str, text: str, when: str = "arrive") -> dict[str, Any]:
    """when: arrive (пришёл) | leave (ушёл). Места ещё нет — {"error", "need_place"}."""
    key = place_key(place)
    data = _load(uid)
    if key not in (data.get("places") or {}):
        return {"error": f"место «{key}» ещё не сохранено", "need_place": key}
    item = {"id": uuid.uuid4().hex[:8], "place": key, "text": " ".join(str(text or "").split())[:200],
            "when": "leave" if when == "leave" else "arrive", "at": time.time()}
    data.setdefault("reminders", []).append(item)
    _save(uid, data)
    return item


def reminders(uid: int) -> list[dict[str, Any]]:
    return list(_load(uid).get("reminders") or [])


def remove_reminder(uid: int, rid: str) -> dict[str, Any] | None:
    data = _load(uid)
    rows = data.get("reminders") or []
    hit = next((r for r in rows if r.get("id") == rid), None)
    if hit:
        data["reminders"] = [r for r in rows if r.get("id") != rid]
        _save(uid, data)
    return hit


TRACK_PREFIX = "place:"


def for_phone(uid: int) -> dict[str, Any]:
    """Что поставить на телефоне: зоны с координатами — напоминания и (29.09) каждое сохранённое место: «пришёл домой 18:40»,
    «ушёл с работы» — в память дел и «где он сейчас» (bot/where.py). Зону ставит Android — батарея почти не тратится."""
    data = _load(uid)
    pl = data.get("places") or {}
    zones = [{"id": TRACK_PREFIX + key, "lat": p["lat"], "lon": p["lon"], "radius": RADIUS_M, "when": "track", "text": "", "place": key}
             for key, p in pl.items()]
    for r in data.get("reminders") or []:
        p = pl.get(r.get("place"))
        if p:
            zones.append({"id": r["id"], "lat": p["lat"], "lon": p["lon"], "radius": RADIUS_M, "when": r["when"], "text": r["text"],
                          "place": r["place"]})
    return {"version": data.get("version") or 0, "zones": zones}


def fired(uid: int, rid: str, entering: bool) -> dict[str, Any] | None:
    """Телефон: он вошёл/вышел из зоны. Совпало с «пришёл/ушёл» — напоминание сработало (и удаляется)."""
    hit = next((r for r in reminders(uid) if r.get("id") == rid), None)
    if hit is None or (hit["when"] == "arrive") != bool(entering):
        return None
    remove_reminder(uid, rid)
    logger.info("geo: сработало «%s» (%s %s)", hit["text"][:60], "пришёл" if entering else "ушёл", hit["place"])
    return hit


__all__ = ["save_place", "places", "add_reminder", "reminders", "remove_reminder", "for_phone", "fired", "place_key", "RADIUS_M",
           "TRACK_PREFIX"]

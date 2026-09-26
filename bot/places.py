"""Где живёт человек — для будильника на фаджр: время намаза и часовой пояс (26.09.2026).

Раньше фаджр считался для всех по Андижану и по ташкентскому времени — клиенту из другого города (тем более страны)
будильник звонил бы не вовремя. Теперь при включении будильника человек один раз присылает геолокацию (кнопка
Telegram) или выбирает город. По координатам — время намаза (Aladhan) и часовой пояс (он же — в ответе Aladhan);
пояс сохраняется в профиль, и напоминания/итоги дня тоже идут по его времени. Подпись места — DATA_DIR/places.json.
"""
from __future__ import annotations

import json
import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

# key, ru, uz, широта, долгота — все в Узбекистане (Asia/Tashkent)
CITIES: list[tuple[str, str, str, float, float]] = [
    ("tashkent", "Ташкент", "Toshkent", 41.2995, 69.2401),
    ("andijan", "Андижан", "Andijon", 40.7821, 72.3442),
    ("namangan", "Наманган", "Namangan", 40.9983, 71.6726),
    ("fergana", "Фергана", "Farg'ona", 40.3894, 71.7874),
    ("kokand", "Коканд", "Qo'qon", 40.5286, 70.9425),
    ("samarkand", "Самарканд", "Samarqand", 39.6542, 66.9597),
    ("bukhara", "Бухара", "Buxoro", 39.7747, 64.4286),
    ("navoi", "Навои", "Navoiy", 40.0844, 65.3792),
    ("karshi", "Карши", "Qarshi", 38.8606, 65.7891),
    ("termez", "Термез", "Termiz", 37.2242, 67.2783),
    ("jizzakh", "Джизак", "Jizzax", 40.1158, 67.8422),
    ("gulistan", "Гулистан", "Guliston", 40.4897, 68.7842),
    ("urgench", "Ургенч", "Urganch", 41.5500, 60.6333),
    ("nukus", "Нукус", "Nukus", 42.4531, 59.6103),
]
CITY_TZ = "Asia/Tashkent"


def city(key: str) -> tuple[str, str, str, float, float] | None:
    return next((c for c in CITIES if c[0] == key), None)


def nearest_city(lat: float, lon: float, *, within_km: float = 40.0) -> tuple[str, str, str, float, float] | None:
    """Ближайший город из списка, если он рядом (подпись «Андижан» вместо координат)."""
    def km(c: tuple[str, str, str, float, float]) -> float:
        dlat, dlon = math.radians(c[3] - lat), math.radians(c[4] - lon)
        h = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat)) * math.cos(math.radians(c[3])) * math.sin(dlon / 2) ** 2
        return 6371 * 2 * math.asin(math.sqrt(h))

    best = min(CITIES, key=km)
    return best if km(best) <= within_km else None


def _file():
    from .tg_user import data_dir

    return data_dir() / "places.json"


def _load() -> dict[str, Any]:
    try:
        return json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def get(uid: int) -> dict[str, Any] | None:
    return _load().get(str(uid))


def has_place(uid: int) -> bool:
    """Место известно. Владелец живёт в Андижане — для него это место по умолчанию (как было)."""
    from . import access

    return get(uid) is not None or access.is_owner(uid)


def label(uid: int, lang: str) -> str:
    place = get(uid)
    if place:
        return str(place.get("uz" if lang == "uz" else "ru") or place.get("ru") or "")
    return "Andijon" if lang == "uz" else "Андижан"


async def set_place(uid: int, lat: float, lon: float, *, ru: str, uz: str, tz: str | None = None) -> dict[str, Any]:
    """Сохранить место: координаты — в настройки будильника, часовой пояс — в профиль. {"tz", "ru", "uz"}."""
    from . import prayer, services
    from .context import db

    tz = tz or await prayer.timezone_at(lat, lon) or CITY_TZ
    await services.save_wake_settings(uid, {"latitude": round(lat, 4), "longitude": round(lon, 4)})
    try:
        await db.update_user_timezone(uid, tz)
    except Exception:
        logger.warning("places: часовой пояс не сохранён", exc_info=True)
    data = _load()
    data[str(uid)] = {"lat": round(lat, 4), "lon": round(lon, 4), "tz": tz, "ru": ru, "uz": uz}
    try:
        _file().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.warning("places: не сохранил", exc_info=True)
    services.forget_profile(uid)
    return {"tz": tz, "ru": ru, "uz": uz}


__all__ = ["CITIES", "city", "nearest_city", "get", "has_place", "label", "set_place"]

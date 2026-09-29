"""Где он сейчас (29.09, его просьба «чтобы JES знал, где я нахожусь именно в этот момент»).

Его выбор: телефон сообщает место, только когда он зовёт JES (без слежки в фоне), + история приходов и уходов:
  • в начале каждого разговора с JES телефон присылает, где он (последняя известная точка сразу, свежая — через 1–3 с);
  • на каждое сохранённое место («дом», «работа»…) телефон ставит зону Android (bot/geo.py, как напоминания по месту) —
    пришёл/ушёл попадает в память дел («пришёл домой 18:40») и сюда.
Адрес — OpenStreetMap (бесплатно, кэш по ~50 м). Хранится в DATA_DIR/where_<uid>.json, на его сервере.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PLACE_EXTRA_M = 150          # «дома» — в пределах 150 м от сохранённого места (или точности телефона, если она хуже)
ADDRESS_REUSE_M = 60         # адрес той же точки не спрашиваем заново
STALE_S = 6 * 3600           # старше 6 часов — «последнее известное место», а не «сейчас»
_resolving: set[int] = set()


def _file(uid: int) -> Path | None:
    folder = os.getenv("DATA_DIR")
    return Path(folder) / f"where_{int(uid)}.json" if folder else None


def _load(uid: int) -> dict[str, Any]:
    path = _file(uid)
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path is not None and path.exists() else {}
    except (OSError, ValueError):
        return {}


def _save(uid: int, data: dict[str, Any]) -> None:
    path = _file(uid)
    if path is None:
        return
    try:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.warning("where: не сохранил", exc_info=True)


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_place(uid: int, lat: float, lon: float, accuracy: float = 0) -> tuple[str | None, float | None]:
    """Сохранённое место, в котором он сейчас («дом», «работа»), и расстояние до него."""
    from . import geo

    best, best_d = None, None
    for key, p in geo.places(uid).items():
        d = distance_m(lat, lon, float(p["lat"]), float(p["lon"]))
        if d <= max(PLACE_EXTRA_M, min(float(accuracy or 0), 500)) and (best_d is None or d < best_d):
            best, best_d = key, d
    return best, best_d


def update(uid: int | None, lat: Any, lon: Any, *, accuracy: Any = 0, age_s: Any = 0, source: str = "телефон") -> dict[str, Any] | None:
    """Новая точка с телефона. Старее уже известной — не трогаем. Сменилось место — в память дел; адрес — фоном."""
    if not uid:
        return None
    try:
        lat, lon = float(lat), float(lon)
        accuracy, age_s = float(accuracy or 0), max(0.0, float(age_s or 0))
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    data = _load(uid)
    at = time.time() - age_s
    if float(data.get("at") or 0) > at + 1:
        return data  # у нас уже свежее
    place, dist = nearest_place(uid, lat, lon, accuracy)
    same_point = data.get("lat") is not None and distance_m(lat, lon, float(data["lat"]), float(data["lon"])) <= ADDRESS_REUSE_M
    prev_place = data.get("place")
    data.update({"lat": round(lat, 6), "lon": round(lon, 6), "accuracy": round(accuracy), "at": at, "place": place,
                 "place_m": round(dist) if dist is not None else None, "source": source})
    if not same_point:
        data.pop("address", None)
    _save(uid, data)
    if place != prev_place or not same_point:
        _note_place(uid, data)
    if not data.get("address"):
        _resolve_soon(uid, lat, lon)
    return data


def _note_place(uid: int, data: dict[str, Any]) -> None:
    """В память дел: где он был, когда звал JES (одно и то же место — не чаще раза в 2 часа)."""
    from . import deeds

    where = data.get("place") or data.get("address")
    if where:
        deeds.note(uid, "place", src="телефон", text=f"был: {where}", dedupe_s=2 * 3600)


def _resolve_soon(uid: int, lat: float, lon: float) -> None:
    if uid in _resolving:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _resolving.add(uid)
    task = loop.create_task(_resolve(uid, lat, lon), name="where-address")
    task.add_done_callback(lambda _t: _resolving.discard(uid))


async def reverse_address(lat: float, lon: float) -> str | None:
    """Точка → «улица Навои 12, Андижан» (OpenStreetMap Nominatim, бесплатно)."""
    import httpx

    params = {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 18, "accept-language": "ru,uz", "addressdetails": 1}
    try:
        async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "JarvisSelfBot/1.5 (personal assistant)"}) as http:
            res = await http.get("https://nominatim.openstreetmap.org/reverse", params=params)
        if res.status_code != 200:
            return None
        body = res.json()
    except Exception:
        logger.warning("where: адрес не определил", exc_info=True)
        return None
    a = body.get("address") or {}
    street = " ".join(x for x in (a.get("road") or a.get("pedestrian") or "", a.get("house_number") or "") if x).strip()
    area = a.get("neighbourhood") or a.get("suburb") or a.get("quarter") or ""
    city = a.get("city") or a.get("town") or a.get("village") or a.get("county") or ""
    name = body.get("name") or ""
    parts = [p for p in (name if name and name not in street else "", street, area, city) if p]
    text = ", ".join(dict.fromkeys(parts))
    return text[:140] or (str(body.get("display_name") or "")[:140] or None)


async def _resolve(uid: int, lat: float, lon: float) -> None:
    address = await reverse_address(lat, lon)
    if not address:
        return
    data = _load(uid)
    if data.get("lat") is not None and distance_m(lat, lon, float(data["lat"]), float(data["lon"])) <= ADDRESS_REUSE_M:
        data["address"] = address
        _save(uid, data)
        if not data.get("place"):
            _note_place(uid, data)


def from_device(uid: int | None, device: dict[str, Any] | None) -> None:
    """В hello/device с телефона может быть «location»: {"lat", "lon", "acc", "age_s"} (приложение 2.17)."""
    loc = (device or {}).get("location")
    if isinstance(loc, dict) and loc.get("lat") is not None:
        update(uid, loc.get("lat"), loc.get("lon"), accuracy=loc.get("acc"), age_s=loc.get("age_s"))


def arrived(uid: int, place: str, entering: bool) -> None:
    """Зона сохранённого места: пришёл / ушёл (телефон, bot/geo.py)."""
    from . import deeds, geo

    p = geo.places(uid).get(place)
    data = _load(uid)
    inside = set(data.get("zones_in") or [])
    # телефон переставляет зоны (при включении экрана, раз в 3 ч) — Android снова говорит «вошёл», пока он внутри: это не приход
    if entering == (place in inside):
        return
    inside = inside | {place} if entering else inside - {place}
    data["zones_in"] = sorted(inside)
    deeds.note(uid, "arrive" if entering else "leave", src="телефон", dedupe_s=600,
               text=("пришёл: " if entering else "ушёл: ") + place)
    if entering and p:
        data.update({"lat": p["lat"], "lon": p["lon"], "accuracy": 150, "at": time.time(), "place": place, "place_m": 0,
                     "source": "зона места", "address": p.get("label") or data.get("address")})
    elif not entering and data.get("place") == place:
        data.update({"place": None, "left": place, "left_at": time.time()})
    _save(uid, data)


def _ago(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 1:
        return "только что"
    if m < 60:
        return f"{m} мин назад"
    h = m // 60
    return f"{h} ч {m % 60} мин назад" if h < 24 else f"{h // 24} дн. назад"


def describe(uid: int) -> dict[str, Any]:
    """Для инструмента where_am_i и промпта."""
    data = _load(uid)
    if data.get("lat") is None:
        return {"known": False, "note": "телефон ещё не присылал место — он появится, когда он позовёт JES (приложение 2.17+)"}
    age = time.time() - float(data.get("at") or 0)
    out = {"known": True, "fresh": age <= STALE_S, "place": data.get("place"), "address": data.get("address"),
           "lat": data["lat"], "lon": data["lon"], "accuracy_m": data.get("accuracy"), "when": _ago(age),
           "map": f"https://maps.google.com/?q={data['lat']},{data['lon']}"}
    if data.get("place") and data.get("place_m") is not None:
        out["from_place_m"] = data["place_m"]
    if data.get("left") and not data.get("place"):
        out["left"] = f"{data['left']} — {_ago(time.time() - float(data.get('left_at') or 0))}"
    return out


def now_line(uid: int) -> str:
    """Строка в инструкцию разговора: «ГДЕ ОН: дома (ул. …, Андижан) — 2 мин назад»."""
    d = describe(uid)
    if not d.get("known"):
        return ""
    where = d.get("place") or ""
    if d.get("address"):
        where = f"{where} ({d['address']})" if where else d["address"]
    if not where:
        where = f"{d['lat']:.5f}, {d['lon']:.5f}"
    head = "ГДЕ ОН СЕЙЧАС" if d.get("fresh") else "ПОСЛЕДНЕЕ ИЗВЕСТНОЕ МЕСТО"
    return f"\n{head}: {where} — {d['when']}. Спросит «где я?» — отвечай этим (точнее — where_am_i).\n"


__all__ = ["update", "describe", "now_line", "from_device", "arrived", "nearest_place", "reverse_address", "distance_m"]

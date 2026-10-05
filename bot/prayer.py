"""Времена намаза (Андижан по умолчанию) — Aladhan API с дневным кэшем.

Азан берём по координатам, такбир (начало джамоата) считаем как «азан + поправка»:
у каждой мечети она своя, пользователь правит её словами («такбир в 5:20» → поправка
пересчитается). Если API недоступен — отдаём вчерашний/последний удачный ответ,
поэтому подъём не срывается из-за сети.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API = "https://api.aladhan.com/v1/timings"
ANDIJAN = (40.7821, 72.3442)
# 05.10.2026 его выбор («на боте и на часах»): Аср по ханафитскому мазхабу (школа Aladhan = 1; тень = 2 длины предмета) — как делают в Узбекистане.
# Часы (jes-face/shared/prayer.js) считают так же без сети. Меняется одной константой.
ASR_SCHOOL = 1
NAMES_RU = {"Fajr": "Бомдод", "Sunrise": "Восход", "Dhuhr": "Пешин", "Asr": "Аср", "Maghrib": "Шом", "Isha": "Хуфтон"}
NAMES_UZ = {"Fajr": "Bomdod", "Sunrise": "Quyosh", "Dhuhr": "Peshin", "Asr": "Asr", "Maghrib": "Shom", "Isha": "Xufton"}
ORDER = ("Fajr", "Sunrise", "Dhuhr", "Asr", "Maghrib", "Isha")

_cache: dict[tuple[str, float, float, int, int], dict[str, str]] = {}
_lock = asyncio.Lock()


def parse_hhmm(value: str | None) -> time | None:
    try:
        hh, mm = (int(x) for x in str(value or "").strip()[:5].split(":"))
        return time(hour=hh % 24, minute=mm % 60)
    except (ValueError, TypeError):
        return None


async def timings(day: date, *, latitude: float = ANDIJAN[0], longitude: float = ANDIJAN[1], method: int = 3) -> dict[str, str]:
    """{'Fajr': '04:27', …} на указанный день. Пустой словарь — если не смогли получить."""
    key = (day.isoformat(), round(latitude, 4), round(longitude, 4), int(method), ASR_SCHOOL)
    if key in _cache:
        return _cache[key]
    async with _lock:
        if key in _cache:
            return _cache[key]
        url = f"{API}/{day:%d-%m-%Y}"
        params = {"latitude": latitude, "longitude": longitude, "method": method, "school": ASR_SCHOOL}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                res = await client.get(url, params=params)
                res.raise_for_status()
                raw = res.json()["data"]["timings"]
        except Exception:
            logger.warning("prayer timings failed for %s", day, exc_info=True)
            return {}
        out = {k: str(v)[:5] for k, v in raw.items() if k in ORDER}
        _cache[key] = out
        if len(_cache) > 120:  # не растём бесконечно: держим последние ~4 месяца одного города
            for old in list(_cache)[:40]:
                _cache.pop(old, None)
        return out


async def timezone_at(latitude: float, longitude: float) -> str | None:
    """Часовой пояс места («Asia/Tashkent», «Europe/Moscow») — Aladhan отдаёт его вместе со временем намаза."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.get(f"{API}/{date.today():%d-%m-%Y}", params={"latitude": latitude, "longitude": longitude, "method": 3})
            res.raise_for_status()
            tz = str(((res.json().get("data") or {}).get("meta") or {}).get("timezone") or "")
            return tz or None
    except Exception:
        logger.warning("prayer: часовой пояс не получил", exc_info=True)
        return None


OVERPASS = ("https://overpass.kumi.systems/api/interpreter", "https://overpass-api.de/api/interpreter",
            "https://overpass.private.coffee/api/interpreter")
_mosque_cache: dict[tuple[float, float, int], tuple[float, list[dict[str, Any]]]] = {}
MOSQUE_TTL_S = 7 * 86400


async def _overpass(query: str) -> list[dict[str, Any]] | None:
    """Запрос ко ВСЕМ зеркалам Overpass сразу — берём первый ответивший (30.09: одно зеркало зависало на 25 с). None — никто."""
    async def one(url: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "JarvisSelfBot/1.5 (personal assistant)", "Accept": "*/*"}) as client:
            res = await client.post(url, data={"data": query})
        if res.status_code != 200:
            raise RuntimeError(f"HTTP {res.status_code}")
        return res.json().get("elements") or []

    tasks = {asyncio.create_task(one(url)): url for url in OVERPASS}
    try:
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception() is None:
                    return t.result()
                logger.info("mosques: %s не ответил (%s)", tasks[t][8:32], type(t.exception()).__name__)
        return None
    finally:
        for t in tasks:
            t.cancel()


async def mosques_near(latitude: float, longitude: float, *, radius: int = 3000, limit: int = 5) -> list[dict[str, Any]]:
    """Мечети рядом (30.09, «мечети рядом со мной»): [{"name", "distance_m", "lat", "lon", "map"}] от ближней. Карта
    OpenStreetMap: безымянные — «Мечеть». Нашли мало — расширяем до 3× радиуса (не больше 10 км). Кэш на неделю."""
    import math
    import time as _t

    key = (round(latitude, 3), round(longitude, 3), int(radius))
    hit = _mosque_cache.get(key)
    if hit and _t.time() - hit[0] < MOSQUE_TTL_S:
        return hit[1][:limit]

    def dist(la: float, lo: float) -> float:
        p1, p2 = math.radians(latitude), math.radians(la)
        a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo - longitude) / 2) ** 2
        return 2 * 6371000.0 * math.asin(math.sqrt(a))

    out: list[dict[str, Any]] = []
    for r in (radius, min(radius * 3, 10000)):
        query = (f'[out:json][timeout:20];(nwr["amenity"="place_of_worship"]["religion"="muslim"](around:{r},{latitude},{longitude}););'
                 "out center 60;")
        elements = await _overpass(query)
        if elements is None:
            return []
        seen: set[tuple[str, int, int]] = set()
        out = []
        for e in elements:
            la = e.get("lat") if e.get("lat") is not None else (e.get("center") or {}).get("lat")
            lo = e.get("lon") if e.get("lon") is not None else (e.get("center") or {}).get("lon")
            if la is None or lo is None:
                continue
            tags = e.get("tags") or {}
            name = str(tags.get("name:ru") or tags.get("name") or tags.get("name:uz") or tags.get("name:en") or "Мечеть").strip()
            cell = (name.lower(), round(la * 500), round(lo * 500))  # одна мечеть могла попасть и точкой, и контуром
            if cell in seen:
                continue
            seen.add(cell)
            out.append({"name": name[:70], "distance_m": int(dist(la, lo)), "lat": round(la, 6), "lon": round(lo, 6),
                        "map": f"https://maps.google.com/?q={la},{lo}"})
        out.sort(key=lambda m: m["distance_m"])
        if len(out) >= 3 or r >= 10000:
            break
    if out:
        _mosque_cache[key] = (_t.time(), out)
        if len(_mosque_cache) > 60:
            _mosque_cache.pop(next(iter(_mosque_cache)))
    return out[:limit]


def takbir_time(fajr: str | None, takbir_offset_min: int) -> time | None:
    """Начало джамоата = азан фаджра + поправка мечети."""
    t = parse_hhmm(fajr)
    if t is None:
        return None
    return (datetime.combine(date(2000, 1, 1), t) + timedelta(minutes=max(0, int(takbir_offset_min)))).time()


def offset_from_takbir(fajr: str | None, takbir: str | None) -> int | None:
    """Пользователь сказал «такбир в 5:20» → сколько это минут после азана."""
    a, b = parse_hhmm(fajr), parse_hhmm(takbir)
    if a is None or b is None:
        return None
    delta = (datetime.combine(date(2000, 1, 1), b) - datetime.combine(date(2000, 1, 1), a)).total_seconds() / 60
    if not (0 <= delta <= 120):
        return None
    return int(round(delta))


def summary(rows: dict[str, str], lang: str = "ru", *, takbir: str | None = None) -> str:
    """Строка «Бомдод 04:27 (такбир 04:47) · Пешин 12:03 · …» для сводки."""
    if not rows:
        return ""
    names = NAMES_UZ if lang == "uz" else NAMES_RU
    parts = []
    for key in ORDER:
        if key not in rows:
            continue
        label = f"{names[key]} {rows[key]}"
        if key == "Fajr" and takbir:
            label += f" ({'takbir' if lang == 'uz' else 'такбир'} {takbir})"
        parts.append(label)
    return " · ".join(parts)


def next_prayer(rows: dict[str, str], now: datetime, lang: str = "ru") -> tuple[str, str, int] | None:
    """(название, 'HH:MM', минут до него) — ближайший намаз сегодня."""
    names = NAMES_UZ if lang == "uz" else NAMES_RU
    best: tuple[str, str, int] | None = None
    for key in ORDER:
        t = parse_hhmm(rows.get(key))
        if t is None or key == "Sunrise":
            continue
        when = datetime.combine(now.date(), t, tzinfo=now.tzinfo)
        left = int((when - now).total_seconds() // 60)
        if left >= 0 and (best is None or left < best[2]):
            best = (names[key], rows[key], left)
    return best


__all__ = ["timings", "takbir_time", "offset_from_takbir", "summary", "next_prayer", "parse_hhmm", "ANDIJAN", "ORDER"]

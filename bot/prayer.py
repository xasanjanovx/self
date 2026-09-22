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

import httpx

logger = logging.getLogger(__name__)

API = "https://api.aladhan.com/v1/timings"
ANDIJAN = (40.7821, 72.3442)
NAMES_RU = {"Fajr": "Бомдод", "Sunrise": "Восход", "Dhuhr": "Пешин", "Asr": "Аср", "Maghrib": "Шом", "Isha": "Хуфтон"}
NAMES_UZ = {"Fajr": "Bomdod", "Sunrise": "Quyosh", "Dhuhr": "Peshin", "Asr": "Asr", "Maghrib": "Shom", "Isha": "Xufton"}
ORDER = ("Fajr", "Sunrise", "Dhuhr", "Asr", "Maghrib", "Isha")

_cache: dict[tuple[str, float, float, int], dict[str, str]] = {}
_lock = asyncio.Lock()


def parse_hhmm(value: str | None) -> time | None:
    try:
        hh, mm = (int(x) for x in str(value or "").strip()[:5].split(":"))
        return time(hour=hh % 24, minute=mm % 60)
    except (ValueError, TypeError):
        return None


async def timings(day: date, *, latitude: float = ANDIJAN[0], longitude: float = ANDIJAN[1], method: int = 3) -> dict[str, str]:
    """{'Fajr': '04:27', …} на указанный день. Пустой словарь — если не смогли получить."""
    key = (day.isoformat(), round(latitude, 4), round(longitude, 4), int(method))
    if key in _cache:
        return _cache[key]
    async with _lock:
        if key in _cache:
            return _cache[key]
        url = f"{API}/{day:%d-%m-%Y}"
        params = {"latitude": latitude, "longitude": longitude, "method": method, "school": 0}
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

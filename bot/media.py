"""Внешние поиски для голосового Джарвиса без платных ключей: видео/музыка на YouTube и адрес для такси.

YouTube — страница результатов поиска (как в браузере), из неё берём id видео: телефон откроет его сразу.
Адрес — OpenStreetMap Nominatim рядом с ним (по умолчанию Андижан): координаты для Яндекс Go.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger(__name__)

# настольный браузер: мобильная версия отдаёт данные JS-строкой с \xNN вместо JSON
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
_INITIAL = re.compile(r"var ytInitialData\s*=\s*(\{.*?\});</script>", re.S)
_INITIAL_STR = re.compile(r"var ytInitialData\s*=\s*'(.*?)';</script>", re.S)
_HEX = re.compile(r"\\x([0-9a-fA-F]{2})")


def _walk(node: Any, out: list[dict[str, Any]], limit: int) -> None:
    if len(out) >= limit:
        return
    if isinstance(node, dict):
        vr = node.get("videoRenderer")
        if isinstance(vr, dict) and vr.get("videoId"):
            title = "".join(r.get("text", "") for r in (vr.get("title") or {}).get("runs") or [])
            channel = "".join(r.get("text", "") for r in (vr.get("ownerText") or {}).get("runs") or [])
            out.append({"id": vr["videoId"], "title": title[:120], "channel": channel[:60],
                        "duration": (vr.get("lengthText") or {}).get("simpleText", ""), "url": f"https://youtu.be/{vr['videoId']}"})
            return
        for v in node.values():
            _walk(v, out, limit)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out, limit)


def parse_youtube(html: str, limit: int = 5) -> list[dict[str, Any]]:
    m = _INITIAL.search(html)
    raw = m.group(1) if m else None
    if raw is None and (m2 := _INITIAL_STR.search(html)):
        raw = _HEX.sub(lambda h: chr(int(h.group(1), 16)), m2.group(1)).replace("\\\\", "\\")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    out: list[dict[str, Any]] = []
    _walk(data, out, limit)
    return out


async def youtube_search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}&hl=ru"
    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": _UA, "Accept-Language": "ru,uz;q=0.8,en;q=0.6"},
                                     follow_redirects=True, cookies={"CONSENT": "YES+"}) as http:
            res = await http.get(url)
            res.raise_for_status()
    except Exception:
        logger.warning("youtube search failed", exc_info=True)
        return []
    return parse_youtube(res.text, limit)


# центр Андижана — если телефон не прислал, где он
DEFAULT_NEAR = (40.7821, 72.3442)


async def geocode(query: str, near: tuple[float, float] | None = None) -> dict[str, Any] | None:
    """Адрес/место → {"lat", "lon", "name"} поблизости (≈60 км вокруг него)."""
    lat, lon = near or DEFAULT_NEAR
    box = f"{lon - 0.6},{lat + 0.45},{lon + 0.6},{lat - 0.45}"
    params = {"q": query, "format": "jsonv2", "limit": 1, "accept-language": "ru,uz", "viewbox": box, "bounded": 1}
    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "JarvisSelfBot/1.5 (personal assistant)"}) as http:
            res = await http.get("https://nominatim.openstreetmap.org/search", params=params)
            rows = res.json() if res.status_code == 200 else []
            if not rows:  # не нашли рядом — по всей стране
                params.pop("bounded")
                params["countrycodes"] = "uz"
                res = await http.get("https://nominatim.openstreetmap.org/search", params=params)
                rows = res.json() if res.status_code == 200 else []
    except Exception:
        logger.warning("geocode failed", exc_info=True)
        return None
    if not rows:
        return None
    r = rows[0]
    return {"lat": float(r["lat"]), "lon": float(r["lon"]), "name": str(r.get("display_name") or query).split(",")[0][:80]}


__all__ = ["youtube_search", "parse_youtube", "geocode", "DEFAULT_NEAR"]

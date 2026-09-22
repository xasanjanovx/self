"""Аят и хадис дня на узбекском.

Аят — готовый published-перевод Муҳаммад Содиқ Муҳаммад Юсуфа (издание
`uzb-muhammadsodikmu` в открытом Quran API), поэтому это не машинный перевод;
кириллицу переводим в латиницу сами. Хадис — арабский оригинал + русский перевод
из сборников Бухари/Муслим/Абу Дауд (открытый hadith API) с номером хадиса;
узбекский текст хадиса помечается как машинный перевод — врать об источнике нельзя.

Выбор — детерминированный по дате: один и тот же день → один и тот же аят/хадис.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import httpx

logger = logging.getLogger(__name__)

QURAN_CDN = "https://cdn.jsdelivr.net/gh/fawazahmed0/quran-api@1/editions"
HADITH_CDN = "https://cdn.jsdelivr.net/gh/fawazahmed0/hadith-api@1/editions"
UZ_EDITION = "uzb-muhammadsodikmu"
AR_EDITION = "ara-quransimple"

# Короткие, ободряющие аяты — то, что уместно слушать в 4 утра.
VERSES: tuple[tuple[int, int], ...] = (
    (2, 152), (2, 153), (2, 186), (2, 255), (2, 286), (3, 139), (3, 159), (3, 200),
    (4, 103), (5, 35), (6, 162), (7, 205), (8, 46), (9, 40), (11, 114), (13, 28),
    (14, 7), (16, 97), (17, 78), (17, 79), (18, 10), (20, 14), (20, 130), (23, 1),
    (24, 35), (25, 74), (29, 45), (29, 69), (31, 17), (39, 53), (40, 60), (41, 33),
    (42, 43), (46, 13), (47, 7), (50, 39), (51, 56), (55, 13), (57, 16), (59, 18),
    (64, 11), (65, 2), (65, 3), (73, 20), (76, 25), (87, 14), (91, 9), (93, 5),
    (94, 5), (94, 6), (103, 1), (103, 2), (103, 3),
)

HADITH_BOOKS: tuple[tuple[str, str, int], ...] = (
    # (книга, название для ссылки, сколько хадисов брать из начала — там самые известные)
    ("bukhari", "Бухорий", 300),
    ("muslim", "Муслим", 300),
    ("abudawud", "Абу Довуд", 300),
)

_CYR2LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "x", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "ʼ", "ы": "i", "ь": "",
    "э": "e", "ю": "yu", "я": "ya", "ғ": "g'", "қ": "q", "ҳ": "h", "ў": "o'", "ц": "ts",
}
_cache: dict[str, Any] = {}


def cyr_to_lat(text: str) -> str:
    """Узбекская кириллица → латиница (для текста аята: пользователь читает на латинице)."""
    out: list[str] = []
    for ch in str(text or ""):
        low = ch.lower()
        rep = _CYR2LAT.get(low)
        if rep is None:
            out.append(ch)
            continue
        if ch.isupper():
            rep = rep[:1].upper() + rep[1:]
        out.append(rep)
    return "".join(out)


def _pick(day: date, size: int) -> int:
    return day.toordinal() % max(1, size)


async def _get_json(url: str) -> Any | None:
    if url in _cache:
        return _cache[url]
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            res = await client.get(url)
            res.raise_for_status()
            data = res.json()
    except Exception:
        logger.warning("daily fetch failed: %s", url, exc_info=True)
        return None
    _cache[url] = data
    if len(_cache) > 60:
        for old in list(_cache)[:20]:
            _cache.pop(old, None)
    return data


async def verse_of_day(day: date) -> dict[str, Any] | None:
    """{'ref': '2:255', 'arabic': …, 'uz': …} — аят дня в переводе Муҳаммад Содиқ Муҳаммад Юсуф."""
    chapter, verse = VERSES[_pick(day, len(VERSES))]
    uz = await _get_json(f"{QURAN_CDN}/{UZ_EDITION}/{chapter}/{verse}.json")
    if not uz or not uz.get("text"):
        return None
    ar = await _get_json(f"{QURAN_CDN}/{AR_EDITION}/{chapter}/{verse}.json")
    return {
        "ref": f"{chapter}:{verse}",
        "arabic": (ar or {}).get("text"),
        "uz": cyr_to_lat(str(uz["text"]).strip()),
        "source": "Muhammad Sodiq Muhammad Yusuf tarjimasi",
    }


async def hadith_of_day(day: date) -> dict[str, Any] | None:
    """{'ref': 'Бухорий 1', 'arabic': …, 'ru': …} — хадис дня (узбекский перевод делает Джарвис)."""
    book, label, limit = HADITH_BOOKS[day.toordinal() % len(HADITH_BOOKS)]
    rus = await _get_json(f"{HADITH_CDN}/rus-{book}.min.json")
    if not rus:
        return None
    items = [h for h in (rus.get("hadiths") or [])[:limit] if 60 <= len(str(h.get("text") or "")) <= 700]
    if not items:
        return None
    item = items[_pick(day, len(items))]
    number = item.get("hadithnumber")
    ara = await _get_json(f"{HADITH_CDN}/ara-{book}.min.json")
    arabic = None
    if ara:
        arabic = next((str(h.get("text")) for h in (ara.get("hadiths") or []) if h.get("hadithnumber") == number), None)
    return {"ref": f"{label} {number}", "book": book, "number": number, "arabic": arabic, "ru": str(item.get("text")).strip()}


def verse_block(verse: dict[str, Any] | None, lang: str = "uz") -> str | None:
    """Готовый текст аята для утреннего сообщения."""
    if not verse:
        return None
    head = "📖 <b>Kun oyati</b>" if lang == "uz" else "📖 <b>Аят дня</b>"
    lines = [head]
    if verse.get("arabic"):
        lines.append(f"<i>{verse['arabic']}</i>")
    lines.append(verse["uz"])
    lines.append(f"<i>Qur'on {verse['ref']} · {verse['source']}</i>")
    return "\n".join(lines)


def speakable_verse(verse: dict[str, Any] | None) -> str:
    """Текст для озвучки в звонке — только узбекский перевод, без арабского."""
    if not verse:
        return ""
    return f"{verse['uz']} Qur'on, {verse['ref']}."


__all__ = ["verse_of_day", "hadith_of_day", "verse_block", "speakable_verse", "cyr_to_lat", "VERSES"]

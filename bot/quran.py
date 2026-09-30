"""Суры и аяты с транслитерацией на латинице (30.09, его просьба: «если есть, что учить, — суры из Корана — пришли в чат с
транслитерацией на латинском»).

Текст НЕ пишет модель — он берётся дословно из открытого API (api.alquran.cloud): арабский (османи) и транслитерация на латинице
(en.transliteration); скачивается один раз на суру и лежит на диске. Модель только определяет, какая сура и какие аяты имелись
в виду («выучить Ихлас», «аль-Каср», «первые 5 аятов Бакары»), а в шапке сообщения всегда стоит номер и название суры — чтобы
он мог убедиться, что это та.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API = "https://api.alquran.cloud/v1/surah/{n}/editions/quran-uthmani,en.transliteration"
_MAX_AYAHS_PER_MESSAGE = 12
_MAX_MESSAGES = 3
# «по смыслу это про Коран»: задача/цель/дело, для которого стоит прислать текст
LEARN_RE = re.compile(r"(сур[аыуе]\b|сурой|суру|sura\b|surah|surasi|suras|аят|ayat|коран|куръон|qur'?an|хифз|hifz|зауч|yodlash|yodla)", re.IGNORECASE)


def _dir() -> Path | None:
    folder = os.getenv("DATA_DIR")
    return Path(folder) / "quran" if folder else None


async def surah(n: int) -> dict[str, Any] | None:
    """{"number", "name", "arabic_name", "ayahs": [{"n", "ar", "lat"}]} — из кэша или API. None — не получилось."""
    if not 1 <= int(n) <= 114:
        return None
    folder = _dir()
    path = folder / f"{int(n)}.json" if folder else None
    if path is not None and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "JarvisSelfBot/1.5"}) as client:
            res = await client.get(API.format(n=int(n)))
        res.raise_for_status()
        editions = res.json()["data"]
        arabic = next(e for e in editions if e["edition"]["identifier"] == "quran-uthmani")
        latin = next(e for e in editions if e["edition"]["identifier"] == "en.transliteration")
    except Exception:
        logger.warning("quran: сура %s не получена", n, exc_info=True)
        return None
    ayahs = []
    for a, t in zip(arabic["ayahs"], latin["ayahs"]):
        text = str(a["text"]).replace("﻿", "").strip()
        if a["numberInSurah"] == 1 and int(n) not in (1, 9):
            words = text.split()
            if len(words) > 4 and words[0].startswith("بِسْمِ"):
                text = " ".join(words[4:])            # API кладёт басмалу в начало первого аята — это не часть суры
        ayahs.append({"n": int(a["numberInSurah"]), "ar": text, "lat": str(t["text"]).strip()})
    data = {"number": int(n), "name": str(arabic.get("englishName") or ""), "arabic_name": str(arabic.get("name") or ""), "ayahs": ayahs}
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            logger.warning("quran: не сохранил кэш", exc_info=True)
    return data


def _range(data: dict[str, Any], start: int | None, end: int | None) -> tuple[int, int]:
    total = len(data["ayahs"])
    a = max(1, int(start or 1))
    b = min(total, int(end or total))
    return (a, b) if a <= b else (b, a)


async def messages(n: int, start: int | None = None, end: int | None = None, *, lang: str = "ru") -> list[str]:
    """Готовые сообщения (HTML) для чата: арабский и транслитерация — цитатами; длинную суру — по частям."""
    data = await surah(n)
    if data is None:
        return []
    a, b = _range(data, start, end)
    picked = [x for x in data["ayahs"] if a <= x["n"] <= b]
    out: list[str] = []
    chunks = [picked[i:i + _MAX_AYAHS_PER_MESSAGE] for i in range(0, len(picked), _MAX_AYAHS_PER_MESSAGE)][:_MAX_MESSAGES]
    for idx, chunk in enumerate(chunks):
        first, last = chunk[0]["n"], chunk[-1]["n"]
        title = data["name"] or f"№{n}"
        span = f"аят {first}" if first == last else f"аяты {first}–{last}"
        span = span if lang != "uz" else (f"{first}-oyat" if first == last else f"{first}–{last}-oyatlar")
        head = f"📖 <b>{html.escape(title)}</b> · {data['arabic_name']} · {'сура' if lang != 'uz' else 'sura'} {data['number']} · {span}"
        if idx:
            head += " (продолжение)" if lang != "uz" else " (davomi)"
        arabic = "\n".join(f"{x['ar']} ﴿{x['n']}﴾" for x in chunk)
        latin = "\n".join(f"{x['n']}. {html.escape(x['lat'])}" for x in chunk)
        out.append(f"{head}\n\n<blockquote>{arabic}</blockquote>\n\n<b>{'Латиницей' if lang != 'uz' else 'Lotinda'}:</b>\n<blockquote>{latin}</blockquote>")
    if len(picked) > _MAX_AYAHS_PER_MESSAGE * _MAX_MESSAGES:
        rest = picked[_MAX_AYAHS_PER_MESSAGE * _MAX_MESSAGES]["n"]
        out[-1] += f"\n\n<i>{'Дальше — скажите «пришли аяты' if lang != 'uz' else 'Davomi — «oyatlarni yubor» deng'} {rest}–{b}».</i>"
    return out


def wants(text: str) -> bool:
    """Про это дело (задача/цель/ежедневное) стоит прислать текст суры?"""
    return bool(LEARN_RE.search(str(text or "")))


__all__ = ["surah", "messages", "wants", "LEARN_RE"]

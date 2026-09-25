"""Бесплатный голос Nurai: нейроголоса Microsoft (Edge «Прочитать вслух», библиотека edge-tts).

Он выбрал (25.09.2026) вместо платного TTS Gemini в экономном режиме телефона: бесплатно, фраза готова за 0.3–0.7 с,
есть русский (Светлана), узбекский (Мадина) и английский. Язык — по самому тексту ответа (Nurai отвечает на языке
вопроса). Доступ неофициальный — Microsoft может его закрыть; тогда phone_cheap.Speaker говорит голосом Gemini.

Звук приходит MP3 — переводим в PCM s16le 24 кГц моно (как голос Live) через ffmpeg.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

logger = logging.getLogger(__name__)

VOICES = {"ru": "ru-RU-SvetlanaNeural", "uz": "uz-UZ-MadinaNeural", "en": "en-US-JennyNeural"}
RATE = 24000
TIMEOUT_S = 6.0
_FAIL_PAUSE_S = 600.0     # Microsoft отказал — 10 минут не пробуем, сразу Gemini

_UZ_MARKERS = re.compile(r"[og][ʻʼ‘’'`](?=[a-z])|\b(siz|sizga|sizni|bo[ʻʼ'‘’`]?ldi|qil|kerak|ertaga|bugun|yaxshi|rahmat|shef|xo[ʻʼ'‘’`]?p|ha|yo[ʻʼ'‘’`]?q|uchun|bilan|va|bu|men|sen|qanday|nima|so[ʻʼ'‘’`]?m)\b", re.I)
_failed_at = 0.0


def available() -> bool:
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        return False
    return time.monotonic() - _failed_at > _FAIL_PAUSE_S or _failed_at == 0.0


_UZ_CYR = re.compile(r"[ўқғҳЎҚҒҲ]")
_CYR2LAT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j", "з": "z", "и": "i", "й": "y",
            "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
            "х": "x", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "ʼ", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya",
            "ў": "oʻ", "қ": "q", "ғ": "gʻ", "ҳ": "h"}


def uz_latin(text: str) -> str:
    """Узбекский кириллицей → латиница (модель иногда пишет кириллицей — русский голос прочитал бы это криво)."""
    out = []
    for i, ch in enumerate(text):
        low = ch.lower()
        lat = _CYR2LAT.get(low)
        if lat is None:
            out.append(ch)
            continue
        if low == "е" and (i == 0 or not text[i - 1].isalpha()):
            lat = "ye"  # в начале слова узбекское «е» — это «ye» («ер» → «yer»)
        out.append(lat.capitalize() if ch != low and lat else lat)
    return "".join(out)


def prepare(text: str) -> tuple[str, str]:
    """(текст для голоса, язык). Узбекская кириллица → латиница и узбекский голос."""
    if _UZ_CYR.search(text):
        return uz_latin(text), "uz"
    cyr = len(re.findall(r"[а-яё]", text, re.I))
    lat = len(re.findall(r"[a-z]", text, re.I))
    if cyr and lat > cyr and _UZ_MARKERS.search(text):
        return uz_latin(text), "uz"  # узбекский ответ со словом-другим кириллицей («…, Шеф, …»)
    return text, lang_of(text)


def lang_of(text: str) -> str:
    """ru — есть кириллица; uz — латиница с узбекскими словами/апострофами; иначе en."""
    if _UZ_CYR.search(text):
        return "uz"
    if re.search(r"[а-яё]", text, re.I):
        return "ru"
    if _UZ_MARKERS.search(text):
        return "uz"
    return "en" if re.search(r"[a-z]", text, re.I) else "ru"


async def synthesize(text: str, lang: str | None = None) -> bytes | None:
    """Текст → PCM s16le 24 кГц моно. None — не вышло (нет библиотеки, сеть, Microsoft закрыл доступ)."""
    global _failed_at
    if not text.strip() or not available():
        return None
    import edge_tts

    text, detected = prepare(text)
    voice = VOICES.get(lang or detected, VOICES["ru"])
    started = time.monotonic()
    try:
        mp3 = bytearray()

        async def collect() -> None:
            async for chunk in edge_tts.Communicate(text, voice).stream():
                if chunk.get("type") == "audio" and chunk.get("data"):
                    mp3.extend(chunk["data"])

        await asyncio.wait_for(collect(), TIMEOUT_S)
        if not mp3:
            raise RuntimeError("пустой звук")
        pcm = await _mp3_to_pcm(bytes(mp3))
    except Exception as exc:
        _failed_at = time.monotonic()
        logger.warning("free voice: Microsoft не ответил (%s): %s — %.0f мин говорю голосом Gemini", voice, str(exc)[:160],
                       _FAIL_PAUSE_S / 60)
        return None
    _failed_at = 0.0
    logger.info("free voice: %s за %.2f с, %.1f с звука", voice, time.monotonic() - started, len(pcm) / 2 / RATE)
    return pcm


async def _mp3_to_pcm(mp3: bytes) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-f", "mp3", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", str(RATE), "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(mp3), 10)
    if proc.returncode != 0 or not out:
        raise RuntimeError(f"ffmpeg: {err.decode('utf-8', 'replace')[:120]}")
    return out


__all__ = ["synthesize", "available", "lang_of", "VOICES"]

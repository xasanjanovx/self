"""Голосовые ответы: текст ZEKI → речь (Gemini TTS) → OGG/Opus для Telegram.

Работает только если доступна TTS-модель и в системе есть ffmpeg; иначе тихо
возвращает None — бот отвечает текстом.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import shutil

from .context import ai

logger = logging.getLogger(__name__)

MAX_CHARS = 700
_TAG_RE = re.compile(r"<[^>]+>")
_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿⬀-⯿️]")


def available() -> bool:
    return bool(ai.tts_model) and shutil.which("ffmpeg") is not None


def speakable(text: str) -> str:
    """Убираем HTML, эмодзи и маркеры списков; обрезаем до MAX_CHARS по границе предложения."""
    plain = html.unescape(_TAG_RE.sub("", text or ""))
    plain = _EMOJI_RE.sub("", plain).replace("•", "").replace("—", "-")
    plain = re.sub(r"[ \t]+", " ", plain)
    plain = re.sub(r"\n{2,}", "\n", plain).strip()
    if len(plain) > MAX_CHARS:
        cut = plain[:MAX_CHARS]
        dot = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
        plain = cut[: dot + 1] if dot > MAX_CHARS // 2 else cut
    return plain.strip()


async def pcm_to_ogg(pcm: bytes, *, rate: int = 24000) -> bytes | None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
        "-c:a", "libopus", "-b:a", "32k", "-application", "voip", "-f", "ogg", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(pcm)
    if proc.returncode != 0 or not out:
        logger.warning("ffmpeg failed: %s", err.decode(errors="ignore")[:200])
        return None
    return out


async def make_voice(text: str) -> bytes | None:
    """OGG/Opus с озвучкой текста или None (нет TTS/ffmpeg, пустой текст, ошибка)."""
    if not available():
        return None
    plain = speakable(text)
    if len(plain) < 2:
        return None
    try:
        pcm = await ai.synthesize(plain)
    except Exception:
        logger.warning("tts failed", exc_info=True)
        return None
    if not pcm:
        return None
    return await pcm_to_ogg(pcm)


__all__ = ["available", "speakable", "make_voice", "pcm_to_ogg"]

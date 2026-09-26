"""Быстрая проверка слова «JES» на сервере — маленький русский распознаватель вместо Gemini.

Телефон решил, что услышал «JES» (читается «Джес»), и прислал запись (~1.5 с). Раньше слово проверял Gemini —
1.0–1.6 с на каждое срабатывание. Теперь — sherpa-onnx zipformer (русский, int8, ~25 МБ): 20–130 мс на запись.
Проверено на голосах Edge (ru, мужской и женский): «Джес», «Эй, Джес», «Джесс» → «джес»; «жесть», «жест», «есть»,
«здесь», «джаз», «джек», «джинсы», «чес», «вес», «Зеки», «Джарвис», «Нурай» — не имя.
С 26.09.2026 (2.1) откликается ТОЛЬКО на JES: прежние имена («Джарвис», «Nurai», «ZEKI») больше не будят.

Модель — DATA_DIR/models/asr (том, переживает пересборку); нет модели или библиотеки — check() вернёт None,
и слово, как раньше, проверит Gemini.
"""
from __future__ import annotations

import asyncio
import logging
import re
import tarfile
import time
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MODEL = "sherpa-onnx-small-zipformer-ru-2024-09-18"
MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{MODEL}.tar.bz2"
FILES = ("encoder.int8.onnx", "decoder.onnx", "joiner.int8.onnx", "tokens.txt")
RATE = 16000

# JES: джес, джесс, джейс, джез, жес, jes, jess — но не «жест», «жесть», «есть», «джек», «джаз», «чес»
_JES = re.compile(r"^(?:дж|ж|дз|j)(?:е|э|ей|эй|e|ey|ei)(?:с|сс|з|s|ss|z)$")

_recognizer: Any = None
_load_error: str | None = None
_lock = asyncio.Lock()


def _model_dir() -> Path:
    from .tg_user import data_dir

    return data_dir() / "models" / "asr" / MODEL


def _download(target: Path) -> None:
    """Из архива (~110 МБ) берём только нужные файлы (~27 МБ)."""
    target.mkdir(parents=True, exist_ok=True)
    logger.info("wakeword: скачиваю распознаватель речи")
    with urllib.request.urlopen(MODEL_URL, timeout=120) as resp, tarfile.open(fileobj=resp, mode="r|bz2") as tar:
        for member in tar:
            name = member.name.rsplit("/", 1)[-1]
            if member.isfile() and name in FILES:
                src = tar.extractfile(member)
                if src is not None:
                    part = target / (name + ".part")
                    part.write_bytes(src.read())
                    part.replace(target / name)


def _load_sync() -> Any:
    import sherpa_onnx  # type: ignore

    d = _model_dir()
    if not all((d / f).exists() for f in FILES):
        _download(d)
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(d / "encoder.int8.onnx"), decoder=str(d / "decoder.onnx"), joiner=str(d / "joiner.int8.onnx"),
        tokens=str(d / "tokens.txt"), num_threads=2, sample_rate=RATE, feature_dim=80, decoding_method="greedy_search")


async def recognizer() -> Any | None:
    global _recognizer, _load_error
    if _recognizer is not None or _load_error is not None:
        return _recognizer
    async with _lock:
        if _recognizer is None and _load_error is None:
            try:
                _recognizer = await asyncio.to_thread(_load_sync)
                logger.info("wakeword: распознаватель готов")
            except Exception as exc:
                _load_error = f"{type(exc).__name__}: {exc}"
                logger.warning("wakeword disabled: %s", _load_error)
    return _recognizer


def match(text: str) -> tuple[bool, str]:
    """(есть ли «JES», что сказано после имени)."""
    words = re.findall(r"[a-zа-яё]+", text.lower().replace("ё", "е"))
    for i, word in enumerate(words):
        if _JES.match(word):
            return True, " ".join(words[i + 1:])
        # «эй джес» склеилось в одно слово
        for hey in ("эй", "хей", "hey"):
            if word.startswith(hey) and _JES.match(word[len(hey):]):
                return True, " ".join(words[i + 1:])
    return False, ""


def _transcribe_sync(rec: Any, x) -> str:  # noqa: ANN001
    s = rec.create_stream()
    s.accept_waveform(RATE, x)
    rec.decode_stream(s)
    return str(s.result.text or "").strip()


async def check(wav: bytes) -> dict[str, Any] | None:
    """{"text", "name": bool, "after": str, "ms"}; None — распознавателя нет (пусть проверит Gemini)."""
    from .voiceprint import pcm16k

    rec = await recognizer()
    if rec is None:
        return None
    started = time.monotonic()
    try:
        text = await asyncio.to_thread(_transcribe_sync, rec, pcm16k(wav))
    except Exception:
        logger.warning("wakeword: не распознал", exc_info=True)
        return None
    found, after = match(text)
    return {"text": text, "name": found, "after": after, "ms": round((time.monotonic() - started) * 1000)}


async def warm() -> None:
    """При запуске: модель в память заранее."""
    await recognizer()


__all__ = ["check", "match", "warm"]

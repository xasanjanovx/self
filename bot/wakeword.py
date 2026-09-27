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

# JES: джес, джесс, джейс, джез, жес, jes, jess; 26.09 его «Джес» распознавалось и как «джест», «джаз», «джас» (5 отказов за
# 4 минуты) — тоже имя (чужие голоса отсекает проверка голоса). Не имя: «жесть», «есть», «джек», «чес», «здесь».
_JES = re.compile(r"^(?:дж|ж|дз|j)(?:е|э|а|ей|эй|e|a|ey|ei)(?:с|сс|з|s|ss|z)т?$")

NAME_MAX_POS = 2  # перед именем в записи может остаться хвост прошлых слов — не больше двух

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
        if i > NAME_MAX_POS:
            break  # имя — в начале фразы («Джес, позвони…»), а не посреди разговора («…вот такой жест…»)
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


# ------------------------------------------------------------------ 27.09: «умнее» — его голос, а имя расслышано криво
# За сутки сервер отказал 236 раз «имя не прозвучало», и почти всё это был он сам (голос из его «банка» 0.8–0.99):
# русский распознаватель слышит «Джес» как «джой», «джесси», «дж», «с» или вообще теряет имя («позвони маме»).
# Когда голос точно его, эти варианты принимаем; ещё и учимся: отказали, а через несколько секунд он повторил ту же
# команду и прошёл — значит, первое слово отказанной фразы и было его «Джес» в ушах распознавателя.
_VERBS = {"позвони", "набери", "звони", "открой", "запусти", "включи", "выключи", "отключи", "поставь", "заведи", "засеки",
          "вызови", "закажи", "напиши", "отправь", "скажи", "найди", "покажи", "сделай", "добавь", "запиши", "напомни",
          "громче", "тише", "пауза", "фонарик", "такси"}
_QUESTIONS = {"какая", "какой", "какое", "сколько", "который", "когда", "где", "кто", "что"}
_TAIL = {"с", "эс", "ес", "есс", "жес", "жэс", "дж", "джс", "джэ"}
_STOP = {"сейчас", "вот", "так", "это", "да", "нет", "а", "и", "ну", "мне", "меня", "там", "тут", "уже", "ещё", "еще",
         "как", "что", "где", "когда", "потом", "тоже", "и", "но", "он", "она", "они", "я", "ты", "вы", "мы"}
LEARN_WINDOW_S = 25.0
_last_reject: dict[int, tuple[float, list[str]]] = {}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-zа-яё]+", str(text or "").lower().replace("ё", "е"))


def _variants_file():  # noqa: ANN202
    from .tg_user import data_dir

    return data_dir() / "wake_variants.json"


def variants(uid: int | None) -> set[str]:
    """Выученные написания его «Джес» (как их слышит распознаватель)."""
    if uid is None:
        return set()
    try:
        import json

        return set(json.loads(_variants_file().read_text(encoding="utf-8")).get(str(uid)) or [])
    except Exception:
        return set()


def _learn(uid: int, word: str) -> None:
    import json

    try:
        path = _variants_file()
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        known = [w for w in data.get(str(uid)) or [] if w != word]
        data[str(uid)] = (known + [word])[-20:]
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        logger.info("wakeword: выучил, как звучит его «Джес»: «%s»", word)
    except Exception:
        logger.warning("wakeword: не запомнил вариант", exc_info=True)


def lenient(text: str, *, strong: bool, confident: bool, uid: int | None = None) -> tuple[bool, str, str]:
    """Имя не распознано строго, но голос точно его: (принять?, что после имени, почему)."""
    words = _words(text)
    if not strong or not words:
        return False, "", ""
    learned = variants(uid)
    for i, w in enumerate(words[: NAME_MAX_POS + 1]):
        if (w.startswith("дж") and len(w) <= 8) or w in learned:
            return True, " ".join(words[i + 1:]), f"имя как «{w}»"
    for i, w in enumerate(words[:2]):
        if w in _VERBS or (confident and w in _QUESTIONS):
            return True, " ".join(words[i:]), "имя обрезано — сразу команда"
    if len(words) == 1 and words[0] in _TAIL:
        return True, "", "хвост имени"
    return False, "", ""


def note_reject(uid: int | None, text: str, strong: bool) -> None:
    if uid is not None and strong:
        _last_reject[uid] = (time.monotonic(), _words(text))


def note_accept(uid: int | None, after: str) -> None:
    """Прошло — если перед этим отказали той же команде, первое слово отказа = его «Джес» в ушах распознавателя."""
    if uid is None:
        return
    rej = _last_reject.pop(uid, None)
    if not rej or time.monotonic() - rej[0] > LEARN_WINDOW_S or len(rej[1]) < 2:
        return
    first, tail = rej[1][0], {w for w in rej[1][1:] if len(w) >= 3}
    if tail & set(_words(after)) and 2 <= len(first) <= 8 and first not in _STOP and first not in _VERBS and first not in _QUESTIONS:
        _learn(uid, first)


__all__ = ["check", "match", "warm", "lenient", "note_reject", "note_accept", "variants"]

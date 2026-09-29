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
    found, after, _ = match_at(text)
    return found, after


def match_at(text: str) -> tuple[bool, str, int]:
    """(есть ли «JES», что после имени, каким словом по счёту оно стоит; -1 — нет)."""
    words = re.findall(r"[a-zа-яё]+", text.lower().replace("ё", "е"))
    for i, word in enumerate(words):
        if i > NAME_MAX_POS:
            break  # имя — в начале фразы («Джес, позвони…»), а не посреди разговора («…вот такой жест…»)
        # «жес/жест/жаз» — обычные слова: именем считаем только первым словом (28.09: «…мышки жёст за…» из видео
        # прошло как «Джес» и заказало такси); дальше в фразе — только «дж…»
        if _JES.match(word) and (i == 0 or not word.startswith("ж")):
            return True, " ".join(words[i + 1:]), i
        # «эй джес» склеилось в одно слово
        for hey in ("эй", "хей", "hey"):
            if word.startswith(hey) and _JES.match(word[len(hey):]):
                return True, " ".join(words[i + 1:]), i
    return False, "", -1


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
    found, after, pos = match_at(text)
    return {"text": text, "name": found, "after": after, "pos": pos, "ms": round((time.monotonic() - started) * 1000)}


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


def name_like(word: str) -> bool:
    """Похоже ли слово на «Джес» в ушах распознавателя («джесси», «жес», «чес», «дес», «тез»). 28.09 выучилось «не» —
    и JES просыпался на любую фразу из видео, где в начале было «не» («а не дома на шапку…»)."""
    w = str(word or "").lower().replace("ё", "е")
    if not 3 <= len(w) <= 8 or w in _STOP or w in _VERBS or w in _QUESTIONS:
        return False
    # шипящий/«дж» перед гласной — «джесси», «жес», «бжес», «чес», «зэс», «jes»
    if re.search(r"(дж|ж|ч|ш|з|j|g|z)[еэиeiy]", w):
        return True
    # «дес», «тес», «дэз» — глухое начало, но конец как у имени
    return w[0] in "дт" and w.rstrip("иы")[-1:] in {"с", "з", "ш"} and len(w) <= 5


def variants(uid: int | None) -> set[str]:
    """Выученные написания его «Джес» (как их слышит распознаватель). Непохожие на имя — отбрасываем (и старые тоже)."""
    if uid is None:
        return set()
    try:
        import json

        return {w for w in json.loads(_variants_file().read_text(encoding="utf-8")).get(str(uid)) or [] if name_like(w)}
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


def lenient(text: str, *, strong: bool, confident: bool, uid: int | None = None, media: bool = False) -> tuple[bool, str, str]:
    """Имя не распознано строго, а голос прошёл проверку (вызывать только тогда): (принять?, что после имени, почему).
    «дж…» в начале — имя при его голосе («дж позвони мам» с похожестью 0.82 отказывали); выученное слово — только
    первым; команда без имени — только если ещё и детектор телефона уверен, что слышал «Джес».
    media — на телефоне играет видео/музыка: тогда никаких поблажек, только чётко расслышанное имя (28.09: в видео
    его же голос или похожий — голос проверку проходит, спасает только само слово)."""
    words = _words(text)
    # 28.09: одиночное «с» («хвост имени») больше НЕ принимаем — за утро 7 раз так проснулся от звуков из видео:
    # на коротком звуке сходство голоса случайно высокое. Всё мягкое — только когда голос точно его (strong)
    if not words or not strong or media:
        return False, "", ""
    learned = variants(uid)
    for i, w in enumerate(words[: NAME_MAX_POS + 1]):
        # «джес», «джэс», «джейс», «джесси» — да; узбекские имена «Джахонгир», «Джасур», «Джонибек» — нет (29.09 он говорил
        # с людьми по-узбекски, и имена в разговоре могли разбудить JES)
        if (w == "дж" or re.match(r"дж(е|э|ей|ой)", w)) and len(w) <= 7:
            return True, " ".join(words[i + 1:]), f"имя как «{w}»"
    if words[0] in learned:
        return True, " ".join(words[1:]), f"имя как «{words[0]}»"
    # 29.09 «ИИ сам по себе работает, когда я говорю с другими людьми»: его голос + «что…» («а что гандиотлерда…», «что пен
    # малида» — это он по-узбекски с людьми) принимались как «имя обрезано». Теперь без имени — только настоящая команда,
    # которую бот сам понимает («позвони маме», «открой ютуб»); вопросы («что…», «какая погода») — только с именем
    if confident:
        from . import instant

        for i, w in enumerate(words[:2]):
            if w in _VERBS and instant.parse(" ".join(words[i:])) is not None:
                return True, " ".join(words[i:]), "имя обрезано — сразу команда"
    return False, "", ""


def words(text: str) -> list[str]:
    return _words(text)


def strong_voice(text: str, voice: dict[str, Any]) -> bool:
    """Голос точно его. Одно короткое слово — только по сходству голоса (score): «банк» на коротком звуке случайно
    высокий (у звуков из видео было 0.97); фраза — ещё и по банку или z."""
    score, bank, z = float(voice.get("score") or 0), float(voice.get("bank") or 0), float(voice.get("z") or 0)
    if len(_words(text)) <= 1:
        return score >= 0.6
    return bank >= 0.85 or score >= 0.6 or (z >= 3.5 and score >= 0.5)


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
    if tail & set(_words(after)) and name_like(first):
        _learn(uid, first)


__all__ = ["check", "match", "match_at", "warm", "lenient", "note_reject", "note_accept", "variants", "words", "strong_voice", "name_like"]

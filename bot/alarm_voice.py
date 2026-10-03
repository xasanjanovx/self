"""Голос будильника в телефоне (03.10.2026: «будильник вообще не говорит ничего»).

Будильник Android в приложении JES раньше играл только мелодию звонка. Теперь между звонками он произносит фразы голосом
JES: «Доброе утро, шеф! Пора вставать на фаджр», «Намаз лучше сна»… Фразы записываются здесь один раз (озвучка Gemini, ~$0.0005
за фразу), приложение скачивает их заранее (GET /jarvis/v1/alarm_voice) и хранит у себя: утром интернет не нужен.
Громкость нормализуется — будильнику нужен не нежный, а слышный голос.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

VERSION = 1
SPEAK_BUDGET_S = 40.0    # запрос приложения ждёт записи не дольше: что успели — отдаём, остальное допишем в фоне
GAIN_MAX = 3.0           # озвучка тихая — подтягиваем, но не раздуваем шум
PEAK = 0.92

# «Намаз лучше сна», «вы же не мунафик», «пусть Аллах будет доволен вами» — фразы из wake.MOTIVATION, которые он выбрал сам
PHRASES = {
    "ru": ["Доброе утро, {hon}! Пора вставать на фаджр.",
           "{Hon}, вставайте! Намаз лучше сна.",
           "Просыпайтесь, {hon}. Фаджр не ждёт.",
           "{Hon}, пора на намаз. Нажмите «Проснулся».",
           "Вставайте, {hon}! Пусть Аллах будет доволен вами.",
           "{Hon}, вы же не мунафик. Вставайте на фаджр."],
    "uz": ["Xayrli tong, {hon}! Bomdodga turish vaqti.",
           "{Hon}, turing! Namoz uyqudan yaxshiroq.",
           "Uyg'oning, {hon}. Bomdod kutmaydi.",
           "{Hon}, namozga turing. Alloh sizdan rozi bo'lsin.",
           "Turing, {hon}! Siz munofiq emassiz-ku.",
           "{Hon}, bomdodga turing. Jannat sizga nasib qilsin."],
}


def texts(persona) -> list[tuple[str, str]]:  # noqa: ANN001
    """[(текст, язык)] по порядку звучания: основной язык и, если он говорит на двух, — через один."""
    from .phone_live import _HON

    hons = _HON.get(persona.honorific) or _HON["mix"]
    cycle = [hons[0], hons[0], *hons[1:]] if len(hons) > 1 else hons   # «чаще сэр, иногда шеф» — как в откликах
    first = persona.lang if persona.lang in PHRASES else "ru"
    langs = [first] + ([l for l in ("ru", "uz") if l != first] if persona.mirror else [])
    out: list[tuple[str, str]] = []
    for i in range(len(PHRASES[first])):
        for lang in langs:
            col = 0 if lang == "ru" else 1
            hon = cycle[(i * len(langs) + langs.index(lang)) % len(cycle)][col]
            text = PHRASES[lang][i].format(hon=hon, Hon=hon[:1].upper() + hon[1:])
            out.append((text, lang))
    return list(dict.fromkeys(out))


def loud(pcm: bytes) -> bytes:
    """Подтянуть громкость до PEAK от максимума (не больше GAIN_MAX раз)."""
    import numpy as np

    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    peak = float(np.abs(x).max()) if len(x) else 0.0
    if peak < 200:
        return pcm
    gain = min(GAIN_MAX, PEAK * 32767.0 / peak)
    return np.clip(x * gain, -32768, 32767).astype(np.int16).tobytes()


def _folder():  # noqa: ANN202
    from .tg_user import data_dir

    folder = data_dir() / "alarm_voice"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _path(voice_tag: str, text: str):  # noqa: ANN202
    return _folder() / (hashlib.sha1(f"{VERSION}|{voice_tag}|{text}".encode()).hexdigest()[:20] + ".wav")


async def _record(persona, text: str) -> bytes | None:  # noqa: ANN001
    """Одна фраза → WAV (24 кГц, моно). Распознавателем не проверяем: он не знает «фаджр» и «мунафик» и забраковал бы верное."""
    from . import phone_live
    from .context import ai

    for attempt in range(3):
        try:
            pcm = await ai.synthesize(text, voice=persona.voice)
        except Exception:
            logger.warning("alarm voice: озвучка не удалась (%s)", text[:40], exc_info=True)
            pcm = None
        if pcm and len(pcm) > 24000 * 2 * 0.8:   # короче 0,8 с — это не фраза, а обрывок
            return phone_live.pcm_to_wav(loud(phone_live.trim_clip(pcm)))
        await asyncio.sleep(1.5 * (attempt + 1))
    return None


_filling: asyncio.Task | None = None


async def _fill(persona, items: list[tuple[str, str]], tag: str) -> None:  # noqa: ANN001
    for text, _lang in items:
        path = _path(tag, text)
        if path.exists():
            continue
        wav = await _record(persona, text)
        if wav:
            path.write_bytes(wav)
        await asyncio.sleep(1.5)  # у озвучки лимит запросов в минуту


async def clips(uid: int) -> dict[str, Any]:
    """{"key", "clips": [{"text", "lang", "wav"}], "ready": bool}. Недостающие фразы дописываются в фоне — приложение
    возьмёт их при следующем обновлении (раз в 3 часа и при запуске)."""
    global _filling
    from . import ai as ai_mod
    from . import services

    persona = await services.persona(uid)
    tag = ai_mod.voice_tag(persona.voice)
    items = texts(persona)
    missing = [it for it in items if not _path(tag, it[0]).exists()]
    if missing:
        if _filling is None or _filling.done():
            _filling = asyncio.create_task(_fill(persona, missing, tag), name="alarm-voice")
        try:
            await asyncio.wait_for(asyncio.shield(_filling), timeout=SPEAK_BUDGET_S)
        except (asyncio.TimeoutError, Exception):
            pass
    out = []
    for text, lang in items:
        path = _path(tag, text)
        if path.exists():
            out.append({"text": text, "lang": lang, "wav": base64.b64encode(path.read_bytes()).decode()})
    key = hashlib.sha1(json.dumps([c["text"] for c in out], ensure_ascii=False).encode()).hexdigest()[:12] + f"_v{VERSION}_{tag}"
    return {"key": key, "clips": out, "ready": len(out) == len(items)}


async def warm(uid: int) -> None:
    """Записать фразы заранее (при запуске бота), чтобы приложение получило их сразу."""
    try:
        res = await clips(uid)
        logger.info("alarm voice: готово %s фраз, всё записано: %s", len(res["clips"]), res["ready"])
    except Exception:
        logger.warning("alarm voice: заготовка не удалась", exc_info=True)


__all__ = ["texts", "clips", "warm", "loud"]

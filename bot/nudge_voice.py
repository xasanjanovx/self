"""Нейтральные фразы JES для экранного времени (04.10.2026): «Сэр, у вас есть дела поважнее», «Сэр, проверьте Telegram — там
важное уведомление» — записаны ОДИН раз голосом JES (как фразы будильника, bot/alarm_voice.py) и хранятся в телефоне: каждый раз
бесплатно и без сети. Его условие: на улице и в транспорте рядом не должны услышать личного — ни названий приложений, ни «намаза»,
поэтому вслух только безличное, подробности — в чат бота (bot/screentime.py).

Наборы по нарастанию: l1 — тихо позвать, l2 — «дела поважнее», l3 — настойчиво, night — после 23:00.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from typing import Any

from .alarm_voice import _record

logger = logging.getLogger(__name__)

VERSION = 2   # 05.10: только русские фразы — телефон скачает набор заново (старые узбекские клипы заменятся)
SPEAK_BUDGET_S = 40.0

PHRASES: dict[str, dict[str, list[str]]] = {
    "l1": {"ru": ["{Hon}, минутку внимания.", "{Hon}, вы меня слышите?", "{Hon}, на секунду."],
           "uz": ["{Hon}, bir daqiqa e'tibor bering.", "{Hon}, meni eshityapsizmi?", "{Hon}, bir soniya."]},
    "l2": {"ru": ["{Hon}, у вас есть дела поважнее.", "{Hon}, проверьте, пожалуйста, Telegram — там важное уведомление.",
                  "{Hon}, важные дела ждут."],
           "uz": ["{Hon}, sizda muhimroq ishlar bor.", "{Hon}, Telegramni tekshiring, muhim xabar bor.", "{Hon}, muhim ishlar kutyapti."]},
    "l3": {"ru": ["{Hon}, пора отложить телефон.", "{Hon}, давайте вернёмся к главному.", "{Hon}, время дорого — дела поважнее ждут."],
           "uz": ["{Hon}, telefonni qo'yish vaqti.", "{Hon}, keling, asosiy ishga qaytamiz.", "{Hon}, vaqt qimmat — muhim ishlar kutyapti."]},
    "night": {"ru": ["{Hon}, уже поздно. Пора отдыхать.", "{Hon}, завтра ранний подъём.", "{Hon}, телефон подождёт до утра."],
              "uz": ["{Hon}, kech bo'ldi. Dam olish vaqti.", "{Hon}, ertaga erta turasiz.", "{Hon}, telefon ertalabgacha kutadi."]},
}


def texts(persona) -> list[tuple[str, str, str]]:  # noqa: ANN001
    """[(вид, текст, язык)] — ТОЛЬКО по-русски (05.10: «упоминания про Telegram должны быть только на русском»), по кругу «сэр, сэр, шеф»."""
    from .phone_live import _HON

    hons = _HON.get(persona.honorific) or _HON["mix"]
    cycle = [hons[0], hons[0], *hons[1:]] if len(hons) > 1 else hons
    langs = ["ru"]
    out: list[tuple[str, str, str]] = []
    for kind, by_lang in PHRASES.items():
        for lang in langs:
            col = 0 if lang == "ru" else 1
            for i, t in enumerate(by_lang[lang]):
                hon = cycle[i % len(cycle)][col]
                out.append((kind, t.format(Hon=hon[:1].upper() + hon[1:]), lang))
    return list(dict.fromkeys(out))


def _folder():  # noqa: ANN202
    from .tg_user import data_dir

    folder = data_dir() / "nudge_voice"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _path(voice_tag: str, text: str):  # noqa: ANN202
    return _folder() / (hashlib.sha1(f"{VERSION}|{voice_tag}|{text}".encode()).hexdigest()[:20] + ".wav")


_filling: asyncio.Task | None = None


async def _fill(persona, items: list[tuple[str, str, str]], tag: str) -> None:  # noqa: ANN001
    for _kind, text, _lang in items:
        path = _path(tag, text)
        if path.exists():
            continue
        wav = await _record(persona, text)
        if wav:
            path.write_bytes(wav)
        await asyncio.sleep(1.5)  # лимит озвучки в минуту


async def clips(uid: int) -> dict[str, Any]:
    """{"key", "clips": [{"kind", "text", "lang", "wav"}], "ready"} — недостающее дописывается в фоне."""
    global _filling
    from . import ai as ai_mod
    from . import services

    persona = await services.persona(uid)
    tag = ai_mod.voice_tag(persona.voice)
    items = texts(persona)
    missing = [it for it in items if not _path(tag, it[1]).exists()]
    if missing:
        if _filling is None or _filling.done():
            _filling = asyncio.create_task(_fill(persona, missing, tag), name="nudge-voice")
        try:
            await asyncio.wait_for(asyncio.shield(_filling), timeout=SPEAK_BUDGET_S)
        except (asyncio.TimeoutError, Exception):
            pass
    out = []
    for kind, text, lang in items:
        path = _path(tag, text)
        if path.exists():
            out.append({"kind": kind, "text": text, "lang": lang, "wav": base64.b64encode(path.read_bytes()).decode()})
    key = hashlib.sha1(json.dumps([c["text"] for c in out], ensure_ascii=False).encode()).hexdigest()[:12] + f"_v{VERSION}_{tag}"
    return {"key": key, "clips": out, "ready": len(out) == len(items)}


async def warm(uid: int) -> None:
    try:
        res = await clips(uid)
        logger.info("nudge voice: готово %s фраз, всё записано: %s", len(res["clips"]), res["ready"])
    except Exception:
        logger.warning("nudge voice: заготовка не удалась", exc_info=True)


__all__ = ["PHRASES", "texts", "clips", "warm"]

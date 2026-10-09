"""Узбекские имена голосом Gemini: имя читается внутри узбекской фразы, а не само по себе.

09.10 он: «в обеих он вообще неправильно читает, улучши намного чтение на узбекском». Замер (IPA-расшифровка записи Gemini-судьёй,
эталон — носитель-нейроголос): имя, поданное в озвучку само по себе, читается наугад — «Xusanboy aka» → [husəm bɔj aˈkæ] («Хусамбой», х как
английское h); то же имя в начале узбекской фразы «Xusanboy aka. Qo'ng'iroq qilyapti.» → [χusanbɔj aˈka] — ровно как по правилам узбекского
(х — гортанный, о — открытое, ударение на последнем слоге). Модель переключается на узбекский целиком, когда видит узбекскую фразу.

Имя озвучивается как начало фразы-носителя, а хвост («Qo'ng'iroq qilyapti») срезается по границе предложения: между словами имени паузы
≤ 0.12 с, на границе — 0.3–0.9 с, а сам хвост длится 0.85–1.05 с (замерено на 9 именах); границей берём паузу, после которой остаётся речь
ближе всего к такой длине — иначе у «Komiljon aka Jalaquduq» терялось последнее слово.

Озвучка каждый раз чуть разная, поэтому записываем несколько дублей, слушаем каждый «умной» моделью (запись узбекской латиницей по звукам, без
подгонки под ожидаемое имя; заодно — прозвучало ли «aka» как обращение, когда ЗОВУТ человека, а не как часть имени) и берём лучший. Готовое имя
хранится на диске: озвучка каждого имени — один раз (потом звонок «Звонит <имя>» собирается из готовых кусков за доли секунды).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

RATE = 24000
CARRIER = "{name}. Qo'ng'iroq qilyapti."     # имя в начале узбекского предложения; после точки — длинная пауза
BOUNDARY_GAP_S = 0.18                         # пауза не короче — кандидат в границу (между словами имени бывает до 0.12 с)
MIN_NAME_S = 0.30
MAX_NAME_S = 4.5
MIN_TAIL_S = 0.55                             # после границы остаётся хвост фразы-носителя: «Qo'ng'iroq qilyapti.» длится 0.85–1.05 с (замерено)…
MAX_TAIL_S = 1.35                             # …пауза внутри имени оставила бы после себя ещё и слово имени — хвост был бы длиннее
TARGET_TAIL_S = 0.95
FADE_S = 0.02
TAKES = 5                                     # дублей имени; лучший по прослушиванию сохраняется
VOCATIVE_PENALTY = 0.4                        # «aka» прозвучало как обращение («Акмал ака!») — дубль хуже
LISTEN_PROMPT = (
    "Это запись одного узбекского имени (может быть со словом «aka», «opa», «bek»). Запиши ТОЛЬКО то, что реально слышишь, узбекской латиницей, "
    "по звукам: если слышно «дал шот а ке» — пиши «dal shot a ke». НЕ исправляй и НЕ угадывай известное имя: неверно произнесённое имя должно "
    "получиться неверно записанным. Если есть обращение («aka», «opa», «uka», «xon»), определи, КАК оно прозвучало: \"neutral\" — как часть имени "
    "в обычном чтении (так читают имя из списка или в предложении «Звонит Akmal aka»); \"vocative\" — как обращение, когда ЗОВУТ человека "
    "(«Акмал ака!»: призывная или приветственная интонация, пауза-выделение после него); \"none\" — обращения в записи нет. "
    "Верни короткий JSON: {\"heard\": \"...\", \"aka\": \"neutral|vocative|none\"}")


def _norm(text: str) -> str:
    """Для сравнения написаний: без апострофов и регистра; x/kh/h, q/k, w/v, c/ch — как слышит расшифровка (в узбекском пишут по-разному)."""
    t = str(text or "").lower()
    t = re.sub(r"[ʻʼ‘’'`\-.,!?\"“”«»]", "", t)
    t = t.replace("kh", "h").replace("x", "h").replace("q", "k").replace("w", "v").replace("ch", "c").replace("sh", "s")
    return " ".join(t.split())


def similarity(heard: str, name: str) -> float:
    """0..1: насколько расшифровка записи похожа на имя (1 — совпало)."""
    a, b = _norm(heard), _norm(name)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def cut_name(pcm: bytes, rate: int = RATE) -> bytes | None:
    """Звук имени из записи «Имя. Qo'ng'iroq qilyapti.»: от начала речи до границы предложения, без тишины в начале и с короткой затухающей
    кромкой. Граница — пауза ≥ BOUNDARY_GAP_S, после которой речи ближе всего к длине хвоста «Qo'ng'iroq qilyapti.» (≈0.95 с). Не «первая пауза»:
    в «Komiljon aka Jalaquduq» после «aka» бывает пауза 0.3 с — терялось «Jalaquduq» и потом читалось отдельно как «Yalaquduq» (09.10).
    None — подходящей паузы нет (хвост не отрезать надёжно)."""
    import numpy as np

    x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16)
    frame = int(rate * 0.01)
    n = len(x) // frame
    if n < 20:
        return None
    rms = np.sqrt((x[: n * frame].astype(np.float32).reshape(n, frame) ** 2).mean(axis=1))
    voiced = rms > max(float(rms.max()) * 0.05, 50.0)
    if not voiced.any():
        return None
    first = int(np.argmax(voiced))
    last = n - 1 - int(np.argmax(voiced[::-1]))
    gaps: list[tuple[int, int]] = []          # (начало паузы, конец паузы) в кадрах по 10 мс
    i = first
    while i <= last:
        if voiced[i]:
            i += 1
            continue
        k = i
        while k <= last and not voiced[k]:
            k += 1
        if (k - i) * 0.01 >= BOUNDARY_GAP_S:
            gaps.append((i, k))
        i = k
    end, best = None, None
    for start, stop in gaps:
        tail = (last - stop) * 0.01
        if MIN_TAIL_S <= tail <= MAX_TAIL_S and (best is None or abs(tail - TARGET_TAIL_S) < best):
            end, best = start, abs(tail - TARGET_TAIL_S)
    if end is None or not MIN_NAME_S <= (end - first) * 0.01 <= MAX_NAME_S:
        return None
    start_s, end_s = max(0, first - 3) * frame, min(n, end + 3) * frame   # 30 мс до начала и после конца: без обрезанных согласных
    out = x[start_s:end_s].astype(np.float32)
    fade = min(len(out), int(rate * FADE_S))
    if fade:
        out[-fade:] *= np.linspace(1.0, 0.0, fade)
    return out.astype(np.int16).tobytes()


def voice_text(name: str) -> str:
    """Имя для озвучки. «J» в начале второго и дальнейших слов («Komiljon aka Jalaquduq») читалось как «Ch»/«Y» — пишем «Dj» ([dʒ], замерено).
    Запятую после «aka» НЕ ставим: с ней «aka» звучало как обращение, когда зовут человека (09.10, его замечание)."""
    words = name.split()
    return " ".join("Dj" + w[1:] if i > 0 and w[:1] in ("J", "j") and w[1:2].lower() in tuple("aeiouy") else w for i, w in enumerate(words))


def _norm_name(name: str) -> str:
    return re.sub(r"[ʻʼ‘’`]", "'", name)      # апостроф узбекской латиницы — обычный (g'ayrat, o'tkir): с «ʻ» озвучка спотыкается


def _clip_path(model: str, voice: str, name: str) -> Path | None:
    folder = os.getenv("DATA_DIR")
    if not folder or not name.strip():
        return None
    key = hashlib.sha1(f"v6|{model}|{voice}|{' '.join(name.split()).lower()}".encode()).hexdigest()[:20]
    return Path(folder) / "name_clips" / f"{key}.pcm"


def cached(name: str, voice: str) -> bool:
    """Лучшее озвучивание этого имени уже записано (звонок — мгновенно)."""
    from . import ai as ai_mod

    path = _clip_path(ai_mod.tts_model_now(), voice, _norm_name(name))
    return bool(path is not None and path.exists())


async def _take(ai: Any, name: str, voice: str) -> bytes | None:
    try:
        full = b"".join([c async for c in ai.speak_stream(CARRIER.format(name=voice_text(name)), voice=voice, language="uz-UZ")])
    except Exception as exc:
        logger.warning("uz name: озвучка фразы-носителя не вышла: %s", str(exc)[:160])
        return None
    return cut_name(full) if full else None


async def _listen(ai: Any, pcm: bytes) -> tuple[str, bool]:
    """(что слышно, «aka» прозвучало как обращение). Слушает «умная» модель: лёгкая подгоняет услышанное под ожидаемое имя
    («Dilsha take» превращала в «Dilshod aka»)."""
    import base64
    import json

    from . import ai as ai_mod
    from .phone_live import pcm_to_wav

    try:
        raw = await ai.generate([{"text": LISTEN_PROMPT}, {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(pcm_to_wav(pcm, RATE)).decode()}}],
                                model=ai_mod.smart_model(), temperature=0.0, json_mode=True, max_tokens=900)
    except Exception as exc:
        logger.info("uz name: расшифровка дубля не вышла: %s", str(exc)[:120])
        return "", False
    try:
        data = json.loads(raw)
        heard = " ".join(str(data.get("heard") or "").split())
        vocative = str(data.get("aka") or "").strip().lower() == "vocative"
    except (ValueError, AttributeError):
        text = " ".join(str(raw or "").split())
        match = re.search(r'"heard"\s*:\s*"([^"]*)"', text)
        heard, vocative = (match.group(1).strip() if match else text[:80]), "vocative" in text.lower()
    return heard, vocative


def score(heard: str, vocative: bool, name: str) -> float:
    return similarity(heard, name) - (VOCATIVE_PENALTY if vocative else 0.0)


async def say_name(ai: Any, name: str, *, voice: str, takes: int = TAKES) -> bytes | None:
    """Имя узбекской латиницей → PCM s16le 24 кГц, прочитанный по-узбекски (лучший из нескольких дублей). None — не вышло (озвучат обычно)."""
    from . import ai as ai_mod

    name = _norm_name(name)
    path = _clip_path(ai_mod.tts_model_now(), voice, name)
    if path is not None and path.exists():
        try:
            return path.read_bytes()
        except OSError:
            pass
    results = await asyncio.gather(*(_take(ai, name, voice) for _ in range(max(1, takes))))
    clips = [c for c in results if c]
    if not clips:
        logger.info("uz name: «%s» — паузу после имени не нашла ни в одном дубле, озвучиваю имя без фразы", name)
        return None
    best = clips[0]
    if len(clips) > 1:
        heard = await asyncio.gather(*(_listen(ai, c) for c in clips))
        scores = [score(h, v, name) for h, v in heard]
        pick = max(range(len(clips)), key=lambda i: scores[i])
        best = clips[pick]
        logger.info("uz name: «%s» — дублей %s, выбран %s: слышно «%s»%s (оценка %.2f; остальные %s)", name, len(clips), pick + 1, heard[pick][0][:40],
                    ", как обращение" if heard[pick][1] else "", scores[pick], ", ".join(f"{x:.2f}" for i, x in enumerate(scores) if i != pick))
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(best)
        except OSError:
            logger.debug("uz name: не сохранила", exc_info=True)
    return best


async def quick_name(ai: Any, name: str, *, voice: str) -> bytes | None:
    """Быстро, прямо во время звонка: один дубль без прослушивания (~2.5 с). Не сохраняется — лучший дубль запишет say_name в фоне."""
    return await _take(ai, _norm_name(name), voice)


__all__ = ["say_name", "quick_name", "cached", "cut_name", "similarity", "voice_text", "score", "CARRIER", "TAKES"]

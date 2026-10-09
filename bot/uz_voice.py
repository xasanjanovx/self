"""Узбекские имена голосом Gemini: имя читается внутри узбекской фразы, а не само по себе.

09.10 он: «в обеих он вообще неправильно читает, улучши намного чтение на узбекском». Замер (IPA-расшифровка записи Gemini-судьёй,
эталон — носитель-нейроголос): имя, поданное в озвучку само по себе, читается наугад — «Xusanboy aka» → [husəm bɔj aˈkæ] («Хусамбой», х как
английское h); то же имя в начале узбекской фразы «Xusanboy aka. Qo'ng'iroq qilyapti.» → [χusanbɔj aˈka] — ровно как по правилам узбекского
(х — гортанный, о — открытое, ударение на последнем слоге). Модель переключается на узбекский целиком, когда видит узбекскую фразу.

Озвучка каждый раз чуть разная, поэтому записываем несколько дублей, слушаем каждый (расшифровка узбекской латиницей) и берём тот, что
расшифровался ближе всего к имени («Dilshod aka» → «Dilsha take» — дубль плохой, выбираем другой).

Поэтому имя озвучивается как начало фразы-носителя, а хвост («Qo'ng'iroq qilyapti») срезается по первой настоящей паузе: между словами имени
паузы ≤ 0.12 с, на границе предложения — 0.3–0.9 с (замерено на 7 именах). Готовый кусок хранится на диске (озвучка каждого имени — один раз).
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
BOUNDARY_GAP_S = 0.18                         # пауза не короче — кандидат в границу (между словами имени бывает до 0.12 с, после запятой ~0.3)
MIN_NAME_S = 0.30
MAX_NAME_S = 4.5
MIN_TAIL_S = 0.55                             # после границы остаётся хвост фразы-носителя: «Qo'ng'iroq qilyapti.» длится 0.85–1.05 с (замерено)…
MAX_TAIL_S = 1.35                             # …пауза внутри имени оставила бы после себя ещё и слово имени — хвост был бы длиннее
TARGET_TAIL_S = 0.95
FADE_S = 0.02
TAKES = 4                                     # дублей имени; лучший по расшифровке сохраняется
LISTEN_PROMPT = ("Это запись одного узбекского имени (может быть со словом «aka», «bek», «opa»). Запиши ТОЛЬКО то, что реально слышишь, "
                 "узбекской латиницей, по звукам: если слышно «дал шот а ке» — пиши «dal shot a ke». НЕ исправляй и НЕ угадывай известное имя: "
                 "неверно произнесённое имя должно получиться неверно записанным. Одна строка, без пояснений.")


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
    """Имя для озвучки: после обращения («aka», «opa») перед следующим словом — запятая. Без неё «Komiljon aka Jalaquduq» читалось слитно, а «J»
    во втором слове — как «Ch»/«Y» (09.10); с запятой — «Jalaquduq» читается верно."""
    from .names import HONORIFICS

    words = name.split()
    return " ".join(w + "," if i < len(words) - 1 and w.lower() in HONORIFICS else w for i, w in enumerate(words))


def _clip_path(model: str, voice: str, name: str) -> Path | None:
    folder = os.getenv("DATA_DIR")
    if not folder or not name.strip():
        return None
    key = hashlib.sha1(f"v5|{model}|{voice}|{' '.join(name.split()).lower()}".encode()).hexdigest()[:20]
    return Path(folder) / "name_clips" / f"{key}.pcm"


async def _take(ai: Any, name: str, voice: str) -> bytes | None:
    try:
        full = b"".join([c async for c in ai.speak_stream(CARRIER.format(name=voice_text(name)), voice=voice, language="uz-UZ")])
    except Exception as exc:
        logger.warning("uz name: озвучка фразы-носителя не вышла: %s", str(exc)[:160])
        return None
    return cut_name(full) if full else None


async def _listen(ai: Any, pcm: bytes) -> str:
    """Что слышно в дубле. Слушает «умная» модель: лёгкая подгоняет услышанное под ожидаемое имя («Dilsha take» превращала в «Dilshod aka»)."""
    import base64

    from . import ai as ai_mod
    from .phone_live import pcm_to_wav

    try:
        model = ai_mod.smart_model()
        text = await ai.generate([{"text": LISTEN_PROMPT}, {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(pcm_to_wav(pcm, RATE)).decode()}}],
                                 model=model, temperature=0.0, max_tokens=900)
        return " ".join(str(text or "").split())      # все строки: слова имени модель иногда пишет столбиком
    except Exception as exc:
        logger.info("uz name: расшифровка дубля не вышла: %s", str(exc)[:120])
        return ""


async def say_name(ai: Any, name: str, *, voice: str, takes: int = TAKES) -> bytes | None:
    """Имя узбекской латиницей → PCM s16le 24 кГц, прочитанный по-узбекски (лучший из нескольких дублей). None — не вышло (озвучат обычно)."""
    from . import ai as ai_mod

    name = re.sub(r"[ʻʼ‘’`]", "'", name)      # апостроф узбекской латиницы — обычный (g'ayrat, o'tkir): с «ʻ» озвучка спотыкается
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
        scores = [similarity(h, name) for h in heard]
        pick = max(range(len(clips)), key=lambda i: scores[i])
        best = clips[pick]
        logger.info("uz name: «%s» — дублей %s, выбран %s: слышно «%s» (совпадение %.2f; остальные %s)", name, len(clips), pick + 1,
                    heard[pick][:40], scores[pick], ", ".join(f"{x:.2f}" for i, x in enumerate(scores) if i != pick))
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(best)
        except OSError:
            logger.debug("uz name: не сохранила", exc_info=True)
    return best


__all__ = ["say_name", "cut_name", "similarity", "voice_text", "CARRIER", "TAKES"]

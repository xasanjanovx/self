"""«Только мой голос»: отпечаток голоса владельца и проверка, что «Джарвис» сказал именно он.

Модель CAM++ (3D-Speaker, sherpa-onnx) превращает запись в вектор голоса; похожесть — косинус.
Отпечаток — среднее по записи: 10 раз «Джарвис» + ~40 с чтения текста (короткое «Джарвис» само по себе
даёт мало данных: у одного и того же голоса на 1–2 с похожесть гуляет 0.3–0.7, у чужих — до ~0.27).
Порог подбирается под человека по его же коротким записям — строго, но так, чтобы его не отсекало.

Сырая похожесть на коротком «Джарвис» у чужих голосов бывает до ~0.58, поэтому решаем по нормированной:
насколько запись ближе к нему, чем к «толпе» из 12 других голосов (z-норма, models/cohort_live.npy).
Проверено на голосах Gemini: свой — z 0.4…1.5, чужие — не выше 0.5; порог — по его собственной записи.

Модель — DATA_DIR/models (том, переживает пересборку); нет модели или библиотеки — проверка выключена,
Джарвис работает как раньше. Отпечаток — DATA_DIR/voiceprint_<uid>.json.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import time
import urllib.request
import wave
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MODEL_NAME = "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"
MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/{MODEL_NAME}"
RATE = 16000
WINDOW_S = 3.0          # длинное чтение режем на куски по 3 с — отпечаток устойчивее
MIN_THRESHOLD = 0.30    # сырая похожесть: ниже — точно не он
MAX_THRESHOLD = 0.55    # выше — его самого будет отсекать на коротком «Джарвис»
Z_MIN, Z_MAX = 0.55, 1.2  # нормированная: чужие голоса до ~0.5, его — от ~0.4 (строго: лучше иногда повторить)
COHORT_FILE = "cohort_live.npy"

_extractor: Any = None
_load_error: str | None = None
_cohort: Any = None
_lock = asyncio.Lock()


def _data_dir() -> Path:
    from .tg_user import data_dir

    return data_dir()


def _model_path() -> Path:
    return _data_dir() / "models" / MODEL_NAME


def _file(uid: int) -> Path:
    return _data_dir() / f"voiceprint_{uid}.json"


# ------------------------------------------------------------------ модель
def _load_sync() -> Any:
    import sherpa_onnx  # type: ignore

    path = _model_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        logger.info("voiceprint: скачиваю модель голоса (~30 МБ)")
        urllib.request.urlretrieve(MODEL_URL, tmp)
        tmp.replace(path)
    cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(path), num_threads=2)
    return sherpa_onnx.SpeakerEmbeddingExtractor(cfg)


async def extractor() -> Any | None:
    global _extractor, _load_error
    if _extractor is not None or _load_error is not None:
        return _extractor
    async with _lock:
        if _extractor is None and _load_error is None:
            try:
                _extractor = await asyncio.to_thread(_load_sync)
                logger.info("voiceprint: модель голоса готова")
            except Exception as exc:
                _load_error = f"{type(exc).__name__}: {exc}"
                logger.warning("voiceprint disabled: %s", _load_error)
    return _extractor


def available() -> bool:
    return _extractor is not None


def cohort():
    """Векторы «толпы» (12 чужих голосов) для нормировки; нет файла — None (решаем по сырой похожести)."""
    global _cohort
    if _cohort is None:
        import numpy as np

        path = _model_path().parent / COHORT_FILE
        _cohort = np.load(path) if path.exists() else False
    return _cohort if _cohort is not False else None


def zscore(vp, e) -> tuple[float, float | None]:
    """(сырая похожесть, насколько ближе к нему, чем к толпе — в стандартных отклонениях толпы)."""
    s = float(vp @ e)
    c = cohort()
    if c is None or not len(c):
        return s, None
    cs = c @ e
    return s, float((s - cs.mean()) / (cs.std() + 1e-6))


def calibrate_z(zs: list[float]) -> float:
    import numpy as np

    if not zs:
        return 0.8
    low = float(np.percentile(zs, 10))
    return round(min(Z_MAX, max(Z_MIN, low - 0.15)), 3)


# ------------------------------------------------------------------ звук → вектор
def pcm16k(wav_bytes: bytes):
    """WAV (любая частота, моно 16 бит) → float32 16 кГц."""
    import numpy as np

    with wave.open(io.BytesIO(wav_bytes)) as w:
        rate, channels = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    if rate != RATE and len(x):
        n = int(len(x) * RATE / rate)
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)
    return x


def trim_silence(x, *, frame: int = 320):
    """Срезать тишину по краям (энергия ниже 15% от громких кадров) — вектор считается по голосу, а не по фону."""
    import numpy as np

    if len(x) < frame * 4:
        return x
    n = len(x) // frame
    energy = np.sqrt((x[: n * frame].reshape(n, frame) ** 2).mean(axis=1))
    loud = np.percentile(energy, 90)
    voiced = np.where(energy > loud * 0.15)[0]
    if not len(voiced):
        return x
    return x[max(0, voiced[0] - 3) * frame: min(n, voiced[-1] + 4) * frame]


def _embed_sync(ext: Any, x):
    import numpy as np

    s = ext.create_stream()
    s.accept_waveform(RATE, x)
    s.input_finished()
    v = np.array(ext.compute(s), dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


# ------------------------------------------------------------------ отпечаток
def calibrate(scores: list[float]) -> float:
    """Порог по его же коротким «Джарвис»: чуть ниже худших 10% — но в разумных рамках."""
    import numpy as np

    if not scores:
        return 0.40
    low = float(np.percentile(scores, 10))
    return round(min(MAX_THRESHOLD, max(MIN_THRESHOLD, low - 0.05)), 3)


async def enroll(uid: int, wake_wavs: list[bytes], reading_wav: bytes | None) -> dict[str, Any]:
    """Записать отпечаток: короткие «Джарвис» + длинное чтение. Возвращает порог и оценки."""
    import numpy as np

    ext = await extractor()
    if ext is None:
        return {"error": f"проверка голоса недоступна на сервере ({_load_error})"}

    def work() -> dict[str, Any]:
        short = [trim_silence(pcm16k(w)) for w in wake_wavs if w]
        short = [x for x in short if len(x) >= RATE * 0.4]
        long_parts = []
        if reading_wav:
            r = trim_silence(pcm16k(reading_wav))
            step = int(WINDOW_S * RATE)
            long_parts = [r[i:i + step] for i in range(0, max(0, len(r) - step // 2), step) if len(r[i:i + step]) >= RATE]
        if len(short) < 4 and len(long_parts) < 3:
            return {"error": "слишком мало голоса — запишите ещё раз, чуть громче"}
        long_emb = [_embed_sync(ext, x) for x in long_parts]
        short_emb = [_embed_sync(ext, x) for x in short]
        base = long_emb or short_emb
        vp = np.mean(base, axis=0)
        vp /= np.linalg.norm(vp) + 1e-9
        # проверяем на тех же коротких «Джарвис» — ровно так, как будет при срабатывании
        pairs = [zscore(vp, e) for e in short_emb]
        scores = [p[0] for p in pairs]
        zs = [p[1] for p in pairs if p[1] is not None]
        threshold = calibrate(scores)
        z_threshold = calibrate_z(zs) if zs else None
        # итоговый отпечаток — и чтение, и короткие (ближе к тому, что услышим утром)
        full = np.mean(long_emb + short_emb, axis=0)
        full /= np.linalg.norm(full) + 1e-9
        data = {"vp": [round(float(v), 6) for v in full], "threshold": threshold, "z_threshold": z_threshold, "created": time.time(),
                "short": len(short_emb), "reading_s": round(sum(len(x) for x in long_parts) / RATE, 1)}
        _file(uid).write_text(json.dumps(data), encoding="utf-8")
        return {"ok": True, "threshold": threshold, "z_threshold": z_threshold, "scores": [round(s, 3) for s in scores],
                "z": [round(z, 2) for z in zs], "short": len(short_emb), "reading_s": data["reading_s"]}

    result = await asyncio.to_thread(work)
    logger.info("voiceprint %s: %s", uid, {k: v for k, v in result.items() if k != "scores"})
    return result


def status(uid: int) -> dict[str, Any]:
    try:
        data = json.loads(_file(uid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"enrolled": False, "available": available()}
    return {"enrolled": True, "threshold": data.get("threshold"), "reading_s": data.get("reading_s"), "available": available()}


def forget(uid: int) -> None:
    _file(uid).unlink(missing_ok=True)


async def verify(uid: int, wav: bytes) -> dict[str, Any]:
    """{"ok": bool, "score", "threshold"}; отпечатка/модели нет — ok (проверка не мешает)."""
    import numpy as np

    try:
        data = json.loads(_file(uid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": True, "enrolled": False}
    ext = await extractor()
    if ext is None:
        return {"ok": True, "enrolled": True, "unchecked": True}
    vp = np.array(data["vp"], dtype=np.float32)
    x = trim_silence(pcm16k(wav))
    if len(x) < RATE * 0.3:
        return {"ok": False, "score": 0.0, "threshold": data["threshold"], "reason": "short"}
    e = await asyncio.to_thread(_embed_sync, ext, x)
    score, z = zscore(vp, e)
    z_thr = data.get("z_threshold")
    if z is not None and z_thr is not None:
        ok = score >= MIN_THRESHOLD and z >= float(z_thr)
    else:
        ok = score >= float(data["threshold"])
    return {"ok": ok, "score": round(score, 3), "z": None if z is None else round(z, 2),
            "threshold": z_thr if z is not None and z_thr is not None else data["threshold"]}


OTHER_Z_MARGIN = 0.15    # посреди разговора мягче, чем на «Джарвис»: его самого лучше лишний раз пропустить, чем не услышать
OTHER_SCORE = 0.25


def enrolled(uid: int) -> bool:
    return _file(uid).exists() and available()


async def is_other(uid: int, wav: bytes) -> dict[str, Any]:
    """Фраза посреди разговора — явно чужой голос (телевизор, кто-то рядом)? {"other": bool, …}.
    Короткое, неразборчивое, нет отпечатка или модели — не чужое."""
    res = await verify(uid, wav)
    if res.get("ok") or res.get("reason") == "short" or res.get("enrolled") is False or res.get("unchecked"):
        return {**res, "other": False}
    score, z, thr = float(res.get("score") or 0), res.get("z"), float(res.get("threshold") or 0)
    other = score < OTHER_SCORE or (z < thr - OTHER_Z_MARGIN if z is not None else score < thr - 0.08)
    return {**res, "other": other}


async def warm() -> None:
    """При запуске: модель в память заранее (первое «Джарвис» не ждёт загрузки)."""
    await extractor()


__all__ = ["enroll", "verify", "status", "forget", "calibrate", "warm", "available"]

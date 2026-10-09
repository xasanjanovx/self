"""Через что работает Gemini: AI Studio (его баланс) или Vertex AI ($300 кредитов Google Cloud на 90 дней, 29.09).

Его выбор 29.09: «перевести на Vertex, но через 90 дней кредит кончится — переключать в админских настройках бота».
Кредиты Google Cloud ($300 «приветственные») на Gemini API в AI Studio не тратятся — только на Vertex AI; ключ Vertex —
VERTEX_API_KEY в .env сервера.

Надёжность: запрос к Vertex не вышел (ключ заблокирован, модели там нет, сбой) — тот же запрос сразу идёт в AI Studio, JES не
замолкает. Ключ/доступ сломан — Vertex на паузе 10 минут (не тратим лишний круг на каждый ответ); модели нет — эта модель
всегда через AI Studio. Кредит кончился по сроку — сам возвращаемся на AI Studio.

09.10, его слова: «вообще не используй пока API из Google AI Studio, только Google Cloud Vertex». Пока studio_allowed() == False
(по умолчанию), ничего из этого отката нет: все запросы Gemini — только в Vertex, при сбое — ошибка (с повторами на временных
сбоях), а не тихий уход в AI Studio; бесплатный ключ и переключатель «AI Studio» выключены; срок кредита ничего не переключает.
Вернуть прежнее поведение — ALLOW_AI_STUDIO=1 в .env сервера.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STUDIO_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
VERTEX_BASE = "https://aiplatform.googleapis.com/v1/publishers/google/models"
# живой голос в Vertex — только региональный адрес и полный путь проекта (29.09 проверено: global и europe-west1 — «not found»,
# us-central1 — готов за 0.36 с, первый звук через 0.83 с)
LIVE_LOCATION = os.getenv("VERTEX_LIVE_LOCATION") or "us-central1"
LIVE_URL = "wss://{loc}-aiplatform.googleapis.com/ws/google.cloud.aiplatform.v1.LlmBidiService/BidiGenerateContent"
TRIAL_DAYS = 90
TRIAL_USD = 300.0
VERTEX_DAILY_LIMIT_USD = 3.0      # на кредитах можно больше: живой голос весь день (на балансе AI Studio — $0.5)
KEY_PAUSE_S = 600
# имена моделей в Vertex, если отличаются от AI Studio. 29.09: озвучки 3.8 Flash-Lite TTS в Vertex нет ни в одном регионе —
# он послушал пять образцов и выбрал 3.1 Flash TTS (первый звук 0.57 с)
VERTEX_MODELS: dict[str, str] = {"gemini-3.8-flash-lite-tts": "gemini-3.1-flash-tts-preview"}
STUDIO_ONLY: set[str] = set()

_state: dict[str, Any] | None = None
_paused_until = 0.0
_bad_models: set[str] = set()


def _file() -> Path | None:
    folder = os.getenv("DATA_DIR")
    return Path(folder) / "ai_provider.json" if folder else None


def _load() -> dict[str, Any]:
    global _state
    if _state is None:
        _state = {}
        path = _file()
        if path is not None and path.exists():
            try:
                _state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _state = {}
    return _state


def _save() -> None:
    path = _file()
    if path is None:
        return
    try:
        path.write_text(json.dumps(_load(), ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        logger.warning("gcloud: не сохранил", exc_info=True)


def vertex_key() -> str:
    return (os.getenv("VERTEX_API_KEY") or "").strip()


def studio_allowed() -> bool:
    """Можно ли вообще ходить в Google AI Studio (generativelanguage.googleapis.com). По умолчанию нет — только Vertex (09.10)."""
    return (os.getenv("ALLOW_AI_STUDIO") or "").strip().lower() in {"1", "true", "yes", "on"}


def _today() -> date:
    return datetime.now(timezone(timedelta(hours=5))).date()


def trial_until() -> date:
    st = _load()
    try:
        return date.fromisoformat(str(st["trial_until"]))
    except (KeyError, ValueError):
        return date(2026, 9, 29) + timedelta(days=TRIAL_DAYS)  # он получил кредит 29.09


def chosen() -> str:
    """Что выбрано в настройках (studio | vertex) — без учёта пауз. AI Studio закрыт — всегда vertex."""
    if not studio_allowed():
        return "vertex"
    return "vertex" if _load().get("provider") == "vertex" else "studio"


def active() -> bool:
    """Сейчас идём в Vertex: выбран, есть ключ, кредит не истёк, ключ не на паузе. AI Studio закрыт — просто есть ключ:
    переключаться некуда, поэтому ни срок кредита, ни пауза после сбоя Vertex не отключают (иначе запрос ушёл бы мимо)."""
    if not studio_allowed():
        return bool(vertex_key())
    if chosen() != "vertex" or not vertex_key():
        return False
    if _today() > trial_until():
        st = _load()
        st["provider"], st["expired_at"] = "studio", _today().isoformat()
        _save()
        logger.warning("gcloud: срок кредита Vertex вышел (%s) — обратно на AI Studio", trial_until())
        return False
    return time.monotonic() >= _paused_until


def use_vertex(model: str) -> bool:
    if not studio_allowed():
        return bool(vertex_key())
    return active() and model not in _bad_models and model not in STUDIO_ONLY


def project() -> str:
    """Номер проекта Google Cloud: из VERTEX_PROJECT или узнан по ответу Vertex (кнопка «Проверить Vertex»)."""
    return (os.getenv("VERTEX_PROJECT") or str(_load().get("project") or "")).strip()


def live_ready() -> bool:
    return bool(project()) and use_vertex_live()


def use_vertex_live() -> bool:
    if not studio_allowed():
        return bool(vertex_key())
    return active() and "live" not in _bad_models


def live_url() -> str:
    return LIVE_URL.format(loc=LIVE_LOCATION)


def live_model(model: str) -> str:
    return f"projects/{project()}/locations/{LIVE_LOCATION}/publishers/google/models/{vertex_model(model)}"


def vertex_model(model: str) -> str:
    return VERTEX_MODELS.get(model, model)


def url(model: str, method: str) -> str:
    """generateContent | streamGenerateContent?alt=sse — в Vertex. Знаем проект — путь проекта в global: короткий путь
    уходит в europe-west1, где озвучки нет (29.09 проверено)."""
    if project():
        return f"https://aiplatform.googleapis.com/v1/projects/{project()}/locations/global/publishers/google/models/{vertex_model(model)}:{method}"
    return f"{VERTEX_BASE}/{vertex_model(model)}:{method}"


def headers() -> dict[str, str]:
    return {"x-goog-api-key": vertex_key()}


def set_provider(name: str) -> None:
    global _paused_until
    if not studio_allowed():
        logger.info("gcloud: AI Studio закрыт (ALLOW_AI_STUDIO не включён) — остаёмся на Vertex, выбор %r не применён", name)
        return
    st = _load()
    st["provider"] = "vertex" if name == "vertex" else "studio"
    st["changed_at"] = datetime.now(timezone.utc).isoformat()
    _paused_until = 0.0
    _bad_models.clear()
    _save()
    logger.info("gcloud: Gemini теперь через %s", st["provider"])


def daily_limit() -> float | None:
    """Дневной лимит, пока работаем на кредите Vertex (None — обычный, AI Studio)."""
    if chosen() != "vertex" or not vertex_key():
        return None
    return float(_load().get("vertex_limit") or VERTEX_DAILY_LIMIT_USD)


def set_limit(usd: float) -> None:
    st = _load()
    st["vertex_limit"] = max(0.5, min(10.0, float(usd)))
    _save()


def ok() -> None:
    st = _load()
    if st.get("error"):
        st.pop("error", None)
        st["ok_at"] = datetime.now(timezone.utc).isoformat()
        _save()


def failed(model: str, status: int | None, text: str) -> None:
    """Vertex отказал — запомнить почему (видно в настройках) и решить, надолго ли в обход."""
    global _paused_until
    low = str(text or "").lower()
    fallback = studio_allowed()    # откат в AI Studio закрыт — не помечаем модели «плохими» и не ставим паузу: идти больше некуда
    if model == "live":
        if fallback:
            _bad_models.add("live")
        why = f"живой голос через Vertex не подключился ({low[:120]})" + (" — он идёт через AI Studio" if fallback else "")
    elif status == 404 or "not found" in low or "is not supported" in low:
        if fallback:
            _bad_models.add(model)
        why = f"модели {model} нет в Vertex" + (" — она идёт через AI Studio" if fallback else "")
    elif status in {400, 401, 403} and ("api_key" in low or "permission" in low or "blocked" in low or "disabled" in low
                                         or "billing" in low or "unauthenticated" in low):
        if fallback:
            _paused_until = time.monotonic() + KEY_PAUSE_S
        why = "ключ не пускают в Vertex AI: " + (
            "у сервисного аккаунта ключа нет роли «Vertex AI User» (Agent Platform User)" if "iam_permission_denied" in low
            or "permission 'aiplatform" in low
            else "в Google Cloud у ключа ограничение API — добавьте «Vertex AI API»" if "blocked" in low
            else "включите Vertex AI API в проекте" if "disabled" in low or "has not been used" in low
            else "нет оплаты/кредита на проекте" if "billing" in low else low[:120])
    else:
        why = f"сбой {status}: {low[:120]}"
    logger.warning("gcloud: Vertex не ответил (%s)%s", why, " — этот запрос через AI Studio" if fallback else " — AI Studio закрыт, запрос не выполнен")
    st = _load()
    st["error"], st["error_at"] = why, datetime.now(timezone.utc).isoformat()
    _save()


async def probe(client, model: str = "gemini-3.5-flash-lite") -> tuple[bool, str]:  # noqa: ANN001 — httpx.AsyncClient
    """Кнопка «Проверить Vertex» (29.09): один крошечный запрос (~$0.00001) — пускают ли ключ и есть ли модель."""
    global _paused_until
    if not vertex_key():
        return False, "нет VERTEX_API_KEY на сервере"
    body = {"contents": [{"role": "user", "parts": [{"text": "Ответь одним словом: да"}]}], "generationConfig": {"maxOutputTokens": 5}}
    try:
        response = await client.post(url(model, "generateContent"), json=body, headers=headers())
    except Exception as exc:
        return False, f"сеть: {type(exc).__name__}"
    if response.status_code == 200:
        _paused_until = 0.0
        _bad_models.discard(model)
        ok()
        await ensure_project(client)
        return True, ("Vertex работает: ответы, расшифровка, озвучка" if not studio_allowed() else
                      "Vertex работает: ответы, расшифровка") + (", живой голос" if project() else "") + (
            "" if not studio_allowed() else " · озвучка — через AI Studio")
    failed(model, response.status_code, response.text)
    return False, str(_load().get("error") or response.status_code)


async def ensure_project(client) -> str:  # noqa: ANN001 — httpx.AsyncClient
    """Номер проекта нужен живому голосу — Vertex сам называет его в ответе «модели нет». Уже знаем — ничего не спрашиваем."""
    if not project() and vertex_key():
        body = {"contents": [{"role": "user", "parts": [{"text": "x"}]}], "generationConfig": {"maxOutputTokens": 1}}
        try:
            miss = await client.post(url("jes-no-such-model", "generateContent"), json=body, headers=headers())
            found = re.search(r"projects/(\d+)/", miss.text)
            if found:
                _load()["project"] = found.group(1)
                _save()
        except Exception:
            logger.debug("gcloud: номер проекта не узнал", exc_info=True)
    return project()


def status() -> dict[str, Any]:
    st = _load()
    return {"chosen": chosen(), "active": active(), "ai_studio_allowed": studio_allowed(), "has_key": bool(vertex_key()),
            "trial_until": trial_until().isoformat(),
            "days_left": (trial_until() - _today()).days, "error": st.get("error"), "error_at": st.get("error_at"),
            "limit": daily_limit(), "bad_models": sorted(_bad_models)}


def reset_cache() -> None:
    """Для тестов."""
    global _state, _paused_until
    _state = None
    _paused_until = 0.0
    _bad_models.clear()


__all__ = ["active", "use_vertex", "url", "headers", "set_provider", "failed", "ok", "status", "daily_limit", "chosen", "studio_allowed",
           "ensure_project"]

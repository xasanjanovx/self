"""Через что работает Gemini: AI Studio (его баланс) или Vertex AI ($300 кредитов Google Cloud на 90 дней, 29.09).

Его выбор 29.09: «перевести на Vertex, но через 90 дней кредит кончится — переключать в админских настройках бота».
Кредиты Google Cloud ($300 «приветственные») на Gemini API в AI Studio не тратятся — только на Vertex AI; ключ Vertex —
VERTEX_API_KEY в .env сервера.

Надёжность: запрос к Vertex не вышел (ключ заблокирован, модели там нет, сбой) — тот же запрос сразу идёт в AI Studio, JES не
замолкает. Ключ/доступ сломан — Vertex на паузе 10 минут (не тратим лишний круг на каждый ответ); модели нет — эта модель
всегда через AI Studio. Кредит кончился по сроку — сам возвращаемся на AI Studio.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STUDIO_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
VERTEX_BASE = "https://aiplatform.googleapis.com/v1/publishers/google/models"
VERTEX_LIVE_URL = "wss://aiplatform.googleapis.com/ws/google.cloud.aiplatform.v1.LlmBidiService/BidiGenerateContent"
TRIAL_DAYS = 90
TRIAL_USD = 300.0
VERTEX_DAILY_LIMIT_USD = 3.0      # на кредитах можно больше: живой голос весь день (на балансе AI Studio — $0.5)
KEY_PAUSE_S = 600
# имена моделей в Vertex, если отличаются от AI Studio (заполняется после проверки его ключа)
VERTEX_MODELS: dict[str, str] = {}

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


def _today() -> date:
    return datetime.now(timezone(timedelta(hours=5))).date()


def trial_until() -> date:
    st = _load()
    try:
        return date.fromisoformat(str(st["trial_until"]))
    except (KeyError, ValueError):
        return date(2026, 9, 29) + timedelta(days=TRIAL_DAYS)  # он получил кредит 29.09


def chosen() -> str:
    """Что выбрано в настройках (studio | vertex) — без учёта пауз."""
    return "vertex" if _load().get("provider") == "vertex" else "studio"


def active() -> bool:
    """Сейчас идём в Vertex: выбран, есть ключ, кредит не истёк, ключ не на паузе."""
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
    return active() and model not in _bad_models


def vertex_model(model: str) -> str:
    return VERTEX_MODELS.get(model, model)


def url(model: str, method: str) -> str:
    """generateContent | streamGenerateContent?alt=sse — в Vertex."""
    return f"{VERTEX_BASE}/{vertex_model(model)}:{method}"


def headers() -> dict[str, str]:
    return {"x-goog-api-key": vertex_key()}


def set_provider(name: str) -> None:
    global _paused_until
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
    if status == 404 or "not found" in low or "is not supported" in low:
        _bad_models.add(model)
        why = f"модели {model} нет в Vertex — она идёт через AI Studio"
    elif status in {400, 401, 403} and ("api_key" in low or "permission" in low or "blocked" in low or "disabled" in low
                                         or "billing" in low or "unauthenticated" in low):
        _paused_until = time.monotonic() + KEY_PAUSE_S
        why = "ключ не пускают в Vertex AI: " + (
            "в Google Cloud у ключа ограничение API — добавьте «Vertex AI API»" if "blocked" in low
            else "включите Vertex AI API в проекте" if "disabled" in low or "has not been used" in low
            else "нет оплаты/кредита на проекте" if "billing" in low else low[:120])
    else:
        why = f"сбой {status}: {low[:120]}"
    logger.warning("gcloud: Vertex не ответил (%s) — этот запрос через AI Studio", why)
    st = _load()
    st["error"], st["error_at"] = why, datetime.now(timezone.utc).isoformat()
    _save()


def status() -> dict[str, Any]:
    st = _load()
    return {"chosen": chosen(), "active": active(), "has_key": bool(vertex_key()), "trial_until": trial_until().isoformat(),
            "days_left": (trial_until() - _today()).days, "error": st.get("error"), "error_at": st.get("error_at"),
            "limit": daily_limit(), "bad_models": sorted(_bad_models)}


def reset_cache() -> None:
    """Для тестов."""
    global _state, _paused_until
    _state = None
    _paused_until = 0.0
    _bad_models.clear()


__all__ = ["active", "use_vertex", "url", "headers", "set_provider", "failed", "ok", "status", "daily_limit", "chosen"]

"""Кто заблокировал бота (29.09, он просил: «не надо пользователям, которые заблокировали бота, отправлять сообщения и тратить
токены»). Узнаём двумя путями: Telegram присылает my_chat_member «kicked», или любая отправка падает «bot was blocked by the user».
Таких нет в access.user_ids() — ни сводок (их текст пишет ИИ), ни напоминаний, ни подсказок, ни будильника. Написал боту
снова / разблокировал (my_chat_member «member») — снимаем отметку. Хранится в DATA_DIR/blocked_users.json.
"""
from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger(__name__)

_cache: dict[str, float] | None = None


def _file():  # noqa: ANN202
    from .tg_user import data_dir

    return data_dir() / "blocked_users.json"


def _load() -> dict[str, float]:
    global _cache
    if _cache is None:
        try:
            _cache = {str(k): float(v) for k, v in json.loads(_file().read_text(encoding="utf-8")).items()}
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save() -> None:
    try:
        _file().write_text(json.dumps(_load()), encoding="utf-8")
    except OSError:
        logger.warning("blocked users not saved", exc_info=True)


def is_blocked(uid: int | None) -> bool:
    return uid is not None and str(int(uid)) in _load()


def mark(uid: int, why: str = "") -> None:
    if is_blocked(uid):
        return
    _load()[str(int(uid))] = time.time()
    _save()
    logger.info("blocked: %s заблокировал бота (%s) — больше ему не пишем", uid, why or "?")


def unmark(uid: int) -> None:
    if not is_blocked(uid):
        return
    _load().pop(str(int(uid)), None)
    _save()
    logger.info("blocked: %s снова с ботом", uid)


def ids() -> set[int]:
    return {int(k) for k in _load()}


def reset_cache() -> None:
    global _cache
    _cache = None


__all__ = ["is_blocked", "mark", "unmark", "ids", "reset_cache"]

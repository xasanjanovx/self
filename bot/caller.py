"""Звонок в Telegram от аккаунта-помощника (userbot) — Telethon + pytgcalls.

Обычный бот звонить не умеет, поэтому звонит отдельный аккаунт «Джарвис»: у тебя
на экране обычный входящий Telegram-звонок, в трубке — синтезированный голос.

Включается, только если в .env заданы TG_CALLER_API_ID / TG_CALLER_API_HASH /
TG_CALLER_SESSION и установлены зависимости (requirements-caller.txt). Иначе
`available()` = False и бот будит сообщениями — ничего не падает.

Сессию получают один раз скриптом `scripts/tg_login.py` (код из Telegram вводит
сам пользователь, нам он не нужен).
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_client: Any = None
_calls: Any = None
_lock = asyncio.Lock()
_import_error: str | None = None


def configured() -> bool:
    return bool(os.getenv("TG_CALLER_API_ID") and os.getenv("TG_CALLER_API_HASH") and os.getenv("TG_CALLER_SESSION"))


def available() -> bool:
    return configured() and _import_error is None


def status() -> dict[str, Any]:
    return {"configured": configured(), "started": _calls is not None, "error": _import_error}


async def start() -> bool:
    """Поднять userbot. Возвращает False, если не настроен или не установлены зависимости."""
    global _client, _calls, _import_error
    if not configured():
        return False
    if _calls is not None:
        return True
    async with _lock:
        if _calls is not None:
            return True
        try:
            from pytgcalls import PyTgCalls  # type: ignore
            from telethon import TelegramClient  # type: ignore
            from telethon.sessions import StringSession  # type: ignore
        except Exception as exc:  # зависимости не поставлены — работаем без звонков
            _import_error = f"{type(exc).__name__}: {exc}"
            logger.warning("caller disabled: %s", _import_error)
            return False
        try:
            _client = TelegramClient(StringSession(os.environ["TG_CALLER_SESSION"]), int(os.environ["TG_CALLER_API_ID"]), os.environ["TG_CALLER_API_HASH"])
            await _client.start()
            _calls = PyTgCalls(_client)
            await _calls.start()
            me = await _client.get_me()
            logger.info("caller started as @%s (id=%s)", getattr(me, "username", None), getattr(me, "id", None))
            return True
        except Exception as exc:
            _import_error = f"{type(exc).__name__}: {exc}"
            logger.exception("caller start failed")
            _client = _calls = None
            return False


async def stop() -> None:
    global _client, _calls
    try:
        if _calls is not None:
            await _calls.stop()
    except Exception:
        logger.debug("calls stop failed", exc_info=True)
    try:
        if _client is not None:
            await _client.disconnect()
    except Exception:
        logger.debug("client disconnect failed", exc_info=True)
    _client = _calls = None


async def call(user_id: int, audio_path: str, *, ring_seconds: int = 45, play_seconds: int = 40) -> dict[str, Any]:
    """Позвонить и проиграть файл. Возвращает {'answered': bool, 'error': str|None}.

    Телеграм звонит, пока не возьмут трубку или не истечёт `ring_seconds`; если
    трубку взяли — проигрываем аудио и кладём трубку.
    """
    if not await start():
        return {"answered": False, "error": _import_error or "not configured"}
    try:
        from pytgcalls.types import CallConfig  # type: ignore
    except Exception as exc:
        return {"answered": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        await _calls.play(int(user_id), audio_path, config=CallConfig(timeout=ring_seconds, auto_start=True))
    except Exception as exc:
        name = type(exc).__name__
        # частые случаи: не взяли трубку / отклонили / занято — это не ошибка, просто «не ответил»
        if any(k in name.lower() for k in ("timeout", "discarded", "busy", "declined")):
            return {"answered": False, "error": None}
        logger.warning("call to %s failed: %s", user_id, exc)
        return {"answered": False, "error": f"{name}: {exc}"}
    # трубку взяли: даём доиграть и вешаем
    await asyncio.sleep(max(5, play_seconds))
    try:
        await _calls.leave_call(int(user_id))
    except Exception:
        logger.debug("leave_call failed", exc_info=True)
    return {"answered": True, "error": None}


async def send_message(user_id: int, text: str) -> bool:
    """Сообщение от лица «Джарвиса» (например, если звонок не прошёл)."""
    if not await start():
        return False
    try:
        await _client.send_message(int(user_id), text)
        return True
    except Exception:
        logger.warning("caller send_message failed", exc_info=True)
        return False


__all__ = ["available", "configured", "status", "start", "stop", "call", "send_message"]

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


# ------------------------------------------------------------------ живой разговор
_incoming: dict[int, "asyncio.Queue[bytes]"] = {}
_hooked = False


def _hook_updates() -> None:
    """Подписка на входящие аудиокадры звонка (один раз на процесс)."""
    global _hooked
    if _hooked or _calls is None:
        return
    try:
        from pytgcalls.types import Direction, StreamFrames  # type: ignore
    except Exception:
        return

    @_calls.on_update()
    async def _on_update(_client, update):  # noqa: ANN001
        try:
            if isinstance(update, StreamFrames) and update.direction == Direction.INCOMING:
                queue = _incoming.get(int(update.chat_id))
                if queue is None:
                    return
                for frame in update.frames:
                    queue.put_nowait(bytes(frame.frame))
        except Exception:
            logger.debug("frame hook failed", exc_info=True)

    _hooked = True


async def talk(user_id: int, *, greeting_pcm: bytes, on_utterance, ring_seconds: int = 45,
               max_seconds: int = 180) -> dict[str, Any]:
    """Позвонить и РАЗГОВАРИВАТЬ: проигрываем фразу, слушаем ответ, отвечаем.

    `on_utterance(pcm | None)` — колбэк уровня бота: получает записанную фразу
    собеседника (или None, если тот молчит) и возвращает {'pcm': bytes|None, 'stop': bool}.
    Возвращает {'answered': bool, 'error': str|None}.
    """
    from . import call_dialog as cd

    if not await start():
        return {"answered": False, "error": _import_error or "not configured"}
    try:
        from pytgcalls.types import AudioQuality, CallConfig, RecordStream  # type: ignore
    except Exception as exc:
        return {"answered": False, "error": f"{type(exc).__name__}: {exc}"}

    uid = int(user_id)
    _hook_updates()
    queue: asyncio.Queue[bytes] = asyncio.Queue()
    _incoming[uid] = queue
    path = await cd.pcm_to_file(greeting_pcm)
    try:
        try:
            await _calls.play(uid, path, config=CallConfig(timeout=ring_seconds, auto_start=True))
        except Exception as exc:
            name = type(exc).__name__.lower()
            if any(k in name for k in ("timeout", "discarded", "busy", "declined", "notanswer")):
                return {"answered": False, "error": None}
            return {"answered": False, "error": f"{type(exc).__name__}: {exc}"}
        # трубку взяли
        try:
            await _calls.record(uid, RecordStream(audio=True, audio_parameters=AudioQuality.LOW))
        except Exception:
            logger.warning("record() failed — разговор без слуха", exc_info=True)
        buffer = cd.VoiceBuffer()
        deadline = asyncio.get_running_loop().time() + max_seconds
        silence_rounds = 0
        while asyncio.get_running_loop().time() < deadline:
            utterance: bytes | None = None
            try:
                while True:
                    chunk = await asyncio.wait_for(queue.get(), timeout=1.0)
                    utterance = buffer.feed(chunk)
                    if utterance:
                        break
            except asyncio.TimeoutError:
                utterance = None
            if utterance is None:
                silence_rounds += 1
                if silence_rounds < 6:  # ~6 секунд тишины — ещё ждём
                    continue
                silence_rounds = 0
            else:
                silence_rounds = 0
            result = await on_utterance(utterance)
            reply_pcm = (result or {}).get("pcm")
            if reply_pcm:
                say_path = await cd.pcm_to_file(reply_pcm)
                if say_path:
                    try:
                        await _calls.play(uid, say_path)
                        await asyncio.sleep(min(30, max(1, cd.ms_of(reply_pcm) / 1000 + 0.6)))
                    except Exception:
                        logger.debug("play reply failed", exc_info=True)
                    finally:
                        cd.cleanup(say_path)
            if (result or {}).get("stop"):
                break
        return {"answered": True, "error": None}
    finally:
        _incoming.pop(uid, None)
        cd.cleanup(path)
        try:
            await _calls.leave_call(uid)
        except Exception:
            logger.debug("leave_call failed", exc_info=True)


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


__all__ = ["available", "configured", "status", "start", "stop", "call", "talk", "send_message"]

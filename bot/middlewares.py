"""Middlewares: доступ только владельцу, защита от двойных нажатий, глобальный error handler."""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import CallbackQuery, ErrorEvent, Message, TelegramObject

from .config import Settings

logger = logging.getLogger(__name__)


class AccessMiddleware(BaseMiddleware):
    """Личный бот: пускаем только ALLOWED_TELEGRAM_IDS. Чужим отвечаем один раз в час."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._notified: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        uid = user.id if user else None
        if self.settings.is_allowed(uid):
            return await handler(event, data)

        now = time.monotonic()
        if uid is not None and now - self._notified.get(uid, 0.0) > 3600:
            self._notified[uid] = now
            text = "⛔ Bu shaxsiy bot. / Это личный бот."
            try:
                if isinstance(event, Message):
                    await event.answer(text)
                elif isinstance(event, CallbackQuery):
                    await event.answer(text, show_alert=True)
            except Exception:
                pass
        elif isinstance(event, CallbackQuery):
            try:
                await event.answer()
            except Exception:
                pass
        logger.info("Access denied for user %s", uid)
        return None


class DedupeMiddleware(BaseMiddleware):
    """Гасит повторное нажатие той же кнопки в течение `window` секунд
    (иначе двойной тап по «быстрой» кнопке создаёт две операции)."""

    def __init__(self, window: float = 1.2) -> None:
        self.window = float(window)
        self._last: dict[int, tuple[str, float]] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, CallbackQuery) or not event.from_user:
            return await handler(event, data)
        uid = event.from_user.id
        cb = event.data or ""
        now = time.monotonic()
        last = self._last.get(uid)
        if last and last[0] == cb and now - last[1] < self.window:
            try:
                await event.answer()
            except Exception:
                pass
            logger.debug("Dropped duplicate callback %s from %s", cb, uid)
            return None
        self._last[uid] = (cb, now)
        return await handler(event, data)


async def global_error_handler(event: ErrorEvent) -> bool:
    exc = event.exception
    update = event.update

    if isinstance(exc, TelegramRetryAfter):
        logger.warning("Flood control: retry after %.1fs", float(exc.retry_after))
        return True
    if isinstance(exc, TelegramForbiddenError):
        logger.info("User blocked the bot (update id=%s)", getattr(update, "update_id", None))
        return True
    if isinstance(exc, TelegramBadRequest):
        msg = str(exc).lower()
        if any(s in msg for s in ("message is not modified", "message to delete not found", "message can't be deleted", "query is too old")):
            return True
        logger.warning("Telegram bad request: %s", exc)
        return True

    logger.exception("Unhandled handler error", exc_info=exc)
    try:
        msg = getattr(update, "message", None)
        cb = getattr(update, "callback_query", None)
        target = msg.from_user if msg is not None else (cb.from_user if cb is not None else None)
        lang_code = (getattr(target, "language_code", "") or "").lower() if target else ""
        text = (
            "⚠️ Nimadir xato ketdi. Bir daqiqadan keyin qayta urinib koʻring."
            if lang_code.startswith("uz")
            else "⚠️ Что-то пошло не так. Попробуй ещё раз через минуту."
        )
        if msg is not None:
            await msg.answer(text)
        elif cb is not None:
            try:
                await cb.answer(text, show_alert=True)
            except Exception:
                if cb.message is not None:
                    await cb.message.answer(text)
    except Exception:
        pass
    return True


__all__ = ["AccessMiddleware", "DedupeMiddleware", "global_error_handler"]

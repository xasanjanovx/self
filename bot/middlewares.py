"""Middlewares: throttling + global error handler."""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import CallbackQuery, ErrorEvent, Message, TelegramObject

logger = logging.getLogger(__name__)


# ----------------------- Throttling -----------------------
class ThrottleMiddleware(BaseMiddleware):
    """Очень дешёвый rate-limiter: не более 1 апдейта раз в `rate` секунд от юзера.

    Не использует Redis — держит in-memory словарь. Этого хватает для 1-replica
    деплоя на Railway, который у проекта сейчас (`numReplicas: 1`).
    """

    def __init__(self, rate: float = 0.4, hot_rate: float | None = None, hot_callback_prefixes: tuple[str, ...] = ()) -> None:
        self.rate = float(rate)
        # Для callback-кнопок ставим чуть мягче (юзеры быстро жмут)
        self.hot_rate = float(hot_rate) if hot_rate is not None else max(0.2, self.rate / 2)
        self.hot_prefixes = hot_callback_prefixes
        self._last_message: dict[int, float] = defaultdict(float)
        self._last_callback: dict[int, float] = defaultdict(float)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        now = time.monotonic()
        if isinstance(event, Message):
            uid = event.from_user.id if event.from_user else None
            if uid is None:
                return await handler(event, data)
            if now - self._last_message[uid] < self.rate:
                logger.debug("Throttled message from %s", uid)
                return
            self._last_message[uid] = now
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            uid = event.from_user.id if event.from_user else None
            if uid is None:
                return await handler(event, data)
            cb_data = event.data or ""
            rate = self.hot_rate if any(cb_data.startswith(p) for p in self.hot_prefixes) else self.rate
            if now - self._last_callback[uid] < rate:
                # тихо подтверждаем кнопку чтобы у юзера не висел "loading"
                try:
                    await event.answer()
                except Exception:
                    pass
                logger.debug("Throttled callback from %s (data=%s)", uid, cb_data)
                return
            self._last_callback[uid] = now
            return await handler(event, data)

        return await handler(event, data)


# ----------------------- Global error handler -----------------------
async def global_error_handler(event: ErrorEvent) -> bool:
    """Регистрируется через `dp.errors.register(global_error_handler)`.

    Любая необработанная ошибка из handler'а попадает сюда. Логируем с traceback
    и (если применимо) тихо отвечаем юзеру — без раскрытия деталей исключения.
    """
    exc = event.exception
    update = event.update

    if isinstance(exc, TelegramRetryAfter):
        # aiogram сам ставит на паузу — нам только логировать
        logger.warning("Flood control: retry after %.1fs", float(exc.retry_after))
        return True

    if isinstance(exc, TelegramForbiddenError):
        logger.info("User blocked the bot (update id=%s)", getattr(update, "update_id", None))
        return True

    if isinstance(exc, TelegramBadRequest):
        msg = str(exc).lower()
        if (
            "message is not modified" in msg
            or "message to delete not found" in msg
            or "message can't be deleted" in msg
            or "query is too old" in msg
        ):
            return True
        logger.warning("Telegram bad request: %s", exc)
        return True

    logger.exception("Unhandled handler error", exc_info=exc)

    # Best-effort уведомление юзера на его языке (берём из Telegram language_code)
    text_ru = "⚠️ Что-то пошло не так. Попробуй ещё раз через минуту."
    text_uz = "⚠️ Nimadir xato ketdi. Bir daqiqadan keyin qayta urinib koʻring."
    try:
        msg = getattr(update, "message", None)
        cb = getattr(update, "callback_query", None)
        target_user = None
        if msg is not None:
            target_user = msg.from_user
        elif cb is not None:
            target_user = cb.from_user
        lang_code = (getattr(target_user, "language_code", "") or "").lower() if target_user else ""
        text = text_uz if lang_code.startswith("uz") else text_ru
        if msg is not None:
            await msg.answer(text)
        elif cb is not None:
            try:
                await cb.answer(text, show_alert=False)
            except Exception:
                if cb.message is not None:
                    await cb.message.answer(text)
    except Exception:
        # уже путь восстановления — больше не пытаемся
        pass

    return True


__all__ = ["ThrottleMiddleware", "global_error_handler"]

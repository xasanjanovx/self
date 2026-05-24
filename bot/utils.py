"""Утилиты: безопасные обёртки для handler'ов и общие helper'ы."""
from __future__ import annotations

import functools
import logging
from typing import Any, Awaitable, Callable

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import CallbackQuery, Message

logger = logging.getLogger(__name__)


# Тексты для отправки юзеру при падении (двуязычные)
_FALLBACK_MESSAGES = {
    "ru": "⚠️ Что-то пошло не так. Попробуй ещё раз через минуту.",
    "uz": "⚠️ Nimadir xato ketdi. Bir daqiqadan keyin qayta urinib koʻring.",
}


def _user_lang_guess(event: Any) -> str:
    """Best-effort определение языка для fallback-сообщения.

    Не лезем в БД — это код обработки ошибок, лезть в Supabase здесь опасно
    (если падение было как раз из-за БД). Берём из language_code Telegram.
    """
    try:
        code = (event.from_user.language_code or "").lower() if getattr(event, "from_user", None) else ""
    except Exception:
        code = ""
    return "uz" if code.startswith("uz") else "ru"


async def _try_notify(event: Any, text: str) -> None:
    """Тихо пытаемся ответить юзеру. Без эскалаций — это уже путь восстановления."""
    try:
        if isinstance(event, CallbackQuery):
            try:
                await event.answer(text, show_alert=False)
            except Exception:
                pass
            if event.message is not None:
                try:
                    await event.message.answer(text)
                except Exception:
                    pass
        elif isinstance(event, Message):
            try:
                await event.answer(text)
            except Exception:
                pass
    except Exception:
        pass


def safe_handler(
    func: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    """Логирует любое необработанное исключение в хендлере и уведомляет юзера.

    Поведение:
    - `TelegramRetryAfter` — пробрасываем (aiogram сам обрабатывает flood control)
    - `TelegramForbiddenError` — юзер заблокировал бота, тихо игнорируем
    - `TelegramBadRequest` про "message is not modified" / "message to delete not found" — игнорируем
    - Всё остальное — log.exception + fallback-сообщение юзеру
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except TelegramRetryAfter:
            raise
        except TelegramForbiddenError as exc:
            logger.info("User blocked the bot in %s: %s", func.__name__, exc)
            return None
        except TelegramBadRequest as exc:
            msg = str(exc).lower()
            if "message is not modified" in msg or "message to delete not found" in msg or "message can't be deleted" in msg:
                return None
            logger.warning("Telegram bad request in %s: %s", func.__name__, exc)
            return None
        except Exception:
            logger.exception("Unhandled error in handler %s", func.__name__)
            event = args[0] if args else None
            if isinstance(event, (Message, CallbackQuery)):
                lang = _user_lang_guess(event)
                await _try_notify(event, _FALLBACK_MESSAGES.get(lang, _FALLBACK_MESSAGES["ru"]))
            return None

    return wrapper


__all__ = ["safe_handler"]

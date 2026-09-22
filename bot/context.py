"""Синглтоны приложения: настройки, БД, AI. Подключение БД — в on_startup."""
from __future__ import annotations

from .ai import AIService
from .config import load_settings
from .db import Database

settings = load_settings()
db = Database(settings)
ai = AIService(settings)

# Экземпляр aiogram.Bot ставится в on_startup — нужен фоновым задачам и инструментам агента,
# которые отправляют сообщения вне обработчика (подъём, проактивные подсказки).
_bot: object | None = None


def set_bot(bot: object) -> None:
    global _bot
    _bot = bot


def bot_instance():
    if _bot is None:
        raise RuntimeError("Bot instance is not set yet")
    return _bot


__all__ = ["settings", "db", "ai", "set_bot", "bot_instance"]

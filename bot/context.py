"""Синглтоны приложения: настройки, БД, AI. Подключение БД — в on_startup."""
from __future__ import annotations

from .ai import AIService
from .config import load_settings
from .db import Database

settings = load_settings()
db = Database(settings)
ai = AIService(settings)

__all__ = ["settings", "db", "ai"]

"""Роутеры бота. Порядок важен: inbox (fallback) — последним."""
from __future__ import annotations

from aiogram import Router

from . import agent, analytics, assistant, finance, finance_extra, inbox, menu, nutrition, settings, vacancy


def build_router() -> Router:
    root = Router(name="root")
    root.include_router(menu.router)
    root.include_router(nutrition.router)
    root.include_router(finance.router)
    root.include_router(finance_extra.router)
    root.include_router(settings.router)
    root.include_router(assistant.router)
    root.include_router(agent.router)
    root.include_router(vacancy.router)
    root.include_router(analytics.router)
    root.include_router(inbox.router)
    return root


__all__ = ["build_router"]

"""Роутеры бота. Порядок важен: inbox (fallback) — последним."""
from __future__ import annotations

from aiogram import Router

from . import agent, analytics, assistant, bank, daily, plan, finance, finance_extra, inbox, members, membership, menu, nutrition, screentime, settings, vacancy, vacancy_feed, wake


def build_router() -> Router:
    root = Router(name="root")
    root.include_router(membership.router)  # 29.09: заблокировал бота — ничего не шлём
    root.include_router(menu.router)
    root.include_router(nutrition.router)
    root.include_router(finance.router)
    root.include_router(finance_extra.router)
    root.include_router(settings.router)
    root.include_router(members.router)
    root.include_router(assistant.router)
    root.include_router(wake.router)
    root.include_router(agent.router)
    root.include_router(vacancy.router)
    root.include_router(vacancy_feed.router)  # 07.10: автоподбор вакансий из чужих каналов (только владелец)
    root.include_router(analytics.router)
    root.include_router(bank.router)  # 28.09: «Записать трату из SMS банка?» (до inbox — он последний)
    root.include_router(daily.router)  # 29.09: кнопки «каждый день»
    root.include_router(plan.router)  # 30.09: кнопки плана дня и вечернего разбора
    root.include_router(screentime.router)  # 04.10: экранное время — лимиты, «я работаю»
    root.include_router(inbox.router)
    return root


__all__ = ["build_router"]

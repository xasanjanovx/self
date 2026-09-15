"""Точка входа: сборка бота, middlewares, роутеры, фоновые задачи."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from . import screen as screen_mod
from .context import ai, db, settings
from .handlers import build_router
from .middlewares import AccessMiddleware, DedupeMiddleware, global_error_handler
from .workers import brief_worker, report_worker

logger = logging.getLogger(__name__)
background_tasks: list[asyncio.Task[Any]] = []


async def on_startup(bot: Bot) -> None:
    await db.connect()
    missing = await db.health_check()
    if missing:
        logger.error("Missing tables: %s — run sql/schema.sql and sql/migrations/* in Supabase", ", ".join(missing))
    screen_mod.configure_persistence(load=db.get_screen_message_id, save=db.set_screen_message_id)
    try:
        await ai.ensure_models()
    except Exception:
        logger.exception("Failed to resolve Gemini models at startup")
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="menu", description="Главное меню / Asosiy menyu"),
                BotCommand(command="vacancy", description="Оформить вакансию / Vakansiya"),
                BotCommand(command="dashboard", description="Аналитика / Tahlil"),
                BotCommand(command="help", description="Помощь / Yordam"),
            ]
        )
    except Exception:
        logger.warning("set_my_commands failed", exc_info=True)
    background_tasks.append(asyncio.create_task(report_worker(bot), name="report-worker"))
    background_tasks.append(asyncio.create_task(brief_worker(bot), name="brief-worker"))
    logger.info("Bot started. Allowed users: %s", sorted(settings.allowed_telegram_ids) or "everyone")


async def on_shutdown() -> None:
    for task in background_tasks:
        task.cancel()
    for task in background_tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    background_tasks.clear()
    await ai.close()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    # httpx логирует каждый запрос на INFO — это шум
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    bot = Bot(token=settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = Dispatcher()

    access = AccessMiddleware(settings)
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.callback_query.middleware(DedupeMiddleware(window=1.2))
    dp.errors.register(global_error_handler)

    dp.include_router(build_router())
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())

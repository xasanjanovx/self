"""Точка входа: сборка бота, middlewares, роутеры, фоновые задачи."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.methods import (AnswerCallbackQuery, EditMessageCaption, EditMessageText, SendDocument, SendMessage, SendPhoto,
                             SendVoice, TelegramMethod)
from aiogram.types import BotCommand

from . import emoji as pe
from . import i18n
from . import screen as screen_mod
from .context import ai, db, settings
from .handlers import build_router
from .middlewares import AccessMiddleware, DedupeMiddleware, global_error_handler
from .workers import brief_worker, proactive_worker, reminder_worker, report_worker, wake_worker

logger = logging.getLogger(__name__)
background_tasks: list[asyncio.Task[Any]] = []


async def _localize(method: TelegramMethod[Any]) -> None:
    """Английский интерфейс: текст/подпись/кнопки → английский (bot/i18n.py). Для остальных языков
    только убираем служебные маркеры KEEP."""
    from aiogram.types import InlineKeyboardMarkup

    if isinstance(method, AnswerCallbackQuery):
        uid = i18n.callback_user(method.callback_query_id)
        if uid and i18n.lang_of(uid) == "en" and i18n.needs(method.text):
            method.text = await i18n.translate(method.text)
        return
    field = "text" if isinstance(method, (SendMessage, EditMessageText)) else (
        "caption" if isinstance(method, (SendPhoto, SendDocument, SendVoice, EditMessageCaption)) else None)
    value = getattr(method, field, None) if field else None
    lang = i18n.lang_of(getattr(method, "chat_id", None))
    if lang != "en":
        if field and value:
            setattr(method, field, i18n.strip_keep(value))
        return
    markup = getattr(method, "reply_markup", None)
    buttons = [b for row in markup.inline_keyboard for b in row] if isinstance(markup, InlineKeyboardMarkup) else []
    texts = [value] + [b.text for b in buttons]
    out = await i18n.translate_many(texts)
    if field and value:
        setattr(method, field, out[0])
    for button, text in zip(buttons, out[1:]):
        if text and text != button.text:
            button.text = text[:64]


class PremiumBot(Bot):
    """Переводит интерфейс для английского и подменяет обычные эмодзи на премиум во всех исходящих текстах."""

    async def __call__(self, method: TelegramMethod[Any], request_timeout: int | None = None) -> Any:
        try:
            await _localize(method)
        except Exception:
            logger.warning("localize failed", exc_info=True)
        try:
            if isinstance(method, (SendMessage, EditMessageText)) and method.text:
                method.text = pe.premiumize(method.text)
            elif isinstance(method, (SendPhoto, SendDocument, SendVoice, EditMessageCaption)) and method.caption:
                method.caption = pe.premiumize(method.caption)
        except Exception:
            logger.debug("premiumize failed", exc_info=True)
        return await super().__call__(method, request_timeout=request_timeout)


async def on_startup(bot: Bot) -> None:
    from .context import set_bot

    set_bot(bot)
    await db.connect()
    missing = await db.health_check()
    if missing:
        logger.error("Missing tables: %s — run sql/schema.sql and sql/migrations/* in Supabase", ", ".join(missing))
    screen_mod.configure_persistence(load=db.get_screen_message_id, save=db.set_screen_message_id)
    if db.available("ephemeral_messages"):
        screen_mod.configure_trash(db)
    from . import access

    await access.refresh(force=True)
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
        await bot.set_my_commands(
            [BotCommand(command="menu", description="Main menu"), BotCommand(command="vacancy", description="Format a job post"),
             BotCommand(command="dashboard", description="Analytics"), BotCommand(command="help", description="Help")],
            language_code="en",
        )
    except Exception:
        logger.warning("set_my_commands failed", exc_info=True)
    background_tasks.append(asyncio.create_task(report_worker(bot), name="report-worker"))
    background_tasks.append(asyncio.create_task(brief_worker(bot), name="brief-worker"))
    background_tasks.append(asyncio.create_task(reminder_worker(bot), name="reminder-worker"))
    background_tasks.append(asyncio.create_task(proactive_worker(bot), name="proactive-worker"))
    background_tasks.append(asyncio.create_task(wake_worker(bot), name="wake-worker"))
    try:
        from . import phone_api

        await phone_api.start()  # голосовой Джарвис на телефоне (нужен JARVIS_TOKEN)
    except Exception:
        logger.exception("phone api failed to start")
    logger.info("Bot started. Owner: %s, members: %s", sorted(settings.allowed_telegram_ids) or "everyone", len(access.user_ids()) - len(settings.allowed_telegram_ids))


async def on_shutdown() -> None:
    for task in background_tasks:
        task.cancel()
    for task in background_tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    background_tasks.clear()
    from . import caller, phone_api

    await caller.stop()
    try:
        await phone_api.stop()
    except Exception:
        logger.warning("phone api stop failed", exc_info=True)
    await ai.close()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    # httpx логирует каждый запрос на INFO — это шум
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    bot = PremiumBot(token=settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
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

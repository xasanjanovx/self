"""Посты в канале вакансий: мгновенная реакция охраны ленты (07.10).

Если Telegram присылает боту посты канала (бот — админ), разбираем их сразу, не дожидаясь чтения канала аккаунтом JES
(bot/channel_guard.py: poll — запасной путь раз в пару минут). Один и тот же пост дважды не разбирается (guard_done).
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Router
from aiogram.types import Message

from .. import channel_guard as guard
from ..context import ai, settings

router = Router(name="channel")
logger = logging.getLogger(__name__)

SETTLE_S = 3.0        # свои публикации бот записывает как «свои» уже после отправки — даём ему секунды


def is_our_channel(message: Message) -> bool:
    channel = str(settings.vacancy_channel or "").lstrip("@").lower()
    if not channel:
        return False
    chat = message.chat
    return (getattr(chat, "username", None) or "").lower() == channel or str(chat.id) == channel


@router.channel_post()
async def on_channel_post(message: Message) -> None:
    if not is_our_channel(message):
        return
    await asyncio.sleep(SETTLE_S)
    try:
        await guard.handle_post(message.bot, {"id": message.message_id, "text": message.text or message.caption or "",
                                              "ts": message.date.timestamp()}, ai=ai)
    except Exception:
        logger.exception("channel_guard: пост %s не разобран", message.message_id)

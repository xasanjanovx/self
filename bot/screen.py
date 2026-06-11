"""Single "live screen" message management.

Keeps the chat clean by maintaining one main screen message that is edited in
place during navigation, while transient messages (hints, AI answers, reminders,
charts, vacancy posts) are auto-removed so they don't accumulate.

State is kept in-memory per chat. The bot runs as a single Railway replica, so
this is sufficient; after a restart old ids are simply forgotten (those messages
just won't be auto-cleaned, which is harmless).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from aiogram import Bot

logger = logging.getLogger(__name__)

# chat_id -> message_id of the main editable screen (dashboard / panel)
_screen: dict[int, int] = {}
# chat_id -> transient message ids to delete on next interaction
_ephemerals: dict[int, list[int]] = defaultdict(list)
# chat_id -> last chart (photo) message id; we keep at most one alive
_chart: dict[int, int] = {}
# chat_id -> last reminder message id; keep at most one alive
_reminder: dict[int, int] = {}


async def _safe_delete(bot: Bot, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


def track_screen(chat_id: int, message_id: int | None) -> None:
    """Remember a message (e.g. one edited by a callback) as the live screen.

    Also removes it from the ephemeral list so it is not deleted as transient
    (e.g. when a callback button lives on a previously-ephemeral message).
    """
    if not message_id:
        return
    _screen[chat_id] = message_id
    pending = _ephemerals.get(chat_id)
    if pending and message_id in pending:
        pending.remove(message_id)


async def clear_ephemerals(bot: Bot, chat_id: int) -> None:
    for message_id in _ephemerals.pop(chat_id, []):
        await _safe_delete(bot, chat_id, message_id)


async def show_screen(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: Any | None = None,
) -> int:
    """Render the main screen: edit the existing one in place when possible,
    otherwise replace it. Clears any transient messages first."""
    await clear_ephemerals(bot, chat_id)

    old = _screen.get(chat_id)
    if old:
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=old, text=text, reply_markup=reply_markup
            )
            return old
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return old
            # Not editable (deleted, or was a media message) -> replace it.
            await _safe_delete(bot, chat_id, old)

    msg = await bot.send_message(chat_id, text, reply_markup=reply_markup)
    _screen[chat_id] = msg.message_id
    return msg.message_id


async def send_ephemeral(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: Any | None = None,
) -> int:
    """Send a transient message that will be removed on the next interaction."""
    await clear_ephemerals(bot, chat_id)
    msg = await bot.send_message(chat_id, text, reply_markup=reply_markup)
    _ephemerals[chat_id].append(msg.message_id)
    return msg.message_id


async def send_chart(
    bot: Bot,
    chat_id: int,
    photo: Any,
    *,
    caption: str | None = None,
    reply_markup: Any | None = None,
) -> int:
    """Send a chart image, keeping at most one chart alive per chat."""
    await _safe_delete(bot, chat_id, _chart.pop(chat_id, None))
    msg = await bot.send_photo(chat_id, photo, caption=caption, reply_markup=reply_markup)
    _chart[chat_id] = msg.message_id
    return msg.message_id


async def send_reminder(bot: Bot, chat_id: int, text: str) -> int:
    """Send a reminder, removing the previous reminder so they don't pile up."""
    await _safe_delete(bot, chat_id, _reminder.pop(chat_id, None))
    msg = await bot.send_message(chat_id, text)
    _reminder[chat_id] = msg.message_id
    return msg.message_id


async def drop_message(message: Any) -> None:
    """Delete a user's incoming message (command, operation, photo, voice)."""
    try:
        await message.delete()
    except Exception:
        pass

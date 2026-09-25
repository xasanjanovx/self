"""Single "live screen" message management.

Keeps the chat clean by maintaining one main screen message that is edited in
place during navigation, while transient messages (hints, AI answers, charts,
vacancy prompts) are auto-removed so they don't accumulate.

State is kept in-memory per chat (single process). The screen message id is
additionally persisted through optional load/save callbacks (sync or async) so
the menu survives restarts without duplicates.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot

logger = logging.getLogger(__name__)

# chat_id -> message_id of the main editable screen (dashboard / panel)
_screen: dict[int, int] = {}
# chat_id -> transient message ids to delete on next interaction
_ephemerals: dict[int, list[int]] = defaultdict(list)
# chat_id -> last chart (photo) message id; we keep at most one alive
_chart: dict[int, int] = {}

# load(chat_id) -> int | None ; save(chat_id, message_id) -> None  (sync or async)
_load_screen = None
_save_screen = None
_loaded: set[int] = set()

# Журнал временных сообщений в БД (010): переживает перезапуск бота, иначе «Звоню»,
# «Не дозвонился» и т.п. оставались в чате навсегда, если бот перезапускался до таймера.
# Объект с async add(chat_id, message_id, delete_at) / list_ephemerals(chat_id=, due_before=) / drop_ephemerals(chat_id, ids).
_trash = None
_trash_loaded: set[int] = set()
EPHEMERAL_MAX = timedelta(hours=24)  # без ttl — удалим на следующем действии или через сутки
_bg: set[asyncio.Task] = set()       # держим ссылки на фоновые задачи, иначе их может съесть GC


def configure_persistence(load=None, save=None) -> None:
    global _load_screen, _save_screen
    _load_screen = load
    _save_screen = save


def configure_trash(store) -> None:  # noqa: ANN001
    global _trash
    _trash = store


def _spawn(coro) -> None:  # noqa: ANN001
    task = asyncio.ensure_future(coro)
    _bg.add(task)
    task.add_done_callback(_bg.discard)


async def _remember(chat_id: int, message_id: int, ttl: float | None) -> None:
    if _trash is None:
        return
    try:
        at = datetime.now(timezone.utc) + (timedelta(seconds=ttl) if ttl else EPHEMERAL_MAX)
        await _trash.add_ephemeral(chat_id, message_id, at)
    except Exception:
        logger.debug("ephemeral persist failed", exc_info=True)


async def _forget(chat_id: int, ids: list[int]) -> None:
    if _trash is None or not ids:
        return
    try:
        await _trash.drop_ephemerals(chat_id, ids)
    except Exception:
        logger.debug("ephemeral forget failed", exc_info=True)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _get_screen(chat_id: int) -> int | None:
    if chat_id in _screen:
        return _screen[chat_id]
    if _load_screen is not None and chat_id not in _loaded:
        _loaded.add(chat_id)
        try:
            mid = await _maybe_await(_load_screen(chat_id))
        except Exception:
            mid = None
        if mid:
            _screen[chat_id] = int(mid)
            return int(mid)
    return None


def _set_screen(chat_id: int, message_id: int) -> None:
    prev = _screen.get(chat_id)
    _screen[chat_id] = message_id
    _loaded.add(chat_id)
    if message_id != prev and _save_screen is not None:
        try:
            result = _save_screen(chat_id, message_id)
            if inspect.isawaitable(result):
                # persist in background so the user is not kept waiting
                asyncio.ensure_future(result)
        except Exception:
            pass


async def _safe_delete(bot: Bot, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


def track_screen(chat_id: int, message_id: int | None) -> None:
    """Remember a message (e.g. one edited by a callback) as the live screen.

    Also removes it from the ephemeral list so it is not deleted as transient."""
    if not message_id:
        return
    _set_screen(chat_id, message_id)
    pending = _ephemerals.get(chat_id)
    if pending and message_id in pending:
        pending.remove(message_id)


def track_ephemeral(chat_id: int, message_id: int, ttl: float | None = None) -> None:
    """Пометить уже отправленное сообщение (голос, файл) как временное."""
    _ephemerals[chat_id].append(message_id)
    _spawn(_remember(chat_id, message_id, ttl))
    if ttl:
        _spawn(_delete_later(None, chat_id, message_id, ttl))


# chat_id -> то, что Джарвис прислал в чат по просьбе («скинь в чат»): исчезает при нажатии любой кнопки
_sent: dict[int, list[int]] = defaultdict(list)


def track_sent(chat_id: int, message_id: int) -> None:
    """Текст, который Nurai скинул в чат (из звонка/с телефона): живёт до первой нажатой кнопки
    (или сутки — потом его подберёт sweep, как любое временное сообщение)."""
    _sent[chat_id].append(message_id)
    _spawn(_remember(chat_id, message_id, None))


async def clear_sent(bot: Bot, chat_id: int) -> None:
    ids = _sent.pop(chat_id, [])
    if not ids:
        return
    await asyncio.gather(*(_safe_delete(bot, chat_id, mid) for mid in ids))
    _spawn(_forget(chat_id, ids))


async def clear_ephemerals(bot: Bot, chat_id: int) -> None:
    ids = _ephemerals.pop(chat_id, [])
    if _trash is not None and chat_id not in _trash_loaded:
        # первый раз после перезапуска — подберём и то, что осталось с прошлого запуска
        _trash_loaded.add(chat_id)
        try:
            ids += [int(r["message_id"]) for r in await _trash.list_ephemerals(chat_id=chat_id) if int(r["message_id"]) not in ids]
        except Exception:
            logger.debug("ephemeral list failed", exc_info=True)
    if not ids:
        return
    await asyncio.gather(*(_safe_delete(bot, chat_id, mid) for mid in ids))
    _spawn(_forget(chat_id, ids))


async def sweep(bot: Bot) -> int:
    """Удалить временные сообщения, у которых вышел срок (фоновый воркер, раз в минуту)."""
    if _trash is None:
        return 0
    rows = await _trash.list_ephemerals(due_before=datetime.now(timezone.utc))
    by_chat: dict[int, list[int]] = defaultdict(list)
    for r in rows:
        by_chat[int(r["chat_id"])].append(int(r["message_id"]))
    for chat_id, ids in by_chat.items():
        await asyncio.gather(*(_safe_delete(bot, chat_id, mid) for mid in ids))
        pending = _ephemerals.get(chat_id)
        if pending:
            _ephemerals[chat_id] = [m for m in pending if m not in ids]
        await _forget(chat_id, ids)
    return sum(len(v) for v in by_chat.values())


async def show_screen(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: Any | None = None,
    *,
    force_new: bool = False,
) -> int:
    """Render the main screen: edit the existing one in place when possible,
    otherwise replace it. Clears any transient messages first.

    force_new=True always sends a fresh message (deleting the old one). Use it
    for explicit /start, /menu, /help so the menu always appears even after the
    user cleared the chat history."""
    await clear_ephemerals(bot, chat_id)

    old = await _get_screen(chat_id)
    if old and not force_new:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=old, text=text, reply_markup=reply_markup)
            return old
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return old
            await _safe_delete(bot, chat_id, old)
    elif old and force_new:
        await _safe_delete(bot, chat_id, old)

    msg = await bot.send_message(chat_id, text, reply_markup=reply_markup)
    _set_screen(chat_id, msg.message_id)
    return msg.message_id


async def send_ephemeral(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: Any | None = None,
    *,
    keep_previous: bool = False,
    ttl: float | None = None,
) -> int:
    """Send a transient message that will be removed on the next interaction.

    `ttl` (seconds) additionally removes it on a timer — for notices like
    «Звоню», «Готово», которые не должны копиться в чате, даже если человек
    больше ничего не пишет."""
    if not keep_previous:
        await clear_ephemerals(bot, chat_id)
    msg = await bot.send_message(chat_id, text, reply_markup=reply_markup)
    _ephemerals[chat_id].append(msg.message_id)
    _spawn(_remember(chat_id, msg.message_id, ttl))
    if ttl:
        _spawn(_delete_later(bot, chat_id, msg.message_id, ttl))
    return msg.message_id


async def _delete_later(bot: Bot | None, chat_id: int, message_id: int, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
        if bot is None:
            from .context import bot_instance

            bot = bot_instance()
        await _safe_delete(bot, chat_id, message_id)
        await _forget(chat_id, [message_id])
        ids = _ephemerals.get(chat_id)
        if ids and message_id in ids:
            ids.remove(message_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("delayed delete failed", exc_info=True)


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


async def drop_chart(bot: Bot, chat_id: int) -> None:
    await _safe_delete(bot, chat_id, _chart.pop(chat_id, None))


async def drop_message(message: Any) -> None:
    """Delete a user's incoming message (command, operation, photo, voice)."""
    try:
        await message.delete()
    except Exception:
        pass

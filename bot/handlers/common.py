"""Общие помощники хендлеров: профиль пользователя (кэш), экран, транскрибация."""
from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path
from typing import Any

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, User

from .. import cache
from .. import screen as screen_mod
from ..context import ai, db, settings
from ..profile import Profile, h

logger = logging.getLogger(__name__)

PROFILE_TTL = 6 * 3600


def _shape(user_row: dict[str, Any], tg_user: User | None = None) -> Profile:
    lang = str(user_row.get("language") or settings.default_language).strip().lower()
    return Profile(
        telegram_id=int(user_row["telegram_id"]),
        lang="uz" if lang == "uz" else "ru",
        tz_name=str(user_row.get("timezone") or settings.app_timezone),
        currency=str(user_row.get("currency") or settings.default_currency),
        first_name=str(user_row.get("first_name") or (tg_user.first_name if tg_user else "") or "").strip(),
        username=str(user_row.get("username") or (tg_user.username if tg_user else "") or "").strip(),
    )


async def get_profile(tg_user: User) -> Profile:
    """Профиль из кэша; при первом обращении (или раз в 6 часов) синхронизируем с БД."""
    cached = cache.get(tg_user.id, ("profile",))
    if isinstance(cached, Profile):
        return cached
    try:
        row = await db.upsert_user(
            tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            language=settings.default_language,
            timezone_name=settings.app_timezone,
            currency=settings.default_currency,
        )
    except Exception:
        logger.exception("upsert_user failed; using defaults")
        row = {"telegram_id": tg_user.id, "username": tg_user.username, "first_name": tg_user.first_name}
    profile = _shape(row, tg_user)
    cache.put(tg_user.id, ("profile",), profile, PROFILE_TTL)
    return profile


async def profile_by_id(telegram_id: int) -> Profile:
    cached = cache.get(telegram_id, ("profile",))
    if isinstance(cached, Profile):
        return cached
    row = await db.get_user(telegram_id) or {"telegram_id": telegram_id}
    profile = _shape(row)
    cache.put(telegram_id, ("profile",), profile, PROFILE_TTL)
    return profile


def set_profile_lang(telegram_id: int, lang: str) -> None:
    cached = cache.get(telegram_id, ("profile",))
    if isinstance(cached, Profile):
        cached.lang = "uz" if lang == "uz" else "ru"


# ------------------------------------------------------------------ screen
async def answer_now(callback: CallbackQuery, text: str | None = None, *, alert: bool = False) -> None:
    """Сразу гасим «часики» на кнопке — до любых запросов к БД/AI."""
    try:
        await callback.answer(text, show_alert=alert)
    except Exception:
        pass


async def safe_delete(message: Message | None) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except Exception:
        pass


async def safe_edit(callback: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    if callback.message is None:
        return
    chat_id = callback.message.chat.id
    screen_mod.track_screen(chat_id, callback.message.message_id)
    await screen_mod.clear_ephemerals(callback.bot, chat_id)
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return
        await screen_mod.show_screen(callback.bot, chat_id, text, reply_markup)


async def remember_panel(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message:
        await state.update_data(panel_message_id=callback.message.message_id)
        screen_mod.track_screen(callback.message.chat.id, callback.message.message_id)


async def show_panel(message: Message, state: FSMContext, text: str, reply_markup: InlineKeyboardMarkup | None) -> None:
    mid = await screen_mod.show_screen(message.bot, message.chat.id, text, reply_markup)
    await state.update_data(panel_message_id=mid)


async def show_progress(target: Message | CallbackQuery, text: str) -> None:
    """Показать «⏳ …» на живом экране, пока работает AI."""
    if isinstance(target, CallbackQuery):
        if target.message is None:
            return
        try:
            await target.message.edit_text(text, reply_markup=None)
        except Exception:
            pass
        return
    try:
        await screen_mod.show_screen(target.bot, target.chat.id, text, None)
    except Exception:
        pass


def message_text(message: Message) -> str:
    return (message.text or message.caption or "").strip()


# ------------------------------------------------------------------ media
async def get_photo_bytes(message: Message) -> tuple[bytes, str, str]:
    photo = message.photo[-1]
    telegram_file = await message.bot.get_file(photo.file_id)
    file_path = str(telegram_file.file_path or "")
    mime_type = "image/png" if file_path.lower().endswith(".png") else "image/jpeg"
    buffer = await message.bot.download_file(file_path)
    return buffer.read(), mime_type, photo.file_id


async def transcribe_audio(message: Message) -> str:
    media = message.voice or message.audio
    if media is None:
        return ""
    telegram_file = await message.bot.get_file(media.file_id)
    suffix = Path(str(telegram_file.file_path or "")).suffix or ".ogg"
    with tempfile.NamedTemporaryFile(prefix="voice_", suffix=suffix, delete=False) as tmp:
        temp_path = Path(tmp.name)
    try:
        await message.bot.download_file(str(telegram_file.file_path), destination=temp_path)
        return (await ai.transcribe_voice(temp_path)).strip()
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


class Timer:
    def __init__(self, label: str) -> None:
        self.label = label
        self.start = time.perf_counter()

    def done(self) -> None:
        logger.info("%s took %.0f ms", self.label, (time.perf_counter() - self.start) * 1000)


__all__ = [
    "Profile", "get_profile", "profile_by_id", "set_profile_lang", "h", "answer_now", "safe_delete", "safe_edit",
    "remember_panel", "show_panel", "show_progress", "message_text", "get_photo_bytes", "transcribe_audio", "Timer",
]

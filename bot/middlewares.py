"""Middlewares: доступ только владельцу, защита от двойных нажатий, глобальный error handler."""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import CallbackQuery, ErrorEvent, Message, TelegramObject

from .config import Settings

logger = logging.getLogger(__name__)


class AccessMiddleware(BaseMiddleware):
    """Личный бот: пускаем только ALLOWED_TELEGRAM_IDS. Чужим отвечаем один раз в час."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._notified: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from . import access

        user = getattr(event, "from_user", None)
        uid = user.id if user else None
        await access.refresh()
        if access.is_allowed(uid):
            if isinstance(event, CallbackQuery) and uid is not None:
                from . import i18n

                i18n.note_callback(event.id, uid)  # чтобы перевести всплывашку ответа на кнопку
            return await handler(event, data)

        # ссылка-приглашение: /start inv_<code>
        code = access.invite_code(event.text) if isinstance(event, Message) else None
        if code and user is not None:
            status = await access.redeem(code, telegram_id=user.id, first_name=user.first_name, username=user.username)
            if status == "ok":
                lang = await _setup_new_member(user)
                result = await handler(event, data)  # /start → главное меню уже на его языке
                await _welcome(event, lang)
                return result
            texts = {"expired": "⌛ Taklif muddati o'tgan. / Приглашение истекло. / The invite has expired.",
                     "used": "🔒 Bu taklif ishlatilgan. / Приглашение уже использовано. / This invite was already used."}
            try:
                await event.answer(texts.get(status, "⛔ Taklif topilmadi. / Приглашение не найдено. / Invite not found."))
            except Exception:
                pass
            return None

        now = time.monotonic()
        if uid is not None and now - self._notified.get(uid, 0.0) > 3600:
            self._notified[uid] = now
            text = "⛔ Bu shaxsiy bot. / Это личный бот. / This is a private bot."
            try:
                if isinstance(event, Message):
                    await event.answer(text)
                elif isinstance(event, CallbackQuery):
                    await event.answer(text, show_alert=True)
            except Exception:
                pass
        elif isinstance(event, CallbackQuery):
            try:
                await event.answer()
            except Exception:
                pass
        logger.info("Access denied for user %s", uid)
        return None


def lang_from_telegram(code: str | None) -> str:
    """Язык нового человека по его Telegram: ru/uz как есть, остальным — английский."""
    code = str(code or "").lower()
    if code.startswith("uz"):
        return "uz"
    if code.startswith(("ru", "be", "uk", "kk", "ky", "tg")):  # в СНГ русский понятнее английского
        return "ru"
    return "en"


async def _setup_new_member(user) -> str:  # noqa: ANN001
    """Первый вход по приглашению: профиль, язык бота и ZEKI — по языку его Telegram."""
    from . import cache, services
    from .context import db, settings

    lang = lang_from_telegram(getattr(user, "language_code", None))
    try:
        await db.upsert_user(user.id, username=user.username, first_name=user.first_name, language=lang,
                             timezone_name=settings.app_timezone, currency=settings.default_currency)
        await db.update_user_language(user.id, lang)
        if db.available("assistant_settings"):
            await services.save_persona(user.id, {"lang": lang})
        cache.invalidate(user.id, "profile")
    except Exception:
        logger.exception("new member setup failed for %s", user.id)
    return lang


WELCOME = {
    "ru": ("👋 <b>Добро пожаловать!</b>\n\nЭто личный помощник с ИИ «ZEKI». Просто пиши или говори голосом, как человеку:\n"
           "• «такси 25 000», «обед 40к» — расходы\n• фото еды — калории\n• «напомни завтра в 9 позвонить маме»\n"
           "• «накопить 5 млн к декабрю» — цели\n• 🤖 ZEKI — звонок, будильник, голос\n\n"
           "Все твои данные видишь только ты.\n\n📞 ZEKI написал тебе из своего аккаунта — добавь его в контакты, чтобы его звонки доходили."),
    "uz": ("👋 <b>Xush kelibsiz!</b>\n\nBu sun'iy intellektli shaxsiy yordamchi — «ZEKI». Odamga yozgandek yozing yoki ovozli gapiring:\n"
           "• «taksi 25 000», «tushlik 40k» — xarajatlar\n• ovqat rasmi — kaloriya\n• «ertaga 9 da onamga qo'ng'iroq qilishni eslat»\n"
           "• «dekabrgacha 5 mln yig'ish» — maqsadlar\n• 🤖 ZEKI — qo'ng'iroq, budilnik, ovoz\n\n"
           "Ma'lumotlaringizni faqat siz ko'rasiz.\n\n📞 ZEKI o'z akkauntidan sizga yozdi — qo'ng'iroqlari kelishi uchun uni kontaktlarga qo'shing."),
    "en": ("👋 <b>Welcome!</b>\n\nThis is your personal AI assistant, ZEKI. Just write or speak to it like to a person:\n"
           "• “taxi 25 000”, “lunch 40k” — expenses\n• a food photo — calories\n• “remind me tomorrow at 9 to call mom”\n"
           "• “save 5 mln by December” — goals\n• 🤖 ZEKI — calls, alarm, voice\n\n"
           "Only you can see your data.\n\n📞 ZEKI has messaged you from its own account — add it to your contacts so its calls come through."),
}


async def _welcome(message: Message, lang: str) -> None:
    from . import screen as screen_mod

    try:
        await screen_mod.send_ephemeral(message.bot, message.chat.id, WELCOME.get(lang, WELCOME["en"]), keep_previous=True, ttl=6 * 3600)
    except Exception:
        logger.debug("welcome failed", exc_info=True)
    # аккаунт Джарвиса сразу пишет новому человеку: у него появляется чат с кнопкой «Добавить в контакты» —
    # без этого звонки Джарвиса часто не доходят (приватность «Мои контакты», iPhone глушит незнакомых)
    try:
        from . import call_assistant
        from .handlers.common import profile_by_id

        await call_assistant.helper_intro(await profile_by_id(message.chat.id))
    except Exception:
        logger.debug("helper intro on join failed", exc_info=True)


class DedupeMiddleware(BaseMiddleware):
    """Гасит повторное нажатие той же кнопки в течение `window` секунд
    (иначе двойной тап по «быстрой» кнопке создаёт две операции)."""

    def __init__(self, window: float = 1.2) -> None:
        self.window = float(window)
        self._last: dict[int, tuple[str, float]] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, CallbackQuery) or not event.from_user:
            return await handler(event, data)
        uid = event.from_user.id
        cb = event.data or ""
        now = time.monotonic()
        last = self._last.get(uid)
        if last and last[0] == cb and now - last[1] < self.window:
            try:
                await event.answer()
            except Exception:
                pass
            logger.debug("Dropped duplicate callback %s from %s", cb, uid)
            return None
        self._last[uid] = (cb, now)
        return await handler(event, data)


class TidyMiddleware(BaseMiddleware):
    """Чистый чат: его сообщение (текст, голос, фото) удаляем, когда бот его обработал; то, что ZEKI
    скинул в чат по просьбе, убираем при нажатии любой кнопки. Упал обработчик — сообщение оставляем,
    чтобы было видно, что не сработало."""

    DELAY = 1.0

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from . import screen

        if isinstance(event, CallbackQuery):
            if event.message is not None and event.bot is not None:
                try:
                    await screen.clear_sent(event.bot, event.message.chat.id)
                except Exception:
                    logger.debug("clear sent failed", exc_info=True)
            return await handler(event, data)
        result = await handler(event, data)
        if isinstance(event, Message) and event.chat.type == "private" and not data.get("keep_message"):
            screen._spawn(self._drop_later(event))
        return result

    async def _drop_later(self, message: Message) -> None:
        import asyncio

        await asyncio.sleep(self.DELAY)
        try:
            await message.delete()
        except Exception:
            pass  # уже удалил сам обработчик — это нормально


async def global_error_handler(event: ErrorEvent) -> bool:
    exc = event.exception
    update = event.update

    if isinstance(exc, TelegramRetryAfter):
        logger.warning("Flood control: retry after %.1fs", float(exc.retry_after))
        return True
    if isinstance(exc, TelegramForbiddenError):
        logger.info("User blocked the bot (update id=%s)", getattr(update, "update_id", None))
        return True
    if isinstance(exc, TelegramBadRequest):
        msg = str(exc).lower()
        if any(s in msg for s in ("message is not modified", "message to delete not found", "message can't be deleted", "query is too old")):
            return True
        logger.warning("Telegram bad request: %s", exc)
        return True

    logger.exception("Unhandled handler error", exc_info=exc)
    try:
        msg = getattr(update, "message", None)
        cb = getattr(update, "callback_query", None)
        target = msg.from_user if msg is not None else (cb.from_user if cb is not None else None)
        lang_code = (getattr(target, "language_code", "") or "").lower() if target else ""
        text = (
            "⚠️ Nimadir xato ketdi. Bir daqiqadan keyin qayta urinib koʻring."
            if lang_code.startswith("uz")
            else "⚠️ Что-то пошло не так. Попробуй ещё раз через минуту."
        )
        if msg is not None:
            await msg.answer(text)
        elif cb is not None:
            try:
                await cb.answer(text, show_alert=True)
            except Exception:
                if cb.message is not None:
                    await cb.message.answer(text)
    except Exception:
        pass
    return True


__all__ = ["AccessMiddleware", "DedupeMiddleware", "TidyMiddleware", "global_error_handler"]

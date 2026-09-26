"""Свободный ввод без выбранного раздела: сам определяем, куда отправить.

Порядок: быстрые правила (регулярки/ключевые слова — дёшево и мгновенно) →
всё остальное отдаём агенту «JES», который сам решает: выполнить команду,
ответить на вопрос по данным, передать запись парсеру или просто поговорить.
Голос транскрибируется один раз."""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from .. import cache
from .. import emoji as pe
from .. import finance as fin
from .. import nutrition as nutri
from .. import screen as screen_mod
from .. import services
from .. import vacancy as vac
from ..context import ai
from ..profile import Profile, h
from . import agent
from . import finance as finance_h
from . import nutrition as nutrition_h
from . import vacancy as vacancy_h
from . import wake as wake_h
from .common import get_photo_bytes, get_profile, message_text, safe_delete, show_progress, transcribe_audio
from .menu import send_main_menu

router = Router(name="inbox")
logger = logging.getLogger(__name__)

_QUESTION_HINTS = ("сколько", "qancha", "статист", "потратил", "sarfladim", "итог за", "сравни", "какой баланс", "balans qancha")


def _is_question(text: str) -> bool:
    low = text.lower()
    if fin.parse_local(text) is not None:
        return False
    return "?" in low or any(w in low for w in _QUESTION_HINTS)


async def _not_understood(message: Message, profile: Profile, transcript: str | None = None) -> None:
    """Сюда попадаем только если JES недоступен (ошибка AI) или сообщение без текста."""
    if transcript:
        notice = profile.tr(f"JES сейчас недоступен, не смог обработать: «{h(transcript[:80])}». Повтори через минуту.",
                            f"JES hozir ishlamayapti: «{h(transcript[:80])}». Bir daqiqadan so'ng qaytaring.")
    else:
        notice = profile.tr("JES сейчас недоступен — повтори через минуту.", "JES hozir ishlamayapti — bir daqiqadan so'ng qaytaring.")
    # возвращаем главный экран (мог остаться «⏳ …») и показываем короткую подсказку
    await send_main_menu(message, profile)
    await screen_mod.send_ephemeral(message.bot, message.chat.id, f"{pe.CROSS} {notice}", keep_previous=True)


MENU_WORDS = {"start", "/start", "menu", "/menu", "меню", "menyu", "главная", "bosh sahifa"}


async def route_text(
    message: Message,
    state: FSMContext,
    profile: Profile,
    text: str,
    *,
    transcript: str | None = None,
    has_photo: bool = False,
    skip_finance: bool = False,
    skip_food: bool = False,
) -> bool:
    """Возвращает True, если сообщение обработано."""
    low = text.lower().strip()
    source = "voice_ai" if transcript else "text"
    if low in MENU_WORDS:
        await safe_delete(message)
        await send_main_menu(message, profile, force_new=True)
        return True
    voice = transcript is not None
    # подъём: «проснулся / ещё 10 минут» — раньше всех остальных правил
    if await wake_h.handle_awake_text(message, profile, text):
        await safe_delete(message)
        return True
    if cache.get(profile.telegram_id, ("agent_ask",)):
        # Джарвис ждёт ответ на свой вопрос — любой текст («вторую», «25000», «да, долг») идёт ему
        if await agent.handle_command(message, state, profile, text, voice=voice):
            return True
    if agent.looks_like_command(text) and not vac.looks_like_vacancy(text):
        if await agent.handle_command(message, state, profile, text, voice=voice):
            return True
    if vac.looks_like_vacancy(text):
        await vacancy_h.process_vacancy(message, state, profile, text)
        return True
    # Быстрые пути — только когда локальные правила разбирают фразу сами (мгновенно и без ошибок роутинга).
    # «Срок долга Uzum до 5 октября» раньше уходил в финансовый парсер из-за слова «долг» и цифры «5» —
    # теперь такое решает агент: он видит долги, память и может уточнить кнопками.
    if not skip_finance and fin.local_finance(text) and not _is_question(text):
        await finance_h.handle_finance_text(message, state, profile, text, source=source, reroute=False)
        return True
    if not skip_food and nutri.looks_like_food(text) and not _is_question(text) and not fin.looks_like_finance(text):
        await nutrition_h.handle_text(message, state, profile, text, transcript=transcript, reroute=False)
        return True
    # Всё остальное — «Джарвис»: команды без ключевых слов, траты с контекстом, вопросы по данным,
    # уточнения к предыдущей реплике («вторую», «да, её»), свободный чат.
    return await agent.handle_command(message, state, profile, text, voice=voice)


async def handle_photo_message(message: Message, state: FSMContext, profile: Profile) -> None:
    """Фото: вакансия по подписи → договорённость с JES
    (expect_photo) → еда или «другое».

    Раньше любое фото считалось едой: пообещал JES «пришли фото челленджа — отмечу», а фото ушло
    в калории. Теперь JES видит фото сам, если есть договорённость, подпись-просьба или на фото не еда."""
    caption = message_text(message)
    if caption and vac.looks_like_vacancy(caption):
        await vacancy_h.process_vacancy(message, state, profile, caption)
        return
    await show_progress(message, profile.tr("⏳ Смотрю фото…", "⏳ Rasmni ko'ryapman…"))
    try:
        photo = await get_photo_bytes(message)
    except Exception:
        logger.exception("photo download failed")
        await nutrition_h.handle_photo(message, state, profile)
        return
    intent = await services.photo_intent(profile.telegram_id)
    to_agent = bool(intent) or bool(caption and not nutri.looks_like_food(caption))
    if not to_agent and not caption:
        try:
            to_agent = await ai.classify_photo(photo[0], photo[1]) == "other"
        except Exception:
            logger.warning("photo classify failed", exc_info=True)
    if to_agent:
        text = "(прислал фото)" + (f" {caption}" if caption else "")
        if intent:
            text += f"\n[договорённость о фото: {intent}]"
        if await agent.handle_command(message, state, profile, text, photo=photo):
            return
    await nutrition_h.handle_photo(message, state, profile, photo=photo)


@router.message()
async def fallback(message: Message, state: FSMContext) -> None:
    profile = await get_profile(message.from_user)
    raw_text = message_text(message)

    if message.photo:
        await handle_photo_message(message, state, profile)
        return

    if raw_text:
        if await route_text(message, state, profile, raw_text):
            return
        await safe_delete(message)
        await _not_understood(message, profile)
        return

    if message.voice or message.audio:
        await show_progress(message, profile.tr("⏳ Распознаю голос…", "⏳ Ovoz aniqlanmoqda…"))
        try:
            transcript = await transcribe_audio(message)
        except Exception:
            logger.exception("transcribe failed")
            transcript = ""
        if transcript and await route_text(message, state, profile, transcript, transcript=transcript):
            return
        await safe_delete(message)
        await _not_understood(message, profile, transcript=transcript or None)
        return

    await safe_delete(message)
    await _not_understood(message, profile)

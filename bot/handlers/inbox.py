"""Свободный ввод без выбранного раздела: сам определяем, куда отправить.

Порядок: быстрые правила (регулярки/ключевые слова) → AI-классификатор только
если правила не сработали. Голос транскрибируется один раз."""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from .. import emoji as pe
from .. import finance as fin
from .. import nutrition as nutri
from .. import screen as screen_mod
from .. import vacancy as vac
from ..context import ai
from ..profile import Profile, h
from . import agent
from . import finance as finance_h
from . import nutrition as nutrition_h
from . import vacancy as vacancy_h
from .common import get_profile, message_text, safe_delete, show_progress, transcribe_audio
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
    if transcript:
        notice = profile.tr(f"Не понял: «{h(transcript[:80])}»", f"Tushunmadim: «{h(transcript[:80])}»")
    else:
        notice = profile.tr("Не понял сообщение. Напиши, например: «такси 25000» или «съел плов»", "Xabar tushunarsiz. Masalan: «taksi 25000» yoki «osh yedim»")
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
    if agent.looks_like_command(text) and not vac.looks_like_vacancy(text):
        if await agent.handle_command(message, state, profile, text):
            return True
    if vac.looks_like_vacancy(text):
        await vacancy_h.process_vacancy(message, state, profile, text)
        return True
    if not skip_finance and fin.looks_like_finance(text) and not _is_question(text):
        await finance_h.handle_finance_text(message, state, profile, text, source=source, reroute=False)
        return True
    if _is_question(text):
        await safe_delete(message)
        await finance_h.answer_question(message, profile, text)
        return True
    if not skip_food and nutri.looks_like_food(text):
        await nutrition_h.handle_text(message, state, profile, text, transcript=transcript, reroute=False)
        return True

    # Ничего не подошло — спрашиваем AI (быстрый вызов без «размышлений»).
    try:
        intent = await ai.classify_inbox_intent(text, has_photo=has_photo, has_voice=transcript is not None)
    except Exception:
        return False
    if intent.confidence < 0.55 or intent.module == "unknown":
        return await agent.handle_command(message, state, profile, text)
    cleaned = (intent.cleaned_text or text).strip() or text
    if intent.module == "menu":
        await safe_delete(message)
        await send_main_menu(message, profile, force_new=True)
        return True
    if intent.module == "vacancy":
        if intent.mode == "process":
            await vacancy_h.process_vacancy(message, state, profile, text)
        else:
            await safe_delete(message)
            await vacancy_h.open_panel(message, state, profile)
        return True
    if intent.module == "finance":
        if intent.mode == "process":
            await finance_h.handle_finance_text(message, state, profile, cleaned, source=source, reroute=False)
        elif intent.mode == "answer":
            await safe_delete(message)
            await finance_h.answer_question(message, profile, cleaned)
        else:
            await safe_delete(message)
            await finance_h.render_panel(message, state, profile)
        return True
    if intent.module == "question":
        await safe_delete(message)
        await finance_h.answer_question(message, profile, cleaned)
        return True
    if intent.module == "calorie":
        if intent.mode == "process":
            await nutrition_h.handle_text(message, state, profile, cleaned, transcript=transcript, reroute=False)
        else:
            await safe_delete(message)
            await nutrition_h.render_panel(message, state, profile)
        return True
    return False


async def handle_photo_message(message: Message, state: FSMContext, profile: Profile) -> None:
    """Фото: с подписью-вакансией → вакансия, иначе → еда."""
    caption = message_text(message)
    if caption and vac.looks_like_vacancy(caption):
        await vacancy_h.process_vacancy(message, state, profile, caption)
        return
    await nutrition_h.handle_photo(message, state, profile)


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

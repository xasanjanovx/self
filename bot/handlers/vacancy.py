"""Вакансии: текст/форвард/фото/голос → пост для канала + промпт для картинки."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import emoji as pe
from .. import vacancy as vac
from ..context import ai, settings
from ..keyboards import vacancy_channel_keyboard, vacancy_panel_keyboard, vacancy_result_keyboard
from ..profile import Profile, h
from ..states import BotStates
from .common import answer_now, get_profile, message_text, remember_panel, safe_delete, safe_edit, show_panel, show_progress, transcribe_audio

router = Router(name="vacancy")
logger = logging.getLogger(__name__)


def panel_text(lang: str) -> str:
    if lang == "uz":
        return (
            "📣 <b>Vakansiya</b>\n\n"
            "Vakansiya matnini, forward qilingan postni yoki rasm + matn yuboring.\n"
            "Bot xatolarni tuzatadi, matnni tushunarli qiladi, kanal shabloniga keltiradi "
            "va rasm uchun prompt beradi."
        )
    return (
        "📣 <b>Вакансия</b>\n\n"
        "Пришли текст вакансии, пересланный пост или фото с подписью.\n"
        "Бот исправит ошибки, сделает текст понятнее, оформит по шаблону канала "
        "и даст промпт для картинки."
    )


async def open_panel(target: Message | CallbackQuery, state: FSMContext, profile: Profile) -> None:
    await state.set_state(BotStates.waiting_vacancy_input)
    await state.update_data(vacancy_post=None, vacancy_contact_url=None, vacancy_photo_id=None, vacancy_prompt=None)
    if isinstance(target, CallbackQuery):
        await remember_panel(target, state)
        await safe_edit(target, panel_text(profile.lang), vacancy_panel_keyboard(profile.lang))
    else:
        await show_panel(target, state, panel_text(profile.lang), vacancy_panel_keyboard(profile.lang))


@router.callback_query(F.data.in_({"menu:vacancy", "vacancy:again"}))
async def cb_open(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    await open_panel(callback, state, await get_profile(callback.from_user))


@router.message(Command("vacancy"))
async def cmd_vacancy(message: Message, state: FSMContext) -> None:
    profile = await get_profile(message.from_user)
    await safe_delete(message)
    await open_panel(message, state, profile)


async def process_vacancy(message: Message, state: FSMContext, profile: Profile, raw_text: str) -> None:
    """Единственный режим: исправить ошибки + сделать понятнее + шаблон + промпт."""
    lang = profile.lang
    photo_id = message.photo[-1].file_id if message.photo else None
    premium = bool(getattr(message.from_user, "is_premium", False))
    await safe_delete(message)

    if len(raw_text) < 40 and not vac.looks_like_vacancy(raw_text):
        await show_panel(
            message, state,
            panel_text(lang) + "\n\n" + profile.tr(
                "Похоже на неполную вакансию. Добавь должность, зарплату/график и контакты.",
                "Bu to'liq vakansiyaga o'xshamaydi. Lavozim, maosh/grafik va aloqani qo'shing.",
            ),
            vacancy_panel_keyboard(lang),
        )
        return

    await show_progress(message, profile.tr("⏳ Оформляю вакансию…", "⏳ Vakansiya tayyorlanmoqda…"))
    try:
        data = await ai.rewrite_vacancy(raw_text, default_region_tag=vac.VACANCY_DEFAULT_REGION_TAG)
        data = vac.finalize(data, raw_text)
        post = vac.format_vacancy_post(data, premium=premium, footer_url=settings.vacancy_footer_url)
        contact_url = vac.build_contact_url(data.telegram)
    except Exception as exc:
        logger.exception("Vacancy rewrite failed")
        await show_panel(
            message, state,
            panel_text(lang) + f"\n\n{pe.CROSS} " + profile.tr("Ошибка обработки вакансии", "Vakansiya tahlil xatosi") + f": {h(str(exc)[:160])}",
            vacancy_panel_keyboard(lang),
        )
        return

    await state.set_state(BotStates.waiting_vacancy_input)
    await state.update_data(vacancy_post=post, vacancy_contact_url=contact_url, vacancy_photo_id=photo_id, vacancy_prompt=data.image_prompt)
    kb = vacancy_result_keyboard(lang, contact_url, can_publish=_channel_for(profile.telegram_id) is not None)
    await show_panel(message, state, post, kb)
    if data.image_prompt:
        # полный промпт — отдельным блоком: в Telegram нажатие на блок копирует его целиком
        from .. import screen as screen_mod

        head = profile.tr("🎨 <b>Промпт для картинки</b> — нажми на блок, чтобы скопировать",
                          "🎨 <b>Rasm uchun prompt</b> — nusxalash uchun blokni bosing")
        await screen_mod.send_ephemeral(message.bot, message.chat.id, f"{head}\n<pre>{h(data.image_prompt)}</pre>", keep_previous=True)


@router.message(BotStates.waiting_vacancy_input)
async def msg_input(message: Message, state: FSMContext) -> None:
    profile = await get_profile(message.from_user)
    raw_text = message_text(message)
    if raw_text.startswith("/"):
        await safe_delete(message)
        return
    if not raw_text and (message.voice or message.audio):
        await show_progress(message, profile.tr("⏳ Распознаю голос…", "⏳ Ovoz aniqlanmoqda…"))
        try:
            raw_text = await transcribe_audio(message)
        except Exception as exc:
            logger.exception("Vacancy voice transcribe failed")
            await safe_delete(message)
            await show_panel(message, state, panel_text(profile.lang) + f"\n\n{pe.CROSS} {h(str(exc)[:120])}", vacancy_panel_keyboard(profile.lang))
            return
    if not raw_text:
        await safe_delete(message)
        await show_panel(
            message, state,
            panel_text(profile.lang) + "\n\n" + profile.tr("Нужен текст вакансии, пересланный пост или голосовое.", "Vakansiya matni, forward post yoki ovozli xabar kerak."),
            vacancy_panel_keyboard(profile.lang),
        )
        return
    await process_vacancy(message, state, profile, raw_text)


def _channel_for(uid: int):  # noqa: ANN202
    """Канал вакансий — только у владельца; приглашённые получают чистую копию себе."""
    from .. import access

    return settings.vacancy_channel if settings.vacancy_channel and access.is_owner(uid) else None


@router.callback_query(F.data == "vacancy:publish")
async def cb_publish(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await get_profile(callback.from_user)
    data = await state.get_data()
    post = str(data.get("vacancy_post") or "").strip()
    contact_url = data.get("vacancy_contact_url")
    photo_id = data.get("vacancy_photo_id")
    if not post or callback.message is None:
        await answer_now(callback, profile.tr("Нет данных для публикации", "E'lon uchun ma'lumot yo'q"), alert=True)
        return
    markup = vacancy_channel_keyboard(profile.lang, contact_url)
    channel = _channel_for(profile.telegram_id)
    target_chat = channel or callback.message.chat.id
    photo_skipped = False
    try:
        if photo_id and len(post) <= 1024:
            await callback.bot.send_photo(target_chat, photo=photo_id, caption=post, reply_markup=markup)
        else:
            photo_skipped = bool(photo_id)
            await callback.bot.send_message(target_chat, post, reply_markup=markup)
    except Exception as exc:
        logger.exception("Vacancy publish failed")
        await answer_now(callback, f"{profile.tr('Ошибка', 'Xato')}: {str(exc)[:150]}", alert=True)
        return
    if channel:
        note = profile.tr("Опубликовано в канал ✅", "Kanalga joylandi ✅")
    else:
        note = profile.tr("Чистая копия отправлена — перешли её в канал.", "Toza nusxa yuborildi — kanalga forward qiling.")
    if photo_skipped:
        note += profile.tr(" Текст длиннее 1024 символов — фото не прикреплено.", " Matn 1024 belgidan uzun — rasm qo'shilmadi.")
    await answer_now(callback, note, alert=photo_skipped)

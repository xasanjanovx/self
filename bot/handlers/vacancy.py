"""Вакансии — только владелец (07.10): текст/форвард/фото/голос → пост для канала и СРАЗУ баннер (дизайн под профессию, каждый раз другой).

Его просьбы: раздел «Вакансии» — только у админа бота (кнопка в главном меню); фото генерируется сразу и в стиле вакансии; вакансию, которую
он разместил сам (заказ на размещение), канал держит наверху не меньше 3 часов — после публикации здесь включается защита ленты
(bot/vacancy_feed.py: publish_gate), и автоподбор до её конца молчит. У приглашённых раздела нет.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from .. import access, image_gen
from .. import emoji as pe
from .. import vacancy as vac
from .. import vacancy_feed as feed
from ..context import ai, settings
from ..keyboards import _btn, vacancy_channel_keyboard, vacancy_panel_keyboard, vacancy_result_keyboard
from ..profile import Profile, h
from ..states import BotStates
from .common import answer_now, get_profile, message_text, remember_panel, safe_delete, safe_edit, show_panel, show_progress, transcribe_audio

router = Router(name="vacancy")
logger = logging.getLogger(__name__)

MAX_REDRAWS = 4


def panel_text(lang: str) -> str:
    if lang == "uz":
        return (
            "📣 <b>Vakansiya</b>\n\n"
            "Vakansiya matnini, forward qilingan postni yoki rasm + matn yuboring.\n"
            "Bot matnni kanal shabloniga keltiradi va darhol kasbga mos banner chizadi."
        )
    return (
        "📣 <b>Вакансии</b>\n\n"
        "Пришли текст вакансии, пересланный пост или голосовое — бот исправит текст, оформит по шаблону канала и сразу нарисует "
        "баннер в подходящем стиле (дизайн каждый раз другой). Фото с подписью — пост выйдет с твоим фото.\n"
        "Опубликованное тобой держится наверху ленты не меньше 3 часов: автоподбор в это время молчит."
    )


def _owner_only(uid: int | None) -> bool:
    return access.is_owner(uid)


async def open_panel(target: Message | CallbackQuery, state: FSMContext, profile: Profile) -> None:
    await state.set_state(BotStates.waiting_vacancy_input)
    await state.update_data(vacancy_post=None, vacancy_contact_url=None, vacancy_photo_id=None, vacancy_prompt=None, vacancy_data=None)
    kb = vacancy_panel_keyboard(profile.lang, feed=True)
    if isinstance(target, CallbackQuery):
        await remember_panel(target, state)
        await safe_edit(target, panel_text(profile.lang), kb)
    else:
        await show_panel(target, state, panel_text(profile.lang), kb)


@router.callback_query(F.data.in_({"menu:vacancy", "vacancy:again"}))
async def cb_open(callback: CallbackQuery, state: FSMContext) -> None:
    if not _owner_only(callback.from_user.id):
        await answer_now(callback, "Раздел недоступен", alert=True)
        return
    await answer_now(callback)
    await open_panel(callback, state, await get_profile(callback.from_user))


@router.message(Command("vacancy"))
async def cmd_vacancy(message: Message, state: FSMContext) -> None:
    if not _owner_only(message.from_user.id):
        return                                    # у приглашённых этой команды нет
    profile = await get_profile(message.from_user)
    await safe_delete(message)
    await open_panel(message, state, profile)


def _card_markup(contact_url: str | None, regen: int) -> InlineKeyboardMarkup:
    channel_kb = vacancy_channel_keyboard("uz", contact_url)
    rows = [
        [_btn("✅ Опубликовать в канал", "vacancy:publish", style="success"), _btn("🔄 Другой дизайн", "vacancy:img")],
        [_btn("🎨 Промпт (ChatGPT)", "vacancy:prompt"), _btn("📝 Новая вакансия", "vacancy:again")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=(channel_kb.inline_keyboard if channel_kb else []) + rows)


async def _draw(state_data: dict, *, again: bool = False) -> image_gen.Banner:
    """Баннер для оформленной вакансии: дизайн по профессии, не из последних; «другой дизайн» — не тот же."""
    data = feed.data_from_dict(state_data["vacancy_data"])
    recent = ([state_data["vacancy_design"]] if again and state_data.get("vacancy_design") else []) + feed.recent_designs()
    design = vac.pick_design(data, state_data.get("vacancy_scene"), recent=recent, seed=f"m:{time.time()}", allowed=feed.allowed_designs())
    banner = await image_gen.vacancy_image(data, state_data.get("vacancy_scene"), design=design)
    feed.note_design(banner.design or design["id"])
    return banner


async def _send_card(message_or_cb, state: FSMContext, chat_id: int, bot, banner: image_gen.Banner | None, data: dict) -> None:
    """Карточка: баннер + пост, как он уйдёт в канал; кнопки публикации. Прежнюю карточку (при «другом дизайне») убираем."""
    from .vacancy_feed import _send_post

    for mid in data.get("vacancy_card_ids") or []:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass
    premium = bool(feed.load().get("premium"))
    vdata = feed.data_from_dict(data["vacancy_data"])
    post = vac.format_vacancy_post(vdata, premium=premium, footer_url=settings.vacancy_footer_url)
    note = "" if banner else "\n\n⚠️ Картинка не получилась — можно опубликовать без неё или нажать «Другой дизайн»."
    head = f"📥 {h(vdata.headline)}"
    ids, file_id, _ = await _send_post(bot, chat_id, post + note, banner.image if banner else None,
                                       _card_markup(data.get("vacancy_contact_url"), int(data.get("vacancy_regen") or 0)), head=head)
    await state.update_data(vacancy_card_ids=ids, vacancy_chat_id=chat_id, vacancy_file_id=file_id,
                            vacancy_design=(banner.design if banner else data.get("vacancy_design")))
    if banner and banner.warning:
        from .. import screen as screen_mod

        await screen_mod.send_note(bot, chat_id, banner.warning, ttl=3600)


async def process_vacancy(message: Message, state: FSMContext, profile: Profile, raw_text: str) -> None:
    """Исправить ошибки + шаблон канала + баннер (сразу, дизайн под профессию). Только владелец."""
    if not _owner_only(profile.telegram_id):
        return
    lang = profile.lang
    photo_id = message.photo[-1].file_id if message.photo else None
    premium = bool(getattr(message.from_user, "is_premium", False))
    feed.remember_premium(premium)
    await safe_delete(message)

    if len(raw_text) < 40 and not vac.looks_like_vacancy(raw_text):
        await show_panel(
            message, state,
            panel_text(lang) + "\n\n" + profile.tr(
                "Похоже на неполную вакансию. Добавь должность, зарплату/график и контакты.",
                "Bu to'liq vakansiyaga o'xshamaydi. Lavozim, maosh/grafik va aloqani qo'shing.",
            ),
            vacancy_panel_keyboard(lang, feed=True),
        )
        return

    await show_progress(message, profile.tr("⏳ Оформляю вакансию…", "⏳ Vakansiya tayyorlanmoqda…"))
    try:
        data = await ai.rewrite_vacancy(raw_text, default_region_tag=vac.VACANCY_DEFAULT_REGION_TAG)
        scene = data.image_prompt                  # короткое описание фона — до того, как finalize заменит его полным промптом
        data = vac.finalize(data, raw_text)
        post = vac.format_vacancy_post(data, premium=premium, footer_url=settings.vacancy_footer_url)
        contact_url = vac.build_contact_url(data.telegram)
    except Exception as exc:
        logger.exception("Vacancy rewrite failed")
        await show_panel(
            message, state,
            panel_text(lang) + f"\n\n{pe.CROSS} " + profile.tr("Ошибка обработки вакансии", "Vakansiya tahlil xatosi") + f": {h(str(exc)[:160])}",
            vacancy_panel_keyboard(lang, feed=True),
        )
        return

    await state.set_state(BotStates.waiting_vacancy_input)
    await state.update_data(vacancy_post=post, vacancy_contact_url=contact_url, vacancy_photo_id=photo_id, vacancy_prompt=data.image_prompt,
                            vacancy_data=feed.data_to_dict(data), vacancy_scene=scene, vacancy_design=None, vacancy_regen=0,
                            vacancy_file_id=None, vacancy_card_ids=[])
    if photo_id or not feed.cfg("images"):
        # его фото или картинки выключены в настройках — как раньше: пост на экране и кнопка «Опубликовать»
        kb = vacancy_result_keyboard(lang, contact_url, can_publish=bool(settings.vacancy_channel))
        await show_panel(message, state, post, kb)
        return
    await show_progress(message, profile.tr("🎨 Рисую баннер в стиле вакансии — до минуты…", "🎨 Banner chizilmoqda — bir daqiqagacha…"))
    stored = await state.get_data()
    try:
        banner = await _draw(stored)
    except image_gen.ImageError as exc:
        logger.warning("Vacancy banner failed: %s", exc)
        banner = None
    except Exception:
        logger.exception("Vacancy banner crashed")
        banner = None
    await _send_card(message, state, message.chat.id, message.bot, banner, await state.get_data())
    await show_panel(message, state, panel_text(lang) + "\n\n✅ Готово — вакансия и баннер ниже. Пришли следующую, когда нужно.", vacancy_panel_keyboard(lang, feed=True))


@router.message(BotStates.waiting_vacancy_input)
async def msg_input(message: Message, state: FSMContext) -> None:
    if not _owner_only(message.from_user.id):
        return
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
            await show_panel(message, state, panel_text(profile.lang) + f"\n\n{pe.CROSS} {h(str(exc)[:120])}", vacancy_panel_keyboard(profile.lang, feed=True))
            return
    if not raw_text:
        await safe_delete(message)
        await show_panel(
            message, state,
            panel_text(profile.lang) + "\n\n" + profile.tr("Нужен текст вакансии, пересланный пост или голосовое.", "Vakansiya matni, forward post yoki ovozli xabar kerak."),
            vacancy_panel_keyboard(profile.lang, feed=True),
        )
        return
    await process_vacancy(message, state, profile, raw_text)


@router.callback_query(F.data == "vacancy:img")
async def cb_redraw(callback: CallbackQuery, state: FSMContext) -> None:
    if not _owner_only(callback.from_user.id):
        await answer_now(callback, "Раздел недоступен", alert=True)
        return
    data = await state.get_data()
    if not data.get("vacancy_data"):
        await answer_now(callback, "Сначала пришли вакансию", alert=True)
        return
    regen = int(data.get("vacancy_regen") or 0)
    if regen >= MAX_REDRAWS:
        await answer_now(callback, f"Больше {MAX_REDRAWS} перерисовок не делаю — публикуй как есть или пришли вакансию заново", alert=True)
        return
    await answer_now(callback, "Рисую в другом дизайне — до минуты…")
    await state.update_data(vacancy_regen=regen + 1)
    try:
        banner = await _draw(await state.get_data(), again=True)
    except Exception as exc:
        logger.warning("Vacancy redraw failed: %s", exc)
        banner = None
    chat_id = int(data.get("vacancy_chat_id") or callback.from_user.id)
    await _send_card(callback, state, chat_id, callback.bot, banner, await state.get_data())


@router.callback_query(F.data == "vacancy:prompt")
async def cb_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    if not _owner_only(callback.from_user.id):
        await answer_now(callback, "Раздел недоступен", alert=True)
        return
    data = await state.get_data()
    prompt = data.get("vacancy_prompt")
    if not prompt:
        await answer_now(callback, "Промпта нет — пришли вакансию", alert=True)
        return
    await answer_now(callback)
    from .. import screen as screen_mod

    await screen_mod.send_ephemeral(callback.bot, callback.from_user.id,
                                    f"🎨 <b>Промпт для ChatGPT</b> — нажми на блок, чтобы скопировать\n<pre>{h(prompt)}</pre>", keep_previous=True)


@router.callback_query(F.data == "vacancy:publish")
async def cb_publish(callback: CallbackQuery, state: FSMContext) -> None:
    if not _owner_only(callback.from_user.id):
        await answer_now(callback, "Раздел недоступен", alert=True)
        return
    profile = await get_profile(callback.from_user)
    data = await state.get_data()
    if not data.get("vacancy_data") or callback.message is None:
        await answer_now(callback, profile.tr("Нет данных для публикации", "E'lon uchun ma'lumot yo'q"), alert=True)
        return
    from .vacancy_feed import _send_post

    premium = bool(getattr(callback.from_user, "is_premium", False))
    feed.remember_premium(premium)
    vdata = feed.data_from_dict(data["vacancy_data"])
    post = vac.format_vacancy_post(vdata, premium=premium, footer_url=settings.vacancy_footer_url)
    contact_url = vac.build_contact_url(vdata.telegram)
    markup = vacancy_channel_keyboard("uz", contact_url)
    channel = settings.vacancy_channel
    target = channel or callback.message.chat.id
    image = data.get("vacancy_photo_id") or (data.get("vacancy_file_id") if feed.cfg("images") else None)
    try:
        ids, _, post_id = await _send_post(callback.bot, target, post, image, markup)
    except Exception as exc:
        logger.exception("Vacancy publish failed")
        await answer_now(callback, f"{profile.tr('Ошибка', 'Xato')}: {str(exc)[:150]}", alert=True)
        return
    for mid in data.get("vacancy_card_ids") or []:
        try:
            await callback.bot.delete_message(data.get("vacancy_chat_id") or callback.from_user.id, mid)
        except Exception:
            pass
    await state.update_data(vacancy_card_ids=[])
    if not channel:
        await answer_now(callback, profile.tr("Чистая копия отправлена — перешли её в канал.", "Toza nusxa yuborildi — kanalga forward qiling."))
        return
    # он сам разместил (заказ или своя вакансия): пост должен постоять наверху — автоподбор до конца защиты молчит
    feed.mark_own(ids)
    feed.note_manual_post(time.time(), post_id)
    until = datetime.fromtimestamp(time.time() + feed.hold_left(), feed.TZ)
    await answer_now(callback, "Опубликовано в канал ✅")
    from .. import screen as screen_mod

    link = f"https://t.me/{str(channel).lstrip('@')}/{post_id}" if str(channel).startswith("@") else ""
    await screen_mod.send_note(callback.bot, callback.from_user.id,
                               "✅ Опубликовано в канал" + (f": {link}" if link else "")
                               + f"\n🛡 Лента под защитой до {until:%H:%M} ({feed.human_wait(feed.hold_left())}): "
                                 "автоподбор ничего не публикует, пока твой пост наверху.", ttl=3 * 3600)

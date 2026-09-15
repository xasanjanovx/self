"""Главный экран (дашборд), /start, /menu, /help, язык."""
from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import categories as cats
from .. import emoji as pe
from .. import finance as fin
from .. import nutrition as nutri
from .. import screen as screen_mod
from .. import services
from ..context import db
from ..keyboards import back_to_menu_keyboard, language_keyboard, main_menu_keyboard
from ..profile import Profile, h
from .common import answer_now, get_profile, safe_delete, safe_edit, set_profile_lang

router = Router(name="menu")
logger = logging.getLogger(__name__)

_WEEKDAYS = {
    "ru": ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"],
    "uz": ["dushanba", "seshanba", "chorshanba", "payshanba", "juma", "shanba", "yakshanba"],
}


async def build_dashboard(profile: Profile) -> str:
    """Один экран: питание + финансы. Все выборки — параллельно."""
    nutrition_profile, logs, snap = await asyncio.gather(
        services.nutrition_profile(profile.telegram_id),
        services.today_calorie_logs(profile),
        services.finance_snapshot(profile),
    )
    totals = nutri.totals(logs)
    lang = profile.lang
    today = profile.today
    weekday = _WEEKDAYS["uz" if lang == "uz" else "ru"][today.weekday()]
    name = h(profile.first_name or ("Do'st" if lang == "uz" else "Друг"))
    cur = profile.currency

    eaten = float(totals["calories"])
    target = float((nutrition_profile or {}).get("daily_calories") or 0.0)
    left = max(0.0, target - eaten)
    ratio = (eaten / target) if target > 0 else 0.0

    month = snap.month
    top_cat = ""
    if month and month.by_category:
        key, amount, _ = month.by_category[0]
        top_cat = f"{cats.label(key, lang)} {fin.fmt_money(amount)}"

    if lang == "uz":
        lines = [f"{pe.HELLO} Assalomu alaykum, <b>{name}</b>", f"{pe.CALENDAR} {weekday}, {today.strftime('%d.%m.%Y')}", ""]
        lines.append(f"{pe.NUTRITION} <b>Oziqlanish</b>")
        if target > 0:
            lines += [
                f"{fin.bar(ratio)} {int(round(ratio * 100))}%",
                f"Yeyildi: <b>{int(eaten)}</b> / {int(target)} kkal · qoldi {int(left)} · {int(totals['meals'])} ta qabul",
            ]
        else:
            lines.append("<i>Profil sozlanmagan — «Oziqlanish» bo'limini oching</i>")
        lines += [
            "",
            f"{pe.WALLET} <b>Moliya</b>",
            f"💳 {fin.fmt_money(snap.balances['card'])}   {pe.CASH} {fin.fmt_money(snap.balances['cash'])}",
            f"Balans: <b>{fin.fmt_money(snap.wallet)} {cur}</b>",
            f"Bugun: {pe.EXPENSE} {fin.fmt_money(snap.today_expense)}  {pe.INCOME} {fin.fmt_money(snap.today_income)}",
        ]
        if month:
            lines.append(f"Bu oy chiqim: <b>{fin.fmt_money(month.expense)} {cur}</b>" + (f" · {top_cat}" if top_cat else ""))
        return "\n".join(lines)

    lines = [f"{pe.HELLO} Привет, <b>{name}</b>", f"{pe.CALENDAR} {weekday}, {today.strftime('%d.%m.%Y')}", ""]
    lines.append(f"{pe.NUTRITION} <b>Питание</b>")
    if target > 0:
        lines += [
            f"{fin.bar(ratio)} {int(round(ratio * 100))}%",
            f"Съедено: <b>{int(eaten)}</b> / {int(target)} ккал · осталось {int(left)} · {int(totals['meals'])} приёмов",
        ]
    else:
        lines.append("<i>Профиль не настроен — открой раздел «Питание»</i>")
    lines += [
        "",
        f"{pe.WALLET} <b>Финансы</b>",
        f"💳 {fin.fmt_money(snap.balances['card'])}   {pe.CASH} {fin.fmt_money(snap.balances['cash'])}",
        f"Баланс: <b>{fin.fmt_money(snap.wallet)} {cur}</b>",
        f"Сегодня: {pe.EXPENSE} {fin.fmt_money(snap.today_expense)}  {pe.INCOME} {fin.fmt_money(snap.today_income)}",
    ]
    if month:
        lines.append(f"За месяц потрачено: <b>{fin.fmt_money(month.expense)} {cur}</b>" + (f" · {top_cat}" if top_cat else ""))
    return "\n".join(lines)


async def send_main_menu(message: Message, profile: Profile, *, force_new: bool = False) -> None:
    try:
        text = await build_dashboard(profile)
    except Exception:
        logger.exception("build_dashboard failed")
        text = profile.tr("Бот запущен. Нажми /menu для главного меню.", "Bot ishga tushdi. Asosiy menyu: /menu")
    await screen_mod.show_screen(message.bot, message.chat.id, text, main_menu_keyboard(profile.lang), force_new=force_new)


MENU_WORDS = {"menu", "меню", "menyu", "start", "главная", "bosh sahifa"}


@router.message(CommandStart())
@router.message(Command("menu"))
@router.message(F.text.func(lambda t: (t or "").strip().lower() in MENU_WORDS))
async def cmd_start(message: Message, state: FSMContext) -> None:
    profile = await get_profile(message.from_user)
    await state.clear()
    await safe_delete(message)
    await send_main_menu(message, profile, force_new=True)


HELP_RU = (
    f"{pe.INFO} <b>Как пользоваться</b>\n\n"
    "Просто пиши боту обычным языком — он сам поймёт раздел:\n\n"
    "🍽️ <b>Питание</b>\n"
    "• Фото еды → бот посчитает КБЖУ\n"
    "• Текст: «омлет из 3 яиц и кофе»\n"
    "• Голосом перечисли блюда\n\n"
    "💰 <b>Финансы</b>\n"
    "• «такси 25000», «обед 40к», «зарплата 5 млн»\n"
    "• «дал в долг 200000 наличными», «снял с карты 300000»\n"
    "• Можно несколько операций через запятую\n"
    "• Вопросы: «сколько я потратил на еду в этом месяце?»\n\n"
    "📣 <b>Вакансии</b> — пришли текст/пересланный пост, бот оформит пост для канала и даст промпт для картинки\n"
    "📊 <b>Аналитика</b> — графики и авто-отчёт\n\n"
    "Команды: /menu · /help"
)
HELP_UZ = (
    f"{pe.INFO} <b>Qanday foydalanish</b>\n\n"
    "Botga oddiy tilda yozing — u bo'limni o'zi tushunadi:\n\n"
    "🍽️ <b>Oziqlanish</b>\n"
    "• Ovqat rasmi → bot BJUni hisoblaydi\n"
    "• Matn: «3 tuxumdan omlet va kofe»\n"
    "• Ovozli ravishda taomlarni sanang\n\n"
    "💰 <b>Moliya</b>\n"
    "• «taksi 25000», «tushlik 40k», «oylik 5 mln»\n"
    "• «qarzga berdim 200000 naqd», «kartadan 300000 yechdim»\n"
    "• Bir nechta operatsiyani vergul bilan\n"
    "• Savollar: «bu oy ovqatga qancha sarfladim?»\n\n"
    "📣 <b>Vakansiya</b> — matn yoki forward yuboring, bot kanal uchun post va rasm uchun prompt tayyorlaydi\n"
    "📊 <b>Tahlil</b> — grafiklar va avto-hisobot\n\n"
    "Buyruqlar: /menu · /help"
)


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext) -> None:
    profile = await get_profile(message.from_user)
    await state.clear()
    await safe_delete(message)
    await screen_mod.show_screen(
        message.bot, message.chat.id, HELP_UZ if profile.lang == "uz" else HELP_RU, back_to_menu_keyboard(profile.lang), force_new=True
    )


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await answer_now(callback)


@router.callback_query(F.data == "menu:open")
async def cb_menu_open(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    await state.clear()
    if callback.message is None:
        return
    await screen_mod.drop_chart(callback.bot, callback.message.chat.id)
    try:
        text = await build_dashboard(profile)
    except Exception:
        logger.exception("build_dashboard failed")
        text = profile.tr("Не удалось загрузить данные. Попробуй ещё раз.", "Ma'lumot yuklanmadi. Qayta urining.")
    await safe_edit(callback, text, main_menu_keyboard(profile.lang))


@router.callback_query(F.data == "menu:language")
async def cb_menu_language(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    await state.clear()
    await safe_edit(callback, profile.tr("🌐 Выбери язык:", "🌐 Tilni tanlang:"), language_keyboard(profile.lang))


@router.callback_query(F.data.startswith("lang:set:"))
async def cb_set_language(callback: CallbackQuery, state: FSMContext) -> None:
    lang = callback.data.split(":")[-1]
    lang = "uz" if lang == "uz" else "ru"
    await answer_now(callback, "O'zbekcha ✅" if lang == "uz" else "Русский ✅")
    await db.update_user_language(callback.from_user.id, lang)
    set_profile_lang(callback.from_user.id, lang)
    profile = await get_profile(callback.from_user)
    profile.lang = lang
    await state.clear()
    text = await build_dashboard(profile)
    await safe_edit(callback, text, main_menu_keyboard(lang))

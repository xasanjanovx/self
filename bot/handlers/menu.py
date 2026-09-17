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
from .. import ui
from ..context import db
from ..keyboards import back_to_menu_keyboard, language_keyboard, main_menu_keyboard
from ..profile import Profile, h
from .common import answer_now, get_profile, safe_delete, safe_edit, set_profile_lang

router = Router(name="menu")
logger = logging.getLogger(__name__)

async def build_dashboard(profile: Profile) -> str:
    """Один экран: финансы + питание. Все выборки — параллельно."""
    nutrition_profile, logs, snap, recurring, limits = await asyncio.gather(
        services.nutrition_profile(profile.telegram_id),
        services.today_calorie_logs(profile),
        services.finance_snapshot(profile),
        services.recurring(profile.telegram_id),
        services.budgets(profile.telegram_id),
    )
    lang, cur = profile.lang, profile.currency
    uz = lang == "uz"
    today = profile.today
    name = h(profile.first_name or ("Do'st" if uz else "Друг"))
    b = snap.balances
    month = snap.month

    header = f"{pe.HELLO} <b>{'Assalomu alaykum' if uz else 'Привет'}, {name}</b>\n{pe.CALENDAR} {ui.human_date(today, lang)}"

    # --- финансы: на телефоне узкий экран, поэтому одна строка = один факт
    fin_lines = [
        f"💼 <b>{fin.fmt_money(snap.wallet)} {cur}</b>",
        f"💳 {'Karta' if uz else 'Карта'} {fin.fmt_money(b['card'])}",
        f"💵 {'Naqd' if uz else 'Наличные'} {fin.fmt_money(b['cash'])}",
        "",
    ]
    today_parts = []
    if snap.today_expense:
        today_parts.append(f"{pe.EXPENSE} {fin.fmt_money(snap.today_expense)}")
    if snap.today_income:
        today_parts.append(f"{pe.INCOME} {fin.fmt_money(snap.today_income)}")
    fin_lines.append(f"{'Bugun' if uz else 'Сегодня'}: " + (" · ".join(today_parts) if today_parts else ("hali yo'q" if uz else "пока ничего")))
    if month:
        change = month.expense_change_pct()
        change_text = f" ({'▲' if change > 0 else '▼'}{abs(change):.0f}%)" if change is not None else ""
        fin_lines.append(f"{'Oy' if uz else 'Месяц'}: {pe.EXPENSE} {fin.fmt_money(month.expense)}{change_text}")
        if month.by_category:
            fin_lines.append(f"{'Eng ko`p' if uz else 'Больше всего'}: {cats.label(month.by_category[0][0], lang)} {fin.fmt_money(month.by_category[0][1])}")
    if recurring:
        remaining, pending = fin.recurring_remaining(recurring, today)
        if remaining > 0:
            fin_lines.append(f"🔁 {'To`lovlar' if uz else 'Платежи'}: {fin.fmt_money(remaining)}")
            fin_lines.append(f"{'Erkin' if uz else 'Свободно'}: <b>{fin.fmt_money(snap.wallet - remaining)}</b>")
    if limits and month:
        for st in fin.budget_statuses(month, limits)[:4]:
            flag = "🚫" if st.ratio >= 1 else "⚠️" if st.ratio >= 0.8 else "🎯"
            fin_lines.append(f"{flag} {cats.label(st.category, lang)}: {fin.fmt_money(st.spent)} / {fin.fmt_money(st.limit)} · {ui.pct(st.ratio)}")
    if b["lent"] or b["debt"]:
        fin_lines.append("")
    if b["lent"]:
        fin_lines.append(f"🤝 {'Menga qarz' if uz else 'Мне должны'}: {fin.fmt_money(b['lent'])}")
    if b["debt"]:
        fin_lines.append(f"📌 {'Mening qarzim' if uz else 'Я должен'}: {fin.fmt_money(b['debt'])}")
    finance_card = ui.card(f"{pe.WALLET} <b>{'Moliya' if uz else 'Финансы'}</b>", fin_lines)

    # --- питание
    totals = nutri.totals(logs)
    eaten = float(totals["calories"])
    target = float((nutrition_profile or {}).get("daily_calories") or 0.0)
    if target > 0:
        ratio = eaten / target
        left = max(0.0, target - eaten)
        nut_lines = [
            f"{fin.bar(ratio, 12)} {ui.pct(ratio)}",
            f"<b>{int(eaten)}</b> / {int(target)} {'kkal' if uz else 'ккал'}",
            f"{'Qoldi' if uz else 'Осталось'}: {int(left)} · {int(totals['meals'])} {'qabul' if uz else 'приёмов'}",
        ]
        if logs:
            nut_lines.append(ui.muted(" · ".join(h(str(r.get("meal_desc") or "")[:22]) for r in logs[:4])))
    else:
        nut_lines = [ui.muted("Profil sozlanmagan — «Oziqlanish» bo'limini oching" if uz else "Профиль не настроен — открой раздел «Питание»")]
    nutrition_card = ui.card(f"{pe.NUTRITION} <b>{'Oziqlanish' if uz else 'Питание'}</b>", nut_lines)

    hint = ui.muted("✍️ «taksi 25000» · «osh yedim» · rasm · ovoz" if uz else "✍️ «такси 25000» · «съел плов» · фото · голос")
    return ui.join(header, finance_card, nutrition_card, hint)


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

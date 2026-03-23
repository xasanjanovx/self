from __future__ import annotations

import asyncio
import io
import logging
import re
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)

from .ai import AIService, CalorieEstimate
from .config import load_settings
from .db import Database
from .keyboards import (
    back_to_menu_keyboard,
    calorie_confirm_keyboard,
    calorie_delete_confirm_keyboard,
    calorie_detail_keyboard,
    calorie_panel_keyboard,
    finance_add_confirm_keyboard,
    finance_delete_confirm_keyboard,
    finance_detail_keyboard,
    finance_panel_keyboard,
    habits_keyboard,
    main_menu_keyboard,
    nutrition_goal_keyboard,
    reminders_keyboard,
)
from .reports import build_weekly_summary, export_weekly_csv, export_weekly_pdf
from .states import BotStates

settings = load_settings()
db = Database(settings)
ai_service = AIService(settings)
router = Router()
logger = logging.getLogger(__name__)
TMP_DIR = Path('.tmp')
TMP_DIR.mkdir(parents=True, exist_ok=True)

background_tasks: list[asyncio.Task[Any]] = []

GOAL_TYPE_ALIASES = {
    'weight': 'weight',
    'вес': 'weight',
    'budget': 'budget',
    'бюджет': 'budget',
    'habit': 'habit',
    'привычка': 'habit',
    'привычки': 'habit',
}


def _fmt_money(value: float) -> str:
    return f"{value:,.0f}".replace(',', ' ')


def _user_profile(telegram_id: int) -> tuple[dict[str, Any], str, str]:
    user = db.get_user(telegram_id) or {}
    timezone_name = str(user.get('timezone') or settings.app_timezone)
    currency = str(user.get('currency') or settings.default_currency)
    return user, timezone_name, currency


def _zone(timezone_name: str | None) -> ZoneInfo | timezone:
    key = str(timezone_name or settings.app_timezone or 'UTC')
    try:
        return ZoneInfo(key)
    except Exception:
        try:
            return ZoneInfo('UTC')
        except Exception:
            return timezone.utc


def _today_local(timezone_name: str) -> date:
    return datetime.now(_zone(timezone_name)).date()


def build_dashboard_text(telegram_id: int) -> str:
    _, tz_name, currency = _user_profile(telegram_id)

    calorie = db.get_today_calorie_totals(telegram_id, tz_name=tz_name)
    habits = db.list_today_habits(telegram_id, tz_name=tz_name)
    finance = db.get_today_finance_totals(telegram_id, tz_name=tz_name)
    checkin_done = db.has_checkin_today(telegram_id, tz_name=tz_name)
    streak = db.get_checkin_streak(telegram_id, tz_name=tz_name)

    total_habits = len(habits)
    done_habits = len([h for h in habits if h.get('completed_today')])
    left_habits = max(0, total_habits - done_habits)
    today = _today_local(tz_name).isoformat()

    balance_today = float(finance["income"]) - float(finance["expense"])
    return (
        "FLOWUZ / Dashboard\n"
        f"{today}\n\n"
        "Питание\n"
        f"• {int(calorie['calories'])} ккал, {int(calorie['meals'])} прием.\n\n"
        "Привычки\n"
        f"• {done_habits}/{total_habits} выполнено, в работе {left_habits}\n\n"
        "Финансы (сегодня)\n"
        f"• Доход: +{_fmt_money(finance['income'])} {currency}\n"
        f"• Расход: -{_fmt_money(finance['expense'])} {currency}\n"
        f"• Баланс: {_fmt_money(balance_today)} {currency}\n\n"
        "Чекин\n"
        f"• {'выполнен' if checkin_done else 'ожидается'} · серия {streak} дн."
    )


def format_calorie_estimate(estimate: CalorieEstimate) -> str:
    confidence = f"{round((estimate.confidence or 0.0) * 100)}%"
    return (
        'Питание / Проверка записи\n\n'
        f'Блюдо: {estimate.meal_desc}\n'
        f'Ккал: {estimate.calories if estimate.calories is not None else "-"}\n'
        f'Белки: {estimate.protein if estimate.protein is not None else "-"}\n'
        f'Жиры: {estimate.fat if estimate.fat is not None else "-"}\n'
        f'Углеводы: {estimate.carbs if estimate.carbs is not None else "-"}\n'
        f'Уверенность AI: {confidence}\n\n'
        'Сохранить запись в дневник?'
    )


def goals_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='➕ Добавить цель', callback_data='goal:add')],
            [InlineKeyboardButton(text='⬅️ Назад', callback_data='menu:open')],
        ]
    )


def nutrition_preset(mode: str) -> dict[str, Any]:
    presets = {
        "loss": {
            "mode": "loss",
            "title": "Снижение веса",
            "daily_calories": 1800,
            "protein": 140,
            "fat": 60,
            "carbs": 170,
        },
        "maintain": {
            "mode": "maintain",
            "title": "Поддержание",
            "daily_calories": 2200,
            "protein": 130,
            "fat": 70,
            "carbs": 250,
        },
        "gain": {
            "mode": "gain",
            "title": "Набор веса",
            "daily_calories": 2800,
            "protein": 160,
            "fat": 80,
            "carbs": 350,
        },
        "muscle": {
            "mode": "muscle",
            "title": "Мышечная масса",
            "daily_calories": 2500,
            "protein": 170,
            "fat": 70,
            "carbs": 280,
        },
    }
    return dict(presets.get(mode, presets["maintain"]))


def build_nutrition_setup_text() -> str:
    return (
        "Питание / Профиль\n\n"
        "Выбери цель, и бот рассчитает дневные нормы КБЖУ и остаток на сегодня."
    )


def build_calorie_panel(telegram_id: int) -> tuple[str, list[dict[str, Any]]]:
    _, tz_name, _ = _user_profile(telegram_id)
    profile = db.get_nutrition_profile(telegram_id)
    totals = db.get_today_nutrition_totals(telegram_id, tz_name=tz_name)
    entries = db.list_today_calorie_entries(telegram_id, tz_name=tz_name)

    if not profile:
        lines = [
            "Питание / Трекер",
            "",
            "Сначала выбери цель питания.",
            "",
            "После выбора цели появятся план и остаток на день.",
        ]
        return "\n".join(lines), entries

    target_kcal = float(profile.get("daily_calories") or 0)
    target_p = float(profile.get("protein") or 0)
    target_f = float(profile.get("fat") or 0)
    target_c = float(profile.get("carbs") or 0)

    left_kcal = target_kcal - totals["calories"]
    left_p = target_p - totals["protein"]
    left_f = target_f - totals["fat"]
    left_c = target_c - totals["carbs"]

    lines = [
        "Питание / Трекер",
        f"Цель: {profile.get('title') or '-'}",
        "",
        f"План: {int(target_kcal)} ккал | Б {int(target_p)} Ж {int(target_f)} У {int(target_c)}",
        f"Факт: {int(totals['calories'])} ккал | Б {int(totals['protein'])} Ж {int(totals['fat'])} У {int(totals['carbs'])}",
        f"Остаток: {int(left_kcal)} ккал | Б {int(left_p)} Ж {int(left_f)} У {int(left_c)}",
        "",
        "Ввод: отправь фото или текст блюда.",
        "",
    ]

    if not entries:
        lines.append('Пока нет записей за сегодня.')
    else:
        lines.append('Последние приемы:')
        for entry in entries[:6]:
            meal = str(entry.get('meal_desc') or 'Блюдо').strip()
            calories = entry.get('calories')
            kcal_text = f"{int(float(calories))} ккал" if calories is not None else 'без ккал'
            lines.append(f'- {meal[:40]} ({kcal_text})')

    lines.append('')
    lines.append('Открой запись ниже для деталей.')
    return '\n'.join(lines), entries


def _finance_bucket_from_note(note: str | None) -> str:
    raw = str(note or "").strip()
    match = re.match(r"^\[b:(card|cash|lent|debt)\]\s*", raw, flags=re.IGNORECASE)
    if not match:
        return "card"
    return match.group(1).lower()


def _finance_note_without_bucket(note: str | None) -> str | None:
    raw = str(note or "").strip()
    cleaned = re.sub(r"^\[b:(card|cash|lent|debt)\]\s*", "", raw, flags=re.IGNORECASE).strip()
    return cleaned or None


def _finance_note_with_bucket(note: str | None, bucket: str) -> str:
    clean = _finance_note_without_bucket(note) or ""
    return f"[b:{bucket}] {clean}".strip()


def _finance_bucket_label(bucket: str) -> str:
    labels = {
        "card": "Карта",
        "cash": "Наличные",
        "lent": "Дал в долг",
        "debt": "Мои долги",
    }
    return labels.get(bucket, "Карта")


def _normalize_fin_bucket(bucket: str | None) -> str:
    raw = str(bucket or "").strip().lower()
    if raw in {"card", "cash", "lent", "debt"}:
        return raw
    return "card"


def _infer_fin_bucket(text: str, entry_type: str) -> str:
    lower = text.lower()
    if "нал" in lower or "налич" in lower:
        return "cash"
    if "долг" in lower or "в долг" in lower:
        if any(token in lower for token in ["дал", "одолжил"]):
            return "lent"
        if any(token in lower for token in ["вернули", "получил обратно"]):
            return "lent"
        if any(token in lower for token in ["занял", "взял"]):
            return "debt"
        if any(token in lower for token in ["вернул", "погасил"]):
            return "debt"
        return "debt" if entry_type == "income" else "lent"
    return "card"


def _finance_account_balances(telegram_id: int) -> dict[str, float]:
    rows = db.list_finance_entries_all(telegram_id)
    balances = {"card": 0.0, "cash": 0.0, "lent": 0.0, "debt": 0.0}

    for row in rows:
        bucket = _finance_bucket_from_note(row.get("note"))
        amount = float(row.get("amount") or 0)
        entry_type = str(row.get("entry_type") or "expense")

        if bucket in {"card", "cash"}:
            balances[bucket] += amount if entry_type == "income" else -amount
        elif bucket == "lent":
            balances[bucket] += amount if entry_type == "expense" else -amount
        elif bucket == "debt":
            balances[bucket] += amount if entry_type == "income" else -amount

    return balances


def build_finance_panel(telegram_id: int) -> tuple[str, list[dict[str, Any]]]:
    _, tz_name, currency = _user_profile(telegram_id)
    totals = db.get_today_finance_totals(telegram_id, tz_name=tz_name)
    entries = db.list_today_finance_entries(telegram_id, tz_name=tz_name)
    balances = _finance_account_balances(telegram_id)

    today_balance = float(totals["income"]) - float(totals["expense"])
    lines = [
        "Финансы / Контроль",
        "",
        f"Сегодня: +{_fmt_money(totals['income'])} / -{_fmt_money(totals['expense'])} / { _fmt_money(today_balance) } {currency}",
        f"Карта: {_fmt_money(balances['card'])} {currency} | Наличные: {_fmt_money(balances['cash'])} {currency}",
        f"Дал в долг: {_fmt_money(balances['lent'])} {currency} | Мои долги: {_fmt_money(balances['debt'])} {currency}",
        "",
        "Ввод: текст или голос.",
        "Пример: расход 25000 еда карта",
        "",
    ]

    if not entries:
        lines.append('Операций за сегодня пока нет.')
    else:
        lines.append('Последние операции:')
        for entry in entries[:8]:
            amount = float(entry.get('amount') or 0)
            sign = '+' if str(entry.get('entry_type')) == 'income' else '-'
            category = str(entry.get('category') or 'прочее').strip()
            bucket = _finance_bucket_from_note(entry.get("note"))
            lines.append(f'- {sign}{_fmt_money(amount)} {currency} | {category} | {_finance_bucket_label(bucket)}')

    lines.append('')
    lines.append('Открой операцию ниже для деталей.')
    return '\n'.join(lines), entries


def format_calorie_detail(log: dict[str, Any]) -> str:
    return (
        "Питание / Детали блюда\n\n"
        f"Название: {log.get('meal_desc') or '-'}\n"
        f"Калории: {log.get('calories') if log.get('calories') is not None else '-'}\n"
        f"Белки: {log.get('protein') if log.get('protein') is not None else '-'}\n"
        f"Жиры: {log.get('fat') if log.get('fat') is not None else '-'}\n"
        f"Углеводы: {log.get('carbs') if log.get('carbs') is not None else '-'}\n"
        f"Уверенность AI: {log.get('confidence') if log.get('confidence') is not None else '-'}\n"
    )


def format_finance_detail(entry: dict[str, Any], currency: str) -> str:
    amount = float(entry.get("amount") or 0)
    entry_type = str(entry.get("entry_type") or "expense")
    sign = "+" if entry_type == "income" else "-"
    bucket = _finance_bucket_from_note(entry.get("note"))
    note_clean = _finance_note_without_bucket(entry.get("note"))
    return (
        "Финансы / Детали операции\n\n"
        f"Тип: {'Доход' if entry_type == 'income' else 'Расход'}\n"
        f"Сумма: {sign}{_fmt_money(amount)} {currency}\n"
        f"Категория: {entry.get('category') or '-'}\n"
        f"Счет: {_finance_bucket_label(bucket)}\n"
        f"Заметка: {note_clean or '-'}\n"
    )


def format_finance_pending(items: list[dict[str, Any]], currency: str) -> str:
    lines = ["Финансы / Проверка перед сохранением", ""]
    for item in items:
        amount = float(item.get("amount") or 0)
        entry_type = str(item.get("type") or "expense")
        sign = "+" if entry_type == "income" else "-"
        category = str(item.get("category") or "прочее")
        bucket = _normalize_fin_bucket(item.get("bucket"))
        note = str(item.get("note") or "").strip()
        note_part = f" | {note}" if note else ""
        lines.append(
            f"- {sign}{_fmt_money(amount)} {currency} | {category} | {_finance_bucket_label(bucket)}{note_part}"
        )
    lines.append("")
    lines.append("Сохранить эти операции?")
    return "\n".join(lines)


def _parse_checkin(text: str) -> tuple[int | None, int | None, float | None, str | None]:
    raw = text.strip()
    lower = raw.lower()

    mood: int | None = None
    energy: int | None = None
    weight: float | None = None

    mood_match = re.search(r'(?:настроение|mood)\s*[:=]?\s*(10|[1-9])', lower)
    energy_match = re.search(r'(?:энергия|energy)\s*[:=]?\s*(10|[1-9])', lower)
    weight_match = re.search(r'(?:вес|weight)\s*[:=]?\s*(\d+(?:[.,]\d+)?)', lower)

    if mood_match:
        mood = int(mood_match.group(1))
    if energy_match:
        energy = int(energy_match.group(1))
    if weight_match:
        weight = float(weight_match.group(1).replace(',', '.'))

    if mood is None or energy is None:
        numbers = re.findall(r'\d+(?:[.,]\d+)?', raw)
        if mood is None and numbers:
            candidate = int(float(numbers[0].replace(',', '.')))
            if 1 <= candidate <= 10:
                mood = candidate
        if energy is None and len(numbers) > 1:
            candidate = int(float(numbers[1].replace(',', '.')))
            if 1 <= candidate <= 10:
                energy = candidate
        if weight is None and len(numbers) > 2:
            w = float(numbers[2].replace(',', '.'))
            if w > 20:
                weight = w

    note = raw if raw else None
    return mood, energy, weight, note


def _parse_goal_input(text: str) -> tuple[str, str, float | None] | None:
    parts = [part.strip() for part in text.split(';')]
    if len(parts) < 2:
        return None

    goal_type_raw = parts[0].lower()
    goal_type = GOAL_TYPE_ALIASES.get(goal_type_raw)
    if goal_type is None:
        return None

    title = parts[1]
    if not title:
        return None

    target_value: float | None = None
    if len(parts) >= 3 and parts[2]:
        try:
            target_value = float(parts[2].replace(',', '.'))
        except ValueError:
            target_value = None

    return goal_type, title, target_value


def _parse_reminder_input(text: str) -> tuple[str, str, list[int]] | None:
    parts = [part.strip() for part in text.split(';')]
    if len(parts) < 2:
        return None

    reminder_time = parts[0]
    if not re.fullmatch(r'(?:[01]?\d|2[0-3]):[0-5]\d', reminder_time):
        return None

    reminder_text = parts[1]
    if not reminder_text:
        return None

    days = [1, 2, 3, 4, 5, 6, 7]
    if len(parts) >= 3 and parts[2]:
        try:
            parsed_days = sorted({int(x.strip()) for x in parts[2].split(',') if x.strip()})
            if not parsed_days or any(day < 1 or day > 7 for day in parsed_days):
                return None
            days = parsed_days
        except ValueError:
            return None

    return reminder_time, reminder_text, days


def _day_names(days: list[int]) -> str:
    names = {1: 'Пн', 2: 'Вт', 3: 'Ср', 4: 'Чт', 5: 'Пт', 6: 'Сб', 7: 'Вс'}
    return ', '.join(names.get(day, str(day)) for day in days)


def _goals_text(goals: list[dict[str, Any]]) -> str:
    if not goals:
        return 'Цели пока не добавлены.\nНажми «Добавить цель».'

    goal_labels = {'weight': 'Вес', 'budget': 'Бюджет', 'habit': 'Привычка'}
    rows = ['Цели / Активные']
    for goal in goals[:12]:
        goal_type = str(goal.get('goal_type') or '')
        title = str(goal.get('title') or '')
        target = goal.get('target_value')
        target_text = f' -> {target}' if target is not None else ''
        label = goal_labels.get(goal_type, goal_type or 'Цель')
        rows.append(f'- [{label}] {title}{target_text}')
    return '\n'.join(rows)


def _habits_text(habits: list[dict[str, Any]]) -> str:
    if not habits:
        return 'Привычек пока нет.\nДобавь первую, чтобы начать трекинг.'
    done = len([h for h in habits if h.get('completed_today')])
    total = len(habits)
    return (
        'Привычки / Сегодня\n'
        f'Выполнено: {done}/{total}\n'
        'Нажми на привычку, чтобы отметить выполнение.'
    )


def _reminders_text(reminders: list[dict[str, Any]]) -> str:
    if not reminders:
        return 'Напоминаний пока нет.\nДобавь первое напоминание.'

    lines = ['Напоминания / Активные']
    for rem in reminders[:10]:
        reminder_time = str(rem.get('reminder_time') or '')[:5]
        reminder_text = str(rem.get('reminder_text') or '').strip()
        days = rem.get('days_of_week') or []
        lines.append(f'- {reminder_time} [{_day_names(days)}] {reminder_text}')
    return '\n'.join(lines)


async def ensure_user_message(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    try:
        db.ensure_user(
            telegram_id=user.id,
            username=user.username,
            first_name=user.first_name,
            language=settings.default_language,
            timezone_name=settings.app_timezone,
            currency=settings.default_currency,
        )
    except Exception:
        logger.exception('ensure_user_message failed')


async def ensure_user_callback(callback: CallbackQuery) -> None:
    user = callback.from_user
    try:
        db.ensure_user(
            telegram_id=user.id,
            username=user.username,
            first_name=user.first_name,
            language=settings.default_language,
            timezone_name=settings.app_timezone,
            currency=settings.default_currency,
        )
    except Exception:
        logger.exception('ensure_user_callback failed')


async def safe_delete_message(message: Message | None) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except Exception:
        pass


async def send_main_menu(message: Message, telegram_id: int) -> None:
    try:
        text = build_dashboard_text(telegram_id)
    except Exception:
        logger.exception('build_dashboard_text failed in send_main_menu')
        text = 'Бот запущен. Нажми /menu для главного меню.'
    await message.answer(text, reply_markup=main_menu_keyboard())


async def edit_main_menu(callback: CallbackQuery, telegram_id: int) -> None:
    if callback.message is None:
        return

    try:
        text = build_dashboard_text(telegram_id)
    except Exception:
        logger.exception('build_dashboard_text failed in edit_main_menu')
        text = 'Бот запущен. Нажми /menu для главного меню.'
    try:
        await callback.message.edit_text(text, reply_markup=main_menu_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=main_menu_keyboard())


async def safe_edit_message(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await callback.message.answer(text, reply_markup=reply_markup)


async def _remember_panel(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message:
        await state.update_data(panel_message_id=callback.message.message_id)


async def _edit_panel_from_state(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    data = await state.get_data()
    panel_message_id = data.get('panel_message_id')
    if panel_message_id is not None:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=int(panel_message_id),
                text=text,
                reply_markup=reply_markup,
            )
            return
        except Exception:
            pass

    sent = await message.answer(text, reply_markup=reply_markup)
    await state.update_data(panel_message_id=sent.message_id)


async def force_remove_reply_keyboard(message: Message) -> None:
    try:
        ghost = await message.answer('\u2063', reply_markup=ReplyKeyboardRemove())
        await safe_delete_message(ghost)
    except Exception:
        pass


async def _get_photo_bytes(message: Message) -> tuple[bytes, str, str]:
    photo = message.photo[-1]
    telegram_file = await message.bot.get_file(photo.file_id)
    file_path = str(telegram_file.file_path or '')
    suffix = Path(file_path).suffix.lower()
    mime_type = 'image/png' if suffix == '.png' else 'image/jpeg'

    buffer = io.BytesIO()
    await message.bot.download_file(file_path, destination=buffer)
    return buffer.getvalue(), mime_type, photo.file_id


async def _download_voice_to_temp(message: Message) -> Path:
    voice = message.voice
    audio = message.audio
    media = voice or audio
    if media is None:
        raise ValueError('No voice or audio in message')

    telegram_file = await message.bot.get_file(media.file_id)
    file_path = str(telegram_file.file_path or '')
    suffix = Path(file_path).suffix or ('.ogg' if voice else '.mp3')

    buffer = io.BytesIO()
    await message.bot.download_file(file_path, destination=buffer)

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(buffer.getvalue())
        return Path(tmp.name)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    await state.clear()
    await force_remove_reply_keyboard(message)
    await safe_delete_message(message)
    await send_main_menu(message, message.from_user.id)


@router.message(Command('menu'))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    await state.clear()
    await force_remove_reply_keyboard(message)
    await safe_delete_message(message)
    await send_main_menu(message, message.from_user.id)


@router.message(Command('help'))
async def cmd_help(message: Message) -> None:
    await ensure_user_message(message)
    await force_remove_reply_keyboard(message)
    await safe_delete_message(message)
    await message.answer('Управление ботом через кнопки ниже.\nДля возврата в главное меню: /menu', reply_markup=main_menu_keyboard())


@router.callback_query(F.data == 'noop')
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == 'menu:open')
async def cb_menu_open(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    await state.clear()
    await edit_main_menu(callback, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == 'menu:calorie')
@router.callback_query(F.data == 'calorie:panel')
async def cb_menu_calorie(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    profile = db.get_nutrition_profile(callback.from_user.id)
    if not profile:
        await state.set_state(BotStates.waiting_calorie_input)
        await _remember_panel(callback, state)
        await safe_edit_message(callback, build_nutrition_setup_text(), reply_markup=nutrition_goal_keyboard())
        await callback.answer()
        return

    text, entries = build_calorie_panel(callback.from_user.id)
    await state.set_state(BotStates.waiting_calorie_input)
    await _remember_panel(callback, state)
    await state.update_data(pending_calorie=None)
    await safe_edit_message(callback, text, reply_markup=calorie_panel_keyboard(entries))
    await callback.answer()


@router.callback_query(F.data.startswith('nutri:set:'))
async def cb_nutri_set(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    mode = callback.data.split("nutri:set:", 1)[1]
    if mode == "custom":
        await state.set_state(BotStates.waiting_nutrition_custom)
        await _remember_panel(callback, state)
        await safe_edit_message(
            callback,
            "Ручной план: калории;белки;жиры;углеводы\nПример: 2400;160;70;260",
            reply_markup=back_to_menu_keyboard(),
        )
        await callback.answer()
        return

    profile = nutrition_preset(mode)
    db.save_nutrition_profile(callback.from_user.id, profile)
    text, entries = build_calorie_panel(callback.from_user.id)
    await state.set_state(BotStates.waiting_calorie_input)
    await _remember_panel(callback, state)
    await safe_edit_message(callback, text, reply_markup=calorie_panel_keyboard(entries))
    await callback.answer("Профиль сохранен")


@router.message(BotStates.waiting_nutrition_custom, F.text)
async def msg_nutri_custom(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    raw = (message.text or "").strip()
    parts = [part.strip() for part in raw.split(";")]
    if len(parts) < 4:
        await safe_delete_message(message)
        await _edit_panel_from_state(message, state, "Формат: калории;белки;жиры;углеводы", back_to_menu_keyboard())
        return

    try:
        calories = int(float(parts[0].replace(",", ".")))
        protein = int(float(parts[1].replace(",", ".")))
        fat = int(float(parts[2].replace(",", ".")))
        carbs = int(float(parts[3].replace(",", ".")))
    except Exception:
        await safe_delete_message(message)
        await _edit_panel_from_state(message, state, "Не удалось распознать числа. Пример: 2400;160;70;260", back_to_menu_keyboard())
        return

    if calories <= 0 or protein < 0 or fat < 0 or carbs < 0:
        await safe_delete_message(message)
        await _edit_panel_from_state(message, state, "Проверь значения: они должны быть положительными.", back_to_menu_keyboard())
        return

    profile = {
        "mode": "custom",
        "title": "Свой план",
        "daily_calories": calories,
        "protein": protein,
        "fat": fat,
        "carbs": carbs,
    }
    db.save_nutrition_profile(message.from_user.id, profile)
    await safe_delete_message(message)
    await state.set_state(BotStates.waiting_calorie_input)

    text, entries = build_calorie_panel(message.from_user.id)
    await _edit_panel_from_state(message, state, text, calorie_panel_keyboard(entries))


@router.message(BotStates.waiting_nutrition_custom)
async def msg_nutri_custom_invalid(message: Message) -> None:
    await safe_delete_message(message)


@router.message(BotStates.waiting_calorie_input, F.photo)
async def msg_calorie_input_photo(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    try:
        image_bytes, mime_type, file_id = await _get_photo_bytes(message)
        estimate = await asyncio.to_thread(ai_service.estimate_calories_by_photo, image_bytes, mime_type)
    except Exception as exc:
        logger.exception('Calorie photo analyze failed')
        await safe_delete_message(message)
        text, entries = build_calorie_panel(message.from_user.id)
        text += f'\n\nОшибка анализа фото: {exc}'
        await _edit_panel_from_state(message, state, text, calorie_panel_keyboard(entries))
        return

    await safe_delete_message(message)
    await state.update_data(
        pending_calorie={
            'photo_url': f'tg_file:{file_id}',
            'meal_desc': estimate.meal_desc,
            'calories': estimate.calories,
            'protein': estimate.protein,
            'fat': estimate.fat,
            'carbs': estimate.carbs,
            'confidence': estimate.confidence,
            'advice': None,
        }
    )
    await state.set_state(BotStates.waiting_calorie_confirm)
    await _edit_panel_from_state(message, state, format_calorie_estimate(estimate), calorie_confirm_keyboard())


@router.message(BotStates.waiting_calorie_input, F.text)
async def msg_calorie_input_text(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    raw_text = (message.text or '').strip()
    if not raw_text:
        await safe_delete_message(message)
        text, entries = build_calorie_panel(message.from_user.id)
        text += '\n\nНужен текст блюда или фото.'
        await _edit_panel_from_state(message, state, text, calorie_panel_keyboard(entries))
        return

    try:
        estimate = await asyncio.to_thread(ai_service.estimate_calories_by_text, raw_text)
    except Exception as exc:
        logger.exception('Calorie text analyze failed')
        await safe_delete_message(message)
        text, entries = build_calorie_panel(message.from_user.id)
        text += f'\n\nОшибка анализа: {exc}'
        await _edit_panel_from_state(message, state, text, calorie_panel_keyboard(entries))
        return

    await safe_delete_message(message)
    await state.update_data(
        pending_calorie={
            'photo_url': None,
            'meal_desc': estimate.meal_desc,
            'calories': estimate.calories,
            'protein': estimate.protein,
            'fat': estimate.fat,
            'carbs': estimate.carbs,
            'confidence': estimate.confidence,
            'advice': None,
        }
    )
    await state.set_state(BotStates.waiting_calorie_confirm)
    await _edit_panel_from_state(message, state, format_calorie_estimate(estimate), calorie_confirm_keyboard())


@router.message(BotStates.waiting_calorie_input)
async def msg_calorie_input_invalid(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    await safe_delete_message(message)
    text, entries = build_calorie_panel(message.from_user.id)
    text += '\n\nОтправь фото или текст.'
    await _edit_panel_from_state(message, state, text, calorie_panel_keyboard(entries))


@router.message(BotStates.waiting_calorie_confirm)
async def msg_calorie_confirm_ignore(message: Message) -> None:
    await safe_delete_message(message)


@router.callback_query(F.data == 'calorie:confirm')
async def cb_calorie_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    data = await state.get_data()
    pending = data.get('pending_calorie')
    if not pending:
        await callback.answer('Нет данных для сохранения', show_alert=True)
        return

    try:
        db.add_calorie_log(
            telegram_id=callback.from_user.id,
            photo_url=pending.get('photo_url'),
            meal_desc=str(pending.get('meal_desc') or ''),
            calories=pending.get('calories'),
            protein=pending.get('protein'),
            fat=pending.get('fat'),
            carbs=pending.get('carbs'),
            confidence=pending.get('confidence'),
            advice=None,
        )
    except Exception as exc:
        logger.exception('Calorie save failed')
        await callback.answer(f'Ошибка: {exc}', show_alert=True)
        return

    text, entries = build_calorie_panel(callback.from_user.id)
    await state.set_state(BotStates.waiting_calorie_input)
    await _remember_panel(callback, state)
    await state.update_data(pending_calorie=None)
    await safe_edit_message(callback, text, reply_markup=calorie_panel_keyboard(entries))
    await callback.answer('Запись сохранена')


@router.callback_query(F.data == 'calorie:cancel')
async def cb_calorie_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    text, entries = build_calorie_panel(callback.from_user.id)
    await state.set_state(BotStates.waiting_calorie_input)
    await _remember_panel(callback, state)
    await state.update_data(pending_calorie=None)
    await safe_edit_message(callback, text, reply_markup=calorie_panel_keyboard(entries))
    await callback.answer('Действие отменено')


@router.callback_query(F.data.startswith('calorie:view:'))
async def cb_calorie_view(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    log_id = callback.data.split('calorie:view:', 1)[1]
    log = db.get_calorie_log(callback.from_user.id, log_id)
    if not log:
        await callback.answer('Запись не найдена', show_alert=True)
        return

    await state.set_state(BotStates.waiting_calorie_input)
    await _remember_panel(callback, state)
    await safe_edit_message(callback, format_calorie_detail(log), reply_markup=calorie_detail_keyboard(log_id))
    await callback.answer()


@router.callback_query(F.data.startswith('calorie:ask_del:'))
async def cb_calorie_ask_delete(callback: CallbackQuery) -> None:
    await ensure_user_callback(callback)
    log_id = callback.data.split('calorie:ask_del:', 1)[1]
    await safe_edit_message(
        callback,
        'Удалить запись о блюде?\nДействие необратимо.',
        reply_markup=calorie_delete_confirm_keyboard(log_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith('calorie:del:'))
async def cb_calorie_delete(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    log_id = callback.data.split('calorie:del:', 1)[1]
    try:
        db.delete_calorie_log(callback.from_user.id, log_id)
    except Exception as exc:
        logger.exception('Calorie delete failed')
        await callback.answer(f'Ошибка: {exc}', show_alert=True)
        return

    text, entries = build_calorie_panel(callback.from_user.id)
    await state.set_state(BotStates.waiting_calorie_input)
    await _remember_panel(callback, state)
    await safe_edit_message(callback, text, reply_markup=calorie_panel_keyboard(entries))
    await callback.answer('Запись удалена')


@router.callback_query(F.data == 'menu:finance')
async def cb_menu_finance(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    text, entries = build_finance_panel(callback.from_user.id)
    await state.set_state(BotStates.waiting_finance_input)
    await _remember_panel(callback, state)
    await state.update_data(pending_finance_items=None, pending_finance_source=None)
    await safe_edit_message(callback, text, reply_markup=finance_panel_keyboard(entries))
    await callback.answer()


@router.message(BotStates.waiting_finance_input)
async def msg_finance_input(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)

    if not message.text and not message.voice and not message.audio:
        await safe_delete_message(message)
        text, entries = build_finance_panel(message.from_user.id)
        text += '\n\nНужен текст или голос.'
        await _edit_panel_from_state(message, state, text, finance_panel_keyboard(entries))
        return

    source = 'text_ai'
    raw_text = (message.text or '').strip()

    if message.voice or message.audio:
        source = 'voice_ai'
        temp_path: Path | None = None
        try:
            temp_path = await _download_voice_to_temp(message)
            raw_text = await asyncio.to_thread(ai_service.transcribe_voice, temp_path)
        except Exception as exc:
            logger.exception('Voice transcribe failed')
            await safe_delete_message(message)
            text, entries = build_finance_panel(message.from_user.id)
            text += f'\n\nОшибка распознавания: {exc}'
            await _edit_panel_from_state(message, state, text, finance_panel_keyboard(entries))
            return
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)

    try:
        items = await asyncio.to_thread(ai_service.parse_finance_items, raw_text)
    except Exception as exc:
        logger.exception('Finance parse failed')
        await safe_delete_message(message)
        text, entries = build_finance_panel(message.from_user.id)
        text += f'\n\nОшибка разбора: {exc}'
        await _edit_panel_from_state(message, state, text, finance_panel_keyboard(entries))
        return

    if not items:
        await safe_delete_message(message)
        text, entries = build_finance_panel(message.from_user.id)
        text += '\n\nНе удалось распознать операции.'
        await _edit_panel_from_state(message, state, text, finance_panel_keyboard(entries))
        return

    prepared: list[dict[str, Any]] = []
    for item in items:
        entry_type = str(item.get("type") or "expense")
        amount = float(item.get("amount") or 0)
        if amount <= 0:
            continue
        category = str(item.get("category") or "прочее").strip() or "прочее"
        note = item.get("note")
        bucket = _normalize_fin_bucket(item.get("bucket"))
        if bucket == "card":
            bucket = _infer_fin_bucket(f"{category} {note or ''} {raw_text}", entry_type)

        prepared.append(
            {
                "type": entry_type,
                "amount": amount,
                "category": category,
                "note": note,
                "bucket": bucket,
            }
        )

    if not prepared:
        await safe_delete_message(message)
        text, entries = build_finance_panel(message.from_user.id)
        text += '\n\nНе найдены корректные суммы.'
        await _edit_panel_from_state(message, state, text, finance_panel_keyboard(entries))
        return

    await safe_delete_message(message)
    await state.set_state(BotStates.waiting_finance_confirm)
    await state.update_data(pending_finance_items=prepared, pending_finance_source=source)
    _, _, currency = _user_profile(message.from_user.id)
    confirm_text = format_finance_pending(prepared, currency)
    await _edit_panel_from_state(message, state, confirm_text, finance_add_confirm_keyboard())


@router.message(BotStates.waiting_finance_confirm)
async def msg_finance_confirm_ignore(message: Message) -> None:
    await safe_delete_message(message)


@router.callback_query(F.data == "finance:add_confirm")
async def cb_finance_add_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    data = await state.get_data()
    items = data.get("pending_finance_items") or []
    source = str(data.get("pending_finance_source") or "text_ai")
    if not items:
        await callback.answer("Нет данных для сохранения", show_alert=True)
        return

    for item in items:
        note = _finance_note_with_bucket(item.get("note"), _normalize_fin_bucket(item.get("bucket")))
        db.add_finance_entry(
            telegram_id=callback.from_user.id,
            entry_type=item["type"],
            amount=item["amount"],
            category=item["category"],
            note=note,
            source=source,
        )

    text, entries = build_finance_panel(callback.from_user.id)
    await state.set_state(BotStates.waiting_finance_input)
    await _remember_panel(callback, state)
    await state.update_data(pending_finance_items=None, pending_finance_source=None)
    await safe_edit_message(callback, text, reply_markup=finance_panel_keyboard(entries))
    await callback.answer("Операции сохранены")


@router.callback_query(F.data == "finance:add_cancel")
async def cb_finance_add_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    text, entries = build_finance_panel(callback.from_user.id)
    await state.set_state(BotStates.waiting_finance_input)
    await _remember_panel(callback, state)
    await state.update_data(pending_finance_items=None, pending_finance_source=None)
    await safe_edit_message(callback, text, reply_markup=finance_panel_keyboard(entries))
    await callback.answer("Действие отменено")


@router.callback_query(F.data.startswith('finance:view:'))
async def cb_finance_view(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    entry_id = callback.data.split('finance:view:', 1)[1]
    entry = db.get_finance_entry(callback.from_user.id, entry_id)
    if not entry:
        await callback.answer("Операция не найдена", show_alert=True)
        return

    _, _, currency = _user_profile(callback.from_user.id)
    await state.set_state(BotStates.waiting_finance_input)
    await _remember_panel(callback, state)
    await safe_edit_message(callback, format_finance_detail(entry, currency), reply_markup=finance_detail_keyboard(entry_id))
    await callback.answer()


@router.callback_query(F.data.startswith('finance:ask_del:'))
async def cb_finance_ask_delete(callback: CallbackQuery) -> None:
    await ensure_user_callback(callback)
    entry_id = callback.data.split('finance:ask_del:', 1)[1]
    await safe_edit_message(
        callback,
        "Удалить эту операцию?\nДействие необратимо.",
        reply_markup=finance_delete_confirm_keyboard(entry_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith('finance:del:'))
async def cb_finance_delete(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    entry_id = callback.data.split('finance:del:', 1)[1]
    try:
        db.delete_finance_entry(callback.from_user.id, entry_id)
    except Exception as exc:
        logger.exception('Finance delete failed')
        await callback.answer(f'Ошибка: {exc}', show_alert=True)
        return

    text, entries = build_finance_panel(callback.from_user.id)
    await state.set_state(BotStates.waiting_finance_input)
    await _remember_panel(callback, state)
    await state.update_data(pending_finance_items=None, pending_finance_source=None)
    await safe_edit_message(callback, text, reply_markup=finance_panel_keyboard(entries))
    await callback.answer('Операция удалена')


@router.callback_query(F.data == 'menu:habits')
async def cb_menu_habits(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    await state.clear()
    _, tz_name, _ = _user_profile(callback.from_user.id)
    habits = db.list_today_habits(callback.from_user.id, tz_name=tz_name)
    await safe_edit_message(callback, _habits_text(habits), reply_markup=habits_keyboard(habits))
    await callback.answer()


@router.callback_query(F.data == 'habit:add')
async def cb_habit_add(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    await state.set_state(BotStates.waiting_habit_name)
    await _remember_panel(callback, state)
    await safe_edit_message(callback, 'Введи название привычки.', reply_markup=back_to_menu_keyboard())
    await callback.answer()


@router.message(BotStates.waiting_habit_name, F.text)
async def msg_habit_add(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    name = (message.text or '').strip()
    if not name:
        await safe_delete_message(message)
        await _edit_panel_from_state(message, state, 'Название не может быть пустым.', back_to_menu_keyboard())
        return

    db.add_habit(message.from_user.id, name=name, target_per_week=7)
    await safe_delete_message(message)
    await state.clear()

    _, tz_name, _ = _user_profile(message.from_user.id)
    habits = db.list_today_habits(message.from_user.id, tz_name=tz_name)
    await message.answer(_habits_text(habits), reply_markup=habits_keyboard(habits))


@router.callback_query(F.data.startswith('habit:done:'))
async def cb_habit_done(callback: CallbackQuery) -> None:
    await ensure_user_callback(callback)
    habit_id = callback.data.split('habit:done:', 1)[1]
    try:
        db.mark_habit_done(callback.from_user.id, habit_id=habit_id)
    except Exception as exc:
        logger.exception('Habit done failed')
        await callback.answer(f'Ошибка: {exc}', show_alert=True)
        return

    _, tz_name, _ = _user_profile(callback.from_user.id)
    habits = db.list_today_habits(callback.from_user.id, tz_name=tz_name)
    await safe_edit_message(callback, _habits_text(habits), reply_markup=habits_keyboard(habits))
    await callback.answer('Отмечено')


@router.callback_query(F.data == 'menu:checkin')
async def cb_menu_checkin(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    await state.set_state(BotStates.waiting_checkin)
    await _remember_panel(callback, state)
    await safe_edit_message(
        callback,
        'Ежедневный чекин. Формат: 8 7 78.5 тренировка',
        reply_markup=back_to_menu_keyboard(),
    )
    await callback.answer()


@router.message(BotStates.waiting_checkin, F.text)
async def msg_checkin(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    mood, energy, weight, note = _parse_checkin(message.text or '')

    if mood is None and energy is None and weight is None and not note:
        await safe_delete_message(message)
        await _edit_panel_from_state(message, state, 'Формат не распознан. Пример: 8 7 78.5 тренировка', back_to_menu_keyboard())
        return

    _, tz_name, _ = _user_profile(message.from_user.id)
    checkin_day = _today_local(tz_name)
    db.add_daily_checkin(
        telegram_id=message.from_user.id,
        checkin_date=checkin_day,
        mood=mood,
        energy=energy,
        weight=weight,
        note=note,
    )

    await safe_delete_message(message)
    await state.clear()
    streak = db.get_checkin_streak(message.from_user.id, tz_name=tz_name)
    await message.answer(f'Чекин сохранен. Серия: {streak} дн.', reply_markup=main_menu_keyboard())


@router.callback_query(F.data == 'menu:goals')
async def cb_menu_goals(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    await state.clear()
    goals = db.list_goals(callback.from_user.id, only_active=True)
    await safe_edit_message(callback, _goals_text(goals), reply_markup=goals_keyboard())
    await callback.answer()


@router.callback_query(F.data == 'goal:add')
async def cb_goal_add(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    await state.set_state(BotStates.waiting_goal)
    await _remember_panel(callback, state)
    await safe_edit_message(callback, 'Новая цель: тип;название;значение\nПример: вес;Снизить до 78;78', reply_markup=back_to_menu_keyboard())
    await callback.answer()


@router.message(BotStates.waiting_goal, F.text)
async def msg_goal_add(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    parsed = _parse_goal_input(message.text or '')
    if parsed is None:
        await safe_delete_message(message)
        await _edit_panel_from_state(message, state, 'Неверный формат цели.', back_to_menu_keyboard())
        return

    goal_type, title, target_value = parsed
    db.add_goal(message.from_user.id, goal_type=goal_type, title=title, target_value=target_value)
    await safe_delete_message(message)
    await state.clear()

    goals = db.list_goals(message.from_user.id, only_active=True)
    await message.answer('Цель добавлена.\n' + _goals_text(goals), reply_markup=goals_keyboard())


@router.callback_query(F.data == 'menu:reminders')
async def cb_menu_reminders(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    await state.clear()
    reminders = db.list_reminders(callback.from_user.id)
    await safe_edit_message(callback, _reminders_text(reminders), reply_markup=reminders_keyboard(reminders))
    await callback.answer()


@router.callback_query(F.data == 'rem:add')
async def cb_reminder_add(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    await state.set_state(BotStates.waiting_reminder)
    await _remember_panel(callback, state)
    await safe_edit_message(
        callback,
        'Новое напоминание: HH:MM;текст;дни\nПример: 09:00;Вода;1,2,3,4,5,6,7',
        reply_markup=back_to_menu_keyboard(),
    )
    await callback.answer()


@router.message(BotStates.waiting_reminder, F.text)
async def msg_reminder_add(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    parsed = _parse_reminder_input(message.text or '')
    if parsed is None:
        await safe_delete_message(message)
        await _edit_panel_from_state(message, state, 'Неверный формат напоминания.', back_to_menu_keyboard())
        return

    reminder_time, reminder_text, days = parsed
    _, tz_name, _ = _user_profile(message.from_user.id)
    db.add_reminder(
        telegram_id=message.from_user.id,
        text=reminder_text,
        reminder_time=reminder_time,
        days_of_week=days,
        tz_name=tz_name,
    )

    await safe_delete_message(message)
    await state.clear()
    reminders = db.list_reminders(message.from_user.id)
    await message.answer('Напоминание добавлено.', reply_markup=reminders_keyboard(reminders))


@router.callback_query(F.data.startswith('rem:del:'))
async def cb_reminder_delete(callback: CallbackQuery) -> None:
    await ensure_user_callback(callback)
    reminder_id = callback.data.split('rem:del:', 1)[1]
    try:
        db.delete_reminder(reminder_id, telegram_id=callback.from_user.id)
    except Exception as exc:
        logger.exception('Reminder delete failed')
        await callback.answer(f'Ошибка удаления: {exc}', show_alert=True)
        return

    reminders = db.list_reminders(callback.from_user.id)
    await safe_edit_message(callback, _reminders_text(reminders), reply_markup=reminders_keyboard(reminders))
    await callback.answer('Напоминание удалено')


def _weekly_summary_for_user(telegram_id: int) -> str:
    _, _, currency = _user_profile(telegram_id)
    payload = db.get_weekly_payload(telegram_id)
    return build_weekly_summary(payload, currency=currency)


@router.callback_query(F.data == 'menu:weekly')
async def cb_menu_weekly(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    await state.clear()
    summary = _weekly_summary_for_user(callback.from_user.id)
    await safe_edit_message(callback, summary, reply_markup=back_to_menu_keyboard())
    await callback.answer()


@router.message(Command('weekly'))
async def cmd_weekly(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    await safe_delete_message(message)
    await state.clear()
    summary = _weekly_summary_for_user(message.from_user.id)
    await message.answer(summary, reply_markup=back_to_menu_keyboard())


async def _send_export_files(message: Message, telegram_id: int) -> None:
    _, _, currency = _user_profile(telegram_id)
    payload = db.get_weekly_payload(telegram_id)
    summary = build_weekly_summary(payload, currency=currency)

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    csv_path = TMP_DIR / f'weekly_{telegram_id}_{stamp}.csv'
    pdf_path = TMP_DIR / f'weekly_{telegram_id}_{stamp}.pdf'

    await asyncio.to_thread(export_weekly_csv, payload, csv_path)
    await asyncio.to_thread(export_weekly_pdf, payload, summary, pdf_path, currency)

    await message.answer_document(FSInputFile(str(csv_path)))
    await message.answer_document(FSInputFile(str(pdf_path)))


@router.callback_query(F.data == 'menu:export')
async def cb_export(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    await state.clear()
    if callback.message:
        await _send_export_files(callback.message, callback.from_user.id)
    await callback.answer()


@router.message(Command('export'))
async def cmd_export(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    await safe_delete_message(message)
    await state.clear()
    await _send_export_files(message, message.from_user.id)


@router.callback_query(F.data == 'menu:ai')
async def cb_menu_ai(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    await state.set_state(BotStates.waiting_ai_question)
    await _remember_panel(callback, state)
    await safe_edit_message(callback, 'Задай вопрос AI-помощнику.', reply_markup=back_to_menu_keyboard())
    await callback.answer()


@router.message(Command('ai'))
async def cmd_ai(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    text = message.text or ''
    parts = text.split(maxsplit=1)
    if len(parts) == 1:
        await state.set_state(BotStates.waiting_ai_question)
        await safe_delete_message(message)
        await message.answer('Отправь вопрос отдельным сообщением.', reply_markup=back_to_menu_keyboard())
        return

    question = parts[1].strip()
    if not question:
        await state.set_state(BotStates.waiting_ai_question)
        await safe_delete_message(message)
        await message.answer('Нужен текстовый вопрос.', reply_markup=back_to_menu_keyboard())
        return

    await safe_delete_message(message)
    context = db.get_ai_context(message.from_user.id)
    answer = await asyncio.to_thread(ai_service.assistant_reply, question, context)
    await message.answer(answer, reply_markup=main_menu_keyboard())


@router.message(BotStates.waiting_ai_question, F.text)
async def msg_ai_question(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    question = (message.text or '').strip()
    if not question:
        await safe_delete_message(message)
        await _edit_panel_from_state(message, state, 'Вопрос пустой.', back_to_menu_keyboard())
        return

    try:
        context = db.get_ai_context(message.from_user.id)
        answer = await asyncio.to_thread(ai_service.assistant_reply, question, context)
    except Exception as exc:
        logger.exception('AI reply failed')
        await safe_delete_message(message)
        await _edit_panel_from_state(message, state, f'Ошибка AI: {exc}', back_to_menu_keyboard())
        return

    await safe_delete_message(message)
    await state.clear()
    await message.answer(answer, reply_markup=main_menu_keyboard())


@router.message()
async def fallback_message(message: Message) -> None:
    await ensure_user_message(message)
    text = (message.text or '').strip().lower()
    if text in {'start', '/start', 'menu', '/menu', 'help', '/help'}:
        await safe_delete_message(message)
        await send_main_menu(message, message.from_user.id)
        return
    await safe_delete_message(message)


async def reminder_worker(bot: Bot) -> None:
    while True:
        try:
            now_utc = datetime.now(timezone.utc).replace(second=0, microsecond=0)
            due = db.get_due_reminders(now_utc)
            for reminder in due:
                telegram_id = int(reminder['telegram_id'])
                reminder_text = str(reminder.get('reminder_text') or '').strip()
                await bot.send_message(
                    telegram_id,
                    f'⏰ Напоминание\n{reminder_text}',
                    reply_markup=back_to_menu_keyboard(),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Reminder worker error')
        await asyncio.sleep(max(10, settings.reminder_check_seconds))


async def weekly_report_worker(bot: Bot) -> None:
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            users = db.list_users()
            for user in users:
                telegram_id = int(user['telegram_id'])
                timezone_name = str(user.get('timezone') or settings.app_timezone)
                currency = str(user.get('currency') or settings.default_currency)
                local_now = now_utc.astimezone(_zone(timezone_name))

                if local_now.weekday() != 6:
                    continue

                scheduled = (settings.weekly_report_hour, settings.weekly_report_minute)
                current = (local_now.hour, local_now.minute)
                if current < scheduled:
                    continue

                iso = local_now.isocalendar()
                iso_year = int(iso[0])
                iso_week = int(iso[1])
                if not db.claim_weekly_report(telegram_id, iso_year, iso_week):
                    continue

                payload = db.get_weekly_payload(telegram_id, end_date=local_now.date())
                summary = build_weekly_summary(payload, currency=currency)
                await bot.send_message(
                    telegram_id,
                    f'Недельный отчет\n\n{summary}',
                    reply_markup=back_to_menu_keyboard(),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Weekly worker error')
        await asyncio.sleep(max(60, settings.weekly_report_check_seconds))


async def on_startup(bot: Bot) -> None:
    logger.info('Starting background workers...')
    background_tasks.append(asyncio.create_task(reminder_worker(bot), name='reminder-worker'))
    background_tasks.append(asyncio.create_task(weekly_report_worker(bot), name='weekly-report-worker'))


async def on_shutdown() -> None:
    logger.info('Stopping background workers...')
    for task in background_tasks:
        task.cancel()
    for task in background_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception('Worker stop error')
    background_tasks.clear()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
    )

    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == '__main__':
    asyncio.run(main())


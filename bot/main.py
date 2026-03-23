from __future__ import annotations

import asyncio
import html
import io
import logging
import re
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
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
    calorie_meals_keyboard,
    calorie_panel_keyboard,
    finance_add_confirm_keyboard,
    finance_delete_confirm_keyboard,
    finance_detail_keyboard,
    finance_operations_keyboard,
    finance_panel_keyboard,
    habits_keyboard,
    language_keyboard,
    main_menu_keyboard,
    nutrition_goal_keyboard,
    report_settings_keyboard,
    trainer_keyboard,
)
from .reports import build_weekly_summary
from .states import BotStates

settings = load_settings()
db = Database(settings)
ai_service = AIService(settings)
router = Router()
logger = logging.getLogger(__name__)

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


def _lang_from_user(user: dict[str, Any] | None) -> str:
    lang = str((user or {}).get("language") or settings.default_language).strip().lower()
    return "uz" if lang == "uz" else "ru"


def _tr(lang: str, ru: str, uz: str) -> str:
    return uz if lang == "uz" else ru


def _h(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _display_name(user: dict[str, Any]) -> str:
    first = str(user.get("first_name") or "").strip()
    username = str(user.get("username") or "").strip()
    if username:
        return f"@{username}"
    if first:
        return first
    return "User"


def _lang_for_user_id(telegram_id: int) -> str:
    return _lang_from_user(db.get_user(telegram_id) or {})


def build_dashboard_text(telegram_id: int) -> str:
    user, tz_name, currency = _user_profile(telegram_id)
    lang = _lang_from_user(user)

    nutrition = db.get_today_nutrition_totals(telegram_id, tz_name=tz_name)
    nutrition_profile = db.get_nutrition_profile(telegram_id)
    habits = db.list_today_habits(telegram_id, tz_name=tz_name)
    finance_settings = db.get_finance_settings(telegram_id)
    live_balances = _finance_account_balances(telegram_id)

    card_balance = float(finance_settings.get("card_base") or 0.0) + float(live_balances["card"])
    cash_balance = float(finance_settings.get("cash_base") or 0.0) + float(live_balances["cash"])
    wallet_total = card_balance + cash_balance

    total_habits = len(habits)
    done_habits = len([h for h in habits if h.get("completed_today")])
    left_habits = max(0, total_habits - done_habits)
    today_text = _today_local(tz_name).strftime("%d.%m.%Y")

    target_kcal = float((nutrition_profile or {}).get("daily_calories") or 0.0)
    left_kcal = max(0.0, target_kcal - float(nutrition["calories"]))
    kcal_ratio = f"{int(left_kcal)}/{int(target_kcal)}" if target_kcal > 0 else "0/0"

    first_name = str(user.get("first_name") or "").strip()
    name = _h(first_name or "Друг")
    if lang == "uz":
        return (
            f"Assalomu Alaykum, <b>{name}</b>\n"
            f"<i>Bugun - {today_text}</i>\n\n"
            f"🍽️ <b>Oziqlanish</b>\n"
            f"• Qoldiq: <b>{kcal_ratio}</b> kkal\n"
            f"• Fakt: {int(nutrition['calories'])} kkal, {int(nutrition['meals'])} ta qabul.\n\n"
            f"✅ <b>Odatlar</b>\n"
            f"• {done_habits}/{total_habits} bajarildi, {left_habits} ta qoldi.\n\n"
            f"💰 <b>Moliya</b>\n"
            f"• Balans: <b>{_fmt_money(wallet_total)} {currency}</b>"
        )

    return (
        f"Assalomu Alaykum, <b>{name}</b>\n"
        f"<i>Сегодня - {today_text}</i>\n\n"
        f"🍽️ <b>Питание</b>\n"
        f"• Осталось: <b>{kcal_ratio}</b> ккал\n"
        f"• Факт: {int(nutrition['calories'])} ккал, {int(nutrition['meals'])} прием.\n\n"
        f"✅ <b>Привычки</b>\n"
        f"• {done_habits}/{total_habits} выполнено, {left_habits} осталось.\n\n"
        f"💰 <b>Финансы</b>\n"
        f"• Баланс: <b>{_fmt_money(wallet_total)} {currency}</b>"
    )


def format_calorie_estimate(estimate: CalorieEstimate, lang: str = "ru") -> str:
    confidence = f"{round((estimate.confidence or 0.0) * 100)}%"
    meal = _h(estimate.meal_desc or "-")
    if lang == "uz":
        return (
            "🍽️ <b>Oziqlanish / Tekshiruv</b>\n\n"
            f"Taom: <b>{meal}</b>\n"
            f"Kkal: <b>{estimate.calories if estimate.calories is not None else '-'}</b>\n"
            f"Oqsil: {estimate.protein if estimate.protein is not None else '-'}\n"
            f"Yog': {estimate.fat if estimate.fat is not None else '-'}\n"
            f"Uglevod: {estimate.carbs if estimate.carbs is not None else '-'}\n"
            f"AI ishonchliligi: <i>{confidence}</i>\n\n"
            "Kunlik jurnalga saqlaymizmi?"
        )
    return (
        "🍽️ <b>Питание / Проверка записи</b>\n\n"
        f"Блюдо: <b>{meal}</b>\n"
        f"Ккал: <b>{estimate.calories if estimate.calories is not None else '-'}</b>\n"
        f"Белки: {estimate.protein if estimate.protein is not None else '-'}\n"
        f"Жиры: {estimate.fat if estimate.fat is not None else '-'}\n"
        f"Углеводы: {estimate.carbs if estimate.carbs is not None else '-'}\n"
        f"Уверенность AI: <i>{confidence}</i>\n\n"
        "Сохранить запись в дневник?"
    )


def goals_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    add_text = "➕ Qo'shish" if lang == "uz" else "➕ Добавить цель"
    back_text = "⬅️ Ortga" if lang == "uz" else "⬅️ Назад"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=add_text, callback_data="goal:add")],
            [InlineKeyboardButton(text=back_text, callback_data="menu:open")],
        ]
    )


def _nutrition_goal_title(mode: str, lang: str = "ru") -> str:
    labels_ru = {
        "loss": "Снижение веса",
        "maintain": "Поддержание",
        "gain": "Набор веса",
        "muscle": "Набор мышц",
        "custom": "Свой план",
    }
    labels_uz = {
        "loss": "Vazn kamaytirish",
        "maintain": "Vaznni ushlab turish",
        "gain": "Vazn yig'ish",
        "muscle": "Mushak yig'ish",
        "custom": "Shaxsiy reja",
    }
    labels = labels_uz if lang == "uz" else labels_ru
    return labels.get(mode, labels["maintain"])


def _parse_nutrition_profile(text: str) -> tuple[float, float, int] | None:
    raw = [part.strip() for part in text.split(";")]
    if len(raw) != 3:
        return None
    try:
        weight = float(raw[0].replace(",", "."))
        height = float(raw[1].replace(",", "."))
        age = int(float(raw[2].replace(",", ".")))
    except Exception:
        return None

    if not (25 <= weight <= 350):
        return None
    if not (120 <= height <= 230):
        return None
    if not (12 <= age <= 90):
        return None
    return weight, height, age


def _nutrition_plan_from_profile(goal: str, weight: float, height: float, age: int, lang: str) -> dict[str, Any]:
    safe_goal = goal if goal in {"loss", "maintain", "gain", "muscle"} else "maintain"
    bmi = weight / ((height / 100.0) ** 2) if height > 0 else 0.0

    bmr_male = 10 * weight + 6.25 * height - 5 * age + 5
    bmr_female = 10 * weight + 6.25 * height - 5 * age - 161
    bmr = (bmr_male + bmr_female) / 2.0

    if age >= 45 or bmi >= 32:
        activity = 1.35
    elif age <= 30 and bmi <= 24:
        activity = 1.55
    else:
        activity = 1.45
    tdee = bmr * activity

    calorie_delta = {
        "loss": -450,
        "maintain": 0,
        "gain": 350,
        "muscle": 250,
    }[safe_goal]
    target_kcal = max(1200, int(round(tdee + calorie_delta)))

    protein_mult = {
        "loss": 2.0,
        "maintain": 1.7,
        "gain": 1.8,
        "muscle": 1.9,
    }[safe_goal]
    fat_mult = {
        "loss": 0.8,
        "maintain": 0.9,
        "gain": 1.0,
        "muscle": 0.95,
    }[safe_goal]

    protein = int(round(weight * protein_mult))
    fat = int(round(weight * fat_mult))
    carbs = int(round((target_kcal - protein * 4 - fat * 9) / 4))
    if carbs < 60:
        carbs = 60

    return {
        "mode": safe_goal,
        "title": _nutrition_goal_title(safe_goal, lang),
        "daily_calories": target_kcal,
        "protein": protein,
        "fat": fat,
        "carbs": carbs,
        "weight": round(weight, 1),
        "height": round(height, 1),
        "age": int(age),
        "bmi": round(bmi, 1),
        "tdee": int(round(tdee)),
    }


def build_nutrition_setup_text(lang: str = "ru") -> str:
    return _tr(
        lang,
        "🍽️ <b>Питание / Настройка профиля</b>\n\n"
        "1) Выбери цель.\n"
        "2) Введи профиль: <b>вес;рост;возраст</b>.\n"
        "3) Бот рассчитает персональный дневной план КБЖУ.\n\n"
        "<i>Пример профиля: <code>82;178;27</code></i>",
        "🍽️ <b>Oziqlanish / Profil sozlamasi</b>\n\n"
        "1) Maqsadni tanlang.\n"
        "2) Profilni kiriting: <b>vazn;bo'y;yosh</b>.\n"
        "3) Bot siz uchun kunlik BJU rejani hisoblaydi.\n\n"
        "<i>Profil misoli: <code>82;178;27</code></i>",
    )


def build_calorie_panel(telegram_id: int) -> tuple[str, list[dict[str, Any]]]:
    user, tz_name, _ = _user_profile(telegram_id)
    lang = _lang_from_user(user)
    profile = db.get_nutrition_profile(telegram_id)
    totals = db.get_today_nutrition_totals(telegram_id, tz_name=tz_name)
    entries = db.list_today_calorie_entries(telegram_id, tz_name=tz_name)

    if not profile:
        lines = (
            [
                "🍽️ <b>Oziqlanish / Professional treker</b>",
                "",
                "Avval maqsad va profilni sozlang.",
                "",
                "<i>Shundan keyin kunlik kaloriya rejasi va qoldiq ko'rinadi.</i>",
            ]
            if lang == "uz"
            else [
                "🍽️ <b>Питание / Профессиональный трекер</b>",
                "",
                "Сначала настрой цель и профиль.",
                "",
                "<i>После настройки появятся персональный план и остаток на день.</i>",
            ]
        )
        return "\n".join(lines), entries

    target_kcal = float(profile.get("daily_calories") or 0)
    target_p = float(profile.get("protein") or 0)
    target_f = float(profile.get("fat") or 0)
    target_c = float(profile.get("carbs") or 0)

    left_kcal = max(0.0, target_kcal - totals["calories"])
    left_p = max(0.0, target_p - totals["protein"])
    left_f = max(0.0, target_f - totals["fat"])
    left_c = max(0.0, target_c - totals["carbs"])
    title = _h(profile.get("title") or "-")
    profile_line = ""
    if profile.get("weight") and profile.get("height") and profile.get("age"):
        if lang == "uz":
            profile_line = (
                f"Profil: <b>{float(profile['weight']):.1f} kg / {int(float(profile['height']))} sm / {int(profile['age'])} yosh</b>"
            )
        else:
            profile_line = (
                f"Профиль: <b>{float(profile['weight']):.1f} кг / {int(float(profile['height']))} см / {int(profile['age'])} лет</b>"
            )

    if lang == "uz":
        lines = [
            "🍽️ <b>Oziqlanish / Professional treker</b>",
            f"Maqsad: <b>{title}</b>",
            "",
            "<b>Kunlik metrikalar</b>",
            f"• Qoldiq: <b>{int(left_kcal)}/{int(target_kcal)}</b> kkal",
            f"• Reja: {int(target_kcal)} kkal | O {int(target_p)} Y {int(target_f)} U {int(target_c)}",
            f"• Fakt: {int(totals['calories'])} kkal | O {int(totals['protein'])} Y {int(totals['fat'])} U {int(totals['carbs'])}",
            f"• Qoldiq: {int(left_kcal)} kkal | O {int(left_p)} Y {int(left_f)} U {int(left_c)}",
            "",
            "<i>Taom rasmi yoki tavsifini yuboring.</i>",
            "",
        ]
    else:
        lines = [
            "🍽️ <b>Питание / Профессиональный трекер</b>",
            f"Цель: <b>{title}</b>",
            "",
            "<b>Дневные метрики</b>",
            f"• Осталось: <b>{int(left_kcal)}/{int(target_kcal)}</b> ккал",
            f"• План: {int(target_kcal)} ккал | Б {int(target_p)} Ж {int(target_f)} У {int(target_c)}",
            f"• Факт: {int(totals['calories'])} ккал | Б {int(totals['protein'])} Ж {int(totals['fat'])} У {int(totals['carbs'])}",
            f"• Остаток: {int(left_kcal)} ккал | Б {int(left_p)} Ж {int(left_f)} У {int(left_c)}",
            "",
            "<i>Отправь фото или описание блюда.</i>",
            "",
        ]
    if profile_line:
        lines.insert(2, profile_line)
        lines.insert(3, "")

    lines.append("")
    lines.append(
        "Qabullar ro'yxatini \"Qabullar\" tugmasi orqali oching."
        if lang == "uz"
        else "Открой список через кнопку «Приемы» и выбери период: день, неделя или месяц."
    )
    return "\n".join(lines), entries


def _finance_transfer_from_note(note: str | None) -> tuple[str, str] | None:
    raw = str(note or "").strip()
    match = re.match(r"^\[x:(card|cash|lent|debt)>(card|cash|lent|debt)\]\s*", raw, flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(1).lower(), match.group(2).lower()


def _finance_strip_note_meta(note: str | None) -> str:
    cleaned = str(note or "").strip()
    pattern = r"^\[(?:b:(?:card|cash|lent|debt)|x:(?:card|cash|lent|debt)>(?:card|cash|lent|debt))\]\s*"
    while cleaned:
        next_value = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip()
        if next_value == cleaned:
            break
        cleaned = next_value
    return cleaned


def _finance_bucket_from_note(note: str | None) -> str:
    transfer = _finance_transfer_from_note(note)
    if transfer:
        return transfer[0]

    raw = str(note or "").strip()
    match = re.match(r"^\[b:(card|cash|lent|debt)\]\s*", raw, flags=re.IGNORECASE)
    if not match:
        return "card"
    return match.group(1).lower()


def _finance_note_without_bucket(note: str | None) -> str | None:
    cleaned = _finance_strip_note_meta(note).strip()
    return cleaned or None


def _finance_note_with_bucket(note: str | None, bucket: str) -> str:
    clean = _finance_note_without_bucket(note) or ""
    return f"[b:{bucket}] {clean}".strip()


def _finance_note_with_transfer(note: str | None, from_bucket: str, to_bucket: str) -> str:
    clean = _finance_note_without_bucket(note) or ""
    return f"[x:{from_bucket}>{to_bucket}] {clean}".strip()


def _finance_bucket_label(bucket: str, lang: str = "ru") -> str:
    labels_ru = {
        "card": "Карта",
        "cash": "Наличные",
        "lent": "Дал в долг",
        "debt": "Мои долги",
    }
    labels_uz = {
        "card": "Karta",
        "cash": "Naqd",
        "lent": "Qarzga berilgan",
        "debt": "Mening qarzim",
    }
    labels = labels_uz if lang == "uz" else labels_ru
    fallback = "Karta" if lang == "uz" else "Карта"
    return labels.get(bucket, fallback)


def _finance_transfer_label(from_bucket: str, to_bucket: str, lang: str = "ru") -> str:
    return f"{_finance_bucket_label(from_bucket, lang)} → {_finance_bucket_label(to_bucket, lang)}"


def _normalize_fin_bucket(bucket: str | None) -> str:
    raw = str(bucket or "").strip().lower()
    if raw in {"card", "cash", "lent", "debt"}:
        return raw
    return "card"


def _infer_fin_bucket(text: str, entry_type: str) -> str:
    lower = text.lower()
    if any(token in lower for token in ["нал", "налич", "naqd"]):
        return "cash"
    if any(token in lower for token in ["долг", "в долг", "qarz"]):
        if any(token in lower for token in ["дал", "одолжил", "berdim"]):
            return "lent"
        if any(token in lower for token in ["вернули", "получил обратно", "qaytar"]):
            return "lent"
        if any(token in lower for token in ["занял", "взял", "oldim"]):
            return "debt"
        if any(token in lower for token in ["вернул", "погасил", "to'ladim"]):
            return "debt"
        return "debt" if entry_type == "income" else "lent"
    return "card"


def _extract_amount_from_text(text: str) -> float | None:
    match = re.search(r"(\d[\d\s]*(?:[.,]\d+)?)", text)
    if not match:
        return None
    raw = match.group(1).replace(" ", "").replace(",", ".")
    try:
        amount = float(raw)
    except Exception:
        return None
    return amount if amount > 0 else None


def _split_finance_chunks(text: str) -> list[str]:
    chunks = [part.strip() for part in re.split(r"[\n;,]+", text) if part.strip()]
    return chunks or ([text.strip()] if text.strip() else [])


def _contains_any(text: str, tokens: list[str]) -> bool:
    return any(token in text for token in tokens)


def _pick_bucket_by_text(lower: str, *, for_destination: bool) -> str | None:
    card_tokens = ["карт", "карта", "kart", "karta"]
    cash_tokens = ["налич", "нал", "naqd"]

    if for_destination:
        if _contains_any(lower, ["на карту", "в карту", "kartaga", "to card"]):
            return "card"
        if _contains_any(lower, ["в налич", "наличными", "naqdga", "to cash"]):
            return "cash"
    else:
        if _contains_any(lower, ["с карты", "из карты", "kartadan", "from card"]):
            return "card"
        if _contains_any(lower, ["с налич", "из налич", "наличными", "naqddan", "from cash"]):
            return "cash"

    has_card = _contains_any(lower, card_tokens)
    has_cash = _contains_any(lower, cash_tokens)
    if has_card and not has_cash:
        return "card"
    if has_cash and not has_card:
        return "cash"
    return None


def _transfer_from_chunk(chunk: str, lang: str) -> dict[str, Any] | None:
    lower = chunk.lower()
    amount = _extract_amount_from_text(chunk)
    if amount is None:
        return None

    transfer_words = ["перевод", "перевел", "перекинул", "o'tkaz", "transfer"]
    give_loan_words = ["дал в долг", "одолжил", "qarzga berd"]
    loan_back_words = ["вернули долг", "долг вернули", "получил обратно долг", "qarzni qaytardi", "qarz qaytdi"]
    take_loan_words = ["взял в долг", "занял", "получил в долг", "qarz oldim"]
    repay_loan_words = ["вернул долг", "погасил долг", "оплатил долг", "qarzni to'la", "qarzni yop"]
    card_to_cash_words = ["снял с карты", "обналичил", "наличные с карты", "kartadan naqd"]
    cash_to_card_words = ["внес на карту", "пополнил карту", "положил на карту", "naqdni kartaga", "kartaga naqd"]

    from_bucket: str | None = None
    to_bucket: str | None = None

    if _contains_any(lower, give_loan_words):
        from_bucket = _pick_bucket_by_text(lower, for_destination=False) or "card"
        to_bucket = "lent"
    elif _contains_any(lower, loan_back_words):
        from_bucket = "lent"
        to_bucket = _pick_bucket_by_text(lower, for_destination=True) or "card"
    elif _contains_any(lower, take_loan_words):
        from_bucket = "debt"
        to_bucket = _pick_bucket_by_text(lower, for_destination=True) or "card"
    elif _contains_any(lower, repay_loan_words):
        from_bucket = _pick_bucket_by_text(lower, for_destination=False) or "card"
        to_bucket = "debt"
    elif _contains_any(lower, card_to_cash_words):
        from_bucket, to_bucket = "card", "cash"
    elif _contains_any(lower, cash_to_card_words):
        from_bucket, to_bucket = "cash", "card"
    elif _contains_any(lower, transfer_words):
        from_bucket = _pick_bucket_by_text(lower, for_destination=False)
        to_bucket = _pick_bucket_by_text(lower, for_destination=True)
        if from_bucket is None and "долг" in lower:
            from_bucket = "lent" if "вернули" in lower else "debt"
        if to_bucket is None and "долг" in lower:
            to_bucket = "debt" if "погас" in lower else "lent"

    if not from_bucket or not to_bucket or from_bucket == to_bucket:
        return None

    if lang == "uz":
        if to_bucket == "lent":
            category = "Qarzga berish"
        elif from_bucket == "lent":
            category = "Qarzni qaytarish"
        elif from_bucket == "debt":
            category = "Qarz olish"
        elif to_bucket == "debt":
            category = "Qarz to'lovi"
        else:
            category = "Hisoblar o'tkazmasi"
    else:
        if to_bucket == "lent":
            category = "Выдача долга"
        elif from_bucket == "lent":
            category = "Возврат долга"
        elif from_bucket == "debt":
            category = "Получение в долг"
        elif to_bucket == "debt":
            category = "Погашение долга"
        else:
            category = "Перевод между счетами"

    return {
        "kind": "transfer",
        "amount": amount,
        "category": category,
        "note": chunk.strip(),
        "from_bucket": from_bucket,
        "to_bucket": to_bucket,
    }


def _extract_finance_transfers(raw_text: str, lang: str) -> list[dict[str, Any]]:
    transfers: list[dict[str, Any]] = []
    for chunk in _split_finance_chunks(raw_text):
        transfer = _transfer_from_chunk(chunk, lang)
        if transfer:
            transfers.append(transfer)
    return transfers


def _is_transfer_like_item(item: dict[str, Any]) -> bool:
    text = f"{item.get('category') or ''} {item.get('note') or ''}".lower()
    return _contains_any(
        text,
        [
            "долг",
            "в долг",
            "перевод",
            "карт",
            "налич",
            "qarz",
            "o'tkaz",
            "naqd",
        ],
    )


def _finance_account_balances(telegram_id: int) -> dict[str, float]:
    rows = db.list_finance_entries_all(telegram_id)
    balances = {"card": 0.0, "cash": 0.0, "lent": 0.0, "debt": 0.0}

    for row in rows:
        amount = float(row.get("amount") or 0)
        if amount <= 0:
            continue

        transfer = _finance_transfer_from_note(row.get("note"))
        if transfer:
            from_bucket, to_bucket = transfer
            if from_bucket in balances:
                balances[from_bucket] -= amount
            if to_bucket in balances:
                balances[to_bucket] += amount
            continue

        bucket = _finance_bucket_from_note(row.get("note"))
        entry_type = str(row.get("entry_type") or "expense")

        if bucket in {"card", "cash"}:
            balances[bucket] += amount if entry_type == "income" else -amount
        elif bucket == "lent":
            balances[bucket] += amount if entry_type == "expense" else -amount
        elif bucket == "debt":
            balances[bucket] += amount if entry_type == "income" else -amount

    return balances


def build_finance_panel(telegram_id: int) -> tuple[str, list[dict[str, Any]]]:
    user, tz_name, currency = _user_profile(telegram_id)
    lang = _lang_from_user(user)
    entries = db.list_today_finance_entries(telegram_id, tz_name=tz_name)
    settings_fin = db.get_finance_settings(telegram_id)
    balances_live = _finance_account_balances(telegram_id)
    balances = {
        "card": float(settings_fin.get("card_base") or 0.0) + float(balances_live["card"]),
        "cash": float(settings_fin.get("cash_base") or 0.0) + float(balances_live["cash"]),
        "lent": float(settings_fin.get("lent_base") or 0.0) + float(balances_live["lent"]),
        "debt": float(settings_fin.get("debt_base") or 0.0) + float(balances_live["debt"]),
    }
    monthly_credit = float(settings_fin.get("monthly_credit_payment") or 0.0)
    wallet_total = float(balances["card"]) + float(balances["cash"])
    if lang == "uz":
        lines = [
            "💰 <b>Moliya / Professional nazorat</b>",
            "",
            f"• Balans: <b>{_fmt_money(wallet_total)} {currency}</b>",
            "",
            "<b>Joriy hisoblar</b>",
            f"• Karta: <b>{_fmt_money(balances['card'])} {currency}</b>",
            f"• Naqd: <b>{_fmt_money(balances['cash'])} {currency}</b>",
            f"• Qarzga berilgan: <b>{_fmt_money(balances['lent'])} {currency}</b>",
            f"• Mening qarzim: <b>{_fmt_money(balances['debt'])} {currency}</b>",
            f"• Oylik kredit to'lovi: <b>{_fmt_money(monthly_credit)} {currency}</b>",
            "",
            "<i>Matn yoki ovoz yuboring. Bot oddiy kirim/chiqim va ichki o'tkazmalarni tushunadi.</i>",
            "Misol: <code>dal v dolg 100000 s karty</code>",
            "",
        ]
    else:
        lines = [
            "💰 <b>Финансы / Профессиональный контроль</b>",
            "",
            f"• Баланс: <b>{_fmt_money(wallet_total)} {currency}</b>",
            "",
            "<b>Текущие счета</b>",
            f"• Карта: <b>{_fmt_money(balances['card'])} {currency}</b>",
            f"• Наличные: <b>{_fmt_money(balances['cash'])} {currency}</b>",
            f"• Дал в долг: <b>{_fmt_money(balances['lent'])} {currency}</b>",
            f"• Мои долги: <b>{_fmt_money(balances['debt'])} {currency}</b>",
            f"• Ежемесячный платёж по кредиту: <b>{_fmt_money(monthly_credit)} {currency}</b>",
            "",
            "<i>Ввод: текст или голос. Поддерживаются обычные операции и внутренние переводы.</i>",
            "Пример: <code>дал в долг 100000 с карты</code>",
            "",
        ]

    lines.append("")
    lines.append(
        "Operatsiyalarni \"Operatsiyalar\" tugmasi orqali oching va davrni tanlang: kun/hafta/oy."
        if lang == "uz"
        else "Открой операции через кнопку «Операции» и выбери период: день, неделя или месяц."
    )
    return "\n".join(lines), entries


def _normalize_period(raw: str | None) -> str:
    value = str(raw or "").strip().lower()
    if value in {"day", "week", "month"}:
        return value
    return "day"


def _period_days(period: str) -> int:
    if period == "month":
        return 30
    if period == "week":
        return 7
    return 1


def _period_label(period: str, lang: str = "ru") -> str:
    labels_ru = {"day": "День", "week": "Неделя", "month": "Месяц"}
    labels_uz = {"day": "Kun", "week": "Hafta", "month": "Oy"}
    labels = labels_uz if lang == "uz" else labels_ru
    return labels.get(period, labels["day"])


def _iso_to_ddmmyyyy(value: str | None) -> str:
    raw = str(value or "")[:10]
    try:
        return date.fromisoformat(raw).strftime("%d.%m.%Y")
    except Exception:
        return raw or "-"


def build_finance_operations_panel(telegram_id: int, period: str = "day") -> tuple[str, list[dict[str, Any]]]:
    user, tz_name, currency = _user_profile(telegram_id)
    lang = _lang_from_user(user)
    period = _normalize_period(period)

    if period == "day":
        entries = db.list_today_finance_entries(telegram_id, tz_name=tz_name)
    else:
        entries = db.list_finance_entries(telegram_id, days=_period_days(period))

    if lang == "uz":
        lines = [
            "📂 <b>Moliya / Operatsiyalar</b>",
            f"<i>Davr: {_period_label(period, lang)}</i>",
            "",
        ]
    else:
        lines = [
            "📂 <b>Финансы / Операции</b>",
            f"<i>Период: {_period_label(period, lang)}</i>",
            "",
        ]

    if not entries:
        lines.append("Operatsiyalar topilmadi." if lang == "uz" else "Операции не найдены.")
    else:
        last_day = None
        for entry in entries[:40]:
            day_raw = str(entry.get("entry_date") or str(entry.get("created_at") or "")[:10])
            day = _iso_to_ddmmyyyy(day_raw)
            if day != last_day:
                if last_day is not None:
                    lines.append("")
                lines.append(f"<b>{day}</b>")
                last_day = day

            amount = float(entry.get("amount") or 0)
            category = _h(str(entry.get("category") or ("boshqa" if lang == "uz" else "прочее")).strip())
            transfer = _finance_transfer_from_note(entry.get("note"))
            if transfer:
                from_bucket, to_bucket = transfer
                route = _finance_transfer_label(from_bucket, to_bucket, lang)
                lines.append(f"• ↔ {_fmt_money(amount)} {currency} | {category} | {route}")
            else:
                sign = "+" if str(entry.get("entry_type")) == "income" else "-"
                bucket = _finance_bucket_from_note(entry.get("note"))
                lines.append(f"• {sign}{_fmt_money(amount)} {currency} | {category} | {_finance_bucket_label(bucket, lang)}")

    lines.append("")
    lines.append(
        "Operatsiya tafsilotini pastdagi ro'yxatdan oching."
        if lang == "uz"
        else "Открой операцию из списка ниже для деталей."
    )
    return "\n".join(lines), entries


def build_calorie_meals_panel(telegram_id: int, period: str = "day") -> tuple[str, list[dict[str, Any]]]:
    user, tz_name, _ = _user_profile(telegram_id)
    lang = _lang_from_user(user)
    period = _normalize_period(period)

    if period == "day":
        entries = db.list_today_calorie_entries(telegram_id, tz_name=tz_name)
    else:
        entries = db.list_calorie_logs(telegram_id, days=_period_days(period))

    if lang == "uz":
        lines = [
            "🍽️ <b>Oziqlanish / Qabullar</b>",
            f"<i>Davr: {_period_label(period, lang)}</i>",
            "",
        ]
    else:
        lines = [
            "🍽️ <b>Питание / Приемы</b>",
            f"<i>Период: {_period_label(period, lang)}</i>",
            "",
        ]

    if not entries:
        lines.append("Qabullar topilmadi." if lang == "uz" else "Приемы не найдены.")
    else:
        last_day = None
        for entry in entries[:40]:
            created = str(entry.get("created_at") or "")
            day = _iso_to_ddmmyyyy(created[:10])
            if day != last_day:
                if last_day is not None:
                    lines.append("")
                lines.append(f"<b>{day}</b>")
                last_day = day

            meal = _h(str(entry.get("meal_desc") or ("Taom" if lang == "uz" else "Блюдо")).strip())
            kcal = entry.get("calories")
            kcal_text = f"{int(float(kcal))} kkal" if (lang == "uz" and kcal is not None) else (
                f"{int(float(kcal))} ккал" if kcal is not None else ("kkalsiz" if lang == "uz" else "без ккал")
            )
            lines.append(f"• {meal[:48]} ({kcal_text})")

    lines.append("")
    lines.append(
        "Yozuv tafsiloti uchun pastdagi ro'yxatdan tanlang."
        if lang == "uz"
        else "Выбери запись из списка ниже для детального разбора."
    )
    return "\n".join(lines), entries


def build_finance_settings_text(telegram_id: int) -> str:
    user, _, currency = _user_profile(telegram_id)
    lang = _lang_from_user(user)
    s = db.get_finance_settings(telegram_id)

    if lang == "uz":
        return (
            "⚙️ <b>Moliya / Sozlamalar</b>\n\n"
            f"Karta (boshlang'ich): {_fmt_money(s['card_base'])} {currency}\n"
            f"Naqd (boshlang'ich): {_fmt_money(s['cash_base'])} {currency}\n"
            f"Qarzga berilgan (boshlang'ich): {_fmt_money(s['lent_base'])} {currency}\n"
            f"Mening qarzim (boshlang'ich): {_fmt_money(s['debt_base'])} {currency}\n"
            f"Oylik kredit to'lovi: {_fmt_money(s['monthly_credit_payment'])} {currency}\n\n"
            "Format:\n<i>karta;naqd;qarzga_berilgan;mening_qarzim;oylik_kredit</i>\n"
            "Misol: <code>500000;200000;150000;300000;250000</code>\n\n"
            "<i>Masalan: «qarzga 100000 kartadan berdim» yozsangiz, karta kamayadi va qarzga berilgan summa oshadi.</i>"
        )

    return (
        "⚙️ <b>Финансы / Настройки</b>\n\n"
        f"Карта (база): {_fmt_money(s['card_base'])} {currency}\n"
        f"Наличные (база): {_fmt_money(s['cash_base'])} {currency}\n"
        f"Дал в долг (база): {_fmt_money(s['lent_base'])} {currency}\n"
        f"Мои долги (база): {_fmt_money(s['debt_base'])} {currency}\n"
        f"Ежемесячный кредит: {_fmt_money(s['monthly_credit_payment'])} {currency}\n\n"
        "Формат:\n<i>карта;наличные;дал_в_долг;мои_долги;кредит_в_месяц</i>\n"
        "Пример: <code>500000;200000;150000;300000;250000</code>\n\n"
        "<i>Пример перевода: «дал в долг 100000 с карты» уменьшит карту и увеличит «дал в долг».</i>"
    )


def _parse_finance_settings_input(text: str) -> tuple[float, float, float, float, float] | None:
    parts = [p.strip() for p in text.split(";")]
    if len(parts) != 5:
        return None
    values: list[float] = []
    for part in parts:
        try:
            values.append(float(part.replace(" ", "").replace(",", ".")))
        except Exception:
            return None
    card, cash, lent, debt, monthly_credit = values
    if any(v < 0 for v in values):
        return None
    return card, cash, lent, debt, monthly_credit


def format_calorie_detail(log: dict[str, Any], lang: str = "ru") -> str:
    meal = _h(log.get("meal_desc") or "-")
    if lang == "uz":
        return (
            "🍽️ <b>Taom / Tafsilot</b>\n\n"
            f"Nomi: <b>{meal}</b>\n"
            f"Kkal: {log.get('calories') if log.get('calories') is not None else '-'}\n"
            f"Oqsil: {log.get('protein') if log.get('protein') is not None else '-'}\n"
            f"Yog': {log.get('fat') if log.get('fat') is not None else '-'}\n"
            f"Uglevod: {log.get('carbs') if log.get('carbs') is not None else '-'}\n"
            f"AI ishonchliligi: <i>{log.get('confidence') if log.get('confidence') is not None else '-'}</i>\n"
        )
    return (
        "🍽️ <b>Питание / Детали блюда</b>\n\n"
        f"Название: <b>{meal}</b>\n"
        f"Калории: {log.get('calories') if log.get('calories') is not None else '-'}\n"
        f"Белки: {log.get('protein') if log.get('protein') is not None else '-'}\n"
        f"Жиры: {log.get('fat') if log.get('fat') is not None else '-'}\n"
        f"Углеводы: {log.get('carbs') if log.get('carbs') is not None else '-'}\n"
        f"Уверенность AI: <i>{log.get('confidence') if log.get('confidence') is not None else '-'}</i>\n"
    )


def format_finance_detail(entry: dict[str, Any], currency: str, lang: str = "ru") -> str:
    amount = float(entry.get("amount") or 0)
    entry_type = str(entry.get("entry_type") or "expense")
    sign = "+" if entry_type == "income" else "-"
    transfer = _finance_transfer_from_note(entry.get("note"))
    bucket = _finance_bucket_from_note(entry.get("note"))
    note_clean = _finance_note_without_bucket(entry.get("note"))
    category = _h(entry.get("category") or "-")
    note = _h(note_clean or "-")
    if transfer:
        from_bucket, to_bucket = transfer
        route = _finance_transfer_label(from_bucket, to_bucket, lang)
        if lang == "uz":
            return (
                "💰 <b>Moliya / O'tkazma tafsiloti</b>\n\n"
                "Turi: Ichki o'tkazma\n"
                f"Yo'nalish: <b>{route}</b>\n"
                f"Summa: <b>{_fmt_money(amount)} {currency}</b>\n"
                f"Kategoriya: {category}\n"
                f"Izoh: {note}\n"
            )
        return (
            "💰 <b>Финансы / Детали перевода</b>\n\n"
            "Тип: Внутренний перевод\n"
            f"Маршрут: <b>{route}</b>\n"
            f"Сумма: <b>{_fmt_money(amount)} {currency}</b>\n"
            f"Категория: {category}\n"
            f"Заметка: {note}\n"
        )

    if lang == "uz":
        return (
            "💰 <b>Moliya / Operatsiya tafsiloti</b>\n\n"
            f"Turi: {'Kirim' if entry_type == 'income' else 'Chiqim'}\n"
            f"Summa: <b>{sign}{_fmt_money(amount)} {currency}</b>\n"
            f"Kategoriya: {category}\n"
            f"Hisob: {_finance_bucket_label(bucket, lang)}\n"
            f"Izoh: {note}\n"
        )
    return (
        "💰 <b>Финансы / Детали операции</b>\n\n"
        f"Тип: {'Доход' if entry_type == 'income' else 'Расход'}\n"
        f"Сумма: <b>{sign}{_fmt_money(amount)} {currency}</b>\n"
        f"Категория: {category}\n"
        f"Счет: {_finance_bucket_label(bucket, lang)}\n"
        f"Заметка: {note}\n"
    )


def format_finance_pending(items: list[dict[str, Any]], currency: str, lang: str = "ru") -> str:
    lines = (
        ["💰 <b>Moliya / Saqlashdan oldin tekshiruv</b>", ""]
        if lang == "uz"
        else ["💰 <b>Финансы / Проверка перед сохранением</b>", ""]
    )
    for item in items:
        amount = float(item.get("amount") or 0)
        category = _h(str(item.get("category") or ("boshqa" if lang == "uz" else "прочее")))
        note = _h(str(item.get("note") or "").strip())
        note_part = f" | {note}" if note else ""
        if str(item.get("kind") or "") == "transfer":
            from_bucket = _normalize_fin_bucket(item.get("from_bucket"))
            to_bucket = _normalize_fin_bucket(item.get("to_bucket"))
            route = _finance_transfer_label(from_bucket, to_bucket, lang)
            lines.append(f"- ↔ {_fmt_money(amount)} {currency} | {category} | {route}{note_part}")
        else:
            entry_type = str(item.get("type") or "expense")
            sign = "+" if entry_type == "income" else "-"
            bucket = _normalize_fin_bucket(item.get("bucket"))
            lines.append(
                f"- {sign}{_fmt_money(amount)} {currency} | {category} | {_finance_bucket_label(bucket, lang)}{note_part}"
            )
    lines.append("")
    lines.append("Ushbu operatsiyalarni saqlaymizmi?" if lang == "uz" else "Сохранить эти операции?")
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


def _day_names(days: list[int], lang: str = "ru") -> str:
    names = (
        {1: "Du", 2: "Se", 3: "Cho", 4: "Pa", 5: "Ju", 6: "Sha", 7: "Ya"}
        if lang == "uz"
        else {1: "Пн", 2: "Вт", 3: "Ср", 4: "Чт", 5: "Пт", 6: "Сб", 7: "Вс"}
    )
    return ', '.join(names.get(day, str(day)) for day in days)


def _goals_text(goals: list[dict[str, Any]], lang: str = "ru") -> str:
    if not goals:
        return (
            "🎯 <b>Maqsadlar / Strategiya</b>\n\n"
            "<i>Hozircha maqsadlar yo'q.</i>\n"
            "Yangi maqsad qo'shish uchun pastdagi tugmadan foydalaning."
            if lang == "uz"
            else "🎯 <b>Цели / Стратегия</b>\n\n"
            "<i>Пока нет активных целей.</i>\n"
            "Нажми «Добавить цель», чтобы запустить трекинг прогресса."
        )

    goal_labels = (
        {"weight": "Vazn", "budget": "Byudjet", "habit": "Odat"}
        if lang == "uz"
        else {"weight": "Вес", "budget": "Бюджет", "habit": "Привычка"}
    )
    rows = ["🎯 <b>Maqsadlar / Faol yo'nalishlar</b>" if lang == "uz" else "🎯 <b>Цели / Активные направления</b>"]
    for goal in goals[:12]:
        goal_type = str(goal.get("goal_type") or "")
        title = _h(str(goal.get("title") or ""))
        target = goal.get("target_value")
        target_text = f" | <i>цель: {target}</i>" if target is not None and lang == "ru" else (
            f" | <i>maqsad: {target}</i>" if target is not None else ""
        )
        label = goal_labels.get(goal_type, goal_type or ("Maqsad" if lang == "uz" else "Цель"))
        rows.append(f"• <b>{label}</b>: {title}{target_text}")
    return '\n'.join(rows)


def _habits_text(habits: list[dict[str, Any]], lang: str = "ru") -> str:
    if not habits:
        return (
            "✅ <b>Odatlar / Intizom</b>\n\n"
            "<i>Hozircha odatlar ro'yxati bo'sh.</i>\n"
            "Birinchi odatni qo'shing va kunlik nazoratni boshlang."
            if lang == "uz"
            else "✅ <b>Привычки / Дисциплина</b>\n\n"
            "<i>Список привычек пока пуст.</i>\n"
            "Добавь первую привычку, чтобы включить ежедневный контроль."
        )
    done = len([h for h in habits if h.get('completed_today')])
    total = len(habits)
    if lang == "uz":
        return (
            "✅ <b>Odatlar / Bugungi holat</b>\n"
            f"• Bajarildi: <b>{done}/{total}</b>\n"
            f"• Qoldi: <b>{max(0, total - done)}</b>\n\n"
            "<i>Bajarilganini belgilash uchun odat tugmasini bosing.</i>"
        )
    return (
        "✅ <b>Привычки / Статус дня</b>\n"
        f"• Выполнено: <b>{done}/{total}</b>\n"
        f"• Осталось: <b>{max(0, total - done)}</b>\n\n"
        "<i>Нажми на привычку ниже, чтобы отметить выполнение.</i>"
    )


def _reminders_text(reminders: list[dict[str, Any]], lang: str = "ru") -> str:
    if not reminders:
        return (
            "⏰ <b>Eslatmalar / Avtomatika</b>\n\n"
            "<i>Faol eslatmalar yo'q.</i>\n"
            "Birinchi eslatmani qo'shing."
            if lang == "uz"
            else "⏰ <b>Напоминания / Автоматизация</b>\n\n"
            "<i>Активных напоминаний пока нет.</i>\n"
            "Добавь первое напоминание."
        )

    lines = ["⏰ <b>Eslatmalar / Faol</b>" if lang == "uz" else "⏰ <b>Напоминания / Активные</b>", ""]
    for rem in reminders[:10]:
        reminder_time = str(rem.get("reminder_time") or "")[:5]
        reminder_text = _h(str(rem.get("reminder_text") or "").strip())
        days = rem.get("days_of_week") or []
        lines.append(f"• <b>{reminder_time}</b> [{_day_names(days, lang)}] {reminder_text}")
    return "\n".join(lines)


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
    user = db.get_user(telegram_id) or {}
    lang = _lang_from_user(user)
    try:
        text = build_dashboard_text(telegram_id)
    except Exception:
        logger.exception('build_dashboard_text failed in send_main_menu')
        text = _tr(lang, "Бот запущен. Нажми /menu для главного меню.", "Bot ishga tushdi. Asosiy menyu: /menu")
    await message.answer(text, reply_markup=main_menu_keyboard(lang))


async def edit_main_menu(callback: CallbackQuery, telegram_id: int) -> None:
    if callback.message is None:
        return
    user = db.get_user(telegram_id) or {}
    lang = _lang_from_user(user)

    try:
        text = build_dashboard_text(telegram_id)
    except Exception:
        logger.exception('build_dashboard_text failed in edit_main_menu')
        text = _tr(lang, "Бот запущен. Нажми /menu для главного меню.", "Bot ishga tushdi. Asosiy menyu: /menu")
    try:
        await callback.message.edit_text(text, reply_markup=main_menu_keyboard(lang))
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return
        await callback.message.answer(text, reply_markup=main_menu_keyboard(lang))


async def safe_edit_message(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return
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
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return
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
async def cmd_help(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    await state.clear()
    await force_remove_reply_keyboard(message)
    await safe_delete_message(message)
    await send_main_menu(message, message.from_user.id)


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
    lang = _lang_for_user_id(callback.from_user.id)
    profile = db.get_nutrition_profile(callback.from_user.id)
    if not profile:
        await state.set_state(BotStates.waiting_nutrition_goal)
        await _remember_panel(callback, state)
        await state.update_data(pending_nutri_goal=None)
        await safe_edit_message(callback, build_nutrition_setup_text(lang), reply_markup=nutrition_goal_keyboard(lang))
        await callback.answer()
        return

    text, entries = build_calorie_panel(callback.from_user.id)
    await state.set_state(BotStates.waiting_calorie_input)
    await _remember_panel(callback, state)
    await state.update_data(pending_calorie=None, pending_nutri_goal=None)
    await safe_edit_message(callback, text, reply_markup=calorie_panel_keyboard(entries, lang))
    await callback.answer()


@router.callback_query(F.data == "calorie:goals")
async def cb_calorie_goals(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    await state.set_state(BotStates.waiting_nutrition_goal)
    await _remember_panel(callback, state)
    await state.update_data(pending_nutri_goal=None)
    await safe_edit_message(callback, build_nutrition_setup_text(lang), reply_markup=nutrition_goal_keyboard(lang))
    await callback.answer()


@router.callback_query(F.data.startswith("calorie:meals:"))
async def cb_calorie_meals_period(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    period = _normalize_period(callback.data.split("calorie:meals:", 1)[1])
    text, entries = build_calorie_meals_panel(callback.from_user.id, period)
    await state.set_state(BotStates.waiting_calorie_input)
    await _remember_panel(callback, state)
    await safe_edit_message(callback, text, reply_markup=calorie_meals_keyboard(entries, period, lang))
    await callback.answer()


@router.callback_query(F.data.startswith('nutri:set:'))
async def cb_nutri_set(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    mode = callback.data.split("nutri:set:", 1)[1].strip().lower()
    if mode == "custom":
        await state.set_state(BotStates.waiting_nutrition_custom)
        await _remember_panel(callback, state)
        await safe_edit_message(
            callback,
            _tr(
                lang,
                "Ручной план: калории;белки;жиры;углеводы\nПример: 2400;160;70;260",
                "Qo'lda reja: kaloriya;oqsil;yog';uglevod\nMisol: 2400;160;70;260",
            ),
            reply_markup=back_to_menu_keyboard(lang),
        )
        await callback.answer()
        return

    if mode not in {"loss", "maintain", "gain", "muscle"}:
        mode = "maintain"

    goal_title = _nutrition_goal_title(mode, lang)
    await state.set_state(BotStates.waiting_nutrition_profile)
    await _remember_panel(callback, state)
    await state.update_data(pending_nutri_goal=mode)
    await safe_edit_message(
        callback,
        _tr(
            lang,
            f"🎯 <b>Цель: {goal_title}</b>\n\n"
            "Теперь введи профиль: <b>вес;рост;возраст</b>\n"
            "Пример: <code>82;178;27</code>\n\n"
            "<i>На основе этих данных бот рассчитает персональный план калорий и КБЖУ.</i>",
            f"🎯 <b>Maqsad: {goal_title}</b>\n\n"
            "Endi profilni kiriting: <b>vazn;bo'y;yosh</b>\n"
            "Misol: <code>82;178;27</code>\n\n"
            "<i>Shu ma'lumotlarga asosan bot sizga shaxsiy kaloriya va BJU reja hisoblaydi.</i>",
        ),
        reply_markup=back_to_menu_keyboard(lang),
    )
    await callback.answer()


@router.message(BotStates.waiting_nutrition_goal, F.text)
async def msg_nutri_goal_invalid(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    lang = _lang_for_user_id(message.from_user.id)
    await safe_delete_message(message)
    await _edit_panel_from_state(message, state, build_nutrition_setup_text(lang), nutrition_goal_keyboard(lang))


@router.message(BotStates.waiting_nutrition_goal)
async def msg_nutri_goal_invalid_non_text(message: Message) -> None:
    await safe_delete_message(message)


@router.message(BotStates.waiting_nutrition_profile, F.text)
async def msg_nutri_profile(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    lang = _lang_for_user_id(message.from_user.id)
    parsed = _parse_nutrition_profile(message.text or "")
    if parsed is None:
        await safe_delete_message(message)
        await _edit_panel_from_state(
            message,
            state,
            _tr(
                lang,
                "Неверный формат.\nНужно: <b>вес;рост;возраст</b>\nПример: <code>82;178;27</code>",
                "Format noto'g'ri.\nKerak: <b>vazn;bo'y;yosh</b>\nMisol: <code>82;178;27</code>",
            ),
            back_to_menu_keyboard(lang),
        )
        return

    data = await state.get_data()
    mode = str(data.get("pending_nutri_goal") or "maintain")
    weight, height, age = parsed
    profile = _nutrition_plan_from_profile(mode, weight, height, age, lang)
    db.save_nutrition_profile(message.from_user.id, profile)

    await safe_delete_message(message)
    await state.set_state(BotStates.waiting_calorie_input)
    await state.update_data(pending_nutri_goal=None)
    text, entries = build_calorie_panel(message.from_user.id)
    await _edit_panel_from_state(message, state, text, calorie_panel_keyboard(entries, lang))


@router.message(BotStates.waiting_nutrition_profile)
async def msg_nutri_profile_invalid_non_text(message: Message) -> None:
    await safe_delete_message(message)


@router.message(BotStates.waiting_nutrition_custom, F.text)
async def msg_nutri_custom(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    lang = _lang_for_user_id(message.from_user.id)
    raw = (message.text or "").strip()
    parts = [part.strip() for part in raw.split(";")]
    if len(parts) < 4:
        await safe_delete_message(message)
        await _edit_panel_from_state(
            message,
            state,
            _tr(lang, "Формат: калории;белки;жиры;углеводы", "Format: kaloriya;oqsil;yog';uglevod"),
            back_to_menu_keyboard(lang),
        )
        return

    try:
        calories = int(float(parts[0].replace(",", ".")))
        protein = int(float(parts[1].replace(",", ".")))
        fat = int(float(parts[2].replace(",", ".")))
        carbs = int(float(parts[3].replace(",", ".")))
    except Exception:
        await safe_delete_message(message)
        await _edit_panel_from_state(
            message,
            state,
            _tr(lang, "Не удалось распознать числа. Пример: 2400;160;70;260", "Raqamlarni aniqlab bo'lmadi. Misol: 2400;160;70;260"),
            back_to_menu_keyboard(lang),
        )
        return

    if calories <= 0 or protein < 0 or fat < 0 or carbs < 0:
        await safe_delete_message(message)
        await _edit_panel_from_state(
            message,
            state,
            _tr(lang, "Проверь значения: они должны быть положительными.", "Qiymatlar musbat bo'lishi kerak."),
            back_to_menu_keyboard(lang),
        )
        return

    profile = {
        "mode": "custom",
        "title": _nutrition_goal_title("custom", lang),
        "daily_calories": calories,
        "protein": protein,
        "fat": fat,
        "carbs": carbs,
    }
    db.save_nutrition_profile(message.from_user.id, profile)
    await safe_delete_message(message)
    await state.set_state(BotStates.waiting_calorie_input)
    await state.update_data(pending_nutri_goal=None)

    text, entries = build_calorie_panel(message.from_user.id)
    await _edit_panel_from_state(message, state, text, calorie_panel_keyboard(entries, lang))


@router.message(BotStates.waiting_nutrition_custom)
async def msg_nutri_custom_invalid(message: Message) -> None:
    await safe_delete_message(message)


@router.message(BotStates.waiting_calorie_input, F.photo)
async def msg_calorie_input_photo(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    lang = _lang_for_user_id(message.from_user.id)
    try:
        image_bytes, mime_type, file_id = await _get_photo_bytes(message)
        estimate = await asyncio.to_thread(ai_service.estimate_calories_by_photo, image_bytes, mime_type)
    except Exception as exc:
        logger.exception('Calorie photo analyze failed')
        await safe_delete_message(message)
        text, entries = build_calorie_panel(message.from_user.id)
        text += f"\n\n{_tr(lang, 'Ошибка анализа фото', 'Rasm tahlili xatosi')}: {_h(exc)}"
        await _edit_panel_from_state(message, state, text, calorie_panel_keyboard(entries, lang))
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
    await _edit_panel_from_state(message, state, format_calorie_estimate(estimate, lang), calorie_confirm_keyboard(lang))


@router.message(BotStates.waiting_calorie_input, F.text)
async def msg_calorie_input_text(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    lang = _lang_for_user_id(message.from_user.id)
    raw_text = (message.text or '').strip()
    if not raw_text:
        await safe_delete_message(message)
        text, entries = build_calorie_panel(message.from_user.id)
        text += "\n\n" + _tr(lang, "Нужен текст блюда или фото.", "Taom matni yoki rasmi kerak.")
        await _edit_panel_from_state(message, state, text, calorie_panel_keyboard(entries, lang))
        return

    try:
        estimate = await asyncio.to_thread(ai_service.estimate_calories_by_text, raw_text)
    except Exception as exc:
        logger.exception('Calorie text analyze failed')
        await safe_delete_message(message)
        text, entries = build_calorie_panel(message.from_user.id)
        text += f"\n\n{_tr(lang, 'Ошибка анализа', 'Tahlil xatosi')}: {_h(exc)}"
        await _edit_panel_from_state(message, state, text, calorie_panel_keyboard(entries, lang))
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
    await _edit_panel_from_state(message, state, format_calorie_estimate(estimate, lang), calorie_confirm_keyboard(lang))


@router.message(BotStates.waiting_calorie_input)
async def msg_calorie_input_invalid(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    lang = _lang_for_user_id(message.from_user.id)
    await safe_delete_message(message)
    text, entries = build_calorie_panel(message.from_user.id)
    text += "\n\n" + _tr(lang, "Отправь фото или текст.", "Rasm yoki matn yuboring.")
    await _edit_panel_from_state(message, state, text, calorie_panel_keyboard(entries, lang))


@router.message(BotStates.waiting_calorie_confirm)
async def msg_calorie_confirm_ignore(message: Message) -> None:
    await safe_delete_message(message)


@router.callback_query(F.data == 'calorie:confirm')
async def cb_calorie_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    data = await state.get_data()
    pending = data.get('pending_calorie')
    if not pending:
        await callback.answer(_tr(lang, 'Нет данных для сохранения', "Saqlash uchun ma'lumot yo'q"), show_alert=True)
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
        await callback.answer(f"{_tr(lang, 'Ошибка', 'Xato')}: {_h(exc)}", show_alert=True)
        return

    text, entries = build_calorie_panel(callback.from_user.id)
    await state.set_state(BotStates.waiting_calorie_input)
    await _remember_panel(callback, state)
    await state.update_data(pending_calorie=None)
    await safe_edit_message(callback, text, reply_markup=calorie_panel_keyboard(entries, lang))
    await callback.answer(_tr(lang, 'Запись сохранена', 'Yozuv saqlandi'))


@router.callback_query(F.data == 'calorie:cancel')
async def cb_calorie_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    text, entries = build_calorie_panel(callback.from_user.id)
    await state.set_state(BotStates.waiting_calorie_input)
    await _remember_panel(callback, state)
    await state.update_data(pending_calorie=None)
    await safe_edit_message(callback, text, reply_markup=calorie_panel_keyboard(entries, lang))
    await callback.answer(_tr(lang, 'Действие отменено', 'Amal bekor qilindi'))


@router.callback_query(F.data.startswith('calorie:view:'))
async def cb_calorie_view(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    log_id = callback.data.split('calorie:view:', 1)[1]
    log = db.get_calorie_log(callback.from_user.id, log_id)
    if not log:
        await callback.answer(_tr(lang, 'Запись не найдена', 'Yozuv topilmadi'), show_alert=True)
        return

    await state.set_state(BotStates.waiting_calorie_input)
    await _remember_panel(callback, state)
    await safe_edit_message(callback, format_calorie_detail(log, lang), reply_markup=calorie_detail_keyboard(log_id, lang))
    await callback.answer()


@router.callback_query(F.data.startswith('calorie:ask_del:'))
async def cb_calorie_ask_delete(callback: CallbackQuery) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    log_id = callback.data.split('calorie:ask_del:', 1)[1]
    await safe_edit_message(
        callback,
        _tr(lang, 'Удалить запись о блюде?\nДействие необратимо.', "Taom yozuvini o'chiraymi?\nBu amal qaytarilmaydi."),
        reply_markup=calorie_delete_confirm_keyboard(log_id, lang),
    )
    await callback.answer()


@router.callback_query(F.data.startswith('calorie:del:'))
async def cb_calorie_delete(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    log_id = callback.data.split('calorie:del:', 1)[1]
    try:
        db.delete_calorie_log(callback.from_user.id, log_id)
    except Exception as exc:
        logger.exception('Calorie delete failed')
        await callback.answer(f"{_tr(lang, 'Ошибка', 'Xato')}: {_h(exc)}", show_alert=True)
        return

    text, entries = build_calorie_panel(callback.from_user.id)
    await state.set_state(BotStates.waiting_calorie_input)
    await _remember_panel(callback, state)
    await safe_edit_message(callback, text, reply_markup=calorie_panel_keyboard(entries, lang))
    await callback.answer(_tr(lang, 'Запись удалена', "Yozuv o'chirildi"))


@router.callback_query(F.data == 'menu:finance')
async def cb_menu_finance(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    text, entries = build_finance_panel(callback.from_user.id)
    await state.set_state(BotStates.waiting_finance_input)
    await _remember_panel(callback, state)
    await state.update_data(pending_finance_items=None, pending_finance_source=None)
    await safe_edit_message(callback, text, reply_markup=finance_panel_keyboard(entries, lang))
    await callback.answer()


@router.callback_query(F.data.startswith("finance:ops:"))
async def cb_finance_ops_period(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    period = _normalize_period(callback.data.split("finance:ops:", 1)[1])
    text, entries = build_finance_operations_panel(callback.from_user.id, period)
    await state.set_state(BotStates.waiting_finance_input)
    await _remember_panel(callback, state)
    await safe_edit_message(callback, text, reply_markup=finance_operations_keyboard(entries, period, lang))
    await callback.answer()


@router.callback_query(F.data == "finance:settings")
async def cb_finance_settings(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    await state.set_state(BotStates.waiting_finance_settings)
    await _remember_panel(callback, state)
    await safe_edit_message(callback, build_finance_settings_text(callback.from_user.id), reply_markup=back_to_menu_keyboard(lang))
    await callback.answer()


@router.message(BotStates.waiting_finance_settings, F.text)
async def msg_finance_settings(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    lang = _lang_for_user_id(message.from_user.id)
    parsed = _parse_finance_settings_input(message.text or "")
    if parsed is None:
        await safe_delete_message(message)
        await _edit_panel_from_state(
            message,
            state,
            _tr(
                lang,
                "Неверный формат.\nПример: <code>500000;200000;150000;300000;250000</code>",
                "Noto'g'ri format.\nMisol: <code>500000;200000;150000;300000;250000</code>",
            ),
            back_to_menu_keyboard(lang),
        )
        return

    card, cash, lent, debt, monthly_credit = parsed
    db.save_finance_settings(
        message.from_user.id,
        card_base=card,
        cash_base=cash,
        lent_base=lent,
        debt_base=debt,
        monthly_credit_payment=monthly_credit,
    )

    await safe_delete_message(message)
    await state.set_state(BotStates.waiting_finance_input)
    text, entries = build_finance_panel(message.from_user.id)
    await _edit_panel_from_state(message, state, text, finance_panel_keyboard(entries, lang))


@router.message(BotStates.waiting_finance_settings)
async def msg_finance_settings_invalid(message: Message) -> None:
    await safe_delete_message(message)


@router.message(BotStates.waiting_finance_input)
async def msg_finance_input(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    lang = _lang_for_user_id(message.from_user.id)

    if not message.text and not message.voice and not message.audio:
        await safe_delete_message(message)
        text, entries = build_finance_panel(message.from_user.id)
        text += "\n\n" + _tr(lang, "Нужен текст или голос.", "Matn yoki ovoz kerak.")
        await _edit_panel_from_state(message, state, text, finance_panel_keyboard(entries, lang))
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
            text += f"\n\n{_tr(lang, 'Ошибка распознавания', 'Ovozni aniqlash xatosi')}: {_h(exc)}"
            await _edit_panel_from_state(message, state, text, finance_panel_keyboard(entries, lang))
            return
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)

    parse_error: Exception | None = None
    try:
        items = await asyncio.to_thread(ai_service.parse_finance_items, raw_text)
    except Exception as exc:
        logger.exception('Finance parse failed')
        parse_error = exc
        items = []

    transfers = _extract_finance_transfers(raw_text, lang)

    prepared: list[dict[str, Any]] = []
    transfer_amounts = [float(item.get("amount") or 0) for item in transfers]

    for item in items:
        entry_type = str(item.get("type") or "expense")
        amount = float(item.get("amount") or 0)
        if amount <= 0:
            continue

        if transfer_amounts and _is_transfer_like_item(item):
            if any(abs(amount - transfer_amount) < 0.01 for transfer_amount in transfer_amounts):
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

    prepared.extend(transfers)

    if not prepared:
        await safe_delete_message(message)
        text, entries = build_finance_panel(message.from_user.id)
        if parse_error is not None:
            text += f"\n\n{_tr(lang, 'Ошибка разбора', 'Tahlil xatosi')}: {_h(parse_error)}"
        else:
            text += "\n\n" + _tr(
                lang,
                "Не удалось распознать операции или переводы.",
                "Operatsiya yoki o'tkazmalar aniqlanmadi.",
            )
        await _edit_panel_from_state(message, state, text, finance_panel_keyboard(entries, lang))
        return

    await safe_delete_message(message)
    await state.set_state(BotStates.waiting_finance_confirm)
    await state.update_data(pending_finance_items=prepared, pending_finance_source=source)
    _, _, currency = _user_profile(message.from_user.id)
    confirm_text = format_finance_pending(prepared, currency, lang)
    await _edit_panel_from_state(message, state, confirm_text, finance_add_confirm_keyboard(lang))


@router.message(BotStates.waiting_finance_confirm)
async def msg_finance_confirm_ignore(message: Message) -> None:
    await safe_delete_message(message)


@router.callback_query(F.data == "finance:add_confirm")
async def cb_finance_add_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    data = await state.get_data()
    items = data.get("pending_finance_items") or []
    source = str(data.get("pending_finance_source") or "text_ai")
    if not items:
        await callback.answer(_tr(lang, "Нет данных для сохранения", "Saqlash uchun ma'lumot yo'q"), show_alert=True)
        return

    for item in items:
        if str(item.get("kind") or "") == "transfer":
            from_bucket = _normalize_fin_bucket(item.get("from_bucket"))
            to_bucket = _normalize_fin_bucket(item.get("to_bucket"))
            if from_bucket == to_bucket:
                continue
            note = _finance_note_with_transfer(item.get("note"), from_bucket, to_bucket)
            entry_type = "expense"
        else:
            note = _finance_note_with_bucket(item.get("note"), _normalize_fin_bucket(item.get("bucket")))
            entry_type = str(item.get("type") or "expense")

        db.add_finance_entry(
            telegram_id=callback.from_user.id,
            entry_type=entry_type,
            amount=item["amount"],
            category=item["category"],
            note=note,
            source=source,
        )

    text, entries = build_finance_panel(callback.from_user.id)
    await state.set_state(BotStates.waiting_finance_input)
    await _remember_panel(callback, state)
    await state.update_data(pending_finance_items=None, pending_finance_source=None)
    await safe_edit_message(callback, text, reply_markup=finance_panel_keyboard(entries, lang))
    await callback.answer(_tr(lang, "Операции сохранены", "Operatsiyalar saqlandi"))


@router.callback_query(F.data == "finance:add_cancel")
async def cb_finance_add_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    text, entries = build_finance_panel(callback.from_user.id)
    await state.set_state(BotStates.waiting_finance_input)
    await _remember_panel(callback, state)
    await state.update_data(pending_finance_items=None, pending_finance_source=None)
    await safe_edit_message(callback, text, reply_markup=finance_panel_keyboard(entries, lang))
    await callback.answer(_tr(lang, "Действие отменено", "Amal bekor qilindi"))


@router.callback_query(F.data.startswith('finance:view:'))
async def cb_finance_view(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    entry_id = callback.data.split('finance:view:', 1)[1]
    entry = db.get_finance_entry(callback.from_user.id, entry_id)
    if not entry:
        await callback.answer(_tr(lang, "Операция не найдена", "Operatsiya topilmadi"), show_alert=True)
        return

    _, _, currency = _user_profile(callback.from_user.id)
    await state.set_state(BotStates.waiting_finance_input)
    await _remember_panel(callback, state)
    await safe_edit_message(
        callback,
        format_finance_detail(entry, currency, lang),
        reply_markup=finance_detail_keyboard(entry_id, lang),
    )
    await callback.answer()


@router.callback_query(F.data.startswith('finance:ask_del:'))
async def cb_finance_ask_delete(callback: CallbackQuery) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    entry_id = callback.data.split('finance:ask_del:', 1)[1]
    await safe_edit_message(
        callback,
        _tr(lang, "Удалить эту операцию?\nДействие необратимо.", "Bu operatsiyani o'chiraymi?\nBu amal qaytarilmaydi."),
        reply_markup=finance_delete_confirm_keyboard(entry_id, lang),
    )
    await callback.answer()


@router.callback_query(F.data.startswith('finance:del:'))
async def cb_finance_delete(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    entry_id = callback.data.split('finance:del:', 1)[1]
    try:
        db.delete_finance_entry(callback.from_user.id, entry_id)
    except Exception as exc:
        logger.exception('Finance delete failed')
        await callback.answer(f"{_tr(lang, 'Ошибка', 'Xato')}: {_h(exc)}", show_alert=True)
        return

    text, entries = build_finance_panel(callback.from_user.id)
    await state.set_state(BotStates.waiting_finance_input)
    await _remember_panel(callback, state)
    await state.update_data(pending_finance_items=None, pending_finance_source=None)
    await safe_edit_message(callback, text, reply_markup=finance_panel_keyboard(entries, lang))
    await callback.answer(_tr(lang, 'Операция удалена', "Operatsiya o'chirildi"))


@router.callback_query(F.data == 'menu:habits')
async def cb_menu_habits(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    await state.clear()
    _, tz_name, _ = _user_profile(callback.from_user.id)
    habits = db.list_today_habits(callback.from_user.id, tz_name=tz_name)
    await safe_edit_message(callback, _habits_text(habits, lang), reply_markup=habits_keyboard(habits, lang))
    await callback.answer()


@router.callback_query(F.data == 'habit:add')
async def cb_habit_add(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    await state.set_state(BotStates.waiting_habit_name)
    await _remember_panel(callback, state)
    await safe_edit_message(
        callback,
        _tr(lang, 'Введи название привычки.', "Odat nomini kiriting."),
        reply_markup=back_to_menu_keyboard(lang),
    )
    await callback.answer()


@router.message(BotStates.waiting_habit_name, F.text)
async def msg_habit_add(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    lang = _lang_for_user_id(message.from_user.id)
    name = (message.text or '').strip()
    if not name:
        await safe_delete_message(message)
        await _edit_panel_from_state(
            message,
            state,
            _tr(lang, 'Название не может быть пустым.', "Nom bo'sh bo'lmasligi kerak."),
            back_to_menu_keyboard(lang),
        )
        return

    db.add_habit(message.from_user.id, name=name, target_per_week=7)
    await safe_delete_message(message)
    await state.clear()

    _, tz_name, _ = _user_profile(message.from_user.id)
    habits = db.list_today_habits(message.from_user.id, tz_name=tz_name)
    await message.answer(_habits_text(habits, lang), reply_markup=habits_keyboard(habits, lang))


@router.callback_query(F.data.startswith('habit:done:'))
async def cb_habit_done(callback: CallbackQuery) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    habit_id = callback.data.split('habit:done:', 1)[1]
    try:
        db.mark_habit_done(callback.from_user.id, habit_id=habit_id)
    except Exception as exc:
        logger.exception('Habit done failed')
        await callback.answer(f"{_tr(lang, 'Ошибка', 'Xato')}: {_h(exc)}", show_alert=True)
        return

    _, tz_name, _ = _user_profile(callback.from_user.id)
    habits = db.list_today_habits(callback.from_user.id, tz_name=tz_name)
    await safe_edit_message(callback, _habits_text(habits, lang), reply_markup=habits_keyboard(habits, lang))
    await callback.answer(_tr(lang, 'Отмечено', 'Belgilandi'))


@router.callback_query(F.data == 'menu:checkin')
async def cb_menu_checkin(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    await state.clear()
    await safe_edit_message(
        callback,
        _tr(
            lang,
            "Раздел <b>Чекин</b> отключен в этой версии бота.",
            "<b>Chekin</b> bo'limi bu versiyada o'chirilgan.",
        ),
        reply_markup=back_to_menu_keyboard(lang),
    )
    await callback.answer(_tr(lang, "Отключено", "O'chirilgan"))


@router.message(BotStates.waiting_checkin, F.text)
async def msg_checkin(message: Message, state: FSMContext) -> None:
    await safe_delete_message(message)
    await state.clear()


@router.callback_query(F.data == 'menu:goals')
async def cb_menu_goals(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    await state.clear()
    goals = db.list_goals(callback.from_user.id, only_active=True)
    await safe_edit_message(callback, _goals_text(goals, lang), reply_markup=goals_keyboard(lang))
    await callback.answer()


@router.callback_query(F.data == 'goal:add')
async def cb_goal_add(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    await state.set_state(BotStates.waiting_goal)
    await _remember_panel(callback, state)
    await safe_edit_message(
        callback,
        _tr(
            lang,
            'Новая цель: тип;название;значение\nПример: вес;Снизить до 78;78',
            "Yangi maqsad: turi;nomi;qiymat\nMisol: вес;78 gacha tushish;78",
        ),
        reply_markup=back_to_menu_keyboard(lang),
    )
    await callback.answer()


@router.message(BotStates.waiting_goal, F.text)
async def msg_goal_add(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    lang = _lang_for_user_id(message.from_user.id)
    parsed = _parse_goal_input(message.text or '')
    if parsed is None:
        await safe_delete_message(message)
        await _edit_panel_from_state(
            message,
            state,
            _tr(lang, 'Неверный формат цели.', "Maqsad formati noto'g'ri."),
            back_to_menu_keyboard(lang),
        )
        return

    goal_type, title, target_value = parsed
    db.add_goal(message.from_user.id, goal_type=goal_type, title=title, target_value=target_value)
    await safe_delete_message(message)
    await state.clear()

    goals = db.list_goals(message.from_user.id, only_active=True)
    await message.answer(
        _tr(lang, "Цель добавлена.\n", "Maqsad qo'shildi.\n") + _goals_text(goals, lang),
        reply_markup=goals_keyboard(lang),
    )


@router.callback_query(F.data == 'menu:reminders')
async def cb_menu_reminders(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    await state.clear()
    await safe_edit_message(
        callback,
        _tr(
            lang,
            "Раздел <b>Напоминания</b> отключен в этой версии бота.",
            "<b>Eslatmalar</b> bo'limi bu versiyada o'chirilgan.",
        ),
        reply_markup=back_to_menu_keyboard(lang),
    )
    await callback.answer(_tr(lang, "Отключено", "O'chirilgan"))


@router.callback_query(F.data.startswith("rem:"))
async def cb_reminder_disabled(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    await state.clear()
    lang = _lang_for_user_id(callback.from_user.id)
    await callback.answer(_tr(lang, "Раздел отключен", "Bo'lim o'chirilgan"), show_alert=True)


@router.message(BotStates.waiting_reminder)
async def msg_reminder_disabled(message: Message, state: FSMContext) -> None:
    await safe_delete_message(message)
    await state.clear()


def _report_prefs_label(lang: str, enabled: bool, frequency: str) -> str:
    if not enabled:
        return _tr(lang, "Отключены", "O'chirilgan")
    return _tr(lang, "Раз в неделю", "Haftada bir marta") if frequency == "weekly" else _tr(lang, "Раз в месяц", "Oyda bir marta")


def _report_days(enabled: bool, frequency: str) -> int:
    if not enabled:
        return 7
    return 30 if frequency == "monthly" else 7


def _report_summary_for_user(telegram_id: int, *, days: int) -> str:
    user, _, currency = _user_profile(telegram_id)
    lang = _lang_from_user(user)
    payload = db.get_period_payload(telegram_id, days=days)
    summary = build_weekly_summary(payload, currency=currency)
    title = _tr(lang, "Недельный срез" if days == 7 else "Месячный срез", "Haftalik kesim" if days == 7 else "Oylik kesim")
    return f"<b>{title}</b>\n{_h(summary)}"


def _report_panel_text(telegram_id: int) -> tuple[str, dict[str, Any], str]:
    user = db.get_user(telegram_id) or {}
    lang = _lang_from_user(user)
    prefs = db.get_report_preferences(telegram_id)
    enabled = bool(prefs.get("enabled", True))
    frequency = str(prefs.get("frequency") or "weekly")
    days = _report_days(enabled, frequency)
    summary = _report_summary_for_user(telegram_id, days=days)
    status = _report_prefs_label(lang, enabled, frequency)
    text = (
        f"📊 <b>{_tr(lang, 'Отчет / Аналитика', 'Hisobot / Analitika')}</b>\n"
        f"<i>{_tr(lang, 'Авто-уведомления', 'Avto-xabarnomalar')}: {status}</i>\n\n"
        f"{summary}\n\n"
        f"{_tr(lang, 'Выбери режим уведомлений ниже.', 'Quyida xabarnoma rejimini tanlang.')}"
    )
    return text, prefs, lang


@router.callback_query(F.data == "menu:report")
@router.callback_query(F.data == "menu:weekly")
async def cb_menu_report(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    await state.clear()
    text, prefs, lang = _report_panel_text(callback.from_user.id)
    await safe_edit_message(
        callback,
        text,
        reply_markup=report_settings_keyboard(
            lang,
            frequency=str(prefs.get("frequency") or "weekly"),
            enabled=bool(prefs.get("enabled", True)),
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("report:set:"))
async def cb_report_set(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    await state.clear()
    lang = _lang_for_user_id(callback.from_user.id)
    mode = callback.data.split("report:set:", 1)[1].strip().lower()
    current = db.get_report_preferences(callback.from_user.id)
    last_key = current.get("last_sent_key")

    if mode == "off":
        db.save_report_preferences(
            callback.from_user.id,
            enabled=False,
            frequency=str(current.get("frequency") or "weekly"),
            last_sent_key=last_key,
        )
    else:
        frequency = "monthly" if mode == "monthly" else "weekly"
        db.save_report_preferences(
            callback.from_user.id,
            enabled=True,
            frequency=frequency,
            last_sent_key=last_key,
        )

    text, prefs, _ = _report_panel_text(callback.from_user.id)
    await safe_edit_message(
        callback,
        text,
        reply_markup=report_settings_keyboard(
            lang,
            frequency=str(prefs.get("frequency") or "weekly"),
            enabled=bool(prefs.get("enabled", True)),
        ),
    )
    await callback.answer(_tr(lang, "Настройки обновлены", "Sozlamalar yangilandi"))


@router.message(Command("weekly"))
@router.message(Command("report"))
async def cmd_report(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    await safe_delete_message(message)
    await state.clear()
    text, prefs, lang = _report_panel_text(message.from_user.id)
    await message.answer(
        text,
        reply_markup=report_settings_keyboard(
            lang,
            frequency=str(prefs.get("frequency") or "weekly"),
            enabled=bool(prefs.get("enabled", True)),
        ),
    )


@router.callback_query(F.data == "menu:language")
async def cb_menu_language(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    await state.clear()
    lang = _lang_for_user_id(callback.from_user.id)
    await safe_edit_message(
        callback,
        _tr(lang, "🌐 <b>Выбери язык интерфейса</b>", "🌐 <b>Interfeys tilini tanlang</b>"),
        reply_markup=language_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("lang:set:"))
async def cb_set_language(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    await state.clear()
    selected = callback.data.split("lang:set:", 1)[1].strip().lower()
    selected = "uz" if selected == "uz" else "ru"
    db.update_user_language(callback.from_user.id, selected)
    await edit_main_menu(callback, callback.from_user.id)
    await callback.answer("Til yangilandi" if selected == "uz" else "Язык обновлен")


EXERCISE_GIF_LINKS = {
    "pushups": "https://commons.wikimedia.org/wiki/File:Pushups.gif",
    "squats": "https://commons.wikimedia.org/wiki/File:Squats_01.gif",
    "jumping": "https://commons.wikimedia.org/wiki/File:Jumpingjacks.gif",
    "mobility": "https://commons.wikimedia.org/wiki/File:Five_tibetan_rite_5.gif",
}


def _parse_trainer_profile(text: str) -> tuple[float, float, int] | None:
    raw = [p.strip() for p in text.split(";")]
    if len(raw) != 3:
        return None
    try:
        weight = float(raw[0].replace(",", "."))
        height = float(raw[1].replace(",", "."))
        age = int(float(raw[2].replace(",", ".")))
    except Exception:
        return None
    if weight <= 0 or height <= 0 or age <= 0:
        return None
    return weight, height, age


def _trainer_weekly_plan(goal: str, weight: float, height: float, age: int, lang: str) -> str:
    bmi = weight / ((height / 100.0) ** 2) if height > 0 else 0.0
    if age >= 45 or bmi >= 33:
        level = _tr(lang, "базовый", "boshlang'ich")
        rest = "90"
    elif age <= 30 and bmi < 27:
        level = _tr(lang, "средний+", "o'rta+")
        rest = "60"
    else:
        level = _tr(lang, "средний", "o'rta")
        rest = "75"

    if goal == "muscle":
        title = _tr(lang, "💪 <b>Тренер / Недельный план на набор мышц</b>", "💪 <b>Trener / Mushak yig'ish uchun haftalik reja</b>")
        week = _tr(
            lang,
            "Пн: Верх тела (отжимания, тяга резинкой, планка)\n"
            "Вт: Низ тела (приседания, выпады, ягодичный мост)\n"
            "Ср: Легкое кардио + мобилити\n"
            "Чт: Верх тела (вариации отжиманий + корпус)\n"
            "Пт: Низ тела (присед, болгарские выпады, икры)\n"
            "Сб: Функционал + корпус\n"
            "Вс: Отдых",
            "Du: Yuqori tana (otjimaniya, rezina bilan tortish, planka)\n"
            "Se: Pastki tana (o'tirib-turish, vypad, glute bridge)\n"
            "Cho: Yengil kardio + mobiliti\n"
            "Pa: Yuqori tana (otjimaniya variantlari + core)\n"
            "Ju: Pastki tana (squat, bolgar vypadlari, boldir)\n"
            "Sha: Funksional + core\n"
            "Ya: Dam olish",
        )
    else:
        title = _tr(lang, "🔥 <b>Тренер / Недельный план для снижения жира</b>", "🔥 <b>Trener / Yog' kamaytirish uchun haftalik reja</b>")
        week = _tr(
            lang,
            "Пн: Интервальное кардио + корпус\n"
            "Вт: Силовой круг (ноги/грудь/спина)\n"
            "Ср: Активное восстановление (ходьба 8-10к шагов)\n"
            "Чт: Интервалы + мобилити\n"
            "Пт: Силовой круг + пресс\n"
            "Сб: Длинная кардио-сессия 35-45 мин\n"
            "Вс: Отдых",
            "Du: Interval kardio + core\n"
            "Se: Kuch aylana mashqlari (oyoq/ko'krak/orqa)\n"
            "Cho: Faol tiklanish (8-10k qadam)\n"
            "Pa: Intervallar + mobiliti\n"
            "Ju: Kuch aylana mashqlari + press\n"
            "Sha: Uzoq kardio 35-45 daqiqa\n"
            "Ya: Dam olish",
        )

    gif_block = _tr(
        lang,
        f"<b>GIF техника:</b>\n"
        f"• Отжимания: {EXERCISE_GIF_LINKS['pushups']}\n"
        f"• Приседания: {EXERCISE_GIF_LINKS['squats']}\n"
        f"• Jumping Jacks: {EXERCISE_GIF_LINKS['jumping']}\n"
        f"• Мобилити: {EXERCISE_GIF_LINKS['mobility']}",
        f"<b>GIF texnika:</b>\n"
        f"• Otjimaniya: {EXERCISE_GIF_LINKS['pushups']}\n"
        f"• O'tirib-turish: {EXERCISE_GIF_LINKS['squats']}\n"
        f"• Jumping Jacks: {EXERCISE_GIF_LINKS['jumping']}\n"
        f"• Mobiliti: {EXERCISE_GIF_LINKS['mobility']}",
    )
    rest_label = _tr(lang, "Отдых между подходами", "Setlar oralig'ida dam")
    years_label = _tr(lang, "лет", "yosh")
    profile_label = _tr(lang, "Профиль", "Profil")
    level_label = _tr(lang, "Уровень", "Daraja")
    sec_label = _tr(lang, "сек", "soniya")

    return (
        f"{title}\n\n"
        f"{profile_label}: {weight:.1f} кг, {height:.0f} см, {age} {years_label}\n"
        f"BMI: {bmi:.1f} | {level_label}: <b>{level}</b>\n"
        f"{rest_label}: {rest} {sec_label}\n\n"
        f"{week}\n\n"
        f"{gif_block}\n\n"
        f"<i>{_tr(lang, 'Важно: следи за техникой и самочувствием.', 'Muhim: texnika va holatni nazorat qiling.')}</i>"
    )


@router.callback_query(F.data == "menu:trainer")
async def cb_menu_trainer(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    await state.clear()
    lang = _lang_for_user_id(callback.from_user.id)
    await safe_edit_message(
        callback,
        _tr(
            lang,
            "🏋️ <b>Тренер / Персональный модуль</b>\n\n"
            "Выбери цель. Далее бот запросит <b>вес;рост;возраст</b> и соберёт недельный план.\n"
            "<i>План включает нагрузку, отдых и ссылки на технику упражнений.</i>",
            "🏋️ <b>Trener / Shaxsiy modul</b>\n\n"
            "Maqsadni tanlang. Keyin bot <b>vazn;bo'y;yosh</b> so'rab haftalik reja tuzadi.\n"
            "<i>Rejada yuklama, dam olish va mashqlar texnikasi havolalari bo'ladi.</i>",
        ),
        reply_markup=trainer_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("trainer:plan:"))
async def cb_trainer_plan(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    mode = callback.data.split("trainer:plan:", 1)[1].strip().lower()
    goal = "muscle" if mode == "muscle" else "fat"
    await state.set_state(BotStates.waiting_trainer_profile)
    await _remember_panel(callback, state)
    await state.update_data(trainer_goal=goal)
    await safe_edit_message(
        callback,
        _tr(
            lang,
            "Введите профиль: <b>вес;рост;возраст</b>\n"
            "Пример: <code>82;178;27</code>\n"
            "<i>На основе профиля рассчитаю безопасный недельный план.</i>",
            "Profilni kiriting: <b>vazn;bo'y;yosh</b>\n"
            "Misol: <code>82;178;27</code>\n"
            "<i>Profil asosida xavfsiz haftalik reja hisoblayman.</i>",
        ),
        reply_markup=back_to_menu_keyboard(lang),
    )
    await callback.answer()


@router.message(BotStates.waiting_trainer_profile, F.text)
async def msg_trainer_profile(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    lang = _lang_for_user_id(message.from_user.id)
    parsed = _parse_trainer_profile(message.text or "")
    if parsed is None:
        await safe_delete_message(message)
        await _edit_panel_from_state(
            message,
            state,
            _tr(
                lang,
                "Формат неверный.\nНужно: <b>вес;рост;возраст</b>\nПример: <code>82;178;27</code>",
                "Format noto'g'ri.\nKerak: <b>vazn;bo'y;yosh</b>\nMisol: <code>82;178;27</code>",
            ),
            back_to_menu_keyboard(lang),
        )
        return

    data = await state.get_data()
    goal = str(data.get("trainer_goal") or "fat")
    weight, height, age = parsed
    plan = _trainer_weekly_plan(goal, weight, height, age, lang)

    await safe_delete_message(message)
    await state.clear()
    await message.answer(plan, reply_markup=trainer_keyboard(lang))


@router.message(BotStates.waiting_trainer_profile)
async def msg_trainer_profile_invalid(message: Message) -> None:
    await safe_delete_message(message)


@router.callback_query(F.data == "trainer:ask")
async def cb_trainer_ask(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    lang = _lang_for_user_id(callback.from_user.id)
    await state.set_state(BotStates.waiting_trainer_question)
    await _remember_panel(callback, state)
    await safe_edit_message(
        callback,
        _tr(
            lang,
            "✍️ Напиши запрос тренеру.\nПример: «Составь план на 3 дня для дома без инвентаря».",
            "✍️ Trenerga so'rov yozing.\nMisol: «Uy sharoitida 3 kunlik mashg'ulot rejasini tuzib bering».",
        ),
        reply_markup=back_to_menu_keyboard(lang),
    )
    await callback.answer()


@router.message(BotStates.waiting_trainer_question, F.text)
async def msg_trainer_question(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    lang = _lang_for_user_id(message.from_user.id)
    question = (message.text or "").strip()
    if not question:
        await safe_delete_message(message)
        await _edit_panel_from_state(
            message,
            state,
            _tr(lang, "Вопрос пустой.", "Savol bo'sh."),
            back_to_menu_keyboard(lang),
        )
        return

    try:
        context = db.get_ai_context(message.from_user.id)
        answer = await asyncio.to_thread(ai_service.trainer_reply, question, context, lang)
    except Exception as exc:
        logger.exception("Trainer AI failed")
        await safe_delete_message(message)
        await _edit_panel_from_state(
            message,
            state,
            f"{_tr(lang, 'Ошибка тренера', 'Trener xatosi')}: {_h(exc)}",
            back_to_menu_keyboard(lang),
        )
        return

    await safe_delete_message(message)
    await state.clear()
    await message.answer(answer, reply_markup=trainer_keyboard(lang))


@router.callback_query(F.data == 'menu:export')
async def cb_export(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    await state.clear()
    lang = _lang_for_user_id(callback.from_user.id)
    await callback.answer(_tr(lang, "Экспорт отключен", "Eksport o'chirilgan"), show_alert=True)


@router.message(Command('export'))
async def cmd_export(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    await safe_delete_message(message)
    await state.clear()
    lang = _lang_for_user_id(message.from_user.id)
    await message.answer(_tr(lang, "Экспорт отключен в этой версии.", "Eksport bu versiyada o'chirilgan."), reply_markup=main_menu_keyboard(lang))


@router.callback_query(F.data == 'menu:ai')
async def cb_menu_ai(callback: CallbackQuery, state: FSMContext) -> None:
    await ensure_user_callback(callback)
    await state.clear()
    lang = _lang_for_user_id(callback.from_user.id)
    await callback.answer(_tr(lang, "AI-помощник отключен", "AI yordamchi o'chirilgan"), show_alert=True)


@router.message(Command('ai'))
async def cmd_ai(message: Message, state: FSMContext) -> None:
    await ensure_user_message(message)
    await safe_delete_message(message)
    await state.clear()
    lang = _lang_for_user_id(message.from_user.id)
    await message.answer(_tr(lang, "AI-помощник отключен в этой версии.", "AI yordamchi bu versiyada o'chirilgan."), reply_markup=main_menu_keyboard(lang))


@router.message(BotStates.waiting_ai_question, F.text)
async def msg_ai_question(message: Message, state: FSMContext) -> None:
    await safe_delete_message(message)
    await state.clear()


@router.message()
async def fallback_message(message: Message) -> None:
    await ensure_user_message(message)
    text = (message.text or '').strip().lower()
    if text in {'start', '/start', 'menu', '/menu', 'help', '/help'}:
        await safe_delete_message(message)
        await send_main_menu(message, message.from_user.id)
        return
    await safe_delete_message(message)


def _report_due_key(local_now: datetime, frequency: str) -> str | None:
    if frequency == "monthly":
        if local_now.day != 1:
            return None
        return f"{local_now.year:04d}-{local_now.month:02d}"
    if local_now.weekday() != 6:
        return None
    iso = local_now.isocalendar()
    return f"{int(iso[0]):04d}-W{int(iso[1]):02d}"


async def weekly_report_worker(bot: Bot) -> None:
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            users = db.list_users()
            for user in users:
                telegram_id = int(user["telegram_id"])
                timezone_name = str(user.get("timezone") or settings.app_timezone)
                currency = str(user.get("currency") or settings.default_currency)
                lang = _lang_from_user(user)
                local_now = now_utc.astimezone(_zone(timezone_name))

                prefs = db.get_report_preferences(telegram_id)
                enabled = bool(prefs.get("enabled", True))
                frequency = str(prefs.get("frequency") or "weekly")
                if not enabled:
                    continue

                scheduled = (settings.weekly_report_hour, settings.weekly_report_minute)
                current = (local_now.hour, local_now.minute)
                if current < scheduled:
                    continue

                due_key = _report_due_key(local_now, frequency)
                if due_key is None:
                    continue
                if str(prefs.get("last_sent_key") or "") == due_key:
                    continue

                days = 30 if frequency == "monthly" else 7
                payload = db.get_period_payload(telegram_id, days=days, end_date=local_now.date())
                summary = build_weekly_summary(payload, currency=currency)
                title = _tr(
                    lang,
                    "📊 Месячный отчет" if frequency == "monthly" else "📊 Недельный отчет",
                    "📊 Oylik hisobot" if frequency == "monthly" else "📊 Haftalik hisobot",
                )
                await bot.send_message(
                    telegram_id,
                    f"{title}\n\n{_h(summary)}",
                    reply_markup=back_to_menu_keyboard(lang),
                )

                db.save_report_preferences(
                    telegram_id,
                    enabled=True,
                    frequency=frequency,
                    last_sent_key=due_key,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Weekly worker error')
        await asyncio.sleep(max(60, settings.weekly_report_check_seconds))


async def on_startup(bot: Bot) -> None:
    logger.info('Starting background workers...')
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

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == '__main__':
    asyncio.run(main())


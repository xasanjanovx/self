"""Финансы, часть 2: лимиты по категориям, регулярные платежи, «голая» сумма,
чек по фото, экспорт в Excel."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from .. import categories as cats
from .. import emoji as pe
from .. import export as export_mod
from .. import finance as fin
from .. import services
from .. import ui
from ..context import ai, db
from ..keyboards import (
    amount_category_keyboard,
    finance_budgets_keyboard,
    finance_recurring_detail_keyboard,
    finance_recurring_keyboard,
    finance_setting_input_keyboard,
)
from ..profile import Profile, h
from ..states import BotStates
from .common import answer_now, get_photo_bytes, get_profile, message_text, remember_panel, safe_delete, safe_edit, show_panel, show_progress
from .finance import render_panel

router = Router(name="finance_extra")
logger = logging.getLogger(__name__)

MIGRATION_HINT = ("Нужна миграция: выполни <code>sql/migrations/004_v2_features.sql</code> в Supabase → SQL Editor.",
                  "Migratsiya kerak: Supabase → SQL Editor da <code>sql/migrations/004_v2_features.sql</code> ni bajaring.")


# ------------------------------------------------------------------ budgets
def _budgets_text(profile: Profile, statuses: list[fin.BudgetStatus], limits: dict[str, float]) -> str:
    lang, cur = profile.lang, profile.currency
    uz = lang == "uz"
    header = ui.title("🎯", "Limitlar" if uz else "Лимиты по категориям", fin.period_title(fin.period_for("month", profile.today), lang))
    if not limits:
        body = ui.muted("Limitlar yo'q." if uz else "Лимитов пока нет.")
        return ui.join(header, body)
    lines = []
    for b in statuses:
        flag = "🚫 " if b.ratio >= 1 else "⚠️ " if b.ratio >= 0.8 else ""
        lines.append(f"{flag}{cats.label(b.category, lang)} — <b>{fin.fmt_money(b.spent)}</b> / {fin.fmt_money(b.limit)} · {ui.pct(b.ratio)}")
        lines.append(fin.bar(min(b.ratio, 1.0), 12))
    total_limit = sum(limits.values())
    total_spent = sum(b.spent for b in statuses)
    lines.append(f"{'Jami' if uz else 'Итого'}: <b>{fin.fmt_money(total_spent)}</b> / {fin.fmt_money(total_limit)} {cur}")
    return ui.join(header, ui.card(f"<b>{'Holat' if uz else 'Состояние'}</b>", lines))


async def render_budgets(target: Message | CallbackQuery, state: FSMContext, profile: Profile, *, notice: str | None = None) -> None:
    if not await db.ensure_available("budgets"):
        text = "⚠️ " + profile.tr(*MIGRATION_HINT)
        limits, statuses = {}, []
    else:
        limits, statuses = await asyncio.gather(services.budgets(profile.telegram_id), services.month_budget_statuses(profile))
        text = _budgets_text(profile, statuses, limits)
    if notice:
        text += f"\n\n{notice}"
    await state.set_state(BotStates.waiting_finance_input)
    await state.update_data(budget_category=None)
    kb = finance_budgets_keyboard(limits, profile.lang)
    if isinstance(target, CallbackQuery):
        await remember_panel(target, state)
        await safe_edit(target, text, kb)
    else:
        await show_panel(target, state, text, kb)


@router.callback_query(F.data == "finance:budgets")
async def cb_budgets(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    await render_budgets(callback, state, await get_profile(callback.from_user))


@router.callback_query(F.data.startswith("finance:budget:"))
async def cb_budget_pick(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await get_profile(callback.from_user)
    key = callback.data.split(":")[-1]
    if not cats.get(key) or not db.available("budgets"):
        await answer_now(callback)
        return
    await answer_now(callback)
    limits = await services.budgets(profile.telegram_id)
    current = limits.get(key)
    await state.set_state(BotStates.waiting_budget_value)
    await state.update_data(budget_category=key)
    await remember_panel(callback, state)
    cur = profile.currency
    now = f"{fin.fmt_money(current)} {cur}" if current else profile.tr("нет", "yo'q")
    await safe_edit(
        callback,
        profile.tr(
            f"{cats.label(key, 'ru')}\nЛимит сейчас: <b>{now}</b>\n\nВведи лимит на месяц (число, <code>0</code> — убрать):",
            f"{cats.label(key, 'uz')}\nHozirgi limit: <b>{now}</b>\n\nOylik limitni kiriting (raqam, <code>0</code> — o'chirish):",
        ),
        finance_setting_input_keyboard(profile.lang),
    )


@router.message(BotStates.waiting_budget_value, F.text)
async def msg_budget_value(message: Message, state: FSMContext) -> None:
    profile = await get_profile(message.from_user)
    await safe_delete(message)
    key = str((await state.get_data()).get("budget_category") or "")
    parsed = fin.parse_amount((message.text or "").strip())
    if parsed is None or not cats.get(key):
        await render_budgets(message, state, profile, notice=profile.tr("Нужно число, например <code>1500000</code> или <code>1.5 млн</code>", "Raqam kerak, masalan <code>1500000</code>"))
        return
    await services.set_budget(profile.telegram_id, key, float(parsed[0]))
    note = profile.tr(f"✅ {cats.label(key, 'ru')}: лимит {fin.fmt_money(parsed[0])}" if parsed[0] > 0 else f"✅ {cats.label(key, 'ru')}: лимит убран",
                      f"✅ {cats.label(key, 'uz')}: limit {fin.fmt_money(parsed[0])}" if parsed[0] > 0 else f"✅ {cats.label(key, 'uz')}: limit o'chirildi")
    await render_budgets(message, state, profile, notice=note)


@router.message(BotStates.waiting_budget_value)
async def msg_budget_other(message: Message, state: FSMContext) -> None:
    await safe_delete(message)
    await render_budgets(message, state, await get_profile(message.from_user))


# ------------------------------------------------------------------ recurring payments
def _recurring_text(profile: Profile, items: list[dict[str, Any]]) -> str:
    lang, cur = profile.lang, profile.currency
    uz = lang == "uz"
    today = profile.today
    header = ui.title("🔁", "Doimiy to'lovlar" if uz else "Регулярные платежи", fin.period_title(fin.period_for("month", today), lang))
    if not items:
        body = ui.muted("Hali yo'q." if uz else "Пока нет.")
        return ui.join(header, body)
    key = today.strftime("%Y-%m")
    lines = []
    for it in items:
        day = fin.recurring_due_day(int(it.get("day_of_month") or 1), today.year, today.month)
        paid = str(it.get("last_done_key") or "") == key
        if not it.get("enabled", True):
            status = "⏸"
        elif paid:
            status = "✅"
        elif day < today.day:
            status = "☑️"
        elif day == today.day:
            status = "🔔"
        else:
            status = "⏳"
        lines.append(f"{status} <b>{day:02d}</b> · {h(it.get('title'))} · {fin.fmt_money(float(it.get('amount') or 0))} · {cats.label(it.get('category'), lang)}")
    remaining, _ = fin.recurring_remaining(items, today)
    total = sum(float(i.get("amount") or 0) for i in items if i.get("enabled", True))
    summary = [
        f"{'Oyiga jami' if uz else 'Всего в месяц'}: <b>{fin.fmt_money(total)} {cur}</b>",
        f"{'Bu oy qoldi' if uz else 'Ещё предстоит в этом месяце'}: <b>{fin.fmt_money(remaining)} {cur}</b>",
    ]
    return ui.join(header, ui.card(f"<b>{'Ro`yxat' if uz else 'Список'}</b>", lines), ui.card(f"<b>{'Xulosa' if uz else 'Итого'}</b>", summary))


async def render_recurring(target: Message | CallbackQuery, state: FSMContext, profile: Profile, *, notice: str | None = None) -> None:
    if not await db.ensure_available("recurring_payments"):
        text, items = "⚠️ " + profile.tr(*MIGRATION_HINT), []
    else:
        items = await services.recurring(profile.telegram_id)
        text = _recurring_text(profile, items)
    if notice:
        text += f"\n\n{notice}"
    await state.set_state(BotStates.waiting_recurring_input)
    kb = finance_recurring_keyboard(items, profile.lang)
    if isinstance(target, CallbackQuery):
        await remember_panel(target, state)
        await safe_edit(target, text, kb)
    else:
        await show_panel(target, state, text, kb)


@router.callback_query(F.data == "finance:recurring")
async def cb_recurring(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    await render_recurring(callback, state, await get_profile(callback.from_user))


@router.callback_query(F.data == "finance:rec_add")
async def cb_recurring_add(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    await state.set_state(BotStates.waiting_recurring_input)
    await remember_panel(callback, state)
    await safe_edit(
        callback,
        profile.tr(
            "Напиши платёж: <b>название сумма число</b>\nПримеры: <code>интернет 150000 5</code> · <code>аренда 2 млн 1 числа</code> · <code>кредит 1.2 млн 15-го картой</code>",
            "To'lovni yozing: <b>nom summa kun</b>\nMisollar: <code>internet 150000 5</code> · <code>ijara 2 mln 1</code> · <code>kredit 1.2 mln 15</code>",
        ),
        finance_recurring_keyboard([], profile.lang),
    )


@router.message(BotStates.waiting_recurring_input, F.text)
async def msg_recurring_input(message: Message, state: FSMContext) -> None:
    profile = await get_profile(message.from_user)
    text = (message.text or "").strip()
    await safe_delete(message)
    if text.startswith("/"):
        return
    parsed = fin.parse_recurring(text)
    if parsed is None:
        # не платёж — может быть обычная операция
        if fin.looks_like_finance(text):
            from .finance import handle_finance_text

            await handle_finance_text(message, state, profile, text, source="text")
            return
        await render_recurring(message, state, profile, notice=profile.tr("Не понял. Формат: <code>интернет 150000 5</code>", "Tushunmadim. Format: <code>internet 150000 5</code>"))
        return
    if not await db.ensure_available("recurring_payments"):
        await render_recurring(message, state, profile)
        return
    created = await db.add_recurring(profile.telegram_id, title=parsed["title"], amount=parsed["amount"], category=parsed["category"],
                                     bucket=parsed["bucket"], day_of_month=parsed["day_of_month"])
    today = profile.today
    if created.get("id") and fin.recurring_due_day(parsed["day_of_month"], today.year, today.month) < today.day:
        # день уже прошёл — этот месяц считаем закрытым, спросим со следующего
        await db.update_recurring(profile.telegram_id, created["id"], {"last_done_key": today.strftime("%Y-%m")})
    services.invalidate_recurring(profile.telegram_id)
    await render_recurring(message, state, profile, notice=profile.tr(
        f"✅ Добавлено: {h(parsed['title'])} · {fin.fmt_money(parsed['amount'])} · каждое {parsed['day_of_month']}-е число · {cats.label(parsed['category'], 'ru')}",
        f"✅ Qo'shildi: {h(parsed['title'])} · {fin.fmt_money(parsed['amount'])} · har oyning {parsed['day_of_month']}-kuni · {cats.label(parsed['category'], 'uz')}",
    ))


@router.message(BotStates.waiting_recurring_input)
async def msg_recurring_other(message: Message, state: FSMContext) -> None:
    await safe_delete(message)
    await render_recurring(message, state, await get_profile(message.from_user))


def _rec_detail(profile: Profile, it: dict[str, Any]) -> str:
    lang, cur = profile.lang, profile.currency
    key = profile.today.strftime("%Y-%m")
    paid = str(it.get("last_done_key") or "") == key
    return (
        f"🔁 <b>{h(it.get('title'))}</b>\n\n"
        f"{'Summa' if lang == 'uz' else 'Сумма'}: <b>{fin.fmt_money(float(it.get('amount') or 0))} {cur}</b>\n"
        f"{'Kun' if lang == 'uz' else 'День'}: {int(it.get('day_of_month') or 1)}\n"
        f"{'Kategoriya' if lang == 'uz' else 'Категория'}: {cats.label(it.get('category'), lang)}\n"
        f"{'Hisob' if lang == 'uz' else 'Счёт'}: {fin.bucket_label(str(it.get('bucket') or 'card'), lang)}\n"
        f"{'Bu oy' if lang == 'uz' else 'В этом месяце'}: {('✅ ' + ('to`langan' if lang == 'uz' else 'оплачено')) if paid else ('⏳ ' + ('kutilmoqda' if lang == 'uz' else 'ещё не записано'))}\n"
        f"{'Holat' if lang == 'uz' else 'Статус'}: {('✅ ' + ('faol' if lang == 'uz' else 'активен')) if it.get('enabled', True) else '⏸ ' + ('pauza' if lang == 'uz' else 'на паузе')}"
    )


@router.callback_query(F.data.startswith("finance:rec:"))
async def cb_recurring_view(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    rec_id = callback.data.split(":")[-1]
    it = await db.get_recurring(profile.telegram_id, rec_id)
    if not it:
        await render_recurring(callback, state, profile)
        return
    await remember_panel(callback, state)
    await safe_edit(callback, _rec_detail(profile, it), finance_recurring_detail_keyboard(rec_id, bool(it.get("enabled", True)), profile.lang))


async def record_recurring_payment(profile: Profile, it: dict[str, Any]) -> None:
    await services.add_finance_entries(
        profile,
        [{"kind": "expense", "amount": float(it.get("amount") or 0), "category": it.get("category") or "home",
          "note": it.get("title"), "bucket": it.get("bucket") or "card"}],
        source="recurring",
    )
    await db.update_recurring(profile.telegram_id, it["id"], {"last_done_key": profile.today.strftime("%Y-%m")})
    services.invalidate_recurring(profile.telegram_id)


@router.callback_query(F.data.startswith("finance:rec_pay:"))
async def cb_recurring_pay(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await get_profile(callback.from_user)
    rec_id = callback.data.split(":")[-1]
    it = await db.get_recurring(profile.telegram_id, rec_id)
    if not it:
        await answer_now(callback)
        return
    await answer_now(callback, profile.tr("Записано ✅", "Yozildi ✅"))
    await record_recurring_payment(profile, it)
    await render_recurring(callback, state, profile, notice=profile.tr(f"✅ {h(it.get('title'))} — {fin.fmt_money(float(it.get('amount') or 0))} записано в расходы",
                                                                         f"✅ {h(it.get('title'))} — {fin.fmt_money(float(it.get('amount') or 0))} chiqimga yozildi"))


@router.callback_query(F.data.startswith("finance:rec_toggle:"))
async def cb_recurring_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await get_profile(callback.from_user)
    rec_id = callback.data.split(":")[-1]
    it = await db.get_recurring(profile.telegram_id, rec_id)
    if not it:
        await answer_now(callback)
        return
    new_value = not bool(it.get("enabled", True))
    await answer_now(callback, "▶️" if new_value else "⏸")
    await db.update_recurring(profile.telegram_id, rec_id, {"enabled": new_value})
    services.invalidate_recurring(profile.telegram_id)
    it["enabled"] = new_value
    await safe_edit(callback, _rec_detail(profile, it), finance_recurring_detail_keyboard(rec_id, new_value, profile.lang))


@router.callback_query(F.data.startswith("finance:rec_del:"))
async def cb_recurring_delete(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await get_profile(callback.from_user)
    rec_id = callback.data.split(":")[-1]
    await answer_now(callback, profile.tr("Удалено", "O'chirildi"))
    await db.delete_recurring(profile.telegram_id, rec_id)
    services.invalidate_recurring(profile.telegram_id)
    await render_recurring(callback, state, profile)


# --- ответы на утренний вопрос «Оплатил?» (сообщение от воркера)
@router.callback_query(F.data.startswith("rec:done:") | F.data.startswith("rec:skip:"))
async def cb_recurring_prompt(callback: CallbackQuery) -> None:
    profile = await get_profile(callback.from_user)
    _, action, rec_id = callback.data.split(":")
    it = await db.get_recurring(profile.telegram_id, rec_id)
    if not it:
        await answer_now(callback)
        return
    key = profile.today.strftime("%Y-%m")
    if action == "done":
        if str(it.get("last_done_key") or "") != key:
            await record_recurring_payment(profile, it)
        text = profile.tr(f"✅ {h(it.get('title'))} — {fin.fmt_money(float(it.get('amount') or 0))} записано", f"✅ {h(it.get('title'))} — {fin.fmt_money(float(it.get('amount') or 0))} yozildi")
    else:
        await db.update_recurring(profile.telegram_id, rec_id, {"last_done_key": key})
        services.invalidate_recurring(profile.telegram_id)
        text = profile.tr(f"⏭ {h(it.get('title'))} — пропущено в этом месяце", f"⏭ {h(it.get('title'))} — bu oy o'tkazib yuborildi")
    await answer_now(callback)
    try:
        await callback.message.edit_text(text, reply_markup=None)
    except Exception:
        pass


# ------------------------------------------------------------------ bare amount → pick category
async def ask_category_for_amount(message: Message, state: FSMContext, profile: Profile, kind: str, amount: float, *, bucket: str = "card") -> None:
    entries = await services.finance_entries(profile.telegram_id)
    recent: list[str] = []
    for row in entries:
        if fin.is_transfer(row) or (row.get("entry_type") == "income") != (kind == "income"):
            continue
        key = fin.entry_category_key(row)
        if key not in recent:
            recent.append(key)
        if len(recent) >= 6:
            break
    await state.set_state(BotStates.waiting_finance_amount_category)
    await state.update_data(pending_amount=amount, pending_kind=kind, pending_bucket=bucket)
    sign = "+" if kind == "income" else "−"
    text = profile.tr(
        f"💰 <b>{sign}{fin.fmt_money(amount)} {profile.currency}</b>\n\nНа что? Выбери категорию:",
        f"💰 <b>{sign}{fin.fmt_money(amount)} {profile.currency}</b>\n\nNimaga? Kategoriyani tanlang:",
    )
    await show_panel(message, state, text, amount_category_keyboard(kind, recent, profile.lang))


@router.callback_query(F.data.startswith("finance:amtkind:"))
async def cb_amount_kind(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    kind = "income" if callback.data.endswith("income") else "expense"
    data = await state.get_data()
    amount = float(data.get("pending_amount") or 0)
    if amount <= 0:
        await render_panel(callback, state, profile)
        return
    await state.update_data(pending_kind=kind)
    sign = "+" if kind == "income" else "−"
    await safe_edit(
        callback,
        profile.tr(f"💰 <b>{sign}{fin.fmt_money(amount)} {profile.currency}</b>\n\nВыбери категорию:", f"💰 <b>{sign}{fin.fmt_money(amount)} {profile.currency}</b>\n\nKategoriyani tanlang:"),
        amount_category_keyboard(kind, [], profile.lang),
    )


@router.callback_query(F.data.startswith("finance:amtcat:"))
async def cb_amount_category(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await get_profile(callback.from_user)
    key = callback.data.split(":")[-1]
    data = await state.get_data()
    amount = float(data.get("pending_amount") or 0)
    kind = "income" if data.get("pending_kind") == "income" else "expense"
    if amount <= 0 or not cats.get(key):
        await answer_now(callback)
        await render_panel(callback, state, profile)
        return
    await answer_now(callback, f"{cats.label(key, profile.lang)} ✅")
    from .finance import commit_items

    await commit_items(callback, state, profile, [{"kind": kind, "amount": amount, "category": key, "note": None, "bucket": data.get("pending_bucket") or "card"}], source="quick")


@router.message(BotStates.waiting_finance_amount_category)
async def msg_amount_category_other(message: Message, state: FSMContext) -> None:
    # пока ждём категорию, новый текст — новая операция
    profile = await get_profile(message.from_user)
    text = message_text(message)
    if text and not text.startswith("/"):
        from .finance import handle_finance_text

        await handle_finance_text(message, state, profile, text, source="text")
        return
    await safe_delete(message)
    await render_panel(message, state, profile)


async def budget_notice(profile: Profile, keys: set[str]) -> str | None:
    """Предупреждение по лимитам для затронутых категорий (после записи расхода)."""
    statuses = await services.month_budget_statuses(profile)
    lines = fin.budget_warnings(statuses, keys, lang=profile.lang)
    return "\n".join(lines) if lines else None


# ------------------------------------------------------------------ receipt photo
async def handle_receipt_photo(message: Message, state: FSMContext, profile: Profile) -> None:
    await show_progress(message, profile.tr("⏳ Читаю чек…", "⏳ Chek o'qilmoqda…"))
    hint = message_text(message) or None
    try:
        image_bytes, mime_type, _ = await get_photo_bytes(message)
        await safe_delete(message)
        item = await ai.parse_receipt(image_bytes, mime_type, hint=hint)
    except Exception as exc:
        logger.exception("receipt parse failed")
        await safe_delete(message)
        await render_panel(message, state, profile, notice=f"{pe.CROSS} {profile.tr('Не удалось прочитать чек', 'Chek o`qilmadi')}: {h(str(exc)[:120])}")
        return
    if not item:
        await render_panel(message, state, profile, notice=profile.tr("Не нашёл сумму на фото. Если это еда — открой раздел «Питание» и отправь фото там.",
                                                                        "Rasmda summa topilmadi. Agar bu ovqat bo'lsa — «Oziqlanish» bo'limida yuboring."))
        return
    from .finance import ask_confirm

    await ask_confirm(message, state, profile, [item], source="receipt")


# ------------------------------------------------------------------ excel export
@router.callback_query(F.data.startswith("finance:excel:"))
async def cb_excel(callback: CallbackQuery) -> None:
    profile = await get_profile(callback.from_user)
    period = fin.period_for(callback.data.split(":")[-1], profile.today)
    await answer_now(callback, profile.tr("Готовлю Excel…", "Excel tayyorlanmoqda…"))
    entries = await services.finance_entries(profile.telegram_id)
    if not fin.entries_between(entries, period.start, period.end):
        await answer_now(callback, profile.tr("Нет операций за период", "Bu davrda operatsiya yo'q"), alert=True)
        return
    try:
        data = await asyncio.to_thread(export_mod.build_xlsx, entries, period=period, lang=profile.lang, currency=profile.currency)
    except Exception:
        logger.exception("xlsx export failed")
        await answer_now(callback, profile.tr("Ошибка экспорта", "Eksport xatosi"), alert=True)
        return
    name = f"finance_{period.start:%Y-%m-%d}_{period.end:%Y-%m-%d}.xlsx"
    if callback.message is not None:
        await callback.bot.send_document(
            callback.message.chat.id,
            BufferedInputFile(data, filename=name),
            caption=profile.tr(f"📥 Операции {fin.period_title(period, 'ru')}", f"📥 Operatsiyalar {fin.period_title(period, 'uz')}"),
        )

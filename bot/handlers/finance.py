"""Финансы: панель, ввод операций (текст/голос, без AI где возможно),
подтверждение, быстрые кнопки, операции, статистика по категориям, счета."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from .. import categories as cats
from .. import cache
from .. import charts as charts_mod
from .. import emoji as pe
from .. import finance as fin
from .. import screen as screen_mod
from .. import services
from .. import ui
from .. import vacancy as vac
from ..context import ai, db
from ..keyboards import (
    back_to_menu_keyboard,
    finance_add_confirm_keyboard,
    finance_category_keyboard,
    finance_delete_confirm_keyboard,
    finance_detail_keyboard,
    finance_operations_keyboard,
    finance_panel_keyboard,
    finance_setting_input_keyboard,
    finance_settings_keyboard,
    finance_stats_keyboard,
)
from ..profile import Profile, h
from ..states import BotStates
from .common import (
    answer_now,
    get_profile,
    remember_panel,
    safe_delete,
    safe_edit,
    show_panel,
    show_progress,
    transcribe_audio,
)

router = Router(name="finance")
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ panel
async def build_panel(profile: Profile) -> tuple[str, list[str], list[dict[str, Any]]]:
    snap, limits, recurring = await asyncio.gather(
        services.finance_snapshot(profile), services.budgets(profile.telegram_id), services.recurring(profile.telegram_id)
    )
    lang, cur = profile.lang, profile.currency
    uz = lang == "uz"
    b = snap.balances
    month = snap.month
    labels = [fin.quick_label(item, lang) for item in snap.quick]

    header = ui.title(pe.WALLET, "Moliya" if uz else "Финансы", ui.human_date(snap.today, lang))

    # Телефон узкий: одна строка — один факт, без склеек через «·».
    balance_lines = [
        f"💼 <b>{fin.fmt_money(snap.wallet)} {cur}</b>",
        f"💳 {'Karta' if uz else 'Карта'}: {fin.fmt_money(b['card'])}",
        f"💵 {'Naqd' if uz else 'Наличные'}: {fin.fmt_money(b['cash'])}",
    ]
    if recurring:
        remaining, pending = fin.recurring_remaining(recurring, snap.today)
        if remaining > 0:
            nxt = pending[0]
            nxt_day = fin.recurring_due_day(int(nxt.get("day_of_month") or 1), snap.today.year, snap.today.month)
            balance_lines.append("")
            balance_lines.append(f"🔁 {'To`lovlar' if uz else 'Платежи'}: {fin.fmt_money(remaining)}")
            balance_lines.append(f"{'Erkin' if uz else 'Свободно'}: <b>{fin.fmt_money(snap.wallet - remaining)}</b>")
            balance_lines.append(ui.muted(f"{'keyingi' if uz else 'ближайший'} {nxt_day:02d}: {h(nxt.get('title'))} {fin.fmt_money(float(nxt.get('amount') or 0))}"))
    credit = float(snap.settings.get("monthly_credit_payment") or 0)
    if credit:
        balance_lines.append(f"🏦 {'Kredit/oy' if uz else 'Кредит/мес'}: {fin.fmt_money(credit)}")
    balance_card = ui.card(f"<b>{'Balans' if uz else 'Баланс'}</b>", balance_lines)
    debts_card = _debts_card(snap, lang, await services.debt_deadlines(profile.telegram_id))

    today_parts = []
    if snap.today_expense:
        today_parts.append(f"{pe.EXPENSE} {fin.fmt_money(snap.today_expense)}")
    if snap.today_income:
        today_parts.append(f"{pe.INCOME} {fin.fmt_money(snap.today_income)}")
    period_lines = [f"{'Bugun' if uz else 'Сегодня'}: " + (" · ".join(today_parts) if today_parts else ("hali yo'q" if uz else "пока ничего"))]
    if month:
        change = month.expense_change_pct()
        change_text = f" ({'▲' if change > 0 else '▼'}{abs(change):.0f}%)" if change is not None else ""
        period_lines.append(f"{'Oy' if uz else 'Месяц'}: {pe.EXPENSE} {fin.fmt_money(month.expense)}{change_text}")
        if month.income:
            period_lines.append(f"{'Kirim' if uz else 'Доход'}: {pe.INCOME} {fin.fmt_money(month.income)}")
        if month.by_category:
            period_lines.append("")
            period_lines.extend(f"{cats.label(k, lang)}: {fin.fmt_money(a)}" for k, a, _ in month.by_category[:3])
        if limits:
            period_lines.extend(fin.budget_warnings(fin.budget_statuses(month, limits), lang=lang)[:3])
    period_card = ui.card(f"<b>{'Xarajatlar' if uz else 'Расходы'}</b>", period_lines)

    today_card = None
    if snap.today_entries:
        today_card = ui.card(f"<b>{'Bugungi operatsiyalar' if uz else 'Операции сегодня'}</b>", ["• " + _entry_line(row, lang) for row in snap.today_entries[:6]])

    hint = ui.muted(
        "✍️ <code>taksi 25000</code> · <code>oylik 5 mln</code>\n<code>qarzga berdim 200000</code> · <code>25000</code>"
        if uz else
        "✍️ <code>такси 25000</code> · <code>зарплата 5 млн</code>\n<code>дал в долг 200000</code> · просто <code>25000</code>"
    )
    return ui.join(header, balance_card, debts_card, period_card, today_card, hint), labels, snap.quick


_DEBT_ROWS = 5  # строк на сторону — чтобы карточка помещалась на экран телефона


def _deadline_tag(name: str, side: str, deadlines: list[dict[str, Any]], today: date, lang: str) -> str:
    """« · до 05.10» или « ⚠️ просрочено 3 дн.» для строки долга."""
    for r in deadlines:
        if r.get("side") != side or str(r.get("person") or "").casefold() != name.casefold():
            continue
        try:
            due = date.fromisoformat(str(r.get("due_date"))[:10])
        except ValueError:
            return ""
        left = (due - today).days
        if left < 0:
            return f" ⚠️ {ui.muted(f'{-left} kun kechikdi' if lang == 'uz' else f'просрочено {-left} дн.')}"
        if left == 0:
            return " · " + ui.muted("bugun" if lang == "uz" else "сегодня")
        return " · " + ui.muted(f"{due:%d.%m} gacha" if lang == "uz" else f"до {due:%d.%m}")
    return ""


def _debts_card(snap: services.FinanceSnapshot, lang: str, deadlines: list[dict[str, Any]] | None = None) -> str | None:
    """Карточка «Долги»: кто должен мне и кому должен я — по именам, компактно (одна строка — один человек), со сроками."""
    uz = lang == "uz"
    b = snap.balances
    if not b["lent"] and not b["debt"]:
        return None
    ledger = fin.debt_ledger(snap.entries, snap.settings)
    deadlines = deadlines or []

    def _side(icon: str, title: str, total: float, items: list[tuple[str, float]], negative_word: str, side: str) -> list[str]:
        if not total and not items:
            return []
        lines = [f"{icon} {title}: <b>{fin.fmt_money(total)}</b>"]
        for name, amount in items[:_DEBT_ROWS]:
            label = h(name) if name else ui.muted("nomsiz" if uz else "без имени")
            tag = _deadline_tag(name, side, deadlines, snap.today, lang) if name else ""
            if amount >= 0:
                lines.append(f"• {label} — {fin.fmt_money(amount)}{tag}")
            else:
                lines.append(f"• {label} — {fin.fmt_money(abs(amount))} {ui.muted(negative_word)}")
        if len(items) > _DEBT_ROWS:
            lines.append(ui.muted(f"… +{len(items) - _DEBT_ROWS}"))
        return lines

    lent = _side("🤝", "Menga qarz" if uz else "Мне должны", b["lent"], ledger["lent"], "ortiqcha qaytardi" if uz else "вернул больше", "lent")
    debt = _side("📌", "Mening qarzim" if uz else "Я должен", b["debt"], ledger["debt"], "ortiqcha to'landi" if uz else "переплата", "debt")
    lines = lent + ([""] if lent and debt else []) + debt
    return ui.card(f"<b>{'Qarzlar' if uz else 'Долги'}</b>", lines)


def _entry_line(row: dict[str, Any], lang: str) -> str:
    amount = float(row.get("amount") or 0)
    key = fin.entry_category_key(row)
    note = fin.clean_note(row.get("note"))
    transfer = fin.transfer_from_note(row.get("note"))
    if transfer:
        return f"↔ {fin.fmt_money(amount)} · {fin.transfer_label(transfer[0], transfer[1], lang)}" + (f" · {h(note)}" if note else "")
    sign = "+" if row.get("entry_type") == "income" else "−"
    return f"{sign}{fin.fmt_money(amount)} · {cats.label(key, lang)}" + (f" · {h(note)}" if note else "")


async def render_panel(target: Message | CallbackQuery, state: FSMContext, profile: Profile, *, notice: str | None = None) -> None:
    text, labels, quick = await build_panel(profile)
    if notice:
        text += f"\n\n{notice}"
    await state.set_state(BotStates.waiting_finance_input)
    await state.update_data(pending_finance_items=None, pending_finance_source=None, quick_ops=quick)
    kb = finance_panel_keyboard(labels, profile.lang)
    if isinstance(target, CallbackQuery):
        await remember_panel(target, state)
        await safe_edit(target, text, kb)
    else:
        await show_panel(target, state, text, kb)


@router.callback_query(F.data == "menu:finance")
async def cb_panel(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    await render_panel(callback, state, await get_profile(callback.from_user))


# ------------------------------------------------------------------ input
def _format_pending(items: list[dict[str, Any]], profile: Profile, balances_before: dict[str, float]) -> str:
    lang, cur = profile.lang, profile.currency
    lines = ["💰 <b>Moliya / Tekshiruv</b>", ""] if lang == "uz" else ["💰 <b>Финансы / Проверка</b>", ""]
    for item in items:
        amount = float(item.get("amount") or 0)
        note = h(str(item.get("note") or "").strip())
        note_part = f" · {note}" if note else ""
        if item.get("kind") == "transfer":
            if item.get("from_bucket") == fin.INIT:
                what = ("Eski qarz — menga qarz" if lang == "uz" else "Старый долг — мне должны") if item.get("to_bucket") == "lent" \
                    else ("Eski qarz — men qarzdorman" if lang == "uz" else "Старый долг — я должен")
                lines.append(f"🕘 <b>{fin.fmt_money(amount)} {cur}</b> · {what}{note_part}")
                continue
            route = fin.transfer_label(item.get("from_bucket", "card"), item.get("to_bucket", "cash"), lang)
            lines.append(f"↔ <b>{fin.fmt_money(amount)} {cur}</b> · {route}{note_part}")
        else:
            sign = "+" if item.get("kind") == "income" else "−"
            lines.append(
                f"{sign}<b>{fin.fmt_money(amount)} {cur}</b> · {cats.label(item.get('category'), lang)} · "
                f"{fin.bucket_label(item.get('bucket', 'card'), lang)}{note_part}"
            )
    after = fin.apply_pending(balances_before, items)
    wallet_before = balances_before["card"] + balances_before["cash"]
    wallet_after = after["card"] + after["cash"]
    lines += ["", f"💼 {'Balans' if lang == 'uz' else 'Баланс'}: {fin.fmt_money(wallet_before)} → <b>{fin.fmt_money(wallet_after)} {cur}</b>"]
    for bucket in ("card", "cash", "lent", "debt"):
        if abs(after[bucket] - balances_before[bucket]) > 0.005:
            lines.append(f"{fin.bucket_label(bucket, lang)}: {fin.fmt_money(balances_before[bucket])} → {fin.fmt_money(after[bucket])}")
    negative = [b for b in ("card", "cash") if after[b] < 0]
    if negative:
        lines.append(("⚠️ Minusga tushadi: " if lang == "uz" else "⚠️ Уходит в минус: ") + ", ".join(fin.bucket_label(b, lang) for b in negative))
    lines += ["", "Saqlaymizmi?" if lang == "uz" else "Сохранить?"]
    return "\n".join(lines)


async def handle_finance_text(
    message: Message, state: FSMContext, profile: Profile, raw_text: str, *, source: str, reroute: bool = True, fallback_agent: bool = True
) -> None:
    """Общая точка входа для текста/голоса: локальный парсер → AI → подтверждение.
    Если текст явно не про деньги (еда, вакансия, вопрос) — отдаём общему роутеру."""
    raw_text = (raw_text or "").strip()
    if not raw_text:
        await safe_delete(message)
        await render_panel(message, state, profile, notice=profile.tr("Нужен текст или голос.", "Matn yoki ovoz kerak."))
        return
    await capture_origin(state)
    if reroute:
        from .agent import handle_command, looks_like_command

        if looks_like_command(raw_text) and await handle_command(message, state, profile, raw_text):
            return
    if reroute and not fin.looks_like_finance(raw_text):
        from .inbox import route_text  # локальный импорт: избегаем цикла

        transcript = raw_text if source == "voice_ai" else None
        if await route_text(message, state, profile, raw_text, transcript=transcript, skip_finance=True):
            return
    bare = fin.bare_amount(raw_text)
    if bare is not None:
        from .finance_extra import ask_category_for_amount

        await safe_delete(message)
        await ask_category_for_amount(message, state, profile, bare[0], bare[1], bucket=fin.bucket_hint(raw_text.lower()))
        return
    await safe_delete(message)

    old_debt = fin.parse_existing_debt(raw_text)
    items = [old_debt] if old_debt else fin.parse_local(raw_text)
    confident = items is not None  # локальный парсер срабатывает только на однозначных фразах
    if items is None:
        await show_progress(message, profile.tr("⏳ Разбираю операцию…", "⏳ Operatsiya tahlil qilinmoqda…"))
        try:
            items = await ai.parse_finance_ops(raw_text, today=profile.today.isoformat())
        except Exception as exc:
            logger.exception("parse_finance_ops failed")
            await render_panel(message, state, profile, notice=f"{pe.CROSS} {profile.tr('Ошибка разбора', 'Tahlil xatosi')}: {h(str(exc)[:120])}")
            return
        confident = bool(items) and all(float(i.get("confidence") or 0) >= AUTO_SAVE_CONFIDENCE for i in items)
    if not items:
        # парсер не понял — пусть разбирается Джарвис (спросит кнопками, если надо); не зацикливаемся, если пришли от него
        from .. import services as services_mod

        await services_mod.log_agent(profile.telegram_id, text=raw_text, kind="unparsed_finance", ok=False)
        if reroute or fallback_agent:
            from .agent import handle_command

            if await handle_command(message, state, profile, raw_text, own_message=False):
                return
        await render_panel(
            message, state, profile,
            notice=profile.tr("Не разобрал операцию. Пример: <code>такси 25000</code>", "Operatsiya tushunilmadi. Misol: <code>taksi 25000</code>"),
        )
        return

    await ask_confirm(message, state, profile, items, source=source, confident=confident)


AUTO_SAVE_CONFIDENCE = 0.9
_FLOW_STATES = {
    BotStates.waiting_finance_confirm.state, BotStates.waiting_debt_note.state, BotStates.waiting_finance_amount_category.state,
}


async def capture_origin(state: FSMContext) -> None:
    """Откуда пришла операция: из панели «Финансы» (panel) или из чата/главного экрана (menu).
    После сохранения возвращаемся туда же."""
    current = await state.get_state()
    data = await state.get_data()
    if current == BotStates.waiting_finance_input.state:
        origin = "panel"
    elif current in _FLOW_STATES and data.get("pending_origin"):
        origin = str(data["pending_origin"])
    else:
        origin = "menu"
    await state.update_data(pending_origin=origin)


def _saved_line(item: dict[str, Any], lang: str) -> str:
    amount = float(item.get("amount") or 0)
    note = h(str(item.get("note") or "").strip())
    note_part = f" · {note}" if note else ""
    if item.get("kind") == "transfer":
        if item.get("from_bucket") == fin.INIT:
            what = ("menga qarz" if lang == "uz" else "мне должны") if item.get("to_bucket") == "lent" else ("men qarzdorman" if lang == "uz" else "я должен")
            return f"🕘 {fin.fmt_money(amount)} · {what}{note_part}"
        return f"↔ {fin.fmt_money(amount)} · {fin.transfer_label(item.get('from_bucket', 'card'), item.get('to_bucket', 'cash'), lang)}{note_part}"
    sign = "+" if item.get("kind") == "income" else "−"
    return f"{sign}{fin.fmt_money(amount)} · {cats.label(item.get('category'), lang)}{note_part}"


async def finish_save(
    target: Message | CallbackQuery, state: FSMContext, profile: Profile, items: list[dict[str, Any]], inserted: list[dict[str, Any]] | None
) -> None:
    """После записи: заметка «✅ Записано …», кнопка отмены, возврат на экран, откуда пришли."""
    from .finance_extra import budget_notice

    lang = profile.lang
    notice = f"{pe.CHECK} <b>{'Yozildi' if lang == 'uz' else 'Записано'}:</b> " + " · ".join(_saved_line(i, lang) for i in items)
    keys = {str(i.get("category")) for i in items if i.get("kind") == "expense"}
    warn = await budget_notice(profile, keys) if keys else None
    if warn:
        notice += f"\n{warn}"
    ids = [r.get("id") for r in (inserted or []) if r.get("id") is not None]
    if ids:
        cache.put(profile.telegram_id, ("undo",), {"type": "delete_entries", "ids": ids}, 1800)
    origin = str((await state.get_data()).get("pending_origin") or "menu")
    if origin == "panel":
        await render_panel(target, state, profile, notice=notice)
        return
    from .menu import render_dashboard

    await render_dashboard(target, state, profile, notice=notice, undo=bool(ids))


async def commit_items(target: Message | CallbackQuery, state: FSMContext, profile: Profile, items: list[dict[str, Any]], *, source: str) -> None:
    try:
        inserted = await services.add_finance_entries(profile, items, source=source)
    except Exception as exc:
        logger.exception("Finance save failed")
        text = f"{pe.CROSS} {profile.tr('Ошибка сохранения', 'Saqlash xatosi')}: {h(str(exc)[:120])}"
        if isinstance(target, CallbackQuery):
            await safe_edit(target, text, back_to_menu_keyboard(profile.lang))
        else:
            await show_panel(target, state, text, back_to_menu_keyboard(profile.lang))
        return
    await _save_debt_deadlines(profile, items)
    await finish_save(target, state, profile, items, inserted)


async def _save_debt_deadlines(profile: Profile, items: list[dict[str, Any]]) -> None:
    """«Дал Асилбеку 1 млн, вернёт до 5 октября» — срок сохраняем рядом с долгом."""
    if not db.available("debt_deadlines"):
        return
    for it in items:
        due, name = it.get("due_date"), str(it.get("note") or "").strip()
        if it.get("kind") != "transfer" or not due or not name:
            continue
        side = "lent" if "lent" in (it.get("from_bucket"), it.get("to_bucket")) else "debt"
        try:
            await db.upsert_debt_deadline(profile.telegram_id, person=name, side=side, due_date=str(due)[:10])
        except Exception:
            logger.warning("save debt deadline failed", exc_info=True)
    cache.invalidate(profile.telegram_id, "debt_deadlines")


async def ask_confirm(
    message: Message, state: FSMContext, profile: Profile, items: list[dict[str, Any]], *, source: str, confident: bool = False
) -> None:
    """Перед подтверждением: если это долг без имени — спросить «кому / у кого».
    Если разбор уверенный (локальный парсер или AI ≥ 90%) и баланс не уходит в минус — сохраняем сразу."""
    missing = [i for i, item in enumerate(items) if fin.needs_counterparty(item)]
    if missing:
        from ..keyboards import debt_note_keyboard

        idx = missing[0]
        item = items[idx]
        await state.set_state(BotStates.waiting_debt_note)
        await state.update_data(pending_finance_items=items, pending_finance_source=source, pending_debt_idx=idx, pending_confident=confident)
        route = fin.transfer_label(item.get("from_bucket", "card"), item.get("to_bucket", "cash"), profile.lang)
        text = ui.join(
            ui.title("🤝", "Qarz" if profile.lang == "uz" else "Долг"),
            ui.card(f"<b>{fin.fmt_money(float(item.get('amount') or 0))} {profile.currency}</b>", [route]),
            f"<b>{fin.debt_direction_label(item, profile.lang)}</b>\n" + ui.muted(
                "Ism yoki bank nomini yozing — «Qarzlar» bo'limida odamlar bo'yicha ko'rinadi." if profile.lang == "uz"
                else "Напиши имя или банк — в разделе «Долги» будет видно по людям."),
        )
        await show_panel(message, state, text, debt_note_keyboard(profile.lang))
        return
    snap = await services.finance_snapshot(profile)
    if confident:
        after = fin.apply_pending(snap.balances, items)
        if all(after[b] >= 0 for b in ("card", "cash")):
            await commit_items(message, state, profile, items, source=source)
            return
    await state.set_state(BotStates.waiting_finance_confirm)
    await state.update_data(pending_finance_items=items, pending_finance_source=source)
    await show_panel(message, state, _format_pending(items, profile, snap.balances), finance_add_confirm_keyboard(profile.lang))


@router.message(BotStates.waiting_debt_note, F.text)
async def msg_debt_note(message: Message, state: FSMContext) -> None:
    profile = await get_profile(message.from_user)
    name = (message.text or "").strip(" .,;:—-")[:60]
    await safe_delete(message)
    data = await state.get_data()
    items = data.get("pending_finance_items") or []
    idx = int(data.get("pending_debt_idx") or 0)
    if not items or name.startswith("/"):
        await render_panel(message, state, profile)
        return
    if 0 <= idx < len(items):
        items[idx]["note"] = name or None
        # то же имя — для остальных долговых операций без имени в этом сообщении
        for other in items[idx + 1:]:
            if fin.needs_counterparty(other):
                other["note"] = name or None
                break
    await ask_confirm(message, state, profile, items, source=str(data.get("pending_finance_source") or "text"), confident=bool(data.get("pending_confident")))


@router.message(BotStates.waiting_debt_note)
async def msg_debt_note_other(message: Message, state: FSMContext) -> None:
    await safe_delete(message)


@router.callback_query(F.data == "finance:debt_note_skip")
async def cb_debt_note_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    data = await state.get_data()
    items = data.get("pending_finance_items") or []
    idx = int(data.get("pending_debt_idx") or 0)
    if 0 <= idx < len(items):
        items[idx]["note"] = "—"
    if callback.message is None:
        return
    snap = await services.finance_snapshot(profile)
    missing = [i for i, item in enumerate(items) if fin.needs_counterparty(item) and str(item.get("note")) != "—"]
    if missing:
        await state.update_data(pending_finance_items=items, pending_debt_idx=missing[0])
        item = items[missing[0]]
        route = fin.transfer_label(item.get("from_bucket", "card"), item.get("to_bucket", "cash"), profile.lang)
        from ..keyboards import debt_note_keyboard

        await safe_edit(callback, ui.join(ui.title("🤝", "Qarz" if profile.lang == "uz" else "Долг"),
                                          ui.card(f"<b>{fin.fmt_money(float(item.get('amount') or 0))} {profile.currency}</b>", [route]),
                                          f"<b>{fin.debt_direction_label(item, profile.lang)}</b>"), debt_note_keyboard(profile.lang))
        return
    for item in items:
        if str(item.get("note")) == "—":
            item["note"] = None
    await state.set_state(BotStates.waiting_finance_confirm)
    await state.update_data(pending_finance_items=items)
    await remember_panel(callback, state)
    await safe_edit(callback, _format_pending(items, profile, snap.balances), finance_add_confirm_keyboard(profile.lang))


async def handle_finance_voice(message: Message, state: FSMContext, profile: Profile, transcript: str | None = None) -> None:
    if transcript is None:
        await show_progress(message, profile.tr("⏳ Распознаю голос…", "⏳ Ovoz aniqlanmoqda…"))
        try:
            transcript = await transcribe_audio(message)
        except Exception as exc:
            logger.exception("Voice transcribe failed")
            await safe_delete(message)
            await render_panel(message, state, profile, notice=f"{pe.CROSS} {profile.tr('Ошибка распознавания', 'Ovozni aniqlash xatosi')}: {h(str(exc)[:120])}")
            return
    await handle_finance_text(message, state, profile, transcript or "", source="voice_ai")


@router.message(BotStates.waiting_finance_input, F.voice | F.audio)
@router.message(BotStates.waiting_finance_confirm, F.voice | F.audio)
async def msg_input_voice(message: Message, state: FSMContext) -> None:
    await handle_finance_voice(message, state, await get_profile(message.from_user))


@router.message(BotStates.waiting_finance_input, F.photo)
@router.message(BotStates.waiting_finance_confirm, F.photo)
@router.message(BotStates.waiting_finance_settings, F.photo)
async def msg_input_photo(message: Message, state: FSMContext) -> None:
    # фото в разделе финансов = чек/квитанция (подпись-вакансия — исключение)
    profile = await get_profile(message.from_user)
    caption = (message.caption or "").strip()
    if caption and vac.looks_like_vacancy(caption):
        from .vacancy import process_vacancy

        await process_vacancy(message, state, profile, caption)
        return
    from .finance_extra import handle_receipt_photo

    await handle_receipt_photo(message, state, profile)


@router.message(BotStates.waiting_finance_input, F.text)
@router.message(BotStates.waiting_finance_confirm, F.text)
async def msg_input_text(message: Message, state: FSMContext) -> None:
    # в состоянии подтверждения новый текст = новая операция (старая отменяется)
    profile = await get_profile(message.from_user)
    text = (message.text or "").strip()
    if text.startswith("/"):
        await safe_delete(message)
        return
    if _looks_like_question(text):
        await safe_delete(message)
        await answer_question(message, profile, text)
        return
    await handle_finance_text(message, state, profile, text, source="text")


@router.message(BotStates.waiting_finance_input)
@router.message(BotStates.waiting_finance_confirm)
async def msg_input_other(message: Message, state: FSMContext) -> None:
    profile = await get_profile(message.from_user)
    await safe_delete(message)
    await render_panel(message, state, profile, notice=profile.tr("Нужен текст или голос.", "Matn yoki ovoz kerak."))


@router.callback_query(F.data == "finance:add_confirm")
async def cb_add_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await get_profile(callback.from_user)
    data = await state.get_data()
    items = data.get("pending_finance_items") or []
    if not items:
        await answer_now(callback, profile.tr("Нет данных для сохранения", "Saqlash uchun ma'lumot yo'q"), alert=True)
        return
    await answer_now(callback, profile.tr("Сохранено ✅", "Saqlandi ✅"))
    await commit_items(callback, state, profile, items, source=str(data.get("pending_finance_source") or "text"))


@router.callback_query(F.data == "finance:add_cancel")
async def cb_add_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await get_profile(callback.from_user)
    await answer_now(callback, profile.tr("Отменено", "Bekor qilindi"))
    if str((await state.get_data()).get("pending_origin") or "menu") == "panel":
        await render_panel(callback, state, profile)
        return
    from .menu import render_dashboard

    await render_dashboard(callback, state, profile)


@router.callback_query(F.data.startswith("finance:quick:"))
async def cb_quick(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await get_profile(callback.from_user)
    try:
        idx = int(callback.data.split(":")[-1])
    except ValueError:
        await answer_now(callback)
        return
    quick = (await state.get_data()).get("quick_ops")
    if quick is None:
        quick = (await services.finance_snapshot(profile)).quick
    if idx < 0 or idx >= len(quick):
        await answer_now(callback, profile.tr("Не найдено, обнови панель", "Topilmadi, panelni yangilang"), alert=True)
        return
    item = quick[idx]
    await answer_now(callback, f"{fin.quick_label(item, profile.lang)} ✅")
    await services.add_finance_entries(
        profile,
        [{"kind": item.get("entry_type"), "amount": item.get("amount"), "category": item.get("category"), "note": item.get("note"), "bucket": item.get("bucket")}],
        source="quick",
    )
    await render_panel(callback, state, profile)


# ------------------------------------------------------------------ operations list
def _period_days(code: str) -> int:
    return {"day": 1, "week": 7, "month": 30}.get(code, 1)


@router.callback_query(F.data.startswith("finance:ops:"))
async def cb_ops(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    period = callback.data.split(":")[-1]
    period = period if period in {"day", "week", "month"} else "day"
    entries = await services.finance_entries(profile.telegram_id)
    today = profile.today
    start = today - timedelta(days=_period_days(period) - 1)
    rows = fin.entries_between(entries, start, today)
    lang, cur = profile.lang, profile.currency
    labels = {"day": ("День", "Kun"), "week": ("7 дней", "7 kun"), "month": ("30 дней", "30 kun")}[period]
    lines = [
        "📂 <b>Moliya / Operatsiyalar</b>" if lang == "uz" else "📂 <b>Финансы / Операции</b>",
        f"<i>{'Davr' if lang == 'uz' else 'Период'}: {labels[1] if lang == 'uz' else labels[0]}</i>",
        "",
    ]
    if not rows:
        lines.append("Operatsiyalar topilmadi." if lang == "uz" else "Операции не найдены.")
    else:
        last_day = None
        income = expense = 0.0
        for row in rows[:40]:
            day = str(row.get("entry_date") or "")[:10]
            if day != last_day:
                if last_day is not None:
                    lines.append("")
                try:
                    lines.append(f"<b>{date.fromisoformat(day).strftime('%d.%m.%Y')}</b>")
                except Exception:
                    lines.append(f"<b>{day}</b>")
                last_day = day
            lines.append("• " + _entry_line(row, lang))
        for row in rows:
            if fin.is_transfer(row):
                continue
            if row.get("entry_type") == "income":
                income += float(row.get("amount") or 0)
            else:
                expense += float(row.get("amount") or 0)
        lines += ["", f"{pe.INCOME} {fin.fmt_money(income)}  {pe.EXPENSE} {fin.fmt_money(expense)} {cur}"]
        if len(rows) > 40:
            lines.append(f"<i>… {'yana' if lang == 'uz' else 'ещё'} {len(rows) - 40}</i>")
    lines += ["", "<i>" + ("Tafsilot uchun operatsiyani tanlang." if lang == "uz" else "Выбери операцию для деталей.") + "</i>"]
    await state.set_state(BotStates.waiting_finance_input)
    await remember_panel(callback, state)
    await safe_edit(callback, "\n".join(lines), finance_operations_keyboard(rows, period, lang))


def _detail_text(entry: dict[str, Any], profile: Profile) -> str:
    lang, cur = profile.lang, profile.currency
    amount = float(entry.get("amount") or 0)
    note = h(fin.clean_note(entry.get("note")) or "-")
    day = str(entry.get("entry_date") or "")[:10]
    transfer = fin.transfer_from_note(entry.get("note"))
    if transfer:
        route = fin.transfer_label(transfer[0], transfer[1], lang)
        is_debt = "lent" in transfer or "debt" in transfer
        who = ("Kim" if lang == "uz" else "Кто / кому") if is_debt else ("Izoh" if lang == "uz" else "Заметка")
        title = ("Qarz" if lang == "uz" else "Долг") if is_debt else ("O'tkazma" if lang == "uz" else "Перевод")
        return (
            f"💰 <b>{title}</b>\n\n"
            f"{'Yo`nalish' if lang == 'uz' else 'Маршрут'}: <b>{route}</b>\n"
            f"{'Summa' if lang == 'uz' else 'Сумма'}: <b>{fin.fmt_money(amount)} {cur}</b>\n"
            f"{'Sana' if lang == 'uz' else 'Дата'}: {day}\n{who}: <b>{note}</b>"
        )
    kind = entry.get("entry_type")
    sign = "+" if kind == "income" else "−"
    key = fin.entry_category_key(entry)
    if lang == "uz":
        return (
            f"💰 <b>{'Kirim' if kind == 'income' else 'Chiqim'}</b>\n\n"
            f"Summa: <b>{sign}{fin.fmt_money(amount)} {cur}</b>\nKategoriya: {cats.label(key, lang)}\n"
            f"Hisob: {fin.bucket_label(fin.bucket_from_note(entry.get('note')), lang)}\nSana: {day}\nIzoh: {note}"
        )
    return (
        f"💰 <b>{'Доход' if kind == 'income' else 'Расход'}</b>\n\n"
        f"Сумма: <b>{sign}{fin.fmt_money(amount)} {cur}</b>\nКатегория: {cats.label(key, lang)}\n"
        f"Счёт: {fin.bucket_label(fin.bucket_from_note(entry.get('note')), lang)}\nДата: {day}\nЗаметка: {note}"
    )


@router.callback_query(F.data.startswith("finance:view:"))
async def cb_view(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    entry_id = callback.data.split(":")[-1]
    entry = await db.get_finance_entry(profile.telegram_id, entry_id)
    if not entry:
        await answer_now(callback, profile.tr("Операция не найдена", "Operatsiya topilmadi"), alert=True)
        return
    await state.set_state(BotStates.waiting_finance_input)
    await remember_panel(callback, state)
    await safe_edit(callback, _detail_text(entry, profile), finance_detail_keyboard(entry_id, profile.lang, transfer=fin.is_transfer(entry)))


@router.callback_query(F.data.startswith("finance:cat:"))
async def cb_category_pick(callback: CallbackQuery) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    entry_id = callback.data.split(":")[-1]
    entry = await db.get_finance_entry(profile.telegram_id, entry_id)
    if not entry:
        await answer_now(callback, profile.tr("Операция не найдена", "Operatsiya topilmadi"), alert=True)
        return
    kind = "income" if entry.get("entry_type") == "income" else "expense"
    await safe_edit(
        callback,
        _detail_text(entry, profile) + "\n\n" + profile.tr("Выбери категорию:", "Kategoriyani tanlang:"),
        finance_category_keyboard(entry_id, kind, profile.lang),
    )


@router.callback_query(F.data.startswith("finance:setcat:"))
async def cb_category_set(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await get_profile(callback.from_user)
    parts = callback.data.split(":")
    if len(parts) < 4 or not cats.is_valid_key(parts[3]):
        await answer_now(callback)
        return
    entry_id, key = parts[2], parts[3]
    await answer_now(callback, f"{cats.label(key, profile.lang)} ✅")
    await services.set_finance_category(profile.telegram_id, entry_id, key)
    entry = await db.get_finance_entry(profile.telegram_id, entry_id)
    if not entry:
        await render_panel(callback, state, profile)
        return
    await safe_edit(callback, _detail_text(entry, profile), finance_detail_keyboard(entry_id, profile.lang, transfer=fin.is_transfer(entry)))


@router.callback_query(F.data.startswith("finance:ask_del:"))
async def cb_ask_delete(callback: CallbackQuery) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    entry_id = callback.data.split(":")[-1]
    await safe_edit(callback, profile.tr("Удалить операцию?", "Operatsiyani o'chiraymi?"), finance_delete_confirm_keyboard(entry_id, profile.lang))


@router.callback_query(F.data.startswith("finance:del:"))
async def cb_delete(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await get_profile(callback.from_user)
    entry_id = callback.data.split(":")[-1]
    await answer_now(callback, profile.tr("Удалено", "O'chirildi"))
    try:
        await services.delete_finance_entry(profile.telegram_id, entry_id)
    except Exception:
        logger.exception("Finance delete failed")
    await render_panel(callback, state, profile)


# ------------------------------------------------------------------ debts by person
# ------------------------------------------------------------------ edit note
@router.callback_query(F.data.startswith("finance:note:"))
async def cb_note_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    entry_id = callback.data.split(":")[-1]
    entry = await db.get_finance_entry(profile.telegram_id, entry_id)
    if not entry:
        return
    await state.set_state(BotStates.waiting_note_value)
    await state.update_data(note_entry_id=entry_id)
    await remember_panel(callback, state)
    current = fin.clean_note(entry.get("note")) or "—"
    await safe_edit(
        callback,
        _detail_text(entry, profile) + "\n\n" + profile.tr(f"Текущий комментарий: <b>{h(current)}</b>\nНапиши новый (имя, за что, кому):",
                                                           f"Hozirgi izoh: <b>{h(current)}</b>\nYangisini yozing:"),
        finance_setting_input_keyboard(profile.lang),
    )


@router.message(BotStates.waiting_note_value, F.text)
async def msg_note_value(message: Message, state: FSMContext) -> None:
    profile = await get_profile(message.from_user)
    text = (message.text or "").strip()[:80]
    await safe_delete(message)
    entry_id = str((await state.get_data()).get("note_entry_id") or "")
    entry = await db.get_finance_entry(profile.telegram_id, entry_id) if entry_id else None
    if not entry or text.startswith("/"):
        await render_panel(message, state, profile)
        return
    transfer = fin.transfer_from_note(entry.get("note"))
    if transfer:
        new_note = fin.note_with_transfer(text, transfer[0], transfer[1])
    else:
        new_note = fin.note_with_bucket(text, fin.bucket_from_note(entry.get("note")))
    await db.update_finance_entry(profile.telegram_id, entry_id, {"note": new_note})
    from .. import cache

    cache.invalidate(profile.telegram_id, "fin_entries")
    entry["note"] = new_note
    await state.set_state(BotStates.waiting_finance_input)
    await show_panel(message, state, _detail_text(entry, profile) + "\n\n✅", finance_detail_keyboard(entry_id, profile.lang, transfer=bool(transfer)))


@router.message(BotStates.waiting_note_value)
async def msg_note_other(message: Message, state: FSMContext) -> None:
    await safe_delete(message)
    await render_panel(message, state, await get_profile(message.from_user))


# ------------------------------------------------------------------ stats
def build_stats_text(stats: fin.Stats, profile: Profile, limits: dict[str, float] | None = None) -> str:
    lang, cur = profile.lang, profile.currency
    uz = lang == "uz"
    limits = limits or {}
    p = stats.period
    header = ui.title("📊", "Statistika" if uz else "Статистика", fin.period_title(p, lang))

    change = stats.expense_change_pct()
    if change is not None:
        change_text = f"  <i>{'▲' if change > 0 else '▼'}{abs(change):.0f}% {'vs' if uz else 'к'} {fin.prev_period_title(p, lang)}</i>"
    elif stats.prev_expense == 0 and stats.expense > 0 and p.code != "year":
        change_text = f"  <i>({fin.prev_period_title(p, lang)}: 0)</i>"
    else:
        change_text = ""
    net = stats.net
    total_lines = [
        f"{pe.EXPENSE} {'Chiqim' if uz else 'Расход'}: <b>{fin.fmt_money(stats.expense)} {cur}</b>",
        f"{pe.INCOME} {'Kirim' if uz else 'Доход'}: <b>{fin.fmt_money(stats.income)} {cur}</b>",
        f"{'Natija' if uz else 'Итог'}: <b>{ui.signed(net, fin.fmt_money)} {cur}</b>",
    ]
    if change_text:
        total_lines.append(change_text.strip())
    if stats.expense > 0 and p.days > 1:
        total_lines.append(ui.muted(f"{'kuniga o`rtacha' if uz else 'в среднем в день'}: {fin.fmt_money(stats.avg_per_day)}"))
        if stats.top_day:
            total_lines.append(ui.muted(f"{'eng qimmat kun' if uz else 'максимум'} {stats.top_day[0].strftime('%d.%m')}: {fin.fmt_money(stats.top_day[1])}"))
    if stats.transfers:
        total_lines.append(ui.muted(f"{'o`tkazmalar' if uz else 'переводов между счетами'}: {stats.transfers}"))
    totals_card = ui.card(f"<b>{'Jami' if uz else 'Итого'}</b>", total_lines)

    cat_lines: list[str] = []
    if stats.by_category:
        total = stats.expense or 1.0
        for key, amount, count in stats.by_category[:12]:
            share = amount / total
            prev = stats.prev_by_category.get(key)
            delta = ""
            if prev and prev > 0:
                pct_change = (amount - prev) / prev * 100
                if abs(pct_change) >= 20:
                    delta = f" {'▲' if pct_change > 0 else '▼'}{abs(pct_change):.0f}%"
            limit = limits.get(key) if limits and p.code in {"month", "prev_month"} else None
            if limit:
                ratio = amount / limit
                flag = "🚫 " if ratio >= 1 else "⚠️ " if ratio >= 0.8 else ""
                cat_lines.append(f"{flag}{cats.label(key, lang)}{delta}")
                cat_lines.append(f"<b>{fin.fmt_money(amount)}</b> / {fin.fmt_money(limit)} · {ui.pct(ratio)}")
                cat_lines.append(f"{fin.bar(min(ratio, 1.0), 10)} <i>×{count}</i>")
            else:
                cat_lines.append(f"{cats.label(key, lang)}{delta}")
                cat_lines.append(f"<b>{fin.fmt_money(amount)}</b> · {ui.pct(share)} {fin.bar(share, 8)} <i>×{count}</i>")
    else:
        cat_lines.append(ui.muted("Bu davrda xarajatlar yo'q." if uz else "Расходов за период нет."))
    cats_card = ui.card(f"<b>{'Xarajatlar toifalar bo`yicha' if uz else 'Расходы по категориям'}</b>", cat_lines)

    income_card = None
    if stats.income_by_category and len(stats.income_by_category) > 1:
        income_card = ui.card(f"<b>{'Kirimlar' if uz else 'Доходы'}</b>",
                              [f"{cats.label(k, lang)} — {fin.fmt_money(a)}" for k, a, _ in stats.income_by_category[:5]])
    return ui.join(header, totals_card, cats_card, income_card)


@router.callback_query(F.data.startswith("finance:stats:"))
async def cb_stats(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    period_code = callback.data.split(":")[-1]
    entries, limits = await asyncio.gather(services.finance_entries(profile.telegram_id), services.budgets(profile.telegram_id))
    period = fin.period_for(period_code, profile.today)
    stats = fin.compute_stats(entries, period)
    await state.set_state(BotStates.waiting_finance_input)
    await remember_panel(callback, state)
    if callback.message is not None:
        await screen_mod.drop_chart(callback.bot, callback.message.chat.id)
    await safe_edit(callback, build_stats_text(stats, profile, limits), finance_stats_keyboard(period.code, profile.lang))


@router.callback_query(F.data.startswith("finance:chart:"))
async def cb_chart(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await get_profile(callback.from_user)
    period_code = callback.data.split(":")[-1]
    entries = await services.finance_entries(profile.telegram_id)
    period = fin.period_for(period_code, profile.today)
    stats = fin.compute_stats(entries, period)
    if not stats.by_category:
        await answer_now(callback, profile.tr("Нет расходов за период", "Bu davrda xarajat yo'q"), alert=True)
        return
    await answer_now(callback, profile.tr("Строю график…", "Grafik tayyorlanmoqda…"))
    items = [(cats.label(k, profile.lang, with_emoji=False), a) for k, a, _ in stats.by_category]
    title = f"{'Xarajatlar' if profile.lang == 'uz' else 'Расходы'} — {fin.period_title(period, profile.lang)}"
    try:
        chart = await asyncio.to_thread(charts_mod.expense_categories_chart, items, currency=profile.currency, lang=profile.lang, title=title)
    except Exception:
        logger.exception("category chart failed")
        chart = None
    if not chart or callback.message is None:
        await answer_now(callback, profile.tr("Не удалось построить график", "Grafik chizilmadi"), alert=True)
        return
    await screen_mod.send_chart(callback.bot, callback.message.chat.id, BufferedInputFile(chart, filename="categories.png"), caption=title)


# ------------------------------------------------------------------ questions
_QUESTION_WORDS = ("сколько", "qancha", "какой", "какая", "сумм", "итог", "статист", "потратил", "sarfladim", "?", "сравн", "больше всего", "eng ko'p")


def _looks_like_question(text: str) -> bool:
    low = text.lower()
    if fin.parse_local(text) is not None:
        return False
    return any(w in low for w in _QUESTION_WORDS) and ("?" in low or "сколько" in low or "qancha" in low or "статист" in low)


async def build_question_context(profile: Profile) -> str:
    entries, settings, logs, nutrition_profile = await asyncio.gather(
        services.finance_entries(profile.telegram_id),
        services.finance_settings(profile.telegram_id),
        services.calorie_logs(profile, 30),
        services.nutrition_profile(profile.telegram_id),
    )
    today = profile.today
    cur = profile.currency
    balances = fin.compute_balances(entries, settings)
    parts = [f"Сегодня: {today.isoformat()}. Валюта: {cur}."]
    if nutrition_profile:
        parts.append(
            f"Питание: цель {nutrition_profile.get('daily_calories')} ккал/день, Б/Ж/У "
            f"{nutrition_profile.get('protein')}/{nutrition_profile.get('fat')}/{nutrition_profile.get('carbs')}."
        )
    if logs:
        by_day: dict[str, float] = {}
        for row in logs:
            day = str(row.get("created_at") or "")[:10]
            by_day[day] = by_day.get(day, 0.0) + float(row.get("calories") or 0)
        parts.append("Калории по дням (UTC-дата): " + "; ".join(f"{d}={int(v)}" for d, v in sorted(by_day.items())[-30:]))
        parts.append("Последние блюда: " + "; ".join(
            f"{str(r.get('created_at'))[:10]} {str(r.get('meal_desc') or '')[:30]} {int(float(r.get('calories') or 0))}ккал" for r in logs[:25]
        ))
    parts.append("Балансы: " + ", ".join(f"{b}={fin.fmt_money(v)}" for b, v in balances.items()))
    labels = {"day": "сегодня", "week": "последние 7 дней", "month": "этот месяц", "prev_month": "прошлый месяц", "year": "этот год"}
    periods = [(code, fin.period_for(code, today)) for code in labels]
    if entries:
        first = min((fin._entry_date(r) for r in entries if fin._entry_date(r)), default=today)
        periods.append(("all", fin.Period("all", first, today, first, first)))
        labels["all"] = "всё время"
    for code, period in periods:
        st = fin.compute_stats(entries, period)
        cats_txt = "; ".join(f"{cats.label(k, 'ru', with_emoji=False)}={fin.fmt_money(a)} (×{c})" for k, a, c in st.by_category[:12]) or "нет"
        inc_txt = "; ".join(f"{cats.label(k, 'ru', with_emoji=False)}={fin.fmt_money(a)}" for k, a, _ in st.income_by_category[:6]) or "нет"
        parts.append(
            f"[{labels[code]}: {st.period.start.isoformat()}..{st.period.end.isoformat()}] расход={fin.fmt_money(st.expense)}, доход={fin.fmt_money(st.income)}, "
            f"операций={st.ops}; расходы по категориям: {cats_txt}; доходы: {inc_txt}"
        )
    recent = fin.entries_between(entries, today - timedelta(days=14), today)[:40]
    if recent:
        parts.append("Последние операции: " + "; ".join(
            f"{str(r.get('entry_date'))[:10]} {'+' if r.get('entry_type') == 'income' else '-'}{fin.fmt_money(float(r.get('amount') or 0))} "
            f"{cats.label(fin.entry_category_key(r), 'ru', with_emoji=False)} {fin.clean_note(r.get('note')) or ''}".strip()
            for r in recent
        ))
    return "\n".join(parts)


async def answer_question(message: Message, profile: Profile, question: str) -> None:
    await show_progress(message, profile.tr("⏳ Считаю…", "⏳ Hisoblanmoqda…"))
    try:
        context = await build_question_context(profile)
        answer = await ai.answer_question(question, context, profile.lang)
    except Exception:
        logger.exception("answer_question failed")
        answer = profile.tr("Не смог посчитать сейчас, попробуй позже.", "Hozir hisoblay olmadim, keyinroq urining.")
    text, labels, _ = await build_panel(profile)
    await screen_mod.show_screen(message.bot, message.chat.id, f"{pe.IDEA} {h(answer)}\n\n{text}", finance_panel_keyboard(labels, profile.lang))


# ------------------------------------------------------------------ settings (accounts)
_FIELDS = {"card": "card_base", "cash": "cash_base", "lent": "lent_base", "debt": "debt_base", "credit": "monthly_credit_payment"}


def _field_label(field: str, lang: str) -> str:
    ru = {"card": "💳 Карта", "cash": "💵 Наличные", "lent": "🤝 Дал в долг", "debt": "📌 Мои долги", "credit": "🏦 Кредит/мес"}
    uz = {"card": "💳 Karta", "cash": "💵 Naqd", "lent": "🤝 Qarzga berilgan", "debt": "📌 Mening qarzim", "credit": "🏦 Kredit/oy"}
    return (uz if lang == "uz" else ru).get(field, field)


async def _settings_view(profile: Profile) -> tuple[dict[str, float], dict[str, float], dict[str, list[tuple[str, float]]]]:
    """Текущие фактические балансы (base + операции), «живые» без base и долги по людям."""
    snap = await services.finance_snapshot(profile)
    view = {
        "card_base": snap.balances["card"],
        "cash_base": snap.balances["cash"],
        "lent_base": snap.balances["lent"],
        "debt_base": snap.balances["debt"],
        "monthly_credit_payment": float(snap.settings.get("monthly_credit_payment") or 0),
    }
    live = fin.compute_balances(snap.entries)  # без base
    ledger = fin.debt_ledger(snap.entries, snap.settings)
    return view, live, ledger


def _settings_text(profile: Profile, view: dict[str, float], ledger: dict[str, list[tuple[str, float]]] | None = None) -> str:
    lang, cur = profile.lang, profile.currency
    uz = lang == "uz"
    header = ui.title("⚙️", "Moliya sozlamalari" if uz else "Настройки финансов")
    acc_lines = [
        f"{_field_label('card', lang)}: <b>{fin.fmt_money(view['card_base'])} {cur}</b>",
        f"{_field_label('cash', lang)}: <b>{fin.fmt_money(view['cash_base'])} {cur}</b>",
        f"{_field_label('credit', lang)}: <b>{fin.fmt_money(view['monthly_credit_payment'])} {cur}</b>",
        ui.muted("Tuzatish uchun hisobni bosing va joriy summani yozing." if uz else "Чтобы поправить — нажми счёт и введи актуальную сумму."),
    ]
    blocks = [header, ui.card(f"<b>{'Balans' if uz else 'Балансы'}</b>", acc_lines)]
    blocks.append(ui.card(f"<b>{'Qarzlar jami' if uz else 'Долги итого'}</b>", [
        f"🤝 {'Menga qarz' if uz else 'Мне должны'}: <b>{fin.fmt_money(view['lent_base'])} {cur}</b>",
        f"📌 {'Mening qarzim' if uz else 'Я должен'}: <b>{fin.fmt_money(view['debt_base'])} {cur}</b>",
        ui.muted("Kimga/kimdan — «Moliya» ekranida." if uz else "По людям — на экране «Финансы»."),
    ]))
    blocks.append(ui.muted(
        "Eski qarzni qo'shish (pul harakatisiz): «menga Abdulaziz qarz 200000» · «men bankka qarzdorman 3 mln». "
        "Yangi: «Abdulazizga 200000 qarz berdim» · «akamdan 500000 qarz oldim» · «Abdulaziz 100000 qaytardi»."
        if uz else
        "Старый долг (деньги не двигаются): «мне должен Абдулазиз 200000» · «я должен банку 3 млн». "
        "Новый: «дал Абдулазизу 200000» · «взял у брата 500000» · «Абдулазиз вернул 100000»."
    ))
    return ui.join(*blocks)


@router.callback_query(F.data == "finance:settings")
async def cb_settings(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    view, _, ledger = await _settings_view(profile)
    await state.set_state(BotStates.waiting_finance_settings)
    await remember_panel(callback, state)
    await safe_edit(callback, _settings_text(profile, view, ledger), finance_settings_keyboard(view, profile.currency, profile.lang))


@router.callback_query(F.data.startswith("finance:set:"))
async def cb_setting_pick(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    field = callback.data.split(":")[-1]
    if field not in _FIELDS:
        return
    view, _, _ = await _settings_view(profile)
    await state.set_state(BotStates.waiting_finance_settings_value)
    await state.update_data(finance_setting_field=field)
    await remember_panel(callback, state)
    cur = profile.currency
    await safe_edit(
        callback,
        profile.tr(
            f"{_field_label(field, 'ru')}\nСейчас: <b>{fin.fmt_money(view[_FIELDS[field]])} {cur}</b>\n\nВведи актуальную сумму (число):",
            f"{_field_label(field, 'uz')}\nHozir: <b>{fin.fmt_money(view[_FIELDS[field]])} {cur}</b>\n\nJoriy summani kiriting (raqam):",
        ),
        finance_setting_input_keyboard(profile.lang),
    )


@router.message(BotStates.waiting_finance_settings_value, F.text)
async def msg_setting_value(message: Message, state: FSMContext) -> None:
    profile = await get_profile(message.from_user)
    await safe_delete(message)
    parsed = fin.parse_amount((message.text or "").strip())
    if parsed is None:
        await show_panel(message, state, profile.tr("Нужно число, например <code>1500000</code> или <code>1.5 млн</code>", "Raqam kerak, masalan <code>1500000</code> yoki <code>1.5 mln</code>"), finance_setting_input_keyboard(profile.lang))
        return
    value = float(parsed[0])
    field = str((await state.get_data()).get("finance_setting_field") or "")
    if field not in _FIELDS:
        await render_panel(message, state, profile)
        return
    settings = dict(await services.finance_settings(profile.telegram_id))
    if field == "credit":
        settings["monthly_credit_payment"] = value
    else:
        _, live, _ = await _settings_view(profile)
        # base = желаемый текущий остаток − сумма операций
        settings[_FIELDS[field]] = value - live[field]
    await services.save_finance_settings(profile.telegram_id, settings)
    view, _, ledger = await _settings_view(profile)
    await state.set_state(BotStates.waiting_finance_settings)
    await show_panel(message, state, _settings_text(profile, view, ledger) + "\n\n✅", finance_settings_keyboard(view, profile.currency, profile.lang))


@router.message(BotStates.waiting_finance_settings_value)
@router.message(BotStates.waiting_finance_settings)
async def msg_settings_other(message: Message, state: FSMContext) -> None:
    profile = await get_profile(message.from_user)
    text = (message.text or "").strip()
    await safe_delete(message)
    # В «Счетах» тоже можно сразу писать операции и старые долги — удобно.
    if text and not text.startswith("/") and (fin.parse_existing_debt(text) or fin.looks_like_finance(text)):
        await handle_finance_text(message, state, profile, text, source="text")
        return
    view, _, ledger = await _settings_view(profile)
    await state.set_state(BotStates.waiting_finance_settings)
    await show_panel(message, state, _settings_text(profile, view, ledger), finance_settings_keyboard(view, profile.currency, profile.lang))

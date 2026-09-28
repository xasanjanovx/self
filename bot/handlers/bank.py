"""Кнопки вопроса «Записать трату из SMS банка?» (bot/bank_events.py): записать, другая категория, не нужно, удалить."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from .. import bank_events
from .. import categories as cats
from ..keyboards import _btn
from .common import profile_by_id

logger = logging.getLogger(__name__)
router = Router(name="bank")


def question_keyboard(event_id: str, lang: str) -> InlineKeyboardMarkup:
    uz = lang == "uz"
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✅ Yozish" if uz else "✅ Записать", f"bank:ok:{event_id}", style="success"),
         _btn("🗂 Boshqa toifa" if uz else "🗂 Другая категория", f"bank:cats:{event_id}", style="primary")],
        [_btn("✖️ Kerak emas" if uz else "✖️ Не нужно", f"bank:no:{event_id}")],
    ])


def categories_keyboard(event_id: str, kind: str, lang: str) -> InlineKeyboardMarkup:
    items = [c for c in (cats.INCOME if kind == "income" else cats.EXPENSE) if c.key != cats.TRANSFER_KEY][:14]
    rows = [[_btn(c.title(lang), f"bank:set:{event_id}:{c.key}") for c in items[i:i + 2]] for i in range(0, len(items), 2)]
    rows.append([_btn("⬅️ Orqaga" if lang == "uz" else "⬅️ Назад", f"bank:back:{event_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _money(amount: float) -> str:
    return f"{int(round(amount)):,}".replace(",", " ")


@router.callback_query(F.data.startswith("bank:"))
async def on_bank(cb: CallbackQuery) -> None:
    parts = (cb.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    target = parts[2] if len(parts) > 2 else ""
    profile = await profile_by_id(cb.from_user.id)
    uz = profile.lang == "uz"
    if action == "del":
        from .. import cache
        from ..context import db

        try:
            await db.delete_finance_entries(profile.telegram_id, [target])
            cache.invalidate(profile.telegram_id, "fin_entries")
            await cb.message.edit_text("🗑 O‘chirildi" if uz else "🗑 Удалил эту запись")
        except Exception:
            logger.warning("bank: не удалилось", exc_info=True)
        await cb.answer()
        return
    ev = bank_events.pending(target)
    if ev is None:
        await cb.answer("Bu savol eskirgan" if uz else "Этот вопрос уже неактуален", show_alert=False)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    if action == "no":
        bank_events.drop(target)
        await cb.message.edit_text(f"✖️ {_money(ev['amount'])} — " + ("yozmadim" if uz else "не записал"))
        await cb.answer()
        return
    if action == "cats":
        await cb.message.edit_reply_markup(reply_markup=categories_keyboard(target, ev["kind"], profile.lang))
        await cb.answer()
        return
    if action == "back":
        await cb.message.edit_reply_markup(reply_markup=question_keyboard(target, profile.lang))
        await cb.answer()
        return
    if action in {"ok", "set"}:
        category = parts[3] if action == "set" and len(parts) > 3 else None
        try:
            inserted = await bank_events.record(profile.telegram_id, ev, category)
        except Exception:
            logger.exception("bank: запись не вышла")
            await cb.answer("Yozib bo‘lmadi" if uz else "Не получилось записать", show_alert=True)
            return
        bank_events.drop(target)
        label = ("Karta → naqd" if uz else "Карта → наличные") if ev["kind"] == "withdraw" else cats.label(category or ev["category"], profile.lang)
        who = f" ({ev['merchant']})" if ev.get("merchant") else ""
        text = (f"✅ Yozdim: {_money(ev['amount'])} so‘m — {label}{who}" if uz else f"✅ Записал: {_money(ev['amount'])} сум — {label}{who}")
        entry_id = inserted[0].get("id") if inserted else None
        markup = InlineKeyboardMarkup(inline_keyboard=[[_btn("🗑 O‘chirish" if uz else "🗑 Удалить", f"bank:del:{entry_id}")]]) if entry_id else None
        await cb.message.edit_text(text, reply_markup=markup)
        await cb.answer("Yozildi" if uz else "Записал")
        return
    await cb.answer()


__all__ = ["router", "question_keyboard", "categories_keyboard"]

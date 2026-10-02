"""Кнопки плана дня (bot/plan.py): ☐/✅ пункты · ✏️ Изменить · 🔄 Заново · 🗓 На завтра — и переноса дел (bot/carry.py).
Старые кнопки прежней версии (plan:*) тоже работают."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from .. import access, carry, plan
from .common import answer_now, get_profile

router = Router(name="plan")
logger = logging.getLogger(__name__)


@router.callback_query(F.data.startswith("pl:") | F.data.startswith("plan:"))
async def cb_plan(callback: CallbackQuery) -> None:
    profile = await get_profile(callback.from_user)
    if not access.is_owner(profile.telegram_id) or callback.message is None:
        await answer_now(callback)
        return
    parts = str(callback.data).split(":")
    action = parts[1] if len(parts) > 1 else ""
    bot = callback.bot
    if action == "d" and len(parts) > 2 and parts[2].isdigit():
        text = await plan.toggle(bot, profile, int(parts[2]))
        await answer_now(callback, "✔️" if text else profile.tr("Этого пункта уже нет", "Bu punkt yo'q"))
        return
    if action == "e":
        await answer_now(callback, profile.tr("Ответьте на сообщение с планом — текстом или голосом: что убрать, добавить или перенести.",
                                              "Reja xabariga javob yozing — matn yoki ovoz bilan: nimani o'zgartiramiz."), alert=True)
        return
    if action in {"m", "move"}:
        moved, asked = await carry.move_open(bot, profile)
        note = profile.tr(f"➡️ Перенесено на завтра: {moved}", f"➡️ Ertaga ga ko'chirildi: {moved}")
        if asked:
            note += profile.tr(f" · про {asked} спрошу отдельно", f" · {asked} tasi haqida alohida so'rayman")
        await answer_now(callback, note, alert=True)
        return
    if action == "q":
        st = plan.load(profile.telegram_id)
        await answer_now(callback, profile.tr("Отправляю суру…", "Sura yuborilmoqda…"))
        await plan.send_quran(bot, profile, st)
        return
    if action in {"r", "today", "t", "tomorrow"}:
        await answer_now(callback, profile.tr("Составляю план…", "Reja tuzyapman…"))
        try:
            if action in {"r", "today"}:
                old = plan.load(profile.telegram_id)
                await plan.send(bot, profile, "morning", city=old.get("place"), force=True)
            else:
                await plan.send(bot, profile, "tomorrow", force=True)
        except Exception:
            logger.exception("plan button failed")
            await callback.message.answer(profile.tr("Не получилось составить план — попробуйте ещё раз.", "Reja tuzilmadi — yana urinib ko'ring."))
        return
    await answer_now(callback)


@router.callback_query(F.data.startswith("carry:"))
async def cb_carry(callback: CallbackQuery) -> None:
    profile = await get_profile(callback.from_user)
    if not access.is_owner(profile.telegram_id) or callback.message is None:
        await answer_now(callback)
        return
    _, action, task_id = (str(callback.data).split(":", 2) + ["", ""])[:3]
    if action == "s":
        await answer_now(callback, profile.tr("Разбиваю на шаги…", "Bosqichlarga bo'lyapman…"))
        steps = await carry.split(profile, task_id)
        text = (profile.tr("🧩 Разбила на шаги — они в задачах на сегодня:\n", "🧩 Bosqichlarga bo'ldim — bugungi vazifalarda:\n")
                + "\n".join(f"• {s}" for s in steps)) if steps else profile.tr("Не получилось разбить — задача осталась как есть.", "Bo'lib bo'lmadi — vazifa o'z holicha qoldi.")
    elif action == "w":
        ok = await carry.postpone_week(profile, task_id)
        text = profile.tr("⏳ Отложила на неделю.", "⏳ Bir haftaga qoldirdim.") if ok else profile.tr("Задачи уже нет.", "Vazifa yo'q.")
        await answer_now(callback)
    elif action == "d":
        ok = await carry.delete(profile, task_id)
        text = profile.tr("🗑 Удалила.", "🗑 O'chirdim.") if ok else profile.tr("Задачи уже нет.", "Vazifa yo'q.")
        await answer_now(callback)
    else:
        await answer_now(callback)
        return
    try:
        await callback.message.edit_text(text, reply_markup=None)
    except Exception:
        await callback.message.answer(text)


async def handle_reply(message: Message, profile, text: str) -> bool:  # noqa: ANN001
    """Ответ на сообщение с планом (текстом или голосом): поменять план. True — обработано."""
    text = (text or "").strip()
    if not text:
        return False
    progress = await message.answer(profile.tr("✏️ Меняю план…", "✏️ Rejani o'zgartiryapman…"))
    try:
        st = await plan.edit(message.bot, profile, text)
    except Exception:
        logger.exception("plan edit failed")
        st = None
    try:
        await progress.delete()
    except Exception:
        pass
    from .. import screen as screen_mod

    if st is None:
        note = profile.tr("Не получилось поменять — попробуйте сказать иначе.", "O'zgartirib bo'lmadi — boshqacha ayting.")
    else:
        done = sum(1 for it in st["items"] if it.get("done"))
        note = profile.tr(f"✅ План обновлён · пунктов: {len(st['items'])}, выполнено {done}", f"✅ Reja yangilandi · {len(st['items'])} punkt, {done} bajarildi")
    # 02.10: короткое подтверждение само исчезает (план обновился на месте — отдельное сообщение не должно висеть)
    await screen_mod.send_ephemeral(message.bot, message.chat.id, note, keep_previous=True, ttl=8)
    return True

"""Настройки → 👥 Пользователи (только владелец): пригласить по ссылке, посмотреть, удалить."""
from __future__ import annotations

import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from .. import access
from .. import ui
from ..context import db
from ..keyboards import _btn, invite_keyboard, members_keyboard
from ..profile import Profile, h
from .common import answer_now, get_profile, safe_edit

router = Router(name="members")
logger = logging.getLogger(__name__)

_bot_username: str | None = None


async def _username(callback: CallbackQuery) -> str:
    global _bot_username
    if not _bot_username:
        _bot_username = (await callback.bot.get_me()).username or ""
    return _bot_username


def _name(row: dict) -> str:
    name = str(row.get("first_name") or "").strip() or str(row.get("telegram_id"))
    return f"{name} (@{row['username']})" if row.get("username") else name


async def _guard(callback: CallbackQuery) -> Profile | None:
    profile = await get_profile(callback.from_user)
    if not access.is_owner(profile.telegram_id):
        await answer_now(callback, profile.tr("Только для владельца бота", "Faqat bot egasi uchun"), alert=True)
        return None
    if not db.available("bot_members"):
        await answer_now(callback, profile.tr("Нужна миграция 011", "011 migratsiyasi kerak"), alert=True)
        return None
    return profile


async def render_members(callback: CallbackQuery, profile: Profile, *, notice: str | None = None) -> None:
    uz = profile.lang == "uz"
    rows = await access.members()
    lines = [f"👑 {h(profile.first_name or '—')} — {'egasi' if uz else 'владелец'}"]
    for r in rows:
        since = ""
        try:
            since = datetime.fromisoformat(str(r.get("created_at")).replace("Z", "+00:00")).astimezone(profile.tz).strftime("%d.%m.%Y")
        except ValueError:
            pass
        lines.append(f"👤 {h(_name(r))}" + (f" · {'dan' if uz else 'с'} {since}" if since else ""))
    hint = (f"Taklif havolasi bir martalik, {access.INVITE_DAYS} kun amal qiladi. Har kimning ma'lumotlari alohida."
            if uz else f"Ссылка-приглашение одноразовая, действует {access.INVITE_DAYS} дней. Данные у каждого свои — никто не видит чужие.")
    text = ui.join(ui.title("👥", "Foydalanuvchilar" if uz else "Пользователи"), ui.card(f"<b>{'Hozir' if uz else 'Сейчас'}</b>", lines), ui.muted(hint))
    if notice:
        text += f"\n\n{notice}"
    await safe_edit(callback, text, members_keyboard(profile.lang, [(int(r["telegram_id"]), _name(r)) for r in rows]))


@router.callback_query(F.data == "members:open")
async def cb_members(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await _guard(callback)
    if profile is None:
        return
    await answer_now(callback)
    await state.clear()
    await render_members(callback, profile)


@router.callback_query(F.data == "members:invite")
async def cb_invite(callback: CallbackQuery) -> None:
    profile = await _guard(callback)
    if profile is None:
        return
    await answer_now(callback)
    code = await access.create_invite(profile.telegram_id)
    link = access.invite_link(await _username(callback), code)
    uz = profile.lang == "uz"
    text = ui.join(
        ui.title("➕", "Taklif" if uz else "Приглашение"),
        f"<code>{h(link)}</code>",
        ui.muted(f"Bir kishi uchun, {access.INVITE_DAYS} kun. «Yuborish» — Telegram'da kimga yuborishni tanlang."
                 if uz else f"Для одного человека, {access.INVITE_DAYS} дней. «Отправить» — выбери, кому в Telegram."),
    )
    await safe_edit(callback, text, invite_keyboard(profile.lang, link))


@router.callback_query(F.data.startswith("members:del:"))
async def cb_delete(callback: CallbackQuery) -> None:
    profile = await _guard(callback)
    if profile is None:
        return
    try:
        uid = int(callback.data.split(":")[-1])
    except ValueError:
        await answer_now(callback)
        return
    await answer_now(callback)
    row = next((r for r in await access.members() if int(r["telegram_id"]) == uid), {"telegram_id": uid})
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        _btn(profile.tr("Да, удалить", "Ha, o'chirish"), f"members:del_ok:{uid}", style="danger"),
        _btn(profile.tr("Нет", "Yo'q"), "members:open"),
    ]])
    await safe_edit(callback, profile.tr(f"Убрать доступ у <b>{h(_name(row))}</b>? Его данные останутся в базе, но бот перестанет ему отвечать.",
                                         f"<b>{h(_name(row))}</b> dan ruxsatni olib tashlaymi? Ma'lumotlari bazada qoladi, lekin bot unga javob bermaydi."), kb)


@router.callback_query(F.data.startswith("members:del_ok:"))
async def cb_delete_ok(callback: CallbackQuery) -> None:
    profile = await _guard(callback)
    if profile is None:
        return
    try:
        uid = int(callback.data.split(":")[-1])
    except ValueError:
        await answer_now(callback)
        return
    if access.is_owner(uid):
        await answer_now(callback, profile.tr("Владельца удалить нельзя", "Egani o'chirib bo'lmaydi"), alert=True)
        return
    await access.remove(uid)
    await answer_now(callback, "✅")
    await render_members(callback, profile, notice=profile.tr("✅ Доступ убран", "✅ Ruxsat olib tashlandi"))


__all__ = ["router"]

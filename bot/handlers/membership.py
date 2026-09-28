"""Заблокировал / разблокировал бота (my_chat_member в личке) — bot/blocked.py: заблокировавшим ничего не шлём и ИИ на них не тратим."""
from __future__ import annotations

from aiogram import Router
from aiogram.types import ChatMemberUpdated

from .. import blocked

router = Router(name="membership")


@router.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated) -> None:
    if event.chat.type != "private":
        return
    status = str(getattr(event.new_chat_member, "status", "") or "")
    if status in {"kicked", "left"}:
        blocked.mark(event.chat.id, "заблокировал в Telegram")
    elif status == "member":
        blocked.unmark(event.chat.id)

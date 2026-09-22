"""Одноразовый вход в аккаунт-помощник → строка сессии для TG_CALLER_SESSION.

Запускать у себя на компьютере:

    pip install telethon
    python scripts/tg_login.py

Скрипт спросит api_id, api_hash, номер и код из Telegram (и пароль 2FA, если он
включён). Код и пароль никуда не отправляются — они нужны только Telegram.
В конце печатает строку сессии: её и вставляем в .env как TG_CALLER_SESSION.
"""
from __future__ import annotations

import asyncio
import os


async def main() -> None:
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        raise SystemExit("Сначала: pip install telethon")

    api_id = os.getenv("TG_CALLER_API_ID") or input("api_id: ").strip()
    api_hash = os.getenv("TG_CALLER_API_HASH") or input("api_hash: ").strip()
    phone = os.getenv("TG_CALLER_PHONE") or input("Номер аккаунта-помощника (+998...): ").strip()

    async with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        await client.start(phone=lambda: phone)
        me = await client.get_me()
        print("\nВошли как:", getattr(me, "first_name", ""), f"@{getattr(me, 'username', '')}", f"id={me.id}")
        print("\nTG_CALLER_SESSION=" + client.session.save())
        print("\nСкопируй строку выше целиком (одной строкой) — это и есть сессия.")


if __name__ == "__main__":
    asyncio.run(main())

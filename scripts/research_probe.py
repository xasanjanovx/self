"""Проверка инструмента research и памяти разговора на настоящей модели (05.10.2026).

    docker exec -w /app -e PYTHONPATH=/app codex-self-bot python scripts/research_probe.py [send]

Без аргумента — только проверка отказов и поиска с контекстом (в чат ничего не уходит); `send` — ещё и две настоящие карточки
(фильм и известный человек) уходят владельцу в чат. Бот создаётся с HTML по умолчанию, как в боте.
"""
from __future__ import annotations

import asyncio
import sys
import time


async def main() -> None:
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    from bot import agent_tools, session_memory
    from bot.agent_tools import ToolContext
    from bot.context import db, set_bot, settings
    from bot.handlers.common import profile_by_id

    await db.connect()
    await db.health_check()
    uid = sorted(settings.allowed_telegram_ids)[0]
    profile = await profile_by_id(uid)
    set_bot(Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML)))

    async def run(name: str, args: dict) -> dict:
        t0 = time.monotonic()
        res = await agent_tools.run(name, args, ToolContext(profile=profile, text=""))
        print(f"\n== {name} {args} → {time.monotonic() - t0:.1f} с\n{str(res)[:900]}")
        return res

    # 1. частное лицо и поиск личных данных — карточки нет
    await run("research", {"kind": "person", "query": "номер телефона и домашний адрес Азиза Каримова"})
    if "send" in sys.argv:
        await run("research", {"kind": "person", "query": "Азиз Каримов, мой друг из Андижана, работает бухгалтером"})

    # 2. поиск с контекстом разговора: «а сколько ему лет?» без имени
    session_memory.forget(uid)
    session_memory.note(uid, "кто сыграл Купера в Интерстеллар", "Купера сыграл Мэттью Макконахи")
    session_memory.set_topic(uid, "актёр: Мэттью Макконахи")
    await run("web_search", {"query": "а сколько ему лет"})

    if "send" in sys.argv:
        await run("research", {"kind": "movie", "query": "Интерстеллар 2014"})
        await run("research", {"kind": "person", "query": "Том Хэнкс"})
        print("\nтема после карточки:", session_memory.topic(uid))
        print(session_memory.block(uid))


asyncio.run(main())

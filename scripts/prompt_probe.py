"""Сколько токенов уходит на инструкцию и инструменты в каждой реплике (телефон / Live-звонок / агент чата).
Считает бесплатный countTokens Gemini — ничего не тратит.

    docker exec -w /app -e PYTHONPATH=/app codex-self-bot python scripts/prompt_probe.py [uid]
"""
from __future__ import annotations

import asyncio
import json
import sys


async def count(http, settings, text: str) -> int:  # noqa: ANN001
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash-lite:countTokens"
    body = {"contents": [{"role": "user", "parts": [{"text": text}]}]}
    async with http.post(url, json=body, headers={"x-goog-api-key": settings.gemini_api_key}) as r:
        data = await r.json()
        return int(data.get("totalTokens") or 0)


async def main() -> None:
    import aiohttp

    from bot import agent_tools, live_call, phone, services
    from bot import agent_tools_extra as extra
    from bot.context import db, settings
    from bot.handlers.common import profile_by_id

    uid = int(sys.argv[1]) if len(sys.argv) > 1 else settings.owner_id
    await db.connect()
    profile = await profile_by_id(uid)
    persona = await services.persona(uid)
    memory = await extra.memory_prompt(uid)
    people = phone.people_line(uid)
    phone_system = live_call.system_instruction(profile, persona, mode="phone", memory=memory, people=people)
    parts = {
        "phone: инструкция целиком": phone_system,
        "phone: память (факты)": live_call.facts_only(memory),
        "phone: недавние реплики": live_call.recent_lines(memory),
        "phone: люди": people,
        "phone: правила PHONE_RULES": live_call.PHONE_RULES,
        "phone: инструменты Live": json.dumps(live_call.tool_declarations("phone"), ensure_ascii=False),
        "agent: все инструменты": json.dumps(agent_tools.declarations(), ensure_ascii=False),
        "agent: снимок данных": await agent_tools.snapshot(profile),
        "agent: память целиком": memory,
    }
    async with aiohttp.ClientSession() as http:
        for name, text in parts.items():
            print(f"{name:32s} {len(text):7d} знаков  {await count(http, settings, text or '-'):6d} токенов")
        for d in live_call.tool_declarations("phone"):
            print(f"   tool {d['name']:22s} {len(json.dumps(d, ensure_ascii=False)):5d} знаков")


asyncio.run(main())

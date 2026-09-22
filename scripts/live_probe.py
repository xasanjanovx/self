"""Проверка Gemini Live без звонка: подключение, голос, инструменты, время до первого звука.

    docker exec codex-self-bot python scripts/live_probe.py [assistant|wake]
"""
from __future__ import annotations

import asyncio
import json
import sys
import time


async def main() -> None:
    import aiohttp

    from bot import live_call, persona
    from bot.context import db, settings
    from bot.handlers.common import profile_by_id

    mode = sys.argv[1] if len(sys.argv) > 1 else "assistant"
    await db.connect()
    await db.health_check()
    uid = sorted(settings.allowed_telegram_ids)[0]
    profile = await profile_by_id(uid)
    p = persona.Persona()
    system = live_call.system_instruction(profile, p, mode=mode, wake={"takbir": "05:07", "minutes_left": 25, "task": "stakan suv iching"})
    sess = live_call._Session(profile, p, mode=mode, system=system)
    async with aiohttp.ClientSession() as http:
        t0 = time.monotonic()
        ws = await sess.connect(http)
        print(f"setup ok: {sess.result.model} за {time.monotonic() - t0:.2f} c, инструментов: {len(live_call.tool_declarations(mode))}")
        t1 = time.monotonic()
        await ws.send_str(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{"text": "[Звонок соединён. Начинай.]"}]}], "turnComplete": True}}))
        first_audio = None
        audio_bytes = 0
        text = []
        while True:
            msg = await asyncio.wait_for(ws.receive(), timeout=30)
            data = live_call._decode(msg)
            if data is None:
                print("закрыто:", msg.type, getattr(msg, "extra", ""))
                break
            sc = data.get("serverContent") or {}
            for part in ((sc.get("modelTurn") or {}).get("parts") or []):
                if (part.get("inlineData") or {}).get("data"):
                    first_audio = first_audio or time.monotonic()
                    audio_bytes += len(part["inlineData"]["data"]) * 3 // 4
            if (t := (sc.get("outputTranscription") or {}).get("text")):
                text.append(t)
            if sc.get("turnComplete"):
                break
        print(f"первый звук через {first_audio - t1:.2f} c" if first_audio else "звука нет")
        print(f"аудио: {audio_bytes / 48000:.1f} c речи")
        print("сказал:", "".join(text).strip())
        # дальше — вопросы текстом (проверка языка и «ума»): live_probe.py assistant "вопрос 1" "вопрос 2"
        for question in sys.argv[2:]:
            await ws.send_str(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{"text": question}]}], "turnComplete": True}}))
            text = []
            while True:
                msg = await asyncio.wait_for(ws.receive(), timeout=60)
                data = live_call._decode(msg)
                if data is None:
                    print("закрыто:", msg.type, getattr(msg, "extra", ""))
                    break
                if "toolCall" in data:
                    calls = data["toolCall"].get("functionCalls") or []
                    print("  инструменты:", [c.get("name") for c in calls])
                    await sess._run_tools(ws, calls)
                sc = data.get("serverContent") or {}
                if (t := (sc.get("outputTranscription") or {}).get("text")):
                    text.append(t)
                if sc.get("turnComplete") and text:  # после инструмента ответ приходит следующим ходом
                    break
            print(f"\n❓ {question}\n💬 {''.join(text).strip()}")
        await ws.close()


if __name__ == "__main__":
    asyncio.run(main())

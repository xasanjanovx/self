"""Проверка экономного режима без телефона: реплики текстом → Flash-Lite → ответ голосом «диктора»; время и цена
каждого хода. И заодно — принимает ли Gemini Live сжатие памяти и минимальные «размышления».

    docker exec -w /app -e PYTHONPATH=/app codex-self-bot python scripts/economy_probe.py ["какая завтра погода?" …]

Действия на телефон никуда не уходят (телефон поддельный), инструменты бота — настоящие: фразы берите безвредные.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

PHRASES = ["Какая завтра погода в Андижане?", "Сколько будет семнадцать умножить на двадцать три?"]


class FakePhone:
    def __init__(self) -> None:
        self.closed = False
        self.audio = 0
        self.first_audio: float | None = None
        self.messages: list[dict] = []

    async def send_str(self, s: str) -> None:
        self.messages.append(json.loads(s))

    async def send_bytes(self, b: bytes) -> None:
        if self.first_audio is None:
            self.first_audio = time.monotonic()
        self.audio += len(b)


async def main() -> None:
    import aiohttp

    from bot import agent_tools_extra as extra
    from bot import billing, live_call, phone_cheap, services
    from bot.context import db, settings
    from bot.handlers.common import profile_by_id

    await db.connect()
    await db.health_check()
    uid = sorted(settings.allowed_telegram_ids)[0]
    profile = await profile_by_id(uid)
    persona = await services.persona(uid)
    memory = await extra.memory_prompt(uid)
    phrases = sys.argv[1:] or PHRASES

    phone = FakePhone()
    sess = phone_cheap.PhoneCheap(profile, persona, phone, {"battery": 90}, memory)
    for phrase in phrases:
        phone.first_audio, phone.audio, phone.messages = None, 0, []
        meter = billing.start_session("probe", "economy")
        t0 = time.monotonic()
        await sess._on_text(phrase, visible=True)
        took = time.monotonic() - t0
        billing.end_session(meter, said=phrase[:60])
        said = next((m["text"] for m in phone.messages if m.get("type") == "jarvis"), "(молча)")
        first = f"{phone.first_audio - t0:.2f} с" if phone.first_audio else "—"
        print(f"«{phrase}» → «{said}»\n   ход {took:.2f} с, первый звук {first}, звука {phone.audio / 48000:.1f} с, "
              f"${meter.usd:.5f} {({k: round(v, 5) for k, v in meter.parts.items()})}")
    await sess.speaker.close()

    live = live_call._Session(profile, persona, mode="phone",
                              system=live_call.system_instruction(profile, persona, mode="phone", memory=memory))
    async with aiohttp.ClientSession() as http:
        t0 = time.monotonic()
        ws = await live.connect(http)
        level = live_call._extras_level.get(live.result.model, 2)
        print(f"Live: {live.result.model} за {time.monotonic() - t0:.2f} с, экономные настройки: "
              + {2: "сжатие + минимальные «размышления»", 1: "только сжатие", 0: "не приняты"}[level])
        await ws.close()
    billing.flush()


if __name__ == "__main__":
    asyncio.run(main())

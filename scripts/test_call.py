"""Тестовый звонок владельцу: проверить голос, слух и разговор.

Запуск на сервере (в контейнере бота):
    docker exec -it codex-self-bot python scripts/test_call.py [ru|uz]

Звонит владельцу (ALLOWED_TELEGRAM_IDS), здоровается, слушает ответ, отвечает
и кладёт трубку. Печатает расшифровку разговора — чтобы видеть, что распозналось.
"""
from __future__ import annotations

import asyncio
import sys


async def main() -> None:
    lang = (sys.argv[1] if len(sys.argv) > 1 else "uz").lower()
    from bot import call_dialog as cd
    from bot import caller
    from bot.context import ai, db, settings

    await db.connect()
    await ai.ensure_models()
    uid = sorted(settings.allowed_telegram_ids)[0]
    print(f"Звоню {uid}, язык: {lang}")
    if not await caller.start():
        print("Звонилка не настроена:", caller.status())
        return

    state = cd.DialogState(lang=lang, name="Xasanjon", takbir="05:07", minutes_left=25,
                           task_text="bir stakan suv iching" if lang == "uz" else "выпей стакан воды")
    greeting = await ai.synthesize(cd.greeting(state))
    if not greeting:
        print("TTS недоступен — звонить нечем")
        return

    async def on_utterance(pcm):
        text = ""
        if pcm:
            path = await cd.pcm_to_ogg_file(pcm)
            if path:
                try:
                    text = (await ai.transcribe_voice(path)).strip()
                finally:
                    cd.cleanup(path)
        print(f"услышал: {text!r}")
        step = cd.decide(state, text)
        if step["action"] == "reply":
            out = await ai.agent_step([{"role": "user", "parts": [{"text": text}]}],
                                      system=cd.system_prompt(state), tools=[], temperature=0.6, max_tokens=160)
            answer = (out.text or "").strip() or cd.nudge(state)
        else:
            answer = str(step.get("say") or "")
        stop = bool(state.confirmed) or state.turns >= 4
        if stop:
            answer = f"{answer} {cd.farewell(state)}".strip()
        print(f"говорю:  {answer!r}")
        return {"pcm": await ai.synthesize(answer), "stop": stop}

    result = await caller.talk(uid, greeting_pcm=greeting, on_utterance=on_utterance, ring_seconds=40, max_seconds=120)
    print("результат:", result)
    print("разговор:", " | ".join(state.transcript))
    await caller.stop()
    await ai.close()


if __name__ == "__main__":
    asyncio.run(main())

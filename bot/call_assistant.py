"""Звонок по требованию: «позвони» → Джарвис звонит и говорит с тобой голосом.

Отличие от подъёма (bot/wake_runner.py): здесь обычный разговор с полным доступом
к данным — тот же агент, что и в чате, со всеми инструментами. Можно спросить
(«сколько я потратил сегодня?») и записать («такси двадцать пять тысяч»), голосом.

Разговор заканчивается, когда человек прощается, молчит подряд или вышел лимит
времени/реплик — трубку кладём сами.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import call_dialog as cd
from . import caller
from . import voice
from .context import ai
from .profile import Profile

logger = logging.getLogger(__name__)

MAX_TURNS = 14
MAX_SECONDS = 420          # 7 минут — больше по телефону не нужно
REPLY_MAX_CHARS = 400      # длинные ответы в трубке не слушают
SILENT_ROUNDS_TO_STOP = 2  # столько раз промолчал подряд — прощаемся

_BYE = ("пока", "до свидания", "отбой", "хватит", "закончим", "кладу трубку", "всё спасибо", "все спасибо",
        "спасибо всё", "спасибо все", "xayr", "rahmat", "bo'ldi", "boldi", "yetar", "tugatdik")
_BYE_EXACT = ("всё", "все", "ок", "ok")


def is_goodbye(text: str) -> bool:
    """Человек прощается. Одиночное «всё» считаем прощанием, внутри фразы — нет."""
    low = " ".join(str(text or "").lower().replace(",", " ").replace("!", " ").replace(".", " ").split())
    if not low:
        return False
    if low in _BYE_EXACT:
        return True
    padded = f" {low} "
    return any(f" {w} " in padded for w in _BYE)


def greeting(profile: Profile, lang: str, topic: str = "") -> str:
    uz = lang != "ru"
    name = profile.first_name or ("do'st" if uz else "друг")
    if topic:
        return (f"Assalomu alaykum, {name}." if uz else f"Ассалому алайкум, {name}.")
    return (f"Assalomu alaykum, {name}. Tinglayapman." if uz else f"Ассалому алайкум, {name}. Слушаю тебя.")


def farewell(lang: str) -> str:
    return "Xayr, ishlaringizga omad." if lang != "ru" else "До связи, хорошего дня."


def voice_reply(text: str) -> str:
    """Ответ агента → пригодная для произношения фраза (без HTML, эмодзи и простыней)."""
    plain = voice.speakable(text)
    if len(plain) <= REPLY_MAX_CHARS:
        return plain
    cut = plain[:REPLY_MAX_CHARS]
    dot = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
    return (cut[: dot + 1] if dot > 120 else cut).strip()


async def _agent_answer(profile: Profile, text: str, history: list[dict[str, Any]], snapshot: str) -> tuple[str, list[dict[str, Any]]]:
    """Ответ «Джарвиса» со всеми инструментами — как в чате, но короткий, для трубки."""
    from .handlers.agent import run_agent

    hint = ("[разговор по телефону: ответь ОДНИМ-ДВУМЯ короткими предложениями, без списков, "
            "эмодзи и markdown; числа называй словами там, где так естественнее]\n")
    result = await run_agent(profile, hint + text, history, snapshot=snapshot)
    return result.text or "", result.contents


async def call_now(profile: Profile, *, topic: str = "", lang: str | None = None) -> dict[str, Any]:
    """Позвонить прямо сейчас и поговорить. `topic` — с чего начать разговор."""
    from . import agent_tools

    if not caller.available():
        return {"ok": False, "error": "caller is not configured"}
    speak_lang = lang or ("uz" if profile.lang == "uz" else "ru")
    snapshot = await agent_tools.snapshot(profile)
    history: list[dict[str, Any]] = []

    opening = greeting(profile, speak_lang, topic)
    if topic:
        answer, history = await _agent_answer(profile, topic, history, snapshot)
        opening = f"{opening} {voice_reply(answer)}".strip()
    greeting_pcm = await ai.synthesize(voice.speakable(opening))
    if not greeting_pcm:
        return {"ok": False, "error": "tts unavailable"}

    transcript: list[str] = [f"я: {opening}"]
    turns = 0
    silent = 0

    async def on_utterance(pcm: bytes | None) -> dict[str, Any]:
        nonlocal turns, silent, history
        text = ""
        if pcm:
            path = await cd.pcm_to_ogg_file(pcm)
            if path:
                try:
                    text = (await ai.transcribe_voice(path)).strip()
                except Exception:
                    logger.warning("call transcribe failed", exc_info=True)
                finally:
                    cd.cleanup(path)
        if not text:
            silent += 1
            if silent >= SILENT_ROUNDS_TO_STOP:
                bye = farewell(speak_lang)
                transcript.append(f"я: {bye}")
                return {"pcm": await ai.synthesize(bye), "stop": True}
            again = "Eshitmadim, qaytaring." if speak_lang != "ru" else "Не расслышал, повтори."
            return {"pcm": await ai.synthesize(again), "stop": False}
        silent = 0
        turns += 1
        transcript.append(f"он: {text}")
        if is_goodbye(text):
            bye = farewell(speak_lang)
            transcript.append(f"я: {bye}")
            return {"pcm": await ai.synthesize(bye), "stop": True}
        try:
            answer, history = await _agent_answer(profile, text, history, snapshot)
        except Exception:
            logger.exception("call agent failed")
            answer = "Hozir javob bera olmadim." if speak_lang != "ru" else "Сейчас не смог ответить."
        say = voice_reply(answer) or ("Bajarildi." if speak_lang != "ru" else "Готово.")
        stop = turns >= MAX_TURNS
        if stop:
            say = f"{say} {farewell(speak_lang)}"
        transcript.append(f"я: {say}")
        return {"pcm": await ai.synthesize(say), "stop": stop}

    result = await caller.talk(profile.telegram_id, greeting_pcm=greeting_pcm, on_utterance=on_utterance,
                               ring_seconds=45, max_seconds=MAX_SECONDS)
    return {"ok": bool(result.get("answered")), "error": result.get("error"), "turns": turns,
            "transcript": transcript}


async def _notify_failure(profile: Profile, error: str) -> None:
    """Звонок не состоялся — сказать об этом в чат, а не молчать."""
    reasons = {
        "tts unavailable": ("Не смог синтезировать голос — отвечаю текстом.", "Ovozni tayyorlay olmadim — matn bilan javob beraman."),
        "caller is not configured": ("Звонки пока не настроены.", "Qo'ng'iroqlar hali sozlanmagan."),
    }
    ru, uz = reasons.get(error, ("Дозвониться не получилось — возможно, звонок отклонён или закрыт настройками приватности.",
                                 "Qo'ng'iroq o'tmadi — rad etilgan yoki maxfiylik sozlamalari to'sib turgan bo'lishi mumkin."))
    try:
        from . import screen as screen_mod
        from .context import bot_instance

        await screen_mod.send_ephemeral(bot_instance(), profile.telegram_id, f"📵 {profile.tr(ru, uz)}",
                                        keep_previous=True, ttl=180)
    except Exception:
        logger.debug("call failure notice failed", exc_info=True)


def call_in_background(profile: Profile, *, topic: str = "", lang: str | None = None) -> asyncio.Task:
    """Звонок фоном: инструмент агента должен ответить сразу, а не ждать конца разговора."""
    async def runner() -> None:
        try:
            result = await call_now(profile, topic=topic, lang=lang)
            logger.info("assistant call to %s: %s", profile.telegram_id, {k: v for k, v in result.items() if k != "transcript"})
            from . import services

            await services.log_agent(profile.telegram_id, text=f"call: {topic or 'разговор'}", kind="call",
                                     reply=" | ".join(result.get("transcript") or [])[:900], ok=bool(result.get("ok")))
            if not result.get("ok"):
                await _notify_failure(profile, str(result.get("error") or ""))
        except Exception:
            logger.exception("assistant call failed")
            await _notify_failure(profile, "internal error")

    return asyncio.create_task(runner(), name=f"call-{profile.telegram_id}")


__all__ = ["call_now", "call_in_background", "is_goodbye", "voice_reply", "greeting", "farewell", "MAX_TURNS"]

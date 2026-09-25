"""Утренняя сводка голосом: ZEKI своим голосом за 30–40 секунд рассказывает день.

Текстовая сводка (bot/briefs.py) → короткий живой пересказ на языке звонков (Gemini) →
TTS выбранным голосом → голосовое в чат с кнопкой «📄 Текстом». Если что-то не вышло —
возвращаем False, и вызывающий отправит обычный текст.
"""
from __future__ import annotations

import logging
import re

from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup

from . import screen as screen_mod
from . import voice as voice_mod
from .context import ai
from .persona import Persona, honorific_rule
from .profile import Profile

logger = logging.getLogger(__name__)


def script_prompt(brief_text: str, profile: Profile, p: Persona) -> str:
    plain = re.sub(r"<[^>]+>", "", brief_text)
    lang = {"uz": "узбекском (литературный, живой)", "en": "английском"}.get(p.lang, "русском")
    address = "на «вы» (siz)" if p.address == "siz" else "на «ты» (sen)"
    if p.lang == "en":
        address = "по-дружески"
    return (
        f"Ты — ZEKI (читается «Зеки»), личный помощник {p.name_for(profile.first_name) or ''}. Перескажи утреннюю сводку ГОЛОСОМ на {lang} языке, "
        f"обращаясь {address}. Голос женский — о себе в женском роде. {honorific_rule(p)}\n"
        "Как живой человек по утрам: тёплое приветствие, затем 3–5 самых важных пунктов (что сегодня запланировано, деньги, цели, намаз), "
        "одна ободряющая фраза в конце. 60–90 слов, без списков, эмодзи, скобок и сокращений; суммы и время — словами. "
        "Не выдумывай ничего, чего нет в сводке. Верни только текст для озвучки.\n\n"
        f"СВОДКА:\n{plain}"
    )


async def send(bot: Bot, profile: Profile, p: Persona, brief_text: str) -> bool:
    """Отправить сводку голосом. True — отправлено."""
    from .keyboards import _btn

    if not voice_mod.available():
        return False
    try:
        script = (await ai.generate_text(script_prompt(brief_text, profile, p), temperature=0.6, max_tokens=500)).strip()
        if not script:
            return False
        pcm = await ai.synthesize(script, voice=p.voice)
        ogg = await voice_mod.pcm_to_ogg(pcm) if pcm else None
        if not ogg:
            return False
        kb = InlineKeyboardMarkup(inline_keyboard=[[_btn("📄 " + profile.tr("Текстом", "Matn bilan"), "brief:text:morning")]])
        caption = "🌞 " + profile.tr("Доброе утро", "Xayrli tong")
        msg = await bot.send_voice(profile.telegram_id, BufferedInputFile(ogg, "jarvis.ogg"), caption=caption, reply_markup=kb)
        screen_mod.track_ephemeral(profile.telegram_id, msg.message_id)  # исчезнет со следующим действием/через сутки
        return True
    except Exception:
        logger.warning("voice brief failed for %s", profile.telegram_id, exc_info=True)
        return False


__all__ = ["send", "script_prompt"]

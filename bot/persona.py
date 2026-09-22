"""Характер «Джарвиса»: голос, язык разговора, обращение, тон, длина ответов.

Одни и те же настройки действуют везде — в чате (системный промпт агента),
в звонках (Gemini Live) и при подъёме. Хранятся в `assistant_settings`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Голоса Gemini (женские спокойные — по выбору пользователя; + пара запасных).
VOICES: dict[str, tuple[str, str]] = {
    "Sulafat": ("Тёплый", "Iliq"),
    "Achernar": ("Мягкий", "Yumshoq"),
    "Vindemiatrix": ("Нежный", "Nozik"),
    "Despina": ("Ровный", "Silliq"),
    "Kore": ("Уверенный", "Ishonchli"),
}
DEFAULT_VOICE = "Sulafat"
TONES = ("friendly", "calm", "strict")
VERBOSITY = ("short", "normal", "detailed")


@dataclass
class Persona:
    voice: str = DEFAULT_VOICE
    lang: str = "uz"            # uz | ru — язык разговора в звонках
    address: str = "sen"        # sen («ты») | siz («вы»)
    tone: str = "friendly"
    verbosity: str = "short"
    call_name: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any] | None) -> "Persona":
        row = row or {}
        voice = str(row.get("voice") or DEFAULT_VOICE)
        return cls(
            voice=voice if voice in VOICES else DEFAULT_VOICE,
            lang="ru" if str(row.get("lang") or "uz") == "ru" else "uz",
            address="siz" if str(row.get("address") or "sen") == "siz" else "sen",
            tone=str(row.get("tone") or "friendly") if str(row.get("tone") or "friendly") in TONES else "friendly",
            verbosity=str(row.get("verbosity") or "short") if str(row.get("verbosity") or "short") in VERBOSITY else "short",
            call_name=(str(row.get("call_name")).strip() or None) if row.get("call_name") else None,
        )

    def name_for(self, first_name: str | None) -> str:
        return self.call_name or (first_name or "").strip()


def style_rules(p: Persona, *, spoken: bool = False) -> str:
    """Как говорить — строка для системного промпта (чат или звонок)."""
    address = ("обращайся на «вы» (по-узбекски «siz»)" if p.address == "siz"
               else "обращайся на «ты» (по-узбекски «sen»), по-дружески")
    tone = {
        "friendly": "тон тёплый и дружелюбный, как близкий помощник",
        "calm": "тон спокойный и ровный, без лишних эмоций",
        "strict": "тон собранный и требовательный, по делу, без сюсюканья",
    }[p.tone]
    if spoken:
        length = {
            "short": "отвечай одним-двумя короткими предложениями",
            "normal": "отвечай двумя-тремя предложениями",
            "detailed": "можно отвечать подробнее, но не больше пяти предложений",
        }[p.verbosity]
    else:
        length = {
            "short": "отвечай максимально коротко — 1–4 строки",
            "normal": "обычная длина ответа — до 8 строк",
            "detailed": "можно отвечать развёрнуто, с цифрами и пояснениями",
        }[p.verbosity]
    return f"Стиль: {address}; {tone}; {length}."


def lang_rule(p: Persona) -> str:
    if p.lang == "ru":
        return "Говори по-русски."
    return ("Говори по-узбекски (латиница в мыслях, произношение естественное узбекское). "
            "Если собеседник перешёл на русский — отвечай по-русски.")


__all__ = ["Persona", "VOICES", "DEFAULT_VOICE", "TONES", "VERBOSITY", "style_rules", "lang_rule"]

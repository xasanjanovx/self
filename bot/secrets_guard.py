"""Ключи и пароли в чате (27.09.2026): он прислал ключ Gemini боту — агент принял набор символов за просьбу и позвонил.

Теперь любое сообщение, похожее на ключ/токен, перехватывается ДО всех обработчиков: в ИИ, журналы и память оно не
попадает, сообщение удаляется из чата. Ключ Gemini от владельца бот проверяет (бесплатный запрос списка моделей) и
сохраняет как «бесплатный ключ» (DATA_DIR/gemini_free_key, доступ только владельцу файла) — дальше простые вопросы
голосом идут через него (bot/ai.py). Всё, что уже попало в память и журнал раньше, маскируется при запуске.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ключ Google AI Studio: классический «AIza…» и новый «AQ.…»
_GEMINI = re.compile(r"(?<![\w.])(AIza[0-9A-Za-z_\-]{30,}|AQ\.[0-9A-Za-z_\-.]{20,})")
_OTHER = [
    re.compile(r"(?<!\w)\d{8,10}:[A-Za-z0-9_\-]{30,}"),        # токен Telegram-бота
    re.compile(r"(?<![\w-])sk-[A-Za-z0-9_\-]{20,}"),             # OpenAI и похожие
    re.compile(r"(?<![\w-])(?:ghp|gho|github_pat)_[A-Za-z0-9_]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT (ключи Supabase и т.п.)
]
_LONG = re.compile(r"(?<![\w/.:@-])[A-Za-z0-9_\-]{36,}(?![\w/.-])")  # длинный «пароль» без пробелов
MASK = "«ключ скрыт»"


def _mixed(token: str) -> bool:
    return bool(re.search(r"\d", token) and re.search(r"[A-Za-z]", token))


def find(text: str | None) -> tuple[str, str] | None:
    """("gemini" | "secret", значение) — если в тексте есть ключ; иначе None. Длинный «код без пробелов» считается
    ключом, только если сообщение — это он один: в банковских SMS бывают длинные номера операций."""
    t = str(text or "")
    if m := _GEMINI.search(t):
        return "gemini", m.group(1)
    for rx in _OTHER:
        if m := rx.search(t):
            return "secret", m.group(0)
    whole = t.strip()
    if " " not in whole and len(whole) >= 36 and _LONG.fullmatch(whole) and _mixed(whole):
        return "secret", whole
    return None


def mask(text: str | None) -> str:
    """Все ключи в тексте — «ключ скрыт» (для журналов, памяти и промптов)."""
    t = _GEMINI.sub(MASK, str(text or ""))
    for rx in _OTHER:
        t = rx.sub(MASK, t)
    return _LONG.sub(lambda m: MASK if _mixed(m.group(0)) else m.group(0), t)


# ------------------------------------------------------------------ бесплатный ключ Gemini
def _key_file() -> Path:
    from .tg_user import data_dir

    return data_dir() / "gemini_free_key"


def saved_free_key() -> str:
    try:
        return _key_file().read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def save_free_key(key: str) -> None:
    path = _key_file()
    path.write_text(key.strip(), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


async def validate_gemini(key: str) -> tuple[bool, str]:
    """Ключ рабочий? Список моделей — бесплатный запрос. (ok, почему нет)."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get("https://generativelanguage.googleapis.com/v1beta/models", params={"pageSize": 50},
                                 headers={"x-goog-api-key": key})
    except Exception as exc:
        return False, f"нет связи с Google ({type(exc).__name__})"
    if r.status_code == 200:
        return True, ""
    try:
        reason = str(((r.json() or {}).get("error") or {}).get("message") or "")[:160]
    except Exception:
        reason = ""
    return False, f"Google ответил {r.status_code}" + (f": {reason}" if reason else "")


async def handle(message: Any, uid: int, lang: str = "ru") -> None:
    """Сообщение с ключом: удалить из чата; ключ Gemini владельца — проверить и сохранить; остальное — только предупредить."""
    from . import access
    from . import ai as ai_mod

    kind, value = find(message.text or message.caption or "") or ("secret", "")
    uz = lang == "uz"
    try:
        await message.delete()
        deleted = True
    except Exception:
        deleted = False
    tail = ("" if not deleted else (" Xabarni chatdan o‘chirdim." if uz else " Сообщение с ключом я удалил из чата."))
    if kind == "gemini" and access.is_owner(uid):
        ok, why = await validate_gemini(value)
        if ok:
            save_free_key(value)
            ai_mod.reload_free_key()
            logger.info("secrets: бесплатный ключ Gemini проверен и сохранён (%s…)", value[:4])
            text = ("🔑 Gemini kaliti tekshirildi va saqlandi — oddiy savollar endi u orqali bepul." if uz
                    else "🔑 Ключ Gemini проверен и сохранён — простые вопросы голосом теперь идут через него бесплатно.") + tail
        else:
            logger.info("secrets: ключ Gemini не принят: %s", why)
            text = (f"🔑 Bu Gemini kalitiga o‘xshaydi, lekin Google uni qabul qilmadi ({why}). To‘liq nusxalab qayta yuboring." if uz
                    else f"🔑 Похоже на ключ Gemini, но Google его не принял ({why}). Скопируйте целиком и пришлите ещё раз.") + tail
    else:
        text = ("🔒 Bu kalit yoki parolga o‘xshaydi — saqlamadim va hech kimga yubormadim. Sirlarni chatga yubormang." if uz
                else "🔒 Похоже на ключ или пароль — я его не сохранял и никуда не отправлял. Не присылайте секреты в чат.") + tail
    try:
        await message.answer(text)
    except Exception:
        logger.debug("secrets: ответ не ушёл", exc_info=True)


async def scrub_existing(uids: list[int]) -> int:
    """Ключи, которые уже успели попасть в память (недавние реплики) и журнал агента, — замаскировать. Сколько записей."""
    from . import services
    from .context import db

    fixed = 0
    for uid in uids:
        try:
            mem = await services.user_memory(uid)
            recent = str(mem.get("recent") or "")
            if find(recent):
                await services.save_user_memory(uid, {"recent": mask(recent)})
                fixed += 1
        except Exception:
            logger.debug("scrub memory failed", exc_info=True)
        try:
            for row in await db.list_agent_log(uid, days=30, limit=500):
                if (find(row.get("text")) or find(row.get("reply"))) and row.get("id") is not None:
                    await db.mask_agent_log(row["id"], text=mask(row.get("text")), reply=mask(row.get("reply")) or None)
                    fixed += 1
        except Exception:
            logger.debug("scrub agent_log failed", exc_info=True)
    if fixed:
        logger.info("secrets: замаскировал ключи в %s записях памяти/журнала", fixed)
    return fixed


__all__ = ["find", "mask", "handle", "validate_gemini", "save_free_key", "saved_free_key", "scrub_existing", "MASK"]

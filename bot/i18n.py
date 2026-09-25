"""Английский интерфейс без переписывания сотен строк: перевод на выходе.

Бот написан на двух языках (`profile.tr(ru, uz)` и `… if uz else …` — больше 500 мест). Для
пользователя с английским интерфейсом эти места отдают русский текст, а PremiumBot перед отправкой
пропускает текст, подписи и кнопки через `translate_many`:

- текст режется на строки; переводим только строки с кириллицей (узбекская латиница, английский,
  цифры и эмодзи остаются как есть);
- числа заменяются заглушками ⟦0⟧, ⟦1⟧…, поэтому «Потрачено 25 000» и «Потрачено 40 000» — одна
  фраза; перевод фразы делается ОДИН раз (Gemini) и хранится в памяти и в базе (ui_translations);
- <pre>/<code> и куски в маркерах KEEP не трогаем: ответы Nurai уже на выбранном языке Nurai.

Если перевод не удался — уходит исходный текст (лучше по-русски, чем ничего).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

LANGS = ("uz", "ru", "en")
LANG_NAMES = {"uz": "O'zbekcha", "ru": "Русский", "en": "English"}
KEEP = "⁣"  # невидимый разделитель: всё между парой KEEP не переводим (ответы Джарвиса)

_CYR = re.compile(r"[А-Яа-яЁё]")
_NUM = re.compile(r"\d+(?:[  .,:/]\d+)*")
_PH = re.compile(r"⟦(\d+)⟧")
_PROTECT = re.compile(r"(<pre>.*?</pre>|<code>.*?</code>|" + KEEP + r".*?" + KEEP + r")", re.S)
BATCH = 60

_langs: dict[int, str] = {}          # telegram_id → язык интерфейса
_callbacks: dict[str, int] = {}      # callback_query_id → telegram_id (для перевода всплывашек)
_mem: dict[str, str] = {}            # hash шаблона → перевод


def norm(lang: str | None) -> str:
    lang = str(lang or "").lower()
    return lang if lang in LANGS else "ru"


def remember(uid: int, lang: str) -> None:
    _langs[int(uid)] = norm(lang)


def lang_of(chat_id: Any) -> str | None:
    try:
        return _langs.get(int(chat_id))
    except (TypeError, ValueError):
        return None


def note_callback(callback_id: str, uid: int) -> None:
    if len(_callbacks) > 2000:
        _callbacks.clear()
    _callbacks[str(callback_id)] = int(uid)


def callback_user(callback_id: str) -> int | None:
    return _callbacks.get(str(callback_id))


def keep(text: str) -> str:
    """Пометить текст как «не переводить» (ответ Nurai уже на языке, выбранном для Nurai)."""
    return f"{KEEP}{text}{KEEP}" if text else text


def strip_keep(text: str | None) -> str | None:
    return text.replace(KEEP, "") if text and KEEP in text else text


# ------------------------------------------------------------------ шаблоны
def mask(line: str) -> tuple[str, list[str]]:
    nums: list[str] = []

    def sub(m: re.Match[str]) -> str:
        nums.append(m.group(0))
        return f"⟦{len(nums) - 1}⟧"

    return _NUM.sub(sub, line), nums


def unmask(template: str, nums: list[str]) -> str:
    return _PH.sub(lambda m: nums[int(m.group(1))] if int(m.group(1)) < len(nums) else m.group(0), template)


def _key(template: str, lang: str) -> str:
    return hashlib.sha1(f"{lang}\x00{template}".encode("utf-8")).hexdigest()


def needs(text: str | None) -> bool:
    return bool(text) and bool(_CYR.search(text))


def _units(text: str) -> list[tuple[bool, str]]:
    """Текст → [(переводить?, кусок)]: защищённые блоки целиком, остальное — построчно."""
    out: list[tuple[bool, str]] = []
    for i, part in enumerate(_PROTECT.split(text)):
        if not part:
            continue
        if i % 2 == 1:  # защищённый блок
            out.append((False, part.replace(KEEP, "")))
            continue
        for line in part.split("\n"):
            out.append((needs(line), line))
            out.append((False, "\n"))
        if out and out[-1] == (False, "\n"):
            out.pop()
    return out


def _valid(src: str, dst: str) -> bool:
    return bool(dst.strip()) and sorted(_PH.findall(src)) == sorted(_PH.findall(dst)) and dst.count("<") == src.count("<")


# ------------------------------------------------------------------ перевод
async def _fetch(templates: list[str], lang: str) -> dict[str, str]:
    """Шаблоны → переводы: память → база → Gemini (одним запросом на пачку)."""
    from .context import ai, db

    want = {t: _key(t, lang) for t in templates}
    found = {t: _mem[k] for t, k in want.items() if k in _mem}
    missing = [t for t in templates if t not in found]
    if missing and db.available("ui_translations"):
        try:
            rows = await db.get_translations([want[t] for t in missing], lang)
            for t in missing:
                if want[t] in rows:
                    found[t] = _mem[want[t]] = rows[want[t]]
        except Exception:
            logger.debug("ui_translations read failed", exc_info=True)
        missing = [t for t in templates if t not in found]
    for i in range(0, len(missing), BATCH):
        chunk = missing[i:i + BATCH]
        prompt = (
            "Translate these Telegram bot interface strings into natural, concise English (they are Russian, some mixed with Uzbek). "
            "It is a personal assistant bot: finance, nutrition, tasks, goals, reminders, prayer-time alarm, AI assistant «Nurai» (Nurai → Nurai). "
            "Rules: keep every HTML tag, emoji and placeholder like ⟦0⟧ exactly (move placeholders only if English word order needs it); "
            "«сум» → «UZS»; keep proper names, brands, @usernames, links; button-length strings stay short; do not add anything. "
            "Return ONLY a JSON array of strings, same order and same length as the input.\n\n" + json.dumps(chunk, ensure_ascii=False)
        )
        try:
            raw = await ai.generate([{"text": prompt}], temperature=0.1, max_tokens=8000)
            from .ai import extract_json

            out = extract_json(raw)
        except Exception:
            logger.warning("ui translate failed", exc_info=True)
            continue
        if not isinstance(out, list) or len(out) != len(chunk):
            logger.warning("ui translate: wrong shape (%s vs %s)", len(out) if isinstance(out, list) else type(out), len(chunk))
            continue
        rows = []
        for src, dst in zip(chunk, out):
            dst = str(dst or "")
            if not _valid(src, dst):
                continue
            found[src] = _mem[want[src]] = dst
            rows.append({"src_hash": want[src], "src": src[:2000], "lang": lang, "dst": dst[:2000]})
        if rows and db.available("ui_translations"):
            try:
                await db.save_translations(rows)
            except Exception:
                logger.debug("ui_translations save failed", exc_info=True)
    return found


async def translate_many(texts: list[str | None], lang: str = "en") -> list[str | None]:
    """Перевести пачку текстов (сообщение + кнопки) за один проход."""
    split = [(_units(t) if t else []) for t in texts]
    masked: dict[tuple[int, int], tuple[str, list[str]]] = {}
    for ti, units in enumerate(split):
        for ui, (todo, piece) in enumerate(units):
            if todo:
                masked[(ti, ui)] = mask(piece)
    if not masked:
        return [strip_keep(t) for t in texts]
    found = await _fetch(sorted({tpl for tpl, _ in masked.values()}), lang)
    result: list[str | None] = []
    for ti, units in enumerate(split):
        if texts[ti] is None:
            result.append(None)
            continue
        parts = []
        for ui, (todo, piece) in enumerate(units):
            if todo:
                tpl, nums = masked[(ti, ui)]
                parts.append(unmask(found[tpl], nums) if tpl in found else piece)
            else:
                parts.append(piece)
        result.append("".join(parts))
    return result


async def translate(text: str | None, lang: str = "en") -> str | None:
    return (await translate_many([text], lang))[0]


__all__ = ["LANGS", "LANG_NAMES", "KEEP", "norm", "remember", "lang_of", "note_callback", "callback_user", "keep",
           "strip_keep", "mask", "unmask", "needs", "translate", "translate_many"]

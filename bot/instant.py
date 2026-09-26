"""Мгновенные команды JES — без Gemini (26.09.2026, его выбор «Мгновенные команды»).

«Джес, позвони маме», «открой ютуб», «фонарик», «пауза», «громче», «будильник на шесть тридцать», «таймер на пять минут»:
фразу распознаёт локальный русский распознаватель (wakeword, ~0.1 с), разбор — здесь, действие — те же инструменты
телефона. Быстрее (~0.3 с после конца фразы против ~1–1.5 с) и бесплатно. Всё, что не разобрали уверенно
(узбекский, вопросы, «позвони мне», такси с адресом), уходит в Gemini как раньше.

Распознаватель пишет строчными, без знаков и числа — словами («семь тридцать»).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_UNITS = {
    "ноль": 0, "один": 1, "одна": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
    "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
    "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19,
}
_TENS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50}
_POLITE = {"пожалуйста", "please", "срочно", "быстро", "сейчас", "ка", "давай", "мне-ка"}

_CALL = re.compile(r"^(?:позвони|позвонить|позвоните|набери|набрать|звони|звякни|вызови|сделай звонок)\s+(.+)$")
_NOT_PERSON = {"мне", "меня", "себе", "такси", "скорую", "полицию"}
_OPEN = re.compile(r"^(?:открой|открою|открыть|запусти|запустить)\s+(?:приложение\s+)?(.+)$")
_FLASH_ON = re.compile(r"^(?:включи\s+|зажги\s+)?фонарик$")
_FLASH_OFF = re.compile(r"^(?:выключи|погаси|отключи|убери)\s+фонарик$")
_MEDIA = [
    (re.compile(r"^(?:пауза|на паузу|поставь на паузу|останови(?: музыку| видео)?|стоп(?: музыка)?)$"), "pause"),
    (re.compile(r"^(?:продолжи|продолжай|играй|включи музыку|воспроизведи)$"), "play"),
    (re.compile(r"^(?:следующая|следующий|следующую|следующая песня|следующий трек|дальше|переключи)$"), "next"),
    (re.compile(r"^(?:предыдущая|предыдущий|предыдущую|предыдущая песня|предыдущий трек)$"), "previous"),
]
_VOLUME = [
    (re.compile(r"^(?:громче|погромче|сделай громче|сделай погромче|прибавь(?: звук| громкость)?)$"), "up"),
    (re.compile(r"^(?:тише|потише|сделай тише|сделай потише|убавь(?: звук| громкость)?)$"), "down"),
    (re.compile(r"^(?:выключи звук|без звука|убери звук)$"), "mute"),
]
_ALARM = re.compile(r"^(?:поставь\s+|заведи\s+|установи\s+)?будильник\s+(?:на|в)\s+(.+)$|^разбуди(?:\s+меня)?\s+(?:в|на)\s+(.+)$")
_TIMER = re.compile(r"^(?:поставь\s+|заведи\s+|установи\s+)?таймер\s+на\s+(.+)$|^засеки\s+(.+)$")


@dataclass
class Command:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    said: str = ""


def _clean(text: str) -> str:
    words = [w for w in re.findall(r"[a-zа-яё0-9]+", text.lower().replace("ё", "е")) if w not in _POLITE]
    return " ".join(words)


def numbers(words: list[str]) -> list[int]:
    """«семь тридцать пять» → [7, 35]; «двадцать» → [20]; цифры тоже."""
    out: list[int] = []
    i = 0
    while i < len(words):
        w = words[i]
        if w.isdigit():
            out.append(int(w))
        elif w in _TENS:
            value = _TENS[w]
            if i + 1 < len(words) and words[i + 1] in _UNITS and _UNITS[words[i + 1]] < 10:
                value += _UNITS[words[i + 1]]
                i += 1
            out.append(value)
        elif w in _UNITS:
            out.append(_UNITS[w])
        elif w in {"полчаса"}:
            out.append(30)
        i += 1
    return out


def _alarm_time(rest: str) -> str | None:
    words = rest.split()
    nums = numbers(words)
    if not nums or nums[0] > 23 or (len(nums) > 1 and nums[1] > 59):
        return None
    # в числах только время: «в семь тридцать утра», «на шесть часов», «в пять сорок пять»
    if any(w not in _UNITS and w not in _TENS and not w.isdigit() and w not in {"утра", "вечера", "дня", "ночи", "часов", "часа", "час",
                                                                              "минут", "минуты", "ровно", "и"} for w in words):
        return None
    hour, minute = nums[0], nums[1] if len(nums) > 1 else 0
    if ("вечера" in words or "дня" in words) and hour < 12:
        hour += 12
    if "ночи" in words and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _timer_seconds(rest: str) -> int | None:
    words = rest.split()
    if rest in {"полчаса"}:
        return 1800
    nums = numbers(words)
    value = nums[0] if nums else (1 if words and words[-1] in {"минуту", "час", "секунду"} else None)
    if value is None or value <= 0:
        return None
    unit = words[-1] if words else ""
    if unit.startswith("сек"):
        return value
    if unit.startswith("час"):
        return value * 3600
    if unit.startswith("мин"):
        return value * 60
    return None


def parse(text: str) -> Command | None:
    """Фраза (уже без имени JES) → команда телефона или None (тогда — Gemini)."""
    t = _clean(text)
    if not t:
        return None
    if m := _CALL.match(t):
        who = m.group(1).strip()
        if who.split()[0] in _NOT_PERSON or len(who.split()) > 3:
            return None
        return Command("phone_call", {"who": who, "variants": []}, t)
    if m := _OPEN.match(t):
        name = m.group(1).strip()
        if len(name.split()) > 3:
            return None
        return Command("open_app", {"name": name, "variants": []}, t)
    if _FLASH_ON.match(t):
        return Command("flashlight", {"on": True}, t)
    if _FLASH_OFF.match(t):
        return Command("flashlight", {"on": False}, t)
    for rx, command in _MEDIA:
        if rx.match(t):
            return Command("media", {"command": command}, t)
    for rx, direction in _VOLUME:
        if rx.match(t):
            return Command("set_volume", {"direction": direction}, t)
    if m := _ALARM.match(t):
        when = _alarm_time((m.group(1) or m.group(2) or "").strip())
        return Command("set_alarm", {"time": when}, t) if when else None
    if m := _TIMER.match(t):
        seconds = _timer_seconds((m.group(1) or m.group(2) or "").strip())
        return Command("set_timer", {"seconds": seconds}, t) if seconds else None
    return None


def succeeded(result: Any) -> bool:
    """Инструмент сделал своё (не ошибка, не «спроси», не «разблокируй») — Gemini не нужен."""
    return isinstance(result, dict) and not (result.get("error") or result.get("ask_exactly") or result.get("need_unlock"))


__all__ = ["Command", "parse", "numbers", "succeeded"]

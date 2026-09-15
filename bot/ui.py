"""Единый визуальный стиль экранов: карточки-цитаты, заголовки, даты, суммы."""
from __future__ import annotations

from datetime import date

from .profile import h

_MONTHS_GEN_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
_MONTHS_UZ = ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr"]
_WEEKDAYS_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
_WEEKDAYS_UZ = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"]


def human_date(d: date, lang: str = "ru", *, weekday: bool = True) -> str:
    if lang == "uz":
        core = f"{d.day} {_MONTHS_UZ[d.month - 1]}"
        return f"{_WEEKDAYS_UZ[d.weekday()]}, {core}" if weekday else core
    core = f"{d.day} {_MONTHS_GEN_RU[d.month - 1]}"
    return f"{_WEEKDAYS_RU[d.weekday()]}, {core}" if weekday else core


def title(icon: str, text: str, subtitle: str | None = None) -> str:
    line = f"{icon} <b>{h(text)}</b>"
    return f"{line}\n<i>{subtitle}</i>" if subtitle else line


def card(head: str, lines: list[str], *, expandable: bool = False) -> str:
    """Карточка: заголовок + тело в blockquote (Telegram рисует вертикальную полосу слева)."""
    body = "\n".join(line for line in lines if line is not None)
    tag = "<blockquote expandable>" if expandable else "<blockquote>"
    return f"{head}\n{tag}{body}</blockquote>"


def kv(label: str, value: str) -> str:
    return f"{label}: <b>{value}</b>"


def muted(text: str) -> str:
    return f"<i>{text}</i>"


def join(*blocks: str | None) -> str:
    return "\n\n".join(b for b in blocks if b)


def pct(ratio: float) -> str:
    return f"{int(round(max(0.0, ratio) * 100))}%"


def signed(value: float, fmt) -> str:
    return ("+" if value >= 0 else "−") + fmt(abs(value))

"""Задачи и дела: локальный разбор «что + когда» без AI и группировка для экранов.

«позвонить маме завтра в 18:00», «купить лампочку», «3 ноября поздравить брата»,
«в пятницу забрать посылку», «ertaga soat 9 da shifokor» → (текст, дата, время).
Дата/время не найдены → задача без срока (попадёт в «Без даты»).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

_TODAY = ("сегодня", "bugun")
_TOMORROW = ("завтра", "ertaga")
_AFTER_TOMORROW = ("послезавтра", "indinga", "indin")
_WEEKDAYS: dict[str, int] = {
    "понедельник": 0, "пн": 0, "dushanba": 0,
    "вторник": 1, "вт": 1, "seshanba": 1,
    "среда": 2, "среду": 2, "ср": 2, "chorshanba": 2,
    "четверг": 3, "чт": 3, "payshanba": 3,
    "пятница": 4, "пятницу": 4, "пт": 4, "juma": 4,
    "суббота": 5, "субботу": 5, "сб": 5, "shanba": 5,
    "воскресенье": 6, "вс": 6, "yakshanba": 6,
}
_MONTHS: dict[str, int] = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "май": 5, "июн": 6, "июл": 7, "август": 8, "сентябр": 9,
    "октябр": 10, "ноябр": 11, "декабр": 12,
    "yanvar": 1, "fevral": 2, "mart": 3, "aprel": 4, "may": 5, "iyun": 6, "iyul": 7, "avgust": 8, "sentabr": 9,
    "sentyabr": 9, "oktabr": 10, "oktyabr": 10, "noyabr": 11, "dekabr": 12,
}

_TIME_RE = re.compile(r"(?:(?:\bв|\bsoat)\s+)?\b(\d{1,2})[:.](\d{2})\b(?:\s*da\b)?", re.IGNORECASE)
_HOUR_RE = re.compile(r"\b(?:в|soat)\s+(\d{1,2})\b(?:\s*(?:часов|час|ч|da))?", re.IGNORECASE)
_DMY_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b")
_DAY_MONTH_RE = re.compile(r"\b(\d{1,2})(?:-?го|-?е|-?чи)?\s+([а-яa-z]+)", re.IGNORECASE)
_DAY_OF_MONTH_RE = re.compile(r"\b(\d{1,2})(?:-?го|-?е)?\s+числа\b", re.IGNORECASE)
_IN_DAYS_RE = re.compile(r"\bчерез\s+(\d+)\s+(?:дн|день|дня|дней)", re.IGNORECASE)
_WEEK_RE = re.compile(r"\bчерез\s+недел[юи]\b", re.IGNORECASE)
_WORD_RE = re.compile(r"[а-яёa-z']+", re.IGNORECASE)


@dataclass
class ParsedTask:
    text: str
    due_date: date | None = None
    due_time: str | None = None  # HH:MM

    def as_row(self) -> dict[str, Any]:
        return {"text": self.text, "due_date": self.due_date.isoformat() if self.due_date else None, "due_time": self.due_time}


def _clean(text: str) -> str:
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\s+(?:в|на|к|soat|da)\s*$", "", text, flags=re.IGNORECASE)
    return text.strip(" ,.;:-–—")


def _next_weekday(today: date, wd: int) -> date:
    delta = (wd - today.weekday()) % 7
    return today + timedelta(days=delta or 7)


def parse_task(text: str, today: date) -> ParsedTask:
    """Вытащить дату и время из фразы; всё остальное — текст задачи."""
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    rest = raw
    due: date | None = None
    hhmm: str | None = None

    m = _TIME_RE.search(rest)
    if m and 0 <= int(m.group(1)) <= 23 and 0 <= int(m.group(2)) <= 59:
        hhmm = f"{int(m.group(1)):02d}:{m.group(2)}"
        rest = rest[: m.start()] + " " + rest[m.end():]
    else:
        m = _HOUR_RE.search(rest)
        if m and 0 <= int(m.group(1)) <= 23:
            hhmm = f"{int(m.group(1)):02d}:00"
            rest = rest[: m.start()] + " " + rest[m.end():]

    m = _DMY_RE.search(rest)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        year = today.year if not y else (int(y) + 2000 if len(y) == 2 else int(y))
        try:
            due = date(year, mo, d)
            if not y and due < today:
                due = date(year + 1, mo, d)
            rest = rest[: m.start()] + " " + rest[m.end():]
        except ValueError:
            due = None
    if due is None:
        m = _DAY_OF_MONTH_RE.search(rest)
        if m and 1 <= int(m.group(1)) <= 31:
            d = int(m.group(1))
            y, mo = today.year, today.month
            if d < today.day:
                mo += 1
                if mo > 12:
                    mo, y = 1, y + 1
            try:
                due = date(y, mo, d)
                rest = rest[: m.start()] + " " + rest[m.end():]
            except ValueError:
                due = None
    if due is None:
        for m in _DAY_MONTH_RE.finditer(rest):
            word = m.group(2).lower()
            mo = next((v for k, v in _MONTHS.items() if word.startswith(k)), None)
            if mo is None or not 1 <= int(m.group(1)) <= 31:
                continue
            try:
                due = date(today.year, mo, int(m.group(1)))
            except ValueError:
                continue
            if due < today:
                due = date(today.year + 1, mo, int(m.group(1)))
            rest = rest[: m.start()] + " " + rest[m.end():]
            break
    if due is None:
        m = _IN_DAYS_RE.search(rest)
        if m:
            due = today + timedelta(days=int(m.group(1)))
            rest = rest[: m.start()] + " " + rest[m.end():]
        elif _WEEK_RE.search(rest):
            due = today + timedelta(days=7)
            rest = _WEEK_RE.sub(" ", rest)
    if due is None:
        low = rest.lower()
        for words, delta in ((_AFTER_TOMORROW, 2), (_TOMORROW, 1), (_TODAY, 0)):
            hit = next((w for w in words if re.search(rf"\b{w}\b", low)), None)
            if hit:
                due = today + timedelta(days=delta)
                rest = re.sub(rf"\b{hit}\b", " ", rest, flags=re.IGNORECASE)
                break
    if due is None:
        for w in _WORD_RE.findall(rest.lower()):
            if w in _WEEKDAYS and len(w) > 2:
                due = _next_weekday(today, _WEEKDAYS[w])
                rest = re.sub(rf"\b(?:в|во|во\s+)?\s*{w}\b", " ", rest, flags=re.IGNORECASE)
                break
    if hhmm and due is None:
        due = today
    text_out = _clean(rest) or raw
    return ParsedTask(text=text_out, due_date=due, due_time=hhmm)


# ------------------------------------------------------------------ grouping
@dataclass
class Grouped:
    overdue: list[dict[str, Any]] = field(default_factory=list)
    today: list[dict[str, Any]] = field(default_factory=list)
    upcoming: list[dict[str, Any]] = field(default_factory=list)   # с датой позже сегодня, по возрастанию
    undated: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.overdue) + len(self.today) + len(self.upcoming) + len(self.undated)


def task_date(t: dict[str, Any]) -> date | None:
    due = str(t.get("due_date") or "")[:10]
    if not due:
        return None
    try:
        return date.fromisoformat(due)
    except ValueError:
        return None


def group(tasks: list[dict[str, Any]], today: date) -> Grouped:
    g = Grouped()
    dated: list[tuple[date, str, dict[str, Any]]] = []
    for t in tasks:
        if t.get("done"):
            continue
        d = task_date(t)
        if d is None:
            g.undated.append(t)
        else:
            dated.append((d, str(t.get("due_time") or "99:99"), t))
    for d, _, t in sorted(dated, key=lambda x: (x[0], x[1])):
        (g.overdue if d < today else g.today if d == today else g.upcoming).append(t)
    return g


def dashboard_pick(tasks: list[dict[str, Any]], today: date, *, upcoming_limit: int = 3, hard_limit: int = 6) -> tuple[list[dict[str, Any]], int]:
    """Для главного экрана: просроченные + сегодняшние, затем до `upcoming_limit`
    ближайших (сначала с датой, потом без). Возвращает (строки, сколько ещё скрыто)."""
    g = group(tasks, today)
    picked = (g.overdue + g.today)[:hard_limit]
    room = min(upcoming_limit, hard_limit - len(picked))
    if room > 0:
        picked += (g.upcoming + g.undated)[:room]
    return picked, max(0, g.total - len(picked))


def when_label(t: dict[str, Any], today: date, lang: str = "ru") -> str:
    """«сегодня 18:00» / «завтра» / «пт 26.09» / «⚠️ 3 дн.» — короткая метка срока."""
    uz = lang == "uz"
    d = task_date(t)
    tm = f" {t['due_time']}" if t.get("due_time") else ""
    if d is None:
        return tm.strip()
    left = (d - today).days
    if left < 0:
        return (f"{-left} kun kechikdi" if uz else f"просрочено {-left} дн.") + tm
    if left == 0:
        return ("bugun" if uz else "сегодня") + tm
    if left == 1:
        return ("ertaga" if uz else "завтра") + tm
    wd = ("du", "se", "ch", "pa", "ju", "sh", "ya")[d.weekday()] if uz else ("пн", "вт", "ср", "чт", "пт", "сб", "вс")[d.weekday()]
    return f"{wd} {d:%d.%m}{tm}"

"""Серии (streaks) для привычек + бейджи.

Серия по привычке = подряд идущие дни, в которые `habit_logs.completed=True`
для этого habit_id. Сегодня учитывается; вчера тоже — если сегодня ещё не отмечено,
серия не обнуляется до конца дня.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable


# ---- Пороги бейджей ----
HABIT_BADGE_THRESHOLDS: tuple[int, ...] = (3, 7, 14, 30, 100)


def habit_badge_key(habit_id: str, threshold: int) -> str:
    return f"streak_habit:{habit_id}:{threshold}"


@dataclass(frozen=True)
class StreakInfo:
    current: int      # текущая серия (в днях)
    best: int         # лучшая когда-либо серия
    today_done: bool  # отмечена ли привычка сегодня
    yesterday_done: bool


def _logs_to_dates(logs: Iterable[dict]) -> set[date]:
    out: set[date] = set()
    for log in logs:
        if not log.get("completed"):
            continue
        raw = log.get("log_date")
        if not raw:
            continue
        try:
            out.add(date.fromisoformat(str(raw)))
        except Exception:
            continue
    return out


def compute_streak(habit_logs: Iterable[dict], *, today: date) -> StreakInfo:
    """Вычисляет current/best streak по списку логов одной привычки."""
    days = _logs_to_dates(habit_logs)
    if not days:
        return StreakInfo(current=0, best=0, today_done=False, yesterday_done=False)

    yesterday = today - timedelta(days=1)
    today_done = today in days
    yesterday_done = yesterday in days

    # current: считаем подряд от сегодня (или вчера, если сегодня ещё нет)
    cursor = today if today_done else yesterday
    current = 0
    while cursor in days:
        current += 1
        cursor -= timedelta(days=1)

    # best: проходим по отсортированным датам, ищем самую длинную серию
    best = 0
    run = 0
    prev: date | None = None
    for d in sorted(days):
        if prev is not None and (d - prev).days == 1:
            run += 1
        else:
            run = 1
        if run > best:
            best = run
        prev = d

    return StreakInfo(current=current, best=best, today_done=today_done, yesterday_done=yesterday_done)


def newly_crossed_thresholds(prev_streak: int, new_streak: int, thresholds: Iterable[int] = HABIT_BADGE_THRESHOLDS) -> list[int]:
    """Список порогов, которые пользователь пересёк именно сейчас."""
    if new_streak <= prev_streak:
        return []
    return [t for t in sorted(thresholds) if prev_streak < t <= new_streak]


def streak_emoji(streak: int) -> str:
    if streak >= 100:
        return "👑"
    if streak >= 30:
        return "💎"
    if streak >= 14:
        return "⚡"
    if streak >= 7:
        return "🔥"
    if streak >= 3:
        return "🌱"
    return "•"


def format_streak_line(name: str, info: StreakInfo, lang: str = "ru") -> str:
    """Короткая строка для /habits."""
    em = streak_emoji(info.current)
    if lang == "uz":
        if info.current <= 0:
            return f"{em} {name} — boshlang!"
        return f"{em} {name} — <b>{info.current}</b> kun (eng yaxshi {info.best})"
    if info.current <= 0:
        return f"{em} {name} — начни!"
    return f"{em} {name} — <b>{info.current}</b> дн. подряд (лучшая {info.best})"


def badge_label(threshold: int, lang: str = "ru") -> str:
    labels = {
        3:   ("🌱 Росток (3 дня)",        "🌱 Nihol (3 kun)"),
        7:   ("🔥 Неделя огня (7 дн.)",   "🔥 Oʻt haftasi (7 kun)"),
        14:  ("⚡ Две недели (14 дн.)",   "⚡ Ikki hafta (14 kun)"),
        30:  ("💎 Алмаз месяца (30 дн.)", "💎 Oy olmosi (30 kun)"),
        100: ("👑 Сотня (100 дн.)",       "👑 Yuzlik (100 kun)"),
    }
    ru, uz = labels.get(threshold, (f"⭐ {threshold} дней", f"⭐ {threshold} kun"))
    return uz if lang == "uz" else ru


__all__ = [
    "StreakInfo",
    "HABIT_BADGE_THRESHOLDS",
    "habit_badge_key",
    "compute_streak",
    "newly_crossed_thresholds",
    "streak_emoji",
    "format_streak_line",
    "badge_label",
]

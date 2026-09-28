"""Календарь телефона (28.09.2026, его выбор «Календарь телефона»).

Телефон присылает ближайшие события (2 недели вперёд) — при запуске, раз в 3 часа, при включении экрана и когда он
сам что-то поменял в календаре. По ним бот и JES отвечают «что у меня завтра?», а утренняя сводка называет дела дня.
Добавить событие голосом — телефон делает сразу (phone.calendar_add); из чата — просьба ждёт здесь, и телефон забирает
её при следующей связи (обычно при включении экрана).
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)

KEEP_DAYS = 31


def _dir():  # noqa: ANN202
    from .tg_user import data_dir

    path = data_dir() / "calendar"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read(name: str, default: Any) -> Any:
    try:
        return json.loads((_dir() / name).read_text(encoding="utf-8"))
    except Exception:
        return default


def _write(name: str, value: Any) -> None:
    (_dir() / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------------ события с телефона
def save_events(uid: int, events: list[Any]) -> int:
    """[{"id", "title", "start" (мс), "end" (мс), "all_day", "location"}] — заменяет прошлую выгрузку."""
    clean = []
    for e in events[:500]:
        if not isinstance(e, dict) or not e.get("start"):
            continue
        try:
            start, end = int(e["start"]), int(e.get("end") or e["start"])
        except (TypeError, ValueError):
            continue
        clean.append({"id": str(e.get("id") or ""), "title": " ".join(str(e.get("title") or "").split())[:120] or "(без названия)",
                      "start": start, "end": end, "all_day": bool(e.get("all_day")), "location": str(e.get("location") or "")[:120],
                      # дату и время по его поясу считает телефон (у событий «на весь день» время хранится в UTC)
                      "day": str(e.get("day") or "")[:10], "time": (str(e.get("time"))[:5] if e.get("time") else None)})
    _write(f"{uid}.json", {"synced_at": time.time(), "events": sorted(clean, key=lambda x: x["start"])})
    return len(clean)


def synced_at(uid: int) -> float | None:
    return _read(f"{uid}.json", {}).get("synced_at")


def events_between(uid: int, start: date, end: date, tz) -> list[dict[str, Any]]:  # noqa: ANN001
    """События с start по end включительно (по его часовому поясу), с временем «ЧЧ:ММ» и датой."""
    out = []
    for e in _read(f"{uid}.json", {}).get("events") or []:
        if e.get("day"):
            day, when = date.fromisoformat(e["day"]), (None if e.get("all_day") else e.get("time"))
        else:
            moment = datetime.fromtimestamp(e["start"] / 1000, tz)
            day, when = moment.date(), (None if e.get("all_day") else moment.strftime("%H:%M"))
        if start <= day <= end:
            out.append({"date": day.isoformat(), "time": when, "title": e["title"], "location": e.get("location") or None})
    return out


def day_lines(uid: int, day: date, tz, lang: str = "ru") -> list[str]:  # noqa: ANN001
    """Для утренней сводки: «10:00 Встреча с Алишером»."""
    rows = events_between(uid, day, day, tz)
    return [f"{r['time'] or ('kun bo‘yi' if lang == 'uz' else 'весь день')} {r['title']}" + (f" ({r['location']})" if r["location"] else "")
            for r in rows]


# ------------------------------------------------------------------ просьбы из чата («добавь встречу…»)
def queue_add(uid: int, title: str, start_ms: int, minutes: int, reminder: int, location: str = "") -> str:
    items = _read(f"{uid}.pending.json", [])
    op_id = uuid.uuid4().hex[:10]
    items.append({"id": op_id, "op": "add", "title": title[:120], "start": start_ms, "minutes": minutes, "reminder": reminder,
                  "location": location[:120], "at": time.time()})
    _write(f"{uid}.pending.json", items[-50:])
    return op_id


def pending(uid: int) -> list[dict[str, Any]]:
    week = time.time() - 7 * 24 * 3600
    return [x for x in _read(f"{uid}.pending.json", []) if float(x.get("at") or 0) > week]


def mark_done(uid: int, ids: list[str]) -> None:
    if not ids:
        return
    _write(f"{uid}.pending.json", [x for x in _read(f"{uid}.pending.json", []) if x.get("id") not in set(ids)])


def start_ms(day: date, hhmm: str, tz) -> int:  # noqa: ANN001
    """«2026-10-02» + «15:00» в его часовом поясе → миллисекунды (для телефона)."""
    hh, mm = (int(x) for x in (hhmm or "09:00").split(":")[:2])
    return int(datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz).timestamp() * 1000)


__all__ = ["save_events", "events_between", "day_lines", "queue_add", "pending", "mark_done", "start_ms", "synced_at"]

"""Каждый день (29.09, его выбор «задача каждый день + напоминание» и «цель с шагом каждый день»).

«Каждый день в 20:00 урок английского» — дело повторяется, в срок бот напоминает кнопками [✅ Сделал] [▶️ Продолжить урок]
[⏭ Не сегодня], считает серию дней подряд. Есть ссылка (плейлист / ролик YouTube) — кнопка открывает урок с того места,
где он остановился (bot/lessons.py). Задано число шагов («курс из 40 уроков») — это цель: прогресс в % и прогноз, когда
закончит, если делать по шагу в день. Хранится в DATA_DIR/habits_<uid>.json.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

ALL_DAYS = [1, 2, 3, 4, 5, 6, 7]   # пн..вс (isoweekday)


def _file(uid: int):  # noqa: ANN202
    from .tg_user import data_dir

    return data_dir() / f"habits_{uid}.json"


def all_items(uid: int) -> list[dict[str, Any]]:
    try:
        return list(json.loads(_file(uid).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []


def _save(uid: int, rows: list[dict[str, Any]]) -> None:
    _file(uid).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def _hhmm(value: Any) -> str | None:
    text = str(value or "").strip().replace(".", ":")
    if not text:
        return None
    try:
        hh, mm = (int(x) for x in (text.split(":") + ["0"])[:2])
    except ValueError:
        return None
    return f"{hh:02d}:{mm:02d}" if 0 <= hh <= 23 and 0 <= mm <= 59 else None


def add(uid: int, title: str, *, at: str | None = None, days: list[int] | None = None, link: str = "", total: int | None = None) -> dict[str, Any]:
    title = " ".join(str(title or "").split())[:120]
    if not title:
        raise ValueError("пустое название")
    rows = all_items(uid)
    old = find(uid, title)
    item = {**(old or {}), "id": (old or {}).get("id") or uuid.uuid4().hex[:8], "title": title, "time": _hhmm(at),
            "days": sorted({int(d) for d in (days or ALL_DAYS) if 1 <= int(d) <= 7}) or ALL_DAYS,
            "link": str(link or (old or {}).get("link") or "")[:300], "total": int(total) if total else (old or {}).get("total"),
            "done": int((old or {}).get("done") or 0), "streak": int((old or {}).get("streak") or 0),
            "last_done": (old or {}).get("last_done"), "created": (old or {}).get("created") or time.time()}
    _save(uid, [r for r in rows if r.get("id") != item["id"]] + [item])
    return item


def find(uid: int, query: Any) -> dict[str, Any] | None:
    from .names import norm

    q = str(query or "").strip()
    rows = all_items(uid)
    for r in rows:
        if r.get("id") == q:
            return r
    nq = norm(q)
    if not nq:
        return None
    exact = [r for r in rows if norm(r.get("title")) == nq]
    if exact:
        return exact[0]
    words = [w for w in nq.split() if len(w) >= 3]
    scored = [(sum(1 for w in words if w in norm(r.get("title"))) / max(1, len(words)), r) for r in rows]
    scored = [s for s in scored if s[0] >= 0.5]
    return max(scored, key=lambda s: s[0])[1] if scored else None


def remove(uid: int, habit: dict[str, Any]) -> None:
    _save(uid, [r for r in all_items(uid) if r.get("id") != habit.get("id")])


def _update(uid: int, habit: dict[str, Any]) -> dict[str, Any]:
    _save(uid, [habit if r.get("id") == habit.get("id") else r for r in all_items(uid)])
    return habit


def done(uid: int, habit: dict[str, Any], today: date, steps: int = 1) -> dict[str, Any]:
    """Сделал сегодня: +шаги к цели; серия — если вчера тоже сделал (или вчера не его день)."""
    if habit.get("last_done") == today.isoformat():
        return habit  # уже отмечено сегодня
    last = date.fromisoformat(habit["last_done"]) if habit.get("last_done") else None
    gap_ok = last is not None and all(d.isoweekday() not in habit.get("days", ALL_DAYS)
                                      for d in (last + timedelta(days=i) for i in range(1, (today - last).days)))
    habit["streak"] = int(habit.get("streak") or 0) + 1 if gap_ok else 1
    habit["done"] = int(habit.get("done") or 0) + max(1, int(steps or 1))
    habit["last_done"] = today.isoformat()
    return _update(uid, habit)


def skip(uid: int, habit: dict[str, Any], today: date) -> dict[str, Any]:
    habit["skipped"] = today.isoformat()
    return _update(uid, habit)


def is_today(habit: dict[str, Any], today: date) -> bool:
    return today.isoweekday() in (habit.get("days") or ALL_DAYS)


def due(uid: int, now: datetime) -> list[dict[str, Any]]:
    """Пора напомнить: сегодня его день, время наступило (не позже 3 часов назад), ещё не сделано/не пропущено/не напоминали."""
    today = now.date().isoformat()
    out = []
    for h in all_items(uid):
        if not h.get("time") or not is_today(h, now.date()):
            continue
        if today in {h.get("last_done"), h.get("skipped"), h.get("reminded")}:
            continue
        hh, mm = (int(x) for x in h["time"].split(":"))
        minutes = now.hour * 60 + now.minute - (hh * 60 + mm)
        if 0 <= minutes <= 180:
            out.append(h)
    return out


def mark_reminded(uid: int, habit: dict[str, Any], today: date) -> None:
    habit["reminded"] = today.isoformat()
    _update(uid, habit)


def progress(habit: dict[str, Any], today: date) -> str:
    """«серия 3 дн. · 12/40 (30%), закончите ~12.11» — для напоминаний, сводки и ответов."""
    parts = []
    if int(habit.get("streak") or 0) > 1:
        parts.append(f"серия {habit['streak']} дн.")
    total = int(habit.get("total") or 0)
    if total:
        done_n = min(total, int(habit.get("done") or 0))
        left = total - done_n
        text = f"{done_n}/{total} ({round(done_n * 100 / total)}%)"
        if left > 0:
            per_week = max(1, len(habit.get("days") or ALL_DAYS))
            finish = today + timedelta(days=round(left * 7 / per_week))
            text += f", закончите ~{finish:%d.%m}"
        else:
            text += " — цель достигнута 🎉"
        parts.append(text)
    return " · ".join(parts)


def today_lines(uid: int, today: date) -> list[str]:
    """Для утренней сводки: «20:00 Урок английского · серия 3 дн. · 12/40 (30%)»."""
    out = []
    for h in sorted((h for h in all_items(uid) if is_today(h, today)), key=lambda h: h.get("time") or "99:99"):
        mark = "✅ " if h.get("last_done") == today.isoformat() else ""
        extra = progress(h, today)
        out.append(f"{mark}{h.get('time') or 'в течение дня'} {h['title']}" + (f" · {extra}" if extra else ""))
    return out


def describe(habit: dict[str, Any], today: date) -> dict[str, Any]:
    names = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    days = habit.get("days") or ALL_DAYS
    return {"id": habit["id"], "title": habit["title"], "time": habit.get("time"),
            "days": "каждый день" if len(days) == 7 else ", ".join(names[d - 1] for d in days),
            "link": habit.get("link") or None, "done_today": habit.get("last_done") == today.isoformat(),
            "progress": progress(habit, today) or None}


__all__ = ["add", "all_items", "find", "remove", "done", "skip", "due", "mark_reminded", "progress", "today_lines", "describe", "is_today"]

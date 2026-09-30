"""Умный перенос невыполненного (30.09, его выбор): не сделанное переезжает на другой день с пометкой «переносилось N раз»;
на третий раз JES не двигает задачу дальше, а спрашивает: разбить на шаги, отложить на неделю или удалить.

Счётчик — в DATA_DIR/carry_<uid>.json {"counts": {task_id: {"n", "day", "text"}}}; одну задачу за день считаем один раз
(утренний перенос просроченного и вечернее «Перенести на завтра» не задваивают счёт).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import services
from .context import ai, db
from .profile import Profile, h

logger = logging.getLogger(__name__)

ASK_AT = 3          # с какого по счёту переноса спрашиваем, а не двигаем
MAX_ASK_PER_RUN = 3


def _file(uid: int) -> Path | None:
    folder = os.getenv("DATA_DIR")
    return Path(folder) / f"carry_{int(uid)}.json" if folder else None


def _load(uid: int) -> dict[str, Any]:
    path = _file(uid)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path is not None and path.exists() else {}
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data.get("counts"), dict) else {"counts": {}}


def _save(uid: int, data: dict[str, Any]) -> None:
    path = _file(uid)
    if path is not None:
        try:
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            logger.warning("carry: не сохранил", exc_info=True)


def count(uid: int, task_id: Any) -> int:
    return int((_load(uid)["counts"].get(str(task_id)) or {}).get("n") or 0)


def reset(uid: int, task_id: Any) -> None:
    data = _load(uid)
    if data["counts"].pop(str(task_id), None) is not None:
        _save(uid, data)


def bump(uid: int, task_id: Any, text: str, today: date) -> int:
    """+1 к числу переносов — не чаще раза в день на задачу. Возвращает новое число."""
    data = _load(uid)
    row = data["counts"].setdefault(str(task_id), {"n": 0, "day": "", "text": text[:80]})
    if row.get("day") != today.isoformat():
        row["n"], row["day"] = int(row.get("n") or 0) + 1, today.isoformat()
        _save(uid, data)
    return int(row["n"])


def ask_keyboard(profile: Profile, task_id: Any):  # noqa: ANN201
    from aiogram.types import InlineKeyboardMarkup

    from .keyboards import _btn

    tid = str(task_id)
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🧩 " + profile.tr("Разбить на шаги", "Bosqichlarga bo'lish"), f"carry:s:{tid}")],
        [_btn("⏳ " + profile.tr("Отложить на неделю", "Bir haftaga"), f"carry:w:{tid}"), _btn("🗑 " + profile.tr("Удалить", "O'chirish"), f"carry:d:{tid}")]])


async def _ask(bot, profile: Profile, row: dict[str, Any], n: int) -> None:  # noqa: ANN001
    text = (f"🔁 <b>{profile.tr(f'Эта задача уже {n} раз переносится', f'Bu vazifa {n} marta ko`chirilgan')}</b>\n"
            f"<blockquote>«{h(str(row.get('text') or ''))}»</blockquote>\n{profile.tr('Что с ней сделать?', 'Nima qilamiz?')}")
    await bot.send_message(profile.telegram_id, text, reply_markup=ask_keyboard(profile, row.get("id")), parse_mode="HTML")


async def _move(bot, profile: Profile, rows: list[dict[str, Any]], to_day: date) -> tuple[int, int]:  # noqa: ANN001
    uid, today = profile.telegram_id, profile.today
    moved = asked = 0
    for r in rows:
        n = bump(uid, r.get("id"), str(r.get("text") or ""), today)
        if n >= ASK_AT:
            if asked < MAX_ASK_PER_RUN:
                await _ask(bot, profile, r, n)
            asked += 1
            continue
        await db.update_task(uid, r["id"], {"due_date": to_day.isoformat(), "notified_key": None})
        moved += 1
    services.invalidate(uid, "tasks")
    return moved, asked


async def rollover(bot, profile: Profile) -> tuple[int, int]:  # noqa: ANN001
    """Утро: просроченные задачи переезжают на сегодня (счёт +1); на третий раз — вопрос. → (перенесено, вопросов)."""
    if not db.available("tasks"):
        return 0, 0
    today = profile.today
    overdue = [r for r in await db.list_tasks(profile.telegram_id) if r.get("due_date") and str(r["due_date"])[:10] < today.isoformat()]
    return await _move(bot, profile, overdue, today) if overdue else (0, 0)


async def move_open(bot, profile: Profile) -> tuple[int, int]:  # noqa: ANN001
    """Вечер, кнопка «Перенести на завтра»: сегодняшние и просроченные → на завтра. → (перенесено, вопросов)."""
    if not db.available("tasks"):
        return 0, 0
    today = profile.today
    rows = [r for r in await db.list_tasks(profile.telegram_id) if r.get("due_date") and str(r["due_date"])[:10] <= today.isoformat()]
    return await _move(bot, profile, rows, today + timedelta(days=1)) if rows else (0, 0)


async def _task(profile: Profile, task_id: str) -> dict[str, Any] | None:
    return next((r for r in await db.list_tasks(profile.telegram_id) if str(r.get("id")) == str(task_id)), None)


async def postpone_week(profile: Profile, task_id: str) -> bool:
    row = await _task(profile, task_id)
    if row is None:
        return False
    await db.update_task(profile.telegram_id, row["id"], {"due_date": (profile.today + timedelta(days=7)).isoformat(), "notified_key": None})
    reset(profile.telegram_id, task_id)
    services.invalidate(profile.telegram_id, "tasks")
    return True


async def delete(profile: Profile, task_id: str) -> bool:
    row = await _task(profile, task_id)
    if row is None:
        return False
    await db.delete_tasks(profile.telegram_id, [row["id"]])
    reset(profile.telegram_id, task_id)
    services.invalidate(profile.telegram_id, "tasks")
    return True


SPLIT_PROMPT = (
    "Задача, которую человек никак не начнёт: «{text}». Разбей её на 2–4 КОНКРЕТНЫХ маленьких шага (каждый — на 10–30 минут, "
    "с глагола, до 60 знаков), чтобы первый шаг можно было сделать прямо сейчас. Верни ТОЛЬКО JSON: {{\"steps\": [\"...\"]}}. "
    "Язык — как в задаче. Ничего не выдумывай сверх задачи.")


async def split(profile: Profile, task_id: str) -> list[str] | None:
    """Разбить на шаги умной моделью: шаги — новые задачи на сегодня, исходная удаляется. None — не вышло."""
    from . import ai as ai_mod
    from .ai import extract_json

    row = await _task(profile, task_id)
    if row is None:
        return None
    try:
        raw = await ai.generate([{"text": SPLIT_PROMPT.format(text=str(row.get("text") or ""))}], model=ai_mod.smart_model(), temperature=0.4,
                                json_mode=True, thinking_budget=0, max_tokens=1200)
        steps = [" ".join(str(s).split())[:80] for s in (extract_json(raw) or {}).get("steps") or [] if str(s).strip()][:4]
    except Exception:
        logger.warning("carry: разбить не вышло", exc_info=True)
        return None
    if len(steps) < 2:
        return None
    uid = profile.telegram_id
    for s in steps:
        await db.add_task(uid, text=s, due_date=profile.today.isoformat(), due_time=None)
    await db.delete_tasks(uid, [row["id"]])
    reset(uid, task_id)
    services.invalidate(uid, "tasks")
    return steps


__all__ = ["count", "bump", "reset", "rollover", "move_open", "split", "postpone_week", "delete", "ask_keyboard"]

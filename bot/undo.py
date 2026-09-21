"""Единый откат последнего действия («↩️ Отменить»).

Любое изменение данных (агентом или кнопками экранов) оставляет в кэше шаги
отката. Один ход агента может изменить несколько сущностей — шаги копятся в
списке и откатываются в обратном порядке. TTL — 30 минут.

Типы шагов (payload):
  restore_entries {rows}            — вернуть удалённые операции
  delete_entries {ids}              — удалить добавленные операции
  restore_fields {entry_id, fields} — вернуть поля операции
  restore_calorie_logs {rows}       — вернуть удалённые записи еды
  delete_calorie_logs {ids}         — удалить добавленные записи еды
  restore_calorie_fields {log_id, fields}
  restore_debt {side, base, rows}   — вернуть «без имени» в долгах
  restore_settings {settings}       — счета (finance_settings)
  restore_budgets {limits}          — лимиты (0 = убрать)
  restore_recurring {rows} · delete_recurring {ids} · recurring_fields {rec_id, fields} · recurring_enabled {ids, enabled}
  restore_reminders {rows} · delete_reminders {ids} · reminder_fields {reminder_id, fields}
  restore_nutrition_profile {profile}
  restore_user_settings {fields}
  restore_report_prefs {enabled, frequency}
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from . import cache
from . import services
from .context import db

logger = logging.getLogger(__name__)

TTL = 1800.0
_KEY = ("undo",)
_TURN_KEY = ("undo_turn",)


# ------------------------------------------------------------------ recording
def remember(uid: int, step: dict[str, Any]) -> None:
    """Запомнить одиночный шаг как «последнее действие» (для кнопок экранов)."""
    cache.put(uid, _KEY, step, TTL)


def begin_turn(uid: int) -> None:
    """Начать накопление шагов за один ход агента."""
    cache.put(uid, _TURN_KEY, [], TTL)


def push(uid: int, step: dict[str, Any]) -> None:
    """Добавить шаг в текущий ход агента (или запомнить как одиночный, если ход не начат)."""
    steps = cache.get(uid, _TURN_KEY)
    if isinstance(steps, list):
        steps.append(step)
        cache.put(uid, _TURN_KEY, steps, TTL)
    else:
        remember(uid, step)


def end_turn(uid: int) -> bool:
    """Завершить ход: если были изменения — они становятся «последним действием». Возвращает, были ли."""
    steps = cache.get(uid, _TURN_KEY)
    cache.put(uid, _TURN_KEY, None, 1)
    if not steps:
        return False
    remember(uid, {"type": "multi", "steps": list(steps)})
    return True


def peek(uid: int) -> dict[str, Any] | None:
    payload = cache.get(uid, _KEY)
    return payload if isinstance(payload, dict) else None


def clear(uid: int) -> None:
    cache.put(uid, _KEY, None, 1)


# ------------------------------------------------------------------ applying
def _entry_payload(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry_type": r.get("entry_type"), "amount": r.get("amount"), "category": r.get("category"),
        "note": r.get("note"), "source": r.get("source") or "restored", "entry_date": str(r.get("entry_date") or "")[:10] or None,
    }


async def _apply_step(uid: int, step: dict[str, Any], *, tz_name: str) -> None:
    kind = step.get("type")
    if kind == "multi":
        for sub in reversed(step.get("steps") or []):
            await _apply_step(uid, sub, tz_name=tz_name)
        return
    if kind == "restore_entries":
        rows = [_entry_payload(r) for r in (step.get("rows") or [])]
        if rows:
            await db.add_finance_entries(uid, rows, entry_date=date.today())
        cache.invalidate(uid, "fin_entries")
    elif kind == "delete_entries":
        await db.delete_finance_entries(uid, step.get("ids") or [])
        cache.invalidate(uid, "fin_entries")
    elif kind == "restore_fields":
        await db.update_finance_entry(uid, step["entry_id"], step.get("fields") or {})
        cache.invalidate(uid, "fin_entries")
    elif kind == "restore_calorie_logs":
        rows = [{**r, "created_at": r.get("created_at")} for r in (step.get("rows") or [])]
        if rows:
            await db.add_calorie_logs(uid, rows)
        cache.invalidate(uid, "kcal_today")
        cache.invalidate(uid, "kcal_days")
    elif kind == "delete_calorie_logs":
        await db.delete_calorie_logs(uid, step.get("ids") or [])
        cache.invalidate(uid, "kcal_today")
        cache.invalidate(uid, "kcal_days")
    elif kind == "restore_calorie_fields":
        await db.update_calorie_log(uid, step["log_id"], step.get("fields") or {})
        cache.invalidate(uid, "kcal_today")
        cache.invalidate(uid, "kcal_days")
    elif kind == "restore_debt":
        settings_ = dict(await services.finance_settings(uid))
        settings_[f"{step['side']}_base"] = float(step.get("base") or 0.0)
        await services.save_finance_settings(uid, settings_)
        rows = [_entry_payload(r) for r in (step.get("rows") or [])]
        if rows:
            await db.add_finance_entries(uid, rows, entry_date=date.today())
        cache.invalidate(uid, "fin_entries")
    elif kind == "restore_settings":
        await services.save_finance_settings(uid, step.get("settings") or {})
    elif kind == "restore_budgets":
        for k, v in (step.get("limits") or {}).items():
            await services.set_budget(uid, k, float(v or 0))
    elif kind == "restore_recurring":
        for r in step.get("rows") or []:
            await db.add_recurring(
                uid, title=str(r.get("title") or ""), amount=float(r.get("amount") or 0), category=r.get("category") or "home",
                bucket=r.get("bucket") or "card", day_of_month=int(r.get("day_of_month") or 1),
            )
        services.invalidate_recurring(uid)
    elif kind == "delete_recurring":
        for rid in step.get("ids") or []:
            await db.delete_recurring(uid, rid)
        services.invalidate_recurring(uid)
    elif kind == "recurring_fields":
        await db.update_recurring(uid, step["rec_id"], step.get("fields") or {})
        services.invalidate_recurring(uid)
    elif kind == "recurring_enabled":
        for rid in step.get("ids") or []:
            await db.update_recurring(uid, rid, {"enabled": bool(step.get("enabled"))})
        services.invalidate_recurring(uid)
    elif kind == "restore_reminders":
        for r in step.get("rows") or []:
            await db.add_reminder(
                uid, text=r.get("reminder_text") or "", reminder_time=str(r.get("reminder_time") or "20:00")[:5],
                days_of_week=r.get("days_of_week") or [1, 2, 3, 4, 5, 6, 7], tz_name=r.get("timezone") or tz_name,
            )
        services.invalidate_reminders(uid)
    elif kind == "delete_reminders":
        for rid in step.get("ids") or []:
            await db.delete_reminder(uid, rid)
        services.invalidate_reminders(uid)
    elif kind == "reminder_fields":
        await db.update_reminder(uid, step["reminder_id"], step.get("fields") or {})
        services.invalidate_reminders(uid)
    elif kind == "restore_nutrition_profile":
        profile = step.get("profile")
        if profile:
            await services.save_nutrition_profile(uid, profile)
        else:
            # профиля не было — обнуляем план
            await services.save_nutrition_profile(uid, {"daily_calories": 0})
    elif kind == "restore_user_settings":
        await services.save_user_settings(uid, step.get("fields") or {})
    elif kind == "restore_report_prefs":
        await db.save_report_preferences(uid, enabled=bool(step.get("enabled", True)), frequency=str(step.get("frequency") or "weekly"))
        cache.invalidate(uid, "report_prefs")
    else:
        logger.warning("unknown undo step: %s", kind)


async def apply(uid: int, *, tz_name: str) -> bool:
    """Откатить последнее действие. Возвращает False, если откатывать нечего."""
    payload = peek(uid)
    if not payload:
        return False
    clear(uid)
    await _apply_step(uid, payload, tz_name=tz_name)
    return True


__all__ = ["remember", "begin_turn", "push", "end_turn", "peek", "clear", "apply", "TTL"]
